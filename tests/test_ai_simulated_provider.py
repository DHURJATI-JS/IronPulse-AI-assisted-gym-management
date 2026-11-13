"""The simulated AI provider for load tests (app/ai/simulated.py + ``SimulatedProvider`` in app/ai/client.py).

* selected only by naming ``simulated`` in AI_PROVIDER_ORDER - never by default, never as an automatic fallback, and
  never twice (a repeated name is used once);
* realistic latency: the first words after AI_SIM_FIRST_TOKEN_MS, the rest at AI_SIM_TOKENS_PER_S; streaming in small
  chunks; deterministic plausible words per prompt; schema-valid, conservative JSON for structured calls, without
  rebuilding JSON schemas on the server's event loop for every answer;
* it is a provider like the others: its own slots (cancelling a stream mid-answer frees the slot and the queue), the
  circuit breaker counts its failures, the AI guard charges and refunds it like a real provider, and the fair queue's
  lanes, per-user caps and max_wait apply; a burst of mixed calls (with and without outages, cancelled every way)
  leaves no slot, ticket or task behind;
* the coach voice (Kokoro lane) is simulated through the voice service's client hook;
* ENVIRONMENT=production refuses it without AI_SIMULATED_ALLOWED=true (settings and registry), and announces it loudly
  when allowed; the super-admin AI page and its provider test name it 'simulated'.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import random
import time
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.fernet import Fernet

from app.ai import guard as guard_module
from app.ai import simulated
from app.ai.assistant import AssistantOutput
from app.ai.client import (
    AIRouter,
    FakeProvider,
    SimulatedProvider,
    ai,
    call_log_queue,
    install_simulated_voice,
)
from app.ai.coach import PlanModel
from app.ai.errors import AICancelled, AIRejected, AIUnavailableError
from app.ai.guard import AIGuard
from app.ai.health import ACSummary, HealthAssessment, MeasurementReading
from app.ai.idcard import IdCardAssessment
from app.ai.intake import IntakeExtraction
from app.ai.live_coach import CoachReply
from app.ai.owner import AIOwner
from app.ai.payroll import PayrollExplanation
from app.ai.scheduler import scheduler
from app.ai.shop import ProductAnswer
from app.ai.triage import TriageOutput
from app.ai.verification import BranchVerdict
from app.ai.wellness import FaceAnalysis
from app.config import SIMULATED_AI, ConfigError, build_settings, get_settings
from app.models.enums import Role
from app.services.gear import _AiGearOut
from app.services.staffing import CertificationCheck
from app.services.tts import tts
from tests.conftest import api, login_as
from tests.factories import make_user

STRONG = {"secret_key": "pT9-vK2_mQ7xZ4rW8nB5cH3jL6dF1gY0sA7eU4iO2", "id_hash_pepper": "Lq4-Wm8_Zt2Kc6Rn9Hy3",
          "data_encryption_key": Fernet.generate_key().decode()}


# ------------------------------------------------------------------ helpers
def fast_settings(**overrides: Any) -> Any:
    """The suite's settings with the simulated provider selected and short, fixed latencies."""
    values: dict[str, Any] = {"ai_provider_order": ["simulated"], "ai_sim_first_token_ms": (20.0, 40.0),
                              "ai_sim_tokens_per_s": (400.0, 400.0), "ai_sim_failure_rate": 0.0,
                              "ai_sim_max_concurrency": 2, "ai_sim_tts_ms": (10.0, 20.0)}
    values.update(overrides)
    return get_settings().model_copy(update=values)


def provider(**overrides: Any) -> SimulatedProvider:
    return SimulatedProvider(fast_settings(**overrides), rng=random.Random(7))


class RecordingGuard:
    """Stands in for ``app.ai.guard.guard``: admits everything and records the settles (``True`` = charged)."""

    def __init__(self) -> None:
        self.admitted: list[tuple[str, str | None]] = []
        self.settled: list[bool] = []

    async def admit(self, owner: Any, feature: str, text: str | None = None, *, scope: str | None = None) -> Any:
        self.admitted.append((feature, text))
        return SimpleNamespace(n=len(self.admitted))

    async def settle(self, admission: Any, *, ok: bool) -> None:
        self.settled.append(ok)


