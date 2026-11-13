"""Failover of the AI provider chain (app/ai/client.py) against stand-in OpenAI-compatible servers (tests/ai_standin.py)
that play the llama.cpp gateway and the Ollama container - up, down, slow, erroring, malformed or half-working.

* order: gateway -> docker -> anthropic -> AIUnavailableError (a feature's friendly fallback text);
* time to fail over: connection refused / HTTP errors / malformed answers at once, a switched-off host within
  CONNECT_TIMEOUT_SECONDS, a slow provider at its first-token deadline;
* a stream that breaks before its first words moves on to the next provider; one that already said something ends
  with the interruption label - never two answers spliced together - and an answer cut at max_tokens says so;
* circuit breakers open after repeated failures (a provider dying mid-answer included), skip the provider while open,
  half-open after the cooldown and close when it answers again; the health check closes and opens them too;
* the AI guard settles once per request whatever the failover; the answer and the call log name the provider that
  produced it; the single Ollama slot is respected and shared fairly between members.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any

import pytest

from app.ai import guard as guard_module
from app.ai.client import (
    CLOUD_CONNECT_TIMEOUT_SECONDS,
    CONNECT_TIMEOUT_SECONDS,
    CUT_MARKER,
    INTERRUPTED_MARKER,
    AnthropicProvider,
    FakeProvider,
    OpenAICompatProvider,
    _FairSlots,
    ai,
    call_log_queue,
    connect_timeout_for,
)
from app.ai.errors import AIUnavailableError
from app.ai.owner import AIOwner
from app.config import get_settings
from tests.ai_standin import BLACKHOLE_URL, StandIn, blackhole_hangs, free_port
from tests.conftest import api, login_as
from tests.factories import make_customer

LABELS = {"gateway": "Gemma (gateway)", "docker": "Gemma (Docker failsafe)"}
DOCKER_ANSWER = "docker answers this question."
GATEWAY_ANSWER = "gateway answers this question."


class RecordingGuard:
    """Stands in for ``app.ai.guard.guard``: admits everything and records each settle (True = charged)."""

    def __init__(self) -> None:
        self.admitted: list[str] = []
        self.settled: list[bool] = []

    async def admit(self, owner: Any, feature: str, text: str | None = None, *, scope: str | None = None) -> Any:
        self.admitted.append(feature)
        return SimpleNamespace(n=len(self.admitted))

    async def settle(self, admission: Any, *, ok: bool) -> None:
        self.settled.append(ok)


@pytest.fixture(autouse=True)
def guard(monkeypatch: pytest.MonkeyPatch) -> RecordingGuard:
    fake = RecordingGuard()
    monkeypatch.setattr(guard_module, "guard", fake)
    call_log_queue.clear()
    yield fake
    call_log_queue.clear()


@pytest.fixture
async def servers():
    gateway = await StandIn("gateway", port=free_port(12301)).start()
    docker = await StandIn("docker", port=free_port(12302)).start()
    yield gateway, docker
    await gateway.stop()
    await docker.stop()


def settings(**overrides: Any) -> Any:
    return get_settings().model_copy(update={"ai_circuit_failures": 2, "ai_circuit_cooldown_seconds": 45, **overrides})


def provider(name: str, target: StandIn | str, *, s: Any = None, slots: int = 4, timeout: float = 30.0,
             connect_timeout: float | None = None) -> OpenAICompatProvider:
    url = target if isinstance(target, str) else target.base_url
    return OpenAICompatProvider(s or settings(), name=name, label=LABELS[name], base_url=url, api_key="k",
                                model="gemma", timeout=timeout, max_concurrency=slots, connect_timeout=connect_timeout)


def chain(gateway: StandIn | str, docker: StandIn | str, *, s: Any = None, docker_slots: int = 1,
          claude_fails: bool = True) -> list[Any]:
    providers = [provider("gateway", gateway, s=s), provider("docker", docker, s=s, slots=docker_slots),
                 FakeProvider("anthropic", fail=claude_fails, responses=["claude answers."])]
    ai.use_providers(providers)
    return providers


async def ask_stream(**kwargs: Any) -> tuple[list[str], dict[str, Any], float]:
    meta: dict[str, Any] = {}
    kwargs.setdefault("owner", AIOwner(user_id=7))
    started = time.perf_counter()
    out = [chunk async for chunk in ai.stream(feature="plans", system="sys", prompt=kwargs.pop("prompt", "p"),
                                              meta=meta, **kwargs)]
    return out, meta, time.perf_counter() - started


async def drain(n: int = 30) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


def logged() -> list[tuple[str, bool]]:
    return [(c.provider, c.ok) for c in call_log_queue]


async def break_gateway(gateway: StandIn, mode: str) -> None:
    if mode == "down":
        await gateway.stop()  # connection refused
    else:
        gateway.mode = mode
        gateway.delay = 30.0  # slow modes: the first words (or the headers) would come after 30 s


BROKEN = ["down", "error500", "error503", "bad_json", "broken_sse", "empty_choices", "die_before_text", "slow",
          "slow_headers"]


# ------------------------------------------------------------------ the failover matrix
@pytest.mark.parametrize("mode", BROKEN)
async def test_a_stream_fails_over_from_a_broken_gateway_to_docker(servers, guard, mode):
    gateway, docker = servers
    await break_gateway(gateway, mode)
    chain(gateway, docker)
    out, meta, took = await ask_stream(first_token_timeout=1.0, user_text="q")
    assert "".join(out) == DOCKER_ANSWER  # docker's answer alone: nothing of the gateway's
    assert meta == {"provider": "docker", "model": "gemma", "label": LABELS["docker"]}
    if mode.startswith("slow"):
        assert 0.9 <= took < 2.0, took  # abandoned at its first-token deadline
    else:
        assert took < 0.9, took  # refused / erroring / malformed: at once
    await drain()
    assert guard.settled == [True]  # charged once, whatever the failover
    assert logged() == [("gateway", False), ("docker", True)]


@pytest.mark.parametrize("mode", BROKEN)
async def test_a_completion_fails_over_from_a_broken_gateway_to_docker(servers, guard, mode):
    gateway, docker = servers
    await break_gateway(gateway, mode)
    chain(gateway, docker)
    started = time.perf_counter()
    res = await ai.complete(feature="plans", system="sys", prompt="p", timeout=1.0, owner=AIOwner(user_id=7))
    took = time.perf_counter() - started
    assert (res.text, res.provider, res.model) == (DOCKER_ANSWER, "docker", "gemma")
    assert took < (2.0 if mode.startswith("slow") else 0.9), took
    await drain()
    assert guard.settled == [True]
    assert logged() == [("gateway", False), ("docker", True)]


async def test_docker_down_too_moves_on_to_claude_then_the_friendly_fallback(servers, guard, client, db):
    gateway, docker = servers
    await gateway.stop()
    docker.mode = "error503"
    chain(gateway, docker, claude_fails=False)
    out, meta, _ = await ask_stream()
    assert "".join(out) == "claude answers." and meta["provider"] == "anthropic"
    assert logged() == [("gateway", False), ("docker", False), ("anthropic", True)]

    chain(gateway, docker, claude_fails=True)  # every provider down
    with pytest.raises(AIUnavailableError):
        await ask_stream()
    with pytest.raises(AIUnavailableError):
        await ai.complete(feature="plans", system="sys", prompt="p", owner=AIOwner(user_id=7))
    await drain()
    assert guard.settled == [True, False, False]  # nothing is charged for an answer nobody gave

    # through a real feature: the plan advisor answers with its deterministic recommendation, labelled as such
    chain(gateway, docker, claude_fails=True)
    user = await make_customer(db)
    await login_as(client, user)
    r = await api(client, "POST", "/api/plans/ask", json={"question": "Which plan suits a beginner?"})
    assert r.status_code == 200
    events = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]
    assert events[-1]["type"] == "done" and events[-1]["fallback"] is True
    assert events[-1]["note"].startswith("AI is unavailable")
    assert any(e["type"] == "chunk" and e["text"].strip() for e in events)


async def test_a_switched_off_gateway_fails_over_within_the_connect_timeout(servers, guard):
    """No RST, no answer (a PC that is off): the TCP connect itself hangs. The provider's own timeout is 90 s; the
    connect timeout ends the attempt after CONNECT_TIMEOUT_SECONDS - for a stream and for a completion without a
    timeout of its own (background jobs)."""
    if not blackhole_hangs():
        pytest.skip("this network rejects TEST-NET-1 at once - a dead host cannot be emulated here")
    _, docker = servers
    gateway = provider("gateway", BLACKHOLE_URL, timeout=90.0)
    ai.use_providers([gateway, provider("docker", docker, slots=1), FakeProvider("anthropic", fail=True)])
    out, meta, took = await ask_stream(first_token_timeout=12.0)
    assert meta["provider"] == "docker" and "".join(out) == DOCKER_ANSWER
    assert CONNECT_TIMEOUT_SECONDS - 0.3 <= took < CONNECT_TIMEOUT_SECONDS + 1.0, took
    started = time.perf_counter()
    res = await ai.complete(feature="plans", system="sys", prompt="p", owner=AIOwner(user_id=7))
    assert res.provider == "docker" and time.perf_counter() - started < CONNECT_TIMEOUT_SECONDS + 1.0
    assert gateway.circuit_open()  # two failures: skipped from now on, no 2 s detour per request
    started = time.perf_counter()
    await ask_stream()
    assert time.perf_counter() - started < 0.5


def test_providers_use_a_short_connect_timeout():
    gateway = provider("gateway", "http://192.0.2.1:9/v1", timeout=90.0)
    timeout = gateway.client().timeout
    assert timeout.connect == CONNECT_TIMEOUT_SECONDS and timeout.read == 90.0
    claude = AnthropicProvider(get_settings().model_copy(update={"anthropic_api_key": "sk-ant-test"}))
    assert claude.client().timeout.connect == CLOUD_CONNECT_TIMEOUT_SECONDS
    assert connect_timeout_for(SimpleNamespace(ai_connect_timeout_seconds=0.5)) == 0.5
    assert connect_timeout_for(SimpleNamespace(ai_connect_timeout_seconds="nonsense")) == CONNECT_TIMEOUT_SECONDS
    assert connect_timeout_for(SimpleNamespace(ai_connect_timeout_seconds=99)) == 30.0
    assert connect_timeout_for(SimpleNamespace(), floor=5.0) == 5.0
    assert provider("docker", "http://127.0.0.1:1/v1", timeout=1.0).connect_timeout == 1.0  # never above the timeout


# ------------------------------------------------------------------ half-working providers
async def test_a_stream_that_dies_after_its_first_words_is_labelled_never_spliced(servers, guard):
    gateway, docker = servers
    gateway.mode = "die_mid_stream"
    chain(gateway, docker)
    out, meta, _ = await ask_stream(user_text="q")
    assert out == ["gateway", " answers", INTERRUPTED_MARKER]
    assert meta["provider"] == "gateway" and meta["truncated"] is True
    assert docker.requests == []  # nobody else was asked: no second answer glued onto the first
    await drain()
    assert guard.settled == [False]  # refunded: the answer broke off
    assert logged() == [("gateway", False)]  # and the call log says so (it used to log this as a success)


async def test_a_stream_that_dies_before_its_first_words_is_retried_on_the_next_provider(servers, guard):
    gateway, docker = servers
    gateway.mode = "die_before_text"  # the role chunk came, no text, then the connection broke
    chain(gateway, docker)
    out, meta, _ = await ask_stream()
    assert "".join(out) == DOCKER_ANSWER and meta["provider"] == "docker" and "truncated" not in meta
    await drain()
    assert guard.settled == [True]


async def test_a_provider_that_keeps_dying_mid_answer_opens_its_breaker(servers, guard):
    gateway, docker = servers
    gateway.mode = "die_mid_stream"
    providers = chain(gateway, docker)
    for _ in range(2):
        out, meta, _ = await ask_stream()
        assert out[-1] == INTERRUPTED_MARKER and meta["provider"] == "gateway"
    assert providers[0].circuit_open()  # (it never opened: the first words used to reset its failure count)
    out, meta, _ = await ask_stream()
    assert "".join(out) == DOCKER_ANSWER and meta["provider"] == "docker" and len(gateway.requests) == 2


async def test_an_answer_cut_at_max_tokens_says_so(servers, guard):
    gateway, docker = servers
    gateway.mode = "length"  # finish_reason 'length'
    providers = chain(gateway, docker)
    out, meta, _ = await ask_stream()
    assert "".join(out[:-1]) == GATEWAY_ANSWER and out[-1] == CUT_MARKER
    assert meta["provider"] == "gateway" and meta["truncated"] is True
    await drain()
    assert guard.settled == [True] and not providers[0].circuit_open() and docker.requests == []


# ------------------------------------------------------------------ circuit breakers
async def test_the_breaker_opens_skips_half_opens_and_recovers(servers, guard):
    gateway, docker = servers
    gateway.mode = "error503"
    providers = chain(gateway, docker, s=settings(ai_circuit_cooldown_seconds=1))
    for _ in range(2):
        _, meta, _ = await ask_stream()
        assert meta["provider"] == "docker"
    assert len(gateway.requests) == 2 and providers[0].circuit_open()
    status = ai.status()
    assert (status["state"], status["active"]) == ("degraded", "docker")
    _, meta, _ = await ask_stream()
    assert meta["provider"] == "docker" and len(gateway.requests) == 2  # skipped while open
    await asyncio.sleep(1.1)  # cooldown over: half-open, the next call tries it once more ...
    _, meta, _ = await ask_stream()
    assert meta["provider"] == "docker" and len(gateway.requests) == 3 and providers[0].circuit_open()  # ... still broken
    gateway.mode = "ok"  # it comes back
    await asyncio.sleep(1.1)
    out, meta, _ = await ask_stream()
    assert "".join(out) == GATEWAY_ANSWER and meta["provider"] == "gateway"
    assert not providers[0].circuit_open() and providers[0].state.consecutive_failures == 0
    assert ai.status()["state"] == "online"


async def test_a_restarted_container_is_found_again_by_calls_and_by_the_health_check(servers, guard):
    gateway, docker = servers
    await gateway.stop()
    await docker.stop()
    providers = chain(gateway, docker, s=settings(ai_circuit_cooldown_seconds=1))
    status = await ai.refresh_health()  # the health job: both Gemma servers down
    assert status["state"] == "offline" and providers[0].circuit_open() and providers[1].circuit_open()
    with pytest.raises(AIUnavailableError):
        await ai.complete(feature="plans", system="sys", prompt="p", owner=AIOwner(user_id=7))
    await docker.restart("ok")  # `docker compose up -d ollama` - same port
    await asyncio.sleep(1.1)
    res = await ai.complete(feature="plans", system="sys", prompt="p", owner=AIOwner(user_id=7))
    assert res.provider == "docker" and not providers[1].circuit_open()
    await gateway.restart("ok")
    await asyncio.sleep(1.1)
    status = await ai.refresh_health()
    assert status["state"] == "online" and status["active"] == "gateway"


# ------------------------------------------------------------------ the single Ollama slot
async def test_the_single_docker_slot_is_respected_and_shared_fairly(servers, guard):
    gateway, docker = servers
    await gateway.stop()
    docker.chunk_delay = 0.12  # each answer takes about 0.6 s
    chain(gateway, docker, docker_slots=1)

    async def ask(uid: int, tag: str) -> str:
        out, meta, _ = await ask_stream(prompt=tag, first_token_timeout=10.0, owner=AIOwner(user_id=uid))
        assert meta["provider"] == "docker" and "".join(out) == DOCKER_ANSWER
        return tag

    first = asyncio.create_task(ask(1, "A1"))
    await asyncio.sleep(0.15)
    second = asyncio.create_task(ask(1, "A2"))  # the same member again
    await asyncio.sleep(0.05)
    other = asyncio.create_task(ask(2, "B1"))  # another member, a moment later
    await asyncio.gather(first, second, other)
    served = [r["body"]["messages"][1]["content"] for r in docker.requests]
    assert served == ["A1", "B1", "A2"]  # B's first request goes before A's second (FIFO would serve A2 first)
    assert docker.max_inflight == 1  # Ollama never ran two at once
    await drain()
    assert guard.settled == [True, True, True]


async def test_fair_slots_order_cancellation_and_timeouts():
    slots = _FairSlots(1)
    order: list[str] = []
    await slots.acquire("A")
    assert slots.locked()

    async def take(bucket: str, tag: str) -> None:
        await slots.acquire(bucket)
        order.append(tag)

    waiters = [asyncio.create_task(take("A", "A2"))]
    await drain()
    waiters.append(asyncio.create_task(take("B", "B1")))
    await drain()
    waiters.append(asyncio.create_task(take("C", "C1")))
    await drain()
    gone = asyncio.create_task(take("D", "D1"))
    await drain()
    gone.cancel()  # left the line: never served, never holds a slot
    await asyncio.gather(gone, return_exceptions=True)
    with pytest.raises(TimeoutError):  # a timed-out waiter leaves the line too
        async with asyncio.timeout(0.05):
            await slots.acquire("E")
    assert slots.waiting() == 3
    for _ in range(3):
        slots.release()
        await drain()
    assert order == ["B1", "C1", "A2"]  # least recently served first; A was served last
    slots.release()
    assert not slots.locked() and slots.waiting() == 0

    # a slot granted to a waiter cancelled at that very moment is handed on, never lost
    await slots.acquire("A")
    victim = asyncio.create_task(slots.acquire("B"))
    heir = asyncio.create_task(slots.acquire("C"))
    await drain()
    slots.release()  # granted to B ...
    victim.cancel()  # ... which is cancelled before it could run
    await asyncio.gather(victim, return_exceptions=True)
    await drain()
    assert heir.done() and slots.locked()  # C got it
    slots.release()
    assert not slots.locked()