@pytest.fixture
def recording_guard(monkeypatch: pytest.MonkeyPatch) -> RecordingGuard:
    fake = RecordingGuard()
    monkeypatch.setattr(guard_module, "guard", fake)
    return fake


@pytest.fixture
async def real_guard(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The real AI guard on its in-process store (no Redis)."""
    async def no_redis() -> None:
        return None

    monkeypatch.setattr(guard_module, "_redis_client", no_redis)
    instance = AIGuard()
    await instance.reset()
    monkeypatch.setattr(guard_module, "guard", instance)
    yield instance
    await instance.reset()


async def drain(n: int = 20) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


def llm_lane() -> dict[str, Any]:
    return scheduler.snapshot()["lanes"]["llm"]


def staff(uid: int) -> AIOwner:
    return AIOwner(user_id=uid, role="staff", ip="198.51.100.7", session_hash=f"{uid:064x}")


# ------------------------------------------------------------------ selection
def test_never_selected_by_default_only_when_named(monkeypatch):
    monkeypatch.delenv("AI_PROVIDER_ORDER", raising=False)
    default = build_settings(_env_file=None, **STRONG)
    assert default.ai_provider_order == ["gateway", "docker", "anthropic"]
    assert not any(isinstance(p, SimulatedProvider) for p in AIRouter(default).providers)
    # unknown names fall back to the default chain - never to the simulated provider
    assert build_settings(_env_file=None, ai_provider_order="bogus,gpu", **STRONG).ai_provider_order == \
        ["gateway", "docker", "anthropic"]
    assert build_settings(_env_file=None, ai_provider_order="Simulated", **STRONG).ai_provider_order == [SIMULATED_AI]
    assert simulated.NAME == SIMULATED_AI

    named = AIRouter(fast_settings())
    assert [type(p) for p in named.providers] == [SimulatedProvider]
    info = named.providers[0].describe()
    assert (info["name"], info["label"], info["model"], info["configured"]) == \
        ("simulated", "Simulated AI (load test)", "simulated", True)
    assert info["detail"]["simulated"] is True and info["detail"]["first_token_ms"] == "20..40"
    assert named.status()["active"] == "simulated" and named.status()["state"] == "online"
    assert [p.name for p in AIRouter(fast_settings(ai_provider_order=["gateway", "simulated"])).providers] == \
        ["gateway", "simulated"]


async def test_a_repeated_provider_name_is_used_once(monkeypatch, recording_guard):
    """AI_PROVIDER_ORDER=simulated,simulated (or gateway,gateway) must not put one provider in the chain twice: a
    failing call would try it again at once and count two failures towards its circuit breaker."""
    monkeypatch.delenv("AI_PROVIDER_ORDER", raising=False)
    s = build_settings(_env_file=None, ai_provider_order="simulated, gateway,SIMULATED,gateway,docker", **STRONG)
    assert s.ai_provider_order == ["simulated", "gateway", "docker"]
    # settings copied without validation (tests, tools): the registry builds each provider once all the same
    router = AIRouter(fast_settings(ai_provider_order=["simulated", "simulated"], ai_sim_failure_rate=1.0))
    assert [p.name for p in router.providers] == ["simulated"]
    ai.use_providers(router.providers)
    with pytest.raises(AIUnavailableError) as err:
        await ai.complete(feature="assistant", system="s", prompt="p", owner=AIOwner(user_id=16))
    assert str(err.value) == "simulated: simulated outage"  # tried once
    assert router.providers[0].state.calls == 1 and router.providers[0].state.failures == 1


# ------------------------------------------------------------------ latency, words, chunks
async def test_complete_takes_the_first_token_delay_plus_generation_time(recording_guard):
    p = provider(ai_sim_first_token_ms=(60.0, 90.0), ai_sim_tokens_per_s=(1000.0, 1000.0))
    ai.use_providers([p])
    started = time.perf_counter()
    res = await ai.complete(feature="assistant", system="sys", prompt="How do I warm up?", max_tokens=30,
                            owner=AIOwner(user_id=5))
    took = time.perf_counter() - started
    words = len(res.text.split())
    assert (res.provider, res.model) == ("simulated", "simulated") and 1 <= words <= 30
    assert 0.055 <= took < 0.09 + words / 1000 + 0.5
    assert 55 <= res.latency_ms < 90 + words + 500
    assert res.text == p.sim.answer("sys", "How do I warm up?", 30)  # deterministic per prompt
    assert p.state.calls == 1 and p.state.healthy is True and not p.semaphore.locked()


async def test_stream_sends_small_chunks_after_the_first_token_delay(recording_guard):
    p = provider(ai_sim_first_token_ms=(50.0, 50.0), ai_sim_tokens_per_s=(200.0, 200.0))
    ai.use_providers([p])
    meta: dict[str, Any] = {}
    stamps: list[float] = []
    chunks: list[str] = []
    started = time.perf_counter()
    async for chunk in ai.stream(feature="plans", system="sys", prompt="Which plan fits me?", max_tokens=60, meta=meta,
                                 owner=AIOwner(user_id=6)):
        stamps.append(time.perf_counter() - started)
        chunks.append(chunk)
    text = "".join(chunks)
    words = len(text.split())
    assert meta["provider"] == "simulated" and "truncated" not in meta
    assert len(chunks) > 3 and all(1 <= len(c.split()) <= 3 for c in chunks)
    assert text == p.sim.answer("sys", "Which plan fits me?", 60) and words <= 60
    assert stamps[0] >= 0.045  # the first words wait for the first-token delay
    rest = words - len(chunks[0].split())
    assert stamps[-1] - stamps[0] >= rest / 200 * 0.75 - 0.01  # the rest at ~200 words/s (+-25 % a chunk)
    assert stamps[-1] < 0.05 + words / 200 * 1.25 + 0.5
    # the same prompt gets the same words through complete(); another prompt other words
    same = await ai.complete(feature="plans", system="sys", prompt="Which plan fits me?", max_tokens=60,
                             owner=AIOwner(user_id=6))
    other = await ai.complete(feature="plans", system="sys", prompt="Which plan suits a beginner?", max_tokens=60,
                              owner=AIOwner(user_id=6))
    assert same.text == text and other.text != text
    assert recording_guard.settled == [True, True, True]


async def test_structured_answers_validate_and_stay_conservative(recording_guard):
    ai.use_providers([provider()])

    async def ask(schema: Any) -> Any:
        return (await ai.complete(feature="intake", system="s", prompt=f"fill {schema.__name__}", schema=schema,
                                  owner=AIOwner(user_id=8))).data

    plan = await ask(PlanModel)
    assert isinstance(plan, PlanModel) and plan.days and plan.days[0].items
    assert (await ask(BranchVerdict)).decision == "needs_review"
    assert (await ask(IdCardAssessment)).looks_like_government_id is False
    assert (await ask(HealthAssessment)).escalate_to_suspension is False
    assistant = await ask(AssistantOutput)
    assert assistant.actions == [] and assistant.reply


def test_every_ai_schema_of_the_app_gets_valid_json():
    model = simulated.SimulatedModel(rng=random.Random(1))
    for schema in (PlanModel, AssistantOutput, BranchVerdict, IdCardAssessment, HealthAssessment, ACSummary,
                   MeasurementReading, CoachReply, ProductAnswer, IntakeExtraction, FaceAnalysis, PayrollExplanation,
                   TriageOutput, CertificationCheck, _AiGearOut):
        text = model.json_answer(schema, "system", "prompt")
        schema.model_validate_json(text)
        assert text == model.json_answer(schema, "system", "prompt")
    assert TriageOutput.model_validate_json(model.json_answer(TriageOutput, "s", "p")).ips == []


def test_structured_answers_build_each_json_schema_once():
    """Pydantic rebuilds a JSON schema on every model_json_schema() call (~0.5-1.5 ms of event-loop time for the app's
    schemas): under load the simulated provider must not spend the server's CPU on it for every answer."""
    from pydantic import BaseModel

    built: list[type] = []

    class Note(BaseModel):
        summary: str
        steps: list[str] = []

        @classmethod
        def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
            built.append(cls)
            return super().model_json_schema(*args, **kwargs)

    model = simulated.SimulatedModel(rng=random.Random(1))
    answers = [model.json_answer(Note, "system", f"prompt {i}") for i in range(25)]
    assert all(Note.model_validate_json(a).summary for a in answers) and len(set(answers)) > 1
    assert built == [Note]


def test_the_engine_is_deterministic_per_prompt_and_bounded():
    a, b = simulated.SimulatedModel(rng=random.Random(1)), simulated.SimulatedModel(rng=random.Random(2))
    assert a.answer("s", "p", 350) == b.answer("s", "p", 350) != a.answer("s", "q", 350)
    assert len(a.answer("s", "p", 8).split()) == 8 and a.answer("s", "p", 8).endswith(".")
    text = a.answer("s", "p", 350)
    assert "".join(a.chunks(text)) == text and a.chunks(text) == b.chunks(text)
    fixed = simulated.SimulatedModel(first_token_ms=(400, 1500), tokens_per_s=(25, 60), rng=random.Random(5))
    delays = [fixed.first_token_seconds() for _ in range(200)]
    rates = [fixed.words_per_second() for _ in range(200)]
    assert 0.4 <= min(delays) and max(delays) <= 1.5 and 25 <= min(rates) and max(rates) <= 60
    assert simulated.silent_mp3(4.0)[:4] == b"\xff\xfb\x90\xc4" and len(simulated.silent_mp3(4.0)) % 417 == 0


# ------------------------------------------------------------------ slots, cancellation, the queue
async def test_cancelling_mid_stream_frees_the_provider_slot_and_the_queue(recording_guard):
    p = provider(ai_sim_max_concurrency=1, ai_sim_first_token_ms=(10.0, 10.0), ai_sim_tokens_per_s=(4.0, 4.0))
    ai.use_providers([p])
    agen = ai.stream(feature="plans", system="s", prompt="a long answer please", max_tokens=200,
                     owner=AIOwner(user_id=9, page_id="page-sim-0001"))
    first = await agen.__anext__()
    assert first and p.semaphore.locked() and llm_lane()["running"] == 1
    started = time.monotonic()
    assert scheduler.cancel(page_id="page-sim-0001", reason="page_closed") == 1
    assert [chunk async for chunk in agen] == []  # nothing after the cancel, and at once
    assert time.monotonic() - started < 0.2
    assert not p.semaphore.locked() and llm_lane()["running"] == 0
    assert scheduler.snapshot()["counters"]["cancelled"] == {"page_closed": 1}
    log = call_log_queue[-1]
    assert (log.provider, log.cancelled, log.user_id) == ("simulated", True, 9) and p.state.failures == 0
    await drain()
    assert recording_guard.settled == [True]  # its first words went out: charged, as with any provider
    # the reader closing the stream frees the slot too
    agen = ai.stream(feature="plans", system="s", prompt="another long answer", max_tokens=200, owner=AIOwner(user_id=9))
    assert await agen.__anext__() and p.semaphore.locked()
    await agen.aclose()
    assert not p.semaphore.locked() and llm_lane()["running"] == 0
    # ... so the next caller gets the only slot at once
    fast = await ai.complete(feature="plans", system="s", prompt="short", max_tokens=1, owner=AIOwner(user_id=10))
    assert fast.provider == "simulated" and call_log_queue[-1].queue_ms < 100


async def test_a_stream_stopped_before_its_first_words_is_free(recording_guard):
    p = provider(ai_sim_max_concurrency=1, ai_sim_first_token_ms=(400.0, 400.0))
    ai.use_providers([p])
    agen = ai.stream(feature="plans", system="s", prompt="p", owner=AIOwner(user_id=11, page_id="page-sim-0002"))
    task = asyncio.create_task(agen.__anext__())
    await asyncio.sleep(0.05)
    assert p.semaphore.locked()  # the provider holds its slot while it 'thinks'
    scheduler.cancel(page_id="page-sim-0002", reason="page_closed")
    with pytest.raises(AICancelled):
        await task
    await agen.aclose()
    await drain()
    assert not p.semaphore.locked() and llm_lane()["running"] == 0 and recording_guard.settled == [False]
    assert p.state.failures == 0  # a cancel is never a provider failure


async def test_the_fair_queue_lanes_and_per_user_caps_apply(recording_guard, monkeypatch):
    p = provider(ai_sim_first_token_ms=(150.0, 150.0), ai_sim_tokens_per_s=(1000.0, 1000.0), ai_sim_max_concurrency=4)
    ai.use_providers([p])
    monkeypatch.setattr(get_settings(), "ai_queue_max_per_user", 1)
    owner = AIOwner(user_id=12)
    first = asyncio.create_task(ai.complete(feature="assistant", system="s", prompt="a", owner=owner))
    await asyncio.sleep(0.03)
    with pytest.raises(AIRejected) as err:
        await ai.complete(feature="assistant", system="s", prompt="b", owner=owner)
    assert err.value.code == "too_many"
    await first
    # one LLM call at a time in this process: the second person's call waits for the first one's ~150 ms
    monkeypatch.setattr(get_settings(), "ai_queue_concurrency", 1)
    monkeypatch.setattr(get_settings(), "ai_queue_max_per_user", 4)
    call_log_queue.clear()
    await asyncio.gather(ai.complete(feature="assistant", system="s", prompt="c", owner=AIOwner(user_id=13)),
                         ai.complete(feature="assistant", system="s", prompt="d", owner=AIOwner(user_id=14)))
    waits = sorted(c.queue_ms or 0 for c in call_log_queue)
    assert len(waits) == 2 and waits[0] < 60 and waits[1] >= 100


async def test_the_queue_max_wait_turns_a_long_simulated_wait_into_busy(recording_guard, monkeypatch):
    """A slow simulated answer holds the only LLM slot; a member whose feature allows a short wait gets 'busy' (free),
    the running answer is untouched."""
    import dataclasses

    from app.ai import policy

    monkeypatch.setitem(policy.FEATURES, "plans", dataclasses.replace(policy.FEATURES["plans"], max_wait=0.1))
    monkeypatch.setattr(get_settings(), "ai_queue_concurrency", 1)
    p = provider(ai_sim_first_token_ms=(400.0, 400.0), ai_sim_tokens_per_s=(1000.0, 1000.0))
    ai.use_providers([p])
    slow = asyncio.create_task(ai.complete(feature="assistant", system="s", prompt="slow", owner=AIOwner(user_id=17)))
    await asyncio.sleep(0.05)
    started = time.perf_counter()
    with pytest.raises(AIRejected) as err:
        await ai.complete(feature="plans", system="s", prompt="quick", owner=AIOwner(user_id=18))
    assert err.value.code == "busy" and 0.09 <= time.perf_counter() - started < 1.5  # (without max_wait: answered)
    assert (await slow).provider == "simulated" and p.state.calls == 1  # the refused call never reached the provider
    await drain()
    assert sorted(recording_guard.settled) == [False, True]  # refused: free; answered: charged


# ------------------------------------------------------------------ the guard and the circuit breaker
async def test_the_guard_charges_it_like_a_real_provider(real_guard):
    question = "how do I warm up properly before squats"
    ai.use_providers([provider()])
    await ai.complete(feature="assistant", system="s", prompt="p", user_text=question, owner=staff(31))
    ai.use_providers([FakeProvider("gateway", responses=["fine"])])
    await ai.complete(feature="assistant", system="s", prompt="p", user_text=question, owner=staff(32))
    await drain()
    simulated_usage = (await real_guard.status(31))["usage"]["chat"]
    assert simulated_usage == (await real_guard.status(32))["usage"]["chat"] == [1, 1, 1]
    # its duplicate check is the real one
    ai.use_providers([provider()])
    with pytest.raises(AIRejected) as err:
        await ai.complete(feature="assistant", system="s", prompt="p", user_text=question, owner=staff(31))
    assert err.value.code == "duplicate"
    # a failed simulated call is refunded, like a failed real one
    ai.use_providers([provider(ai_sim_failure_rate=1.0)])
    with pytest.raises(AIUnavailableError):
        await ai.complete(feature="assistant", system="s", prompt="p", user_text="and how do I cool down after it",
                          owner=staff(31))
    await drain()
    assert (await real_guard.status(31))["usage"]["chat"] == [1, 1, 1]
    # a stream cut after its first words is charged
    ai.use_providers([provider(ai_sim_tokens_per_s=(5.0, 5.0))])
    agen = ai.stream(feature="assistant", system="s", prompt="p", user_text="tell me about rest days please",
                     owner=staff(31))
    assert await agen.__anext__()
    await agen.aclose()
    await drain()
    assert (await real_guard.status(31))["usage"]["chat"] == [2, 2, 2]


@pytest.mark.parametrize("failure_rate", [0.0, 0.2])
async def test_a_burst_of_mixed_calls_leaves_no_slot_ticket_or_task_behind(real_guard, monkeypatch, failure_rate):
    """Stress: 240 overlapping calls from 120 people through the real guard and queue - completions (free text, JSON,
    the live coach's 8 s / 9 s budget), streams read to the end, closed after the first words or stopped by a closed
    tab, callers cancelled at random - with and without simulated outages. Whatever each call's fate, nothing leaks:
    every provider slot is free again, the lanes are empty and no AI task is left running."""
    s = get_settings()
    monkeypatch.setattr(s, "ai_queue_concurrency", 4)
    monkeypatch.setattr(s, "ai_queue_max_per_user", 3)
    monkeypatch.setattr(s, "ai_circuit_failures", 10_000)  # keep the provider in play for the whole burst
    p = provider(ai_sim_first_token_ms=(5.0, 40.0), ai_sim_tokens_per_s=(400.0, 2000.0), ai_sim_max_concurrency=3,
                 ai_sim_failure_rate=failure_rate)
    ai.use_providers([p])
    rng = random.Random(5)
    seen: dict[str, int] = {}
    unexpected: list[BaseException] = []

    async def one(i: int) -> None:
        owner = staff(200 + rng.randrange(120))
        owner.page_id = f"page-burst-{i:05d}"
        kind = rng.choice(["text", "json", "coach", "stream", "stream_close", "stream_tab_closed", "cancelled"])
        words = f"question number {i} about training {rng.random()}"
        try:
            if kind == "text":
                await ai.complete(feature="assistant", system="s", prompt=words, user_text=words, owner=owner,
                                  max_tokens=rng.choice([5, 60, 300]))
            elif kind == "json":
                res = await ai.complete(feature="assistant", system="s", prompt=words, user_text=words, owner=owner,
                                        schema=AssistantOutput, timeout=20)
                assert isinstance(res.data, AssistantOutput)
            elif kind == "coach":
                res = await asyncio.wait_for(ai.complete(feature="live_coach", system="s", prompt=words, owner=owner,
                                                         user_text=words, schema=CoachReply, max_tokens=160,
                                                         timeout=8.0), timeout=9.0)
                assert isinstance(res.data, CoachReply)
            elif kind == "stream":
                text = "".join([c async for c in ai.stream(feature="plans", system="s", prompt=words, user_text=words,
                                                            owner=owner)])
                assert text
            elif kind == "stream_close":
                agen = ai.stream(feature="plans", system="s", prompt=words, user_text=words, owner=owner)
                try:
                    await agen.__anext__()
                finally:
                    await agen.aclose()
            elif kind == "stream_tab_closed":
                agen = ai.stream(feature="plans", system="s", prompt=words, user_text=words, owner=owner)
                tab_closed = functools.partial(scheduler.cancel, page_id=owner.page_id, reason="page_closed")
                closer = asyncio.get_running_loop().call_later(rng.random() * 0.04, tab_closed)
                try:
                    async for _ in agen:
                        pass
                finally:
                    closer.cancel()
                    await agen.aclose()
            else:
                task = asyncio.create_task(ai.complete(feature="assistant", system="s", prompt=words, user_text=words,
                                                       owner=owner))
                await asyncio.sleep(rng.random() * 0.05)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    kind = "cancelled:by_caller"
            outcome = f"{kind}:ok"
        except AIRejected as exc:
            outcome = f"rejected:{exc.code}"
        except AICancelled as exc:
            outcome = f"cancelled:{exc.reason}"
        except AIUnavailableError:
            outcome = f"{kind}:unavailable"
        except BaseException as exc:  # noqa: BLE001 - anything else is a bug
            unexpected.append(exc)
            return
        seen[outcome] = seen.get(outcome, 0) + 1

    for wave in range(3):
        await asyncio.gather(*(one(wave * 1000 + i) for i in range(80)))
    await drain(50)
    print(f"burst outcomes (failure rate {failure_rate}): {dict(sorted(seen.items()))}")
    assert not unexpected, [repr(e) for e in unexpected[:5]]
    assert sum(seen.values()) == 240 and any(k.endswith(":ok") for k in seen), seen
    assert p.semaphore._value == 3, "a provider slot leaked"  # noqa: SLF001
    lane = llm_lane()
    assert lane["running"] == 0 and lane.get("queued", 0) == 0, lane
    assert not [t for t in asyncio.all_tasks() if not t.done() and t.get_name().startswith("ai:")]
    assert (p.state.failures > 0) == (failure_rate > 0) and p.state.calls > 0


async def test_its_failures_feed_the_circuit_breaker_like_a_real_provider(recording_guard):
    bad = provider(ai_sim_failure_rate=1.0)
    ai.use_providers([bad])
    for _ in range(get_settings().ai_circuit_failures):
        with pytest.raises(AIUnavailableError, match="simulated outage"):
            await ai.complete(feature="assistant", system="s", prompt="p", owner=AIOwner(user_id=15))
    assert bad.circuit_open() and bad.describe()["status"] == "down" and bad.state.failures == 2
    with pytest.raises(AIUnavailableError, match="temporarily disabled"):
        await ai.complete(feature="assistant", system="s", prompt="p", owner=AIOwner(user_id=15))
    with pytest.raises(AIUnavailableError):
        async for _ in ai.stream(feature="plans", system="s", prompt="p", owner=AIOwner(user_id=15)):
            pass
    good = provider(ai_sim_failure_rate=0.0)
    ai.use_providers([good])
    for i in range(5):
        await ai.complete(feature="assistant", system="s", prompt=f"p{i}", owner=AIOwner(user_id=15))
    assert good.state.failures == 0 and not good.circuit_open() and good.describe()["status"] == "up"


# ------------------------------------------------------------------ production
def test_production_refuses_it_without_the_flag_and_announces_it_with_it(caplog, monkeypatch):
    monkeypatch.delenv("AI_PROVIDER_ORDER", raising=False)
    # the suite's PUBLIC_BASE_URL is plain http://testserver, which production now refuses next to Secure cookies
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://testserver")
    with pytest.raises(ConfigError) as err:
        build_settings(_env_file=None, environment="production", ai_provider_order="simulated", **STRONG)
    assert err.value.problems[0].startswith("AI_PROVIDER_ORDER names the simulated AI provider") and \
        "AI_SIMULATED_ALLOWED=true" in err.value.problems[0]
    allowed = build_settings(_env_file=None, environment="production", ai_provider_order="simulated",
                             ai_simulated_allowed=True, **STRONG)
    with caplog.at_level(logging.WARNING, logger="ironpulse.ai"):
        router = AIRouter(allowed)
    assert [p.name for p in router.providers] == ["simulated"]
    loud = [r for r in caplog.records if "SIMULATED AI PROVIDER ACTIVE" in r.getMessage()]
    assert len(loud) == 1 and loud[0].levelno == logging.WARNING
    assert "members get simulated answers" in loud[0].getMessage()
    # the registry's own safety net: production settings that skipped validation never build it
    caplog.clear()
    sneaked = allowed.model_copy(update={"ai_simulated_allowed": False, "ai_provider_order": ["simulated", "gateway"]})
    with caplog.at_level(logging.WARNING, logger="ironpulse.ai"):
        assert [p.name for p in AIRouter(sneaked).providers] == ["gateway"]
    assert any(r.levelno == logging.ERROR and "AI_SIMULATED_ALLOWED" in r.getMessage() for r in caplog.records)


def test_latency_settings_accept_ranges_and_name_the_variable_when_wrong(monkeypatch):
    monkeypatch.setenv("AI_SIM_FIRST_TOKEN_MS", "50..80")
    monkeypatch.setenv("AI_SIM_TOKENS_PER_S", "120")
    s = build_settings()
    assert s.ai_sim_first_token_ms == (50.0, 80.0) and s.ai_sim_tokens_per_s == (120.0, 120.0)
    assert s.ai_sim_tts_ms == (300.0, 1200.0) and s.ai_sim_failure_rate == 0.0 and not s.ai_simulated_allowed
    for var, value, why in (("AI_SIM_FIRST_TOKEN_MS", "1500..400", "runs backwards"),
                            ("AI_SIM_FIRST_TOKEN_MS", "soon", "use a number or a range"),
                            ("AI_SIM_TOKENS_PER_S", "0", "must be above 0"),
                            ("AI_SIM_TTS_MS", "-5", "0 or more"),
                            ("AI_SIM_FAILURE_RATE", "1.5", "less than or equal to 1")):
        monkeypatch.setenv(var, value)
        with pytest.raises(ConfigError) as err:
            build_settings()
        assert err.value.problems[0].startswith(f"{var} is invalid") and why in err.value.problems[0], err.value
        monkeypatch.delenv(var)
        monkeypatch.setenv("AI_SIM_FIRST_TOKEN_MS", "50..80")


# ------------------------------------------------------------------ the coach voice (Kokoro lane)
async def test_the_coach_voice_is_simulated_through_the_voice_service():
    model = simulated.SimulatedModel(tts_ms=(30.0, 30.0), rng=random.Random(3))
    speech = simulated.SimulatedSpeech(model)
    tts.use_client(speech)
    try:
        started = time.perf_counter()
        audio = await tts.synthesize("Rest 45 seconds, then the next set.")
        took = time.perf_counter() - started
        assert audio is not None and audio[:4] == b"\xff\xfb\x90\xc4" and len(audio) % len(simulated.MP3_FRAME) == 0
        assert 0.025 <= took < 0.5 and speech.calls == 1
        assert scheduler.snapshot()["lanes"]["tts"]["avg_service_ms"] is not None  # it went through the voice lane
        assert await tts.synthesize("Rest 45 seconds, then the next set.") == audio and speech.calls == 1  # cached
        tts.use_client(simulated.SimulatedSpeech(simulated.SimulatedModel(tts_ms=(1, 1), failure_rate=1.0)))
        assert await tts.synthesize("Another line") is None and tts.status()["state"] == "down"  # its breaker trips
    finally:
        tts.use_client(None)


def test_the_voice_is_installed_only_with_the_simulated_provider_outside_tests():
    try:
        assert install_simulated_voice(AIRouter(fast_settings(environment="development"))) is True
        assert tts.configured and getattr(tts._get_client(), "engine", None) == "simulated"
        tts.use_client(None)
        assert install_simulated_voice(AIRouter(fast_settings(environment="development", ai_sim_tts=False))) is False
        assert install_simulated_voice(AIRouter(fast_settings())) is False  # ENVIRONMENT=test injects its own
        assert install_simulated_voice(AIRouter(fast_settings(environment="development",
                                                              ai_provider_order=["gateway"]))) is False
        assert not tts.configured
    finally:
        tts.use_client(None)


# ------------------------------------------------------------------ the super-admin AI page
async def test_the_superadmin_ai_page_and_provider_test_name_it_simulated(client, db):
    admin = await make_user(db, role=Role.SUPERADMIN, full_name="Platform Admin")
    await login_as(client, admin)
    ai.use_providers([provider()])
    page = await client.get("/superadmin/ai")
    assert page.status_code == 200 and "Simulated AI (load test)" in page.text
    j = await api(client, "POST", "/superadmin/ai/test")
    data = j.json()
    assert j.status_code == 200 and data["ok"] is True and data["provider"] == "simulated"
    assert data["label"] == "Simulated AI (load test)" and data["model"] == "simulated"
    assert data["providers"][0]["name"] == "simulated" and data["providers"][0]["status"] == "up"
