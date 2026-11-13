"""Hardening of the AI guard / AI queue core (docs/ai-guard-and-queue.md section 10, "Hardening").

1. dedupe scope   ``guard.admit(..., scope=)`` / ``ai.complete|stream(dedupe_scope=)``: the duplicate check covers
                  (feature, scope, normalised text) - the same suggested question about another gym is a new one
2. truncation     ``ai.stream`` sets ``meta['truncated']`` whenever it cuts an answer short (total_timeout, provider
                  failure mid-answer, cancel); the visible marker chunks stay
3. queue + leave  the HTTP disconnect watcher also runs while a request waits in the AI queue: a browser that goes
                  away frees its place at once (AICancelled('client_left')), it never reaches a provider
4. 'notes'        the background AC-advice wording has its own quota group - it never competes with coach plans
5. OCR guarded    the OCR vision step is charged to a person's 'vision' quota; jobs and system work are never judged
6. entitlement    ``forget_entitlement`` bumps a shared version: another process re-reads the tier at its next
                  request, inside the round trip ``admit`` makes anyway
7. counters       ``guard.snapshot()``: today's refusals by code, cooldowns started, escalations (Redis, memory)
8. payloads       ``Admission.to_payload()`` / ``guard.settle_payload(payload, ok)``: a background job gives back what
                  the click was charged when it falls back without AI - once, whoever settles first
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import Request

from app.ai import client as client_module
from app.ai import guard as guard_mod
from app.ai import policy as ai_policy
from app.ai.client import FakeProvider, ProviderError, ai
from app.ai.errors import AICancelled, AIRejected
from app.ai.guard import Admission, AIGuard, guard, text_fingerprint
from app.ai.owner import AIOwner, current_owner, owner_scope
from app.ai.policy import BACKGROUND, EXIT_KEEP, EXIT_PAUSE
from app.ai.scheduler import scheduler
from app.config import get_settings
from app.core.deps import AuthUser
from app.db import QueryCounter
from tests.conftest import api, login_as
from tests.droppable_client import droppable_client
from tests.factories import make_customer, make_gym, make_owner, make_plans, make_subscription, png_bytes
from tests.test_ai_guard import CountingRedis, FakeClock, _no_redis, owner, redis_reachable


# ============================================================================================ fixtures / helpers
@pytest.fixture(params=["memory", "redis"])
async def store(request: Any, monkeypatch: pytest.MonkeyPatch) -> str:
    if request.param == "memory":
        monkeypatch.setattr(guard_mod, "_redis_client", _no_redis)
    elif not await redis_reachable():
        pytest.skip("Redis (REDIS_URL) is not reachable")
    return request.param


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def g(store: str, clock: FakeClock) -> Any:
    instance = AIGuard(clock=clock)
    instance.redis_retry_seconds = 0.0
    await instance.reset()
    yield instance
    await instance.reset()


@pytest.fixture
async def redis_client() -> Any:
    if not await redis_reachable():
        pytest.skip("Redis (REDIS_URL) is not reachable")
    return await guard_mod._redis_client()


@pytest.fixture
def guard_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """The singleton guard as configured by default (whatever the local .env says)."""
    monkeypatch.setattr(get_settings(), "ai_guard_enabled", True)
    monkeypatch.setattr(get_settings(), "ai_quota_scale", 1.0)


@pytest.fixture
def one_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "ai_queue_concurrency", 1)


@pytest.fixture
def fast_poll(monkeypatch: pytest.MonkeyPatch) -> float:
    monkeypatch.setattr(client_module, "DISCONNECT_POLL_SECONDS", 0.05)
    return 0.05


async def refused(g: AIGuard, who: AIOwner | None, feature: str, text: str | None = None, **kw: Any) -> AIRejected:
    with pytest.raises(AIRejected) as info:
        await g.admit(who, feature, text, **kw)
    return info.value


async def wait_until(predicate: Callable[[], Any], *, timeout: float = 5.0, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.01)


async def drain(n: int = 20) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


def queued() -> int:
    return scheduler.snapshot()["lanes"]["llm"]["queued"]


def watchers() -> list[asyncio.Task]:
    return [t for t in asyncio.all_tasks() if t.get_name().startswith("ai-watch") and not t.done()]


class Recording:
    """Stands in for ``app.ai.guard.guard``: admits everything, records (feature, text, scope) and settles."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, str | None]] = []
        self.settled: list[bool] = []

    async def admit(self, owner: Any, feature: str, text: str | None = None, *, scope: str | None = None) -> Any:
        self.calls.append((feature, text, scope))
        return SimpleNamespace(n=len(self.calls))

    async def settle(self, admission: Any, *, ok: bool) -> None:
        self.settled.append(ok)


class OldGuard:
    """A stand-in written before dedupe scopes existed: ``admit(owner, feature, text=None)`` only."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def admit(self, owner: Any, feature: str, text: str | None = None) -> Any:
        self.calls.append((feature, text))
        return None

    async def settle(self, admission: Any, *, ok: bool) -> None:
        return None


class Gate:
    """FakeProvider handler: a call whose prompt is held (``hold``) waits until ``release(prompt)``; every other call
    answers at once. Records the prompts it saw and the calls that were cancelled while they waited."""

    def __init__(self, *hold: str, answer: str = "answer") -> None:
        self.answer = answer
        self.events = {prompt: asyncio.Event() for prompt in ("holder", *hold)}
        self.prompts: list[str] = []
        self.cancelled: list[str] = []

    def release(self, *prompts: str) -> None:
        for prompt in prompts or tuple(self.events):
            self.events[prompt].set()

    async def __call__(self, **kwargs: Any) -> str:
        prompt = str(kwargs.get("prompt"))
        self.prompts.append(prompt)
        event = self.events.get(prompt)
        if event is not None:
            try:
                await event.wait()
            except asyncio.CancelledError:
                self.cancelled.append(prompt)
                raise
        return self.answer


def http_owner(uid: int, flag: dict[str, bool], *, page: str = "page-hardening-01", role: str = "customer") -> AIOwner:
    """A signed-in HTTP request's owner; ``flag['gone']`` is what its disconnect probe reports."""

    async def disconnected() -> bool:
        return flag["gone"]

    return AIOwner(user_id=uid, role=role, session_hash=f"sess-{uid}", page_id=page, disconnected=disconnected)


async def hold_the_only_slot(gate: Gate) -> asyncio.Task:
    """System work (no owner) that holds the AI queue's only slot until ``gate.release('holder')``."""
    holder = asyncio.create_task(ai.complete(feature="security_triage", system="s", prompt="holder"))
    await wait_until(lambda: "holder" in gate.prompts, what="the holder at the provider")
    return holder


# ============================================================================================ 1. dedupe scope
async def test_the_duplicate_check_is_per_dedupe_scope(g: AIGuard) -> None:
    who, question = owner(), "Which plan is best for weight loss?"
    first = await g.admit(who, "plans", question, scope="gym:1")
    assert first.scope == "gym:1" and first.fingerprint == text_fingerprint(question, "gym:1")
    assert first.fingerprint != text_fingerprint(question)  # the scope is part of the fingerprint
    assert await g.admit(who, "plans", question, scope="gym:2") is not None  # the same chip on another gym's page
    assert await g.admit(who, "plans", question) is not None  # the generic plans page: no scope
    err = await refused(g, who, "plans", "which PLAN is best for   weight loss", scope=" gym:1 ")
    assert (err.code, err.status) == ("duplicate", 409)  # normalised text, trimmed scope
    assert (await refused(g, who, "plans", question, scope="")).code == "duplicate"  # '' is no scope
    assert await g.admit(who, "shop", question, scope="gym:1") is not None  # another feature
    assert await g.admit(owner(8), "plans", question, scope="gym:1") is not None  # another member
    assert (await g.status(7))["usage"]["qa"][0] == 4  # duplicates are never charged


async def test_a_failed_scoped_call_may_be_asked_again_at_once(g: AIGuard) -> None:
    who, question = owner(), "Is this whey protein vegan?"
    failed = await g.admit(who, "shop", question, scope="product:7")
    await g.settle(failed, ok=False)  # the provider failed: refunded, fingerprint forgotten
    answered = await g.admit(who, "shop", question, scope="product:7")
    await g.settle(answered, ok=True)
    assert (await refused(g, who, "shop", question, scope="product:7")).code == "duplicate"
    assert await g.admit(who, "shop", question, scope="product:8") is not None


def test_text_fingerprint_with_a_scope() -> None:
    assert text_fingerprint("Hi there!", "gym:1") == text_fingerprint("hi   THERE", " gym:1 ")
    assert text_fingerprint("hi", "") == text_fingerprint("hi", None) == text_fingerprint("hi")
    assert len({text_fingerprint("hi"), text_fingerprint("hi", "gym:1"), text_fingerprint("hi", "gym:2"),
                text_fingerprint("gym:1 hi")}) == 4


async def test_the_router_passes_the_dedupe_scope_to_the_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = Recording()
    monkeypatch.setattr(guard_mod, "guard", rec)
    ai.use_providers([FakeProvider("gateway", responses=["fine"])])
    who = AIOwner(user_id=5)
    await ai.complete(feature="plans", system="s", prompt="p", user_text="Which plan?", dedupe_scope="gym:3",
                      owner=who)
    got = [c async for c in ai.stream(feature="shop", system="s", prompt="p", user_text="Vegan?",
                                      dedupe_scope="product:9", owner=who)]
    await ai.complete(feature="plans", system="s", prompt="p", user_text="Which plan?", owner=who)
    assert got == ["fine"]
    assert rec.calls == [("plans", "Which plan?", "gym:3"), ("shop", "Vegan?", "product:9"),
                         ("plans", "Which plan?", None)]
    old = OldGuard()  # stand-ins without a scope parameter keep working for unscoped calls
    monkeypatch.setattr(guard_mod, "guard", old)
    assert (await ai.complete(feature="plans", system="s", prompt="p", user_text="Cheapest?", owner=who)).text == "fine"
    assert old.calls == [("plans", "Cheapest?")]


async def test_scoped_questions_through_the_router_and_the_real_guard(store: str, guard_on: None) -> None:
    ai.use_providers([FakeProvider("gateway", responses=["answer"])])
    who = AIOwner(user_id=21, role="staff")
    for scope in ("gym:1", "gym:2"):
        res = await ai.complete(feature="plans", system="s", prompt="p", user_text="Which plan suits me?",
                                dedupe_scope=scope, owner=who)
        assert res.text == "answer"
    with pytest.raises(AIRejected) as err:
        await ai.complete(feature="plans", system="s", prompt="p", user_text="which plan suits me",
                          dedupe_scope="gym:1", owner=who)
    assert err.value.code == "duplicate"
    agen = ai.stream(feature="plans", system="s", prompt="p", user_text="Which plan suits me?", dedupe_scope="gym:2",
                     owner=who)
    with pytest.raises(AIRejected) as err:
        await agen.__anext__()
    assert err.value.code == "duplicate"


# ============================================================================================ 2. truncation flag
class Stalling(FakeProvider):
    """Says its first words, then nothing for a long time."""

    async def stream_text(self, **kwargs: Any):
        async with self.slot():
            try:
                yield "first words"
                await asyncio.sleep(30)
                yield "never"  # pragma: no cover
            finally:
                self.streams_closed += 1


class Breaks(FakeProvider):
    """Fails after its first words."""

    async def stream_text(self, **kwargs: Any):
        async with self.slot():
            yield "Online plans start at "
            raise ProviderError("connection reset by the gateway")


async def test_an_answer_cut_by_the_total_timeout_is_flagged() -> None:
    ai.use_providers([Stalling("gateway")])
    meta: dict[str, Any] = {}
    got = [c async for c in ai.stream(feature="plans", system="s", prompt="p", total_timeout=0.2, meta=meta)]
    assert got == ["first words", client_module.CUT_MARKER] == ["first words", " …"]  # the visible marker stays
    assert meta["truncated"] is True and "cancelled" not in meta


async def test_an_answer_interrupted_by_the_provider_is_flagged() -> None:
    ai.use_providers([Breaks("gateway")])
    meta: dict[str, Any] = {}
    got = [c async for c in ai.stream(feature="plans", system="s", prompt="p", meta=meta)]
    assert got == ["Online plans start at ", client_module.INTERRUPTED_MARKER]
    assert client_module.INTERRUPTED_MARKER == " … (the answer was interrupted)"
    assert meta["truncated"] is True


async def test_an_answer_stopped_by_a_cancel_is_flagged() -> None:
    ai.use_providers([FakeProvider("gateway", chunks=["one ", "two ", "three"], delay=0.05)])
    meta: dict[str, Any] = {}
    agen = ai.stream(feature="plans", system="s", prompt="p", meta=meta,
                     owner=AIOwner(user_id=8, page_id="page-trunc-0001"))
    got = [await agen.__anext__()]
    assert scheduler.cancel(page_id="page-trunc-0001", reason="page_closed") == 1
    got += [c async for c in agen]
    assert got == ["one "] and meta["cancelled"] == "page_closed" and meta["truncated"] is True


async def test_an_answer_its_consumer_stopped_reading_is_flagged() -> None:
    ai.use_providers([FakeProvider("gateway", chunks=["a", "b", "c"], delay=0.05)])
    meta: dict[str, Any] = {}
    agen = ai.stream(feature="plans", system="s", prompt="p", meta=meta)
    assert await agen.__anext__() == "a"
    await agen.aclose()
    assert meta["truncated"] is True


async def test_complete_answers_and_no_answers_are_not_flagged() -> None:
    ai.use_providers([FakeProvider("gateway", chunks=["a", "b"])])
    meta: dict[str, Any] = {}
    assert [c async for c in ai.stream(feature="plans", system="s", prompt="p", meta=meta)] == ["a", "b"]
    assert "truncated" not in meta and "cancelled" not in meta
    ai.use_providers([FakeProvider("gateway", fail=True)])  # nothing was said at all: an error, not a cut answer
    meta = {}
    with pytest.raises(client_module.AIUnavailableError):
        await ai.stream(feature="plans", system="s", prompt="p", meta=meta).__anext__()
    assert "truncated" not in meta


# ============================================================================================ 3. leaving the queue
async def test_a_request_whose_browser_left_while_queued_leaves_the_queue_at_once(monkeypatch: pytest.MonkeyPatch,
                                                                                 one_slot: None,
                                                                                 fast_poll: float) -> None:
    rec = Recording()
    monkeypatch.setattr(guard_mod, "guard", rec)
    gate = Gate()
    ai.use_providers([FakeProvider("gateway", handler=gate)])
    holder = await hold_the_only_slot(gate)
    flag = {"gone": False}
    waiting = asyncio.create_task(ai.complete(feature="assistant", system="s", prompt="queued", user_text="hi there",
                                              owner=http_owner(31, flag)))
    await wait_until(lambda: queued() == 1, what="the request queued")
    await asyncio.sleep(fast_poll * 4)  # watched for a few polls: still connected, still waiting
    assert not waiting.done() and len(watchers()) == 1
    left = time.monotonic()
    flag["gone"] = True
    with pytest.raises(AICancelled) as err:
        await asyncio.wait_for(waiting, 3)
    assert err.value.reason == "client_left" and time.monotonic() - left < 1.0
    assert gate.prompts == ["holder"]  # it never reached a provider
    snap = scheduler.snapshot()
    assert snap["lanes"]["llm"]["queued"] == 0 and snap["lanes"]["llm"]["running"] == 1
    assert snap["counters"]["cancelled"] == {"client_left": 1} and snap["saved"]["cancelled_queued"] == 1
    await drain()
    assert rec.settled == [False]  # refunded: nobody got an answer
    gate.release("holder")
    assert (await holder).text == "answer"
    await drain()
    assert watchers() == []


async def test_a_stream_whose_browser_left_while_queued_leaves_the_queue_at_once(one_slot: None,
                                                                                fast_poll: float) -> None:
    gate = Gate()
    ai.use_providers([FakeProvider("gateway", handler=gate)])
    holder = await hold_the_only_slot(gate)
    flag = {"gone": False}
    agen = ai.stream(feature="plans", system="s", prompt="queued stream", user_text="Which plan?",
                     owner=http_owner(32, flag))

    async def first() -> str:
        return await agen.__anext__()

    waiting = asyncio.create_task(first())
    await wait_until(lambda: queued() == 1, what="the stream queued")
    left = time.monotonic()
    flag["gone"] = True
    with pytest.raises(AICancelled) as err:
        await asyncio.wait_for(waiting, 3)
    assert err.value.reason == "client_left" and time.monotonic() - left < 1.0
    assert gate.prompts == ["holder"] and scheduler.snapshot()["counters"]["cancelled"] == {"client_left": 1}
    gate.release("holder")
    await holder
    await drain()
    assert watchers() == []


async def test_a_connected_request_keeps_its_place_and_is_still_watched_once_it_runs(one_slot: None,
                                                                                    fast_poll: float) -> None:
    gate = Gate("mine")
    ai.use_providers([FakeProvider("gateway", handler=gate)])
    holder = await hold_the_only_slot(gate)
    flag = {"gone": False}
    call = asyncio.create_task(ai.complete(feature="assistant", system="s", prompt="mine", owner=http_owner(33, flag)))
    await wait_until(lambda: queued() == 1, what="the request queued")
    await asyncio.sleep(fast_poll * 6)
    assert not call.done()  # connected: it keeps its place
    gate.release("holder")
    await holder
    await wait_until(lambda: "mine" in gate.prompts, what="the request at the provider")
    assert len(watchers()) == 1  # the watcher that ran while it waited goes on watching
    flag["gone"] = True  # the browser leaves while the provider is answering
    with pytest.raises(AICancelled) as err:
        await asyncio.wait_for(call, 3)
    assert err.value.reason == "client_left" and gate.cancelled == ["mine"]
    await drain()
    assert watchers() == []


async def test_keep_policy_work_waits_in_the_queue_even_if_the_browser_left(one_slot: None, fast_poll: float) -> None:
    """Safety reviews (exit policy 'keep') are never dropped: the watcher does not look at them."""
    gate = Gate()
    ai.use_providers([FakeProvider("gateway", handler=gate)])
    holder = await hold_the_only_slot(gate)
    waiting = asyncio.create_task(ai.complete(feature="health_metrics", system="s", prompt="review",
                                              owner=http_owner(34, {"gone": True})))
    await wait_until(lambda: queued() == 1, what="the review queued")
    await asyncio.sleep(fast_poll * 5)
    assert not waiting.done() and watchers() == []
    gate.release("holder")
    await holder
    assert (await asyncio.wait_for(waiting, 3)).text == "answer"


async def test_on_queued_is_called_once_and_only_when_the_ticket_waits(one_slot: None) -> None:
    seen: list[str] = []

    def hook(ticket: Any) -> None:
        seen.append(ticket.state)

    first = await scheduler.acquire(AIOwner(user_id=1), "assistant", on_queued=hook)
    assert seen == []  # a free slot: no wait, no hook
    waiting = asyncio.create_task(scheduler.acquire(AIOwner(user_id=2), "assistant", on_queued=hook))
    await wait_until(lambda: queued() == 1, what="the second ticket queued")
    assert seen == ["queued"]
    scheduler.release(first, outcome="done")
    second = await asyncio.wait_for(waiting, 2)
    assert second.state == "running" and seen == ["queued"]

    def broken(ticket: Any) -> None:
        raise RuntimeError("hook bug")

    third = asyncio.create_task(scheduler.acquire(AIOwner(user_id=3), "assistant", on_queued=broken))
    await wait_until(lambda: queued() == 1, what="the third ticket queued")
    scheduler.release(second, outcome="done")
    assert (await asyncio.wait_for(third, 2)).state == "running"  # a broken hook never loses the place
    scheduler.release(third.result(), outcome="done")


async def _ask(request: Request, user: AuthUser) -> dict[str, Any]:
    """A migrated AI endpoint (no fallback): AICancelled reaches the app's error handler (503 'cancelled')."""
    body = await request.json()
    res = await ai.complete(feature="assistant", system="hardening test", prompt=f"{user.id}|{body['message']}",
                            user_text=str(body["message"]))
    owner = current_owner()
    return {"text": res.text, "page_id": owner.page_id if owner is not None else None}


ASK = "/api/hardening/ask"


@pytest.fixture
def ask_route(app: Any) -> str:
    app.add_api_route(ASK, _ask, methods=["POST"])
    added = [r for r in app.router.routes if getattr(r, "path", None) == ASK]
    yield ASK
    for route in added:
        app.router.routes.remove(route)


async def test_a_browser_that_goes_away_while_its_request_waits_in_the_queue(db: Any, ask_route: str, guard_on: None,
                                                                            one_slot: None, fast_poll: float) -> None:
    """End to end: the real app, the real guard and queue - the browser aborts the fetch while its request still waits
    for the only AI slot. The request ends at once (503 'cancelled', nobody reads it), it never reaches the provider,
    the person is not charged, and the queue counts it as a 'client_left' stop."""
    gate = Gate()
    ai.use_providers([FakeProvider("gateway", handler=gate)])
    holder = await hold_the_only_slot(gate)
    user = await make_customer(db)
    gone = asyncio.Event()
    async with droppable_client(gone) as c:
        await login_as(c, user)
        request = asyncio.create_task(api(c, "POST", ask_route, json={"message": "what should I train today?"},
                                          headers={"X-Page-Id": "page-hardening-02"}))
        await wait_until(lambda: queued() == 1, what="the request queued")
        left = time.monotonic()
        gone.set()  # what uvicorn reports when the browser aborts the fetch
        r = await asyncio.wait_for(request, 5)
    assert time.monotonic() - left < 1.5
    assert r.status_code == 503 and r.json()["code"] == "cancelled"
    assert gate.prompts == ["holder"]
    assert scheduler.snapshot()["counters"]["cancelled"] == {"client_left": 1}
    await drain()
    assert (await guard.status(user.id))["usage"]["chat"][2] == 0  # refunded
    gate.release("holder")
    await holder


# ============================================================================================ 4. 'notes' quota group
def test_the_ac_wording_has_a_quota_group_of_its_own() -> None:
    pol = ai_policy.policy_for("health_ac")
    assert (pol.group, pol.cost, pol.priority, pol.on_exit, pol.guarded) == ("notes", 1, BACKGROUND, EXIT_PAUSE, True)
    assert {tier: windows["notes"] for tier, windows in ai_policy.QUOTAS.items()} == {
        "member_ai": (6, 40, 120), "member": (3, 15, 40), "staff": (6, 40, 120), "owner": (6, 40, 120),
        "superadmin": (30, 200, 600)}
    assert ai_policy.policy_for("coach").group == "gen"


async def test_ac_wordings_never_use_up_the_coach_plan_quota(g: AIGuard) -> None:
    member = owner(12, role="customer")  # no subscription: 'member' - gen (2, 6, 12), notes (3, 15, 40)
    plan = await g.admit(member, "coach")
    assert (plan.group, plan.units) == ("gen", 3)  # bigger than the member's whole minute: allowed alone
    for _ in range(3):
        note = await g.admit(member, "health_ac")
        assert (note.group, note.units, note.tier) == ("notes", 1, "member")
    err = await refused(g, member, "health_ac")
    assert err.code == "quota" and err.message.startswith("You've used your AI-written notes for this minute.")
    assert (await refused(g, member, "coach")).code == "quota"  # and the coach plan keeps its own budget
    status = await g.status(12)
    assert status["usage"]["gen"] == [3, 3, 3] and status["usage"]["notes"] == [3, 3, 3]
    assert status["limits"]["notes"] == [3, 15, 40]


# ============================================================================================ 5. OCR is guarded
def test_the_ocr_vision_step_is_a_guarded_vision_request() -> None:
    pol = ai_policy.policy_for("ocr")
    assert (pol.guarded, pol.group, pol.cost, pol.on_exit, pol.safety) == (True, "vision", 1, EXIT_KEEP, False)


async def test_a_persons_ocr_is_charged_and_jobs_or_system_work_never_are(g: AIGuard) -> None:
    person = owner(14, role="customer")  # 'member': vision (3, 10, 20)
    for _ in range(3):
        admission = await g.admit(person, "ocr")
        assert (admission.group, admission.units) == ("vision", 1)
    assert (await refused(g, person, "ocr")).code == "quota"
    verification_job = AIOwner(user_id=14, kind="job", job_id=9, exit_policy="keep")
    assert await g.admit(verification_job, "ocr") is None and await g.admit(None, "ocr") is None
    assert (await g.status(14))["usage"]["vision"] == [3, 3, 3]


async def test_the_ocr_engine_call_is_charged_to_the_person_who_scans(store: str, guard_on: None) -> None:
    from app.services import ocr as ocr_service

    ai.use_providers([FakeProvider("gateway", responses=["75.5 kg"])])
    person = AIOwner(user_id=41, role="customer", ip="198.51.100.41")
    with owner_scope(person):
        res = await ocr_service._run_engine("vision_llm", png_bytes(), "measurement")
    assert res.ok and res.text == "75.5 kg"
    assert (await guard.status(41))["usage"]["vision"] == [1, 1, 1]
    with owner_scope(AIOwner(user_id=41, kind="job", job_id=3, exit_policy="keep")):  # identity verification job
        res = await ocr_service._run_engine("vision_llm", png_bytes(), "id_card")
    assert res.ok and (await guard.status(41))["usage"]["vision"] == [1, 1, 1]
    with owner_scope(person):  # the 'member' minute quota is 3: a refusal is a failed engine, never an error
        for _ in range(2):
            assert (await ocr_service._run_engine("vision_llm", png_bytes(), "measurement")).ok
        res = await ocr_service._run_engine("vision_llm", png_bytes(), "measurement")
    assert not res.ok and res.error == "AI unavailable"


# ============================================================================================ 6. entitlement versions
async def _customer_and_plans(db: Any) -> tuple[Any, dict[str, Any]]:
    gym = await make_gym(db, await make_owner(db))
    plans = {p.code: p for p in await make_plans(db, gym)}
    return await make_customer(db), plans


async def test_a_tier_change_reaches_another_process_at_its_next_request(db: Any, redis_client: Any) -> None:
    member, plans = await _customer_and_plans(db)
    first, second = AIGuard(clock=FakeClock()), AIGuard(clock=FakeClock())  # two app processes, one Redis
    who = owner(member.id, role="customer")
    assert (await first.admit(who, "tts")).tier == "member"
    assert (await second.admit(who, "tts")).tier == "member"  # cached in both from here on
    await make_subscription(db, member, plans["online_ai"])
    await first.forget_entitlement(member.id)  # the process that committed the change
    assert (await first.admit(who, "tts")).tier == "member_ai"
    with QueryCounter() as qc:
        admission = await second.admit(who, "tts")
    assert admission.tier == "member_ai" and qc.count == 1  # one fresh lookup, in the request that saw the new version
    assert (await second.status(member.id))["tier"] == "member_ai"
    with QueryCounter() as qc:
        assert (await second.admit(who, "tts")).tier == "member_ai"
    assert qc.count == 0  # cached again


async def test_forgetting_everyone_reaches_another_process_too(db: Any, redis_client: Any) -> None:
    member, plans = await _customer_and_plans(db)
    first, second = AIGuard(clock=FakeClock()), AIGuard(clock=FakeClock())
    who = owner(member.id, role="customer")
    assert (await second.admit(who, "tts")).tier == "member"
    await make_subscription(db, member, plans["offline_ai"])
    await first.forget_entitlement()
    assert (await second.admit(who, "tts")).tier == "member_ai"


async def test_the_version_check_needs_no_extra_round_trip(db: Any, redis_client: Any,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    member, _plans = await _customer_and_plans(db)
    counting = CountingRedis(redis_client)

    async def client() -> CountingRedis:
        return counting

    monkeypatch.setattr(guard_mod, "_redis_client", client)
    g = AIGuard(clock=FakeClock())
    who = owner(member.id, role="customer")
    await g.admit(who, "tts")  # the first request looks the entitlement up (and its version)
    counting.pipelines = 0
    with QueryCounter() as qc:
        await g.admit(who, "tts")
        await g.admit(who, "assistant", "How long should I rest between sets?")
    assert counting.pipelines == 2 and qc.count == 0  # one round trip per admit, version check included


async def test_a_change_committed_during_the_lookup_is_never_cached_as_current(db: Any, redis_client: Any,
                                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    """The version is read BEFORE the database: a change that commits (and is announced) while the lookup runs leaves
    the cached answer behind the shared version, so it is looked up again in the same request."""
    member, plans = await _customer_and_plans(db)
    first, second = AIGuard(clock=FakeClock()), AIGuard(clock=FakeClock())
    real_lookup = second._lookup_entitlement
    lookups: list[bool] = []

    async def racing_lookup(user_id: int) -> bool:
        value = await real_lookup(user_id)
        if not lookups:  # the old state was read; meanwhile another process activates an AI plan and says so
            await make_subscription(db, member, plans["online_ai"])
            await first.forget_entitlement(member.id)
        lookups.append(value)
        return value

    monkeypatch.setattr(second, "_lookup_entitlement", racing_lookup)
    admission = await second.admit(owner(member.id, role="customer"), "tts")
    assert lookups == [False, True] and admission.tier == "member_ai"


async def test_an_unknown_version_never_costs_a_lookup_per_request(db: Any, g: AIGuard,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    member, _plans = await _customer_and_plans(db)

    async def unreadable(user_id: int) -> None:
        return None  # the version could not be read when the answer was looked up

    monkeypatch.setattr(g, "_read_entitlement_version", unreadable)
    who = owner(member.id, role="customer")
    with QueryCounter() as qc:
        for _ in range(3):
            assert (await g.admit(who, "tts")).tier == "member"
    assert qc.count == 1  # looked up once, then cached for its ENTITLEMENT_TTL like before


async def test_every_pending_bump_is_held_until_it_lands(g: AIGuard) -> None:
    first, second = g.forget_entitlement(7), g.forget_entitlement(7)  # two changes committed back to back
    assert len(g._bumps[7]) == 2  # both tasks are referenced (and waited for), not just the latest
    await first
    await second
    assert g._bumps == {}


async def test_without_redis_forgetting_stays_in_this_process(db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guard_mod, "_redis_client", _no_redis)
    member, plans = await _customer_and_plans(db)
    first, second = AIGuard(clock=FakeClock()), AIGuard(clock=FakeClock())
    who = owner(member.id, role="customer")
    assert (await first.admit(who, "tts")).tier == (await second.admit(who, "tts")).tier == "member"
    await make_subscription(db, member, plans["online_ai"])
    first.forget_entitlement(member.id)  # a synchronous caller (call_after_commit) may ignore the result
    assert (await first.admit(who, "tts")).tier == "member_ai"
    assert (await second.admit(who, "tts")).tier == "member"  # its own memory: unchanged, as before


async def test_a_committed_activation_reaches_another_process(db: Any, redis_client: Any, guard_on: None) -> None:
    """checkout.activate_subscription -> guard.forget_entitlement after the COMMIT (app/services/checkout.py): another
    app process with the old tier cached judges the member's next request with the new one."""
    from app.models import Subscription
    from app.models.enums import SubscriptionStatus
    from app.services import checkout as svc

    member, plans = await _customer_and_plans(db)
    sub = await make_subscription(db, member, plans["online_ai"], status=SubscriptionStatus.PENDING_PAYMENT)
    sub_id, uid = sub.id, member.id
    other = AIGuard(clock=FakeClock())
    who = owner(uid, role="customer")
    assert (await other.admit(who, "tts")).tier == "member"
    key = f"{guard._ns()}entv:{uid}"
    assert await redis_client.get(key) is None
    await svc.activate_subscription(db, await db.get(Subscription, sub_id))
    await db.commit()
    await wait_until(lambda: not guard._bumps, what="the version bump")
    assert await redis_client.get(key) is not None and 0 < await redis_client.ttl(key) <= 86400
    assert (await other.admit(who, "tts")).tier == "member_ai"


# ============================================================================================ 7. refusal counters
async def test_snapshot_counts_todays_refusals_cooldowns_and_escalations(g: AIGuard, store: str,
                                                                        clock: FakeClock) -> None:
    zero = {"refused": {"quota": 0, "duplicate": 0, "spam": 0, "cooldown": 0}, "cooldowns_started_today": 0,
            "escalations_today": 0, "day": "2027-01-01", "backend": store}
    assert await g.snapshot() == zero
    who, question = owner(ip="198.51.100.60"), "How many rest days do I need?"
    await refused(g, who, "assistant", "#" * 30)  # spam: 3 points
    await g.admit(who, "assistant", question)
    for _ in range(2):
        await refused(g, who, "assistant", question)  # duplicates: free
    for _ in range(9):
        await g.admit(who, "assistant")  # staff chat: 10 a minute
    await refused(g, who, "assistant")  # quota: 1 point (4)
    pause = await refused(g, who, "assistant", "%" * 30)  # spam: 7 points, and the 5th refusal in 10 s (+2 flood)
    assert pause.code == "cooldown"  # shown as the pause it started - counted under its reason
    assert (await refused(g, who, "plans", "A question while paused?")).code == "cooldown"
    await g.note_rejection(who, "assistant", "too_many")  # the queue's refusal: counted by the queue, not here
    snap = await g.snapshot()
    assert snap["refused"] == {"quota": 1, "duplicate": 2, "spam": 2, "cooldown": 1}
    assert (snap["cooldowns_started_today"], snap["escalations_today"]) == (1, 0)
    for _ in range(12):  # injections push the same pause to 15 and then 60 minutes: reported to security once
        await g.note_injection(who, "assistant")
    status = await g.status(7)
    assert status["cooldown_seconds"] == ai_policy.ESCALATE_MIN_SECONDS
    snap = await g.snapshot()
    assert (snap["cooldowns_started_today"], snap["escalations_today"]) == (1, 1)  # extensions are not new pauses
    clock.advance(14 * 3600)  # past midnight UTC: a new day starts from zero
    assert await g.snapshot() == {**zero, "day": "2027-01-02"}


async def test_whitelisted_addresses_are_not_counted_as_escalations(g: AIGuard,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "rate_limit_whitelist", ["198.51.100.99"])
    who = owner(ip="198.51.100.99")
    await g._start_cooldown(who, "assistant", level=3, seconds=ai_policy.ESCALATE_MIN_SECONDS, code="spam", strikes=20)
    snap = await g.snapshot()
    assert (snap["cooldowns_started_today"], snap["escalations_today"]) == (1, 0)


async def test_two_processes_share_the_counters_through_redis(redis_client: Any) -> None:
    clock = FakeClock()
    first, second = AIGuard(clock=clock), AIGuard(clock=clock)
    await refused(first, owner(), "assistant", "!" * 40)
    await refused(second, owner(8), "assistant", "?" * 40)
    assert (await first.snapshot())["refused"]["spam"] == (await second.snapshot())["refused"]["spam"] == 2


async def test_counting_rides_on_the_refusal_round_trips(redis_client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    counting = CountingRedis(redis_client)

    async def client() -> CountingRedis:
        return counting

    monkeypatch.setattr(guard_mod, "_redis_client", client)
    g = AIGuard(clock=FakeClock())
    who = owner()
    await refused(g, who, "assistant", "#" * 30)
    assert counting.pipelines == 3  # admit + strike bookkeeping (+ the counter) + the strike points
    await g._start_cooldown(who, "assistant", level=1, seconds=120, code="spam", strikes=6)
    counting.pipelines = 0
    await refused(g, who, "assistant", "During the pause?")
    assert counting.pipelines == 2  # admit + rollback of its charges (+ the counter)
    assert (await g.snapshot())["refused"] == {"quota": 0, "duplicate": 0, "spam": 1, "cooldown": 1}


async def test_the_singleton_snapshot_is_reset_between_tests(store: str) -> None:
    await refused(guard, owner(56), "assistant", "!" * 40)
    assert (await guard.snapshot())["refused"]["spam"] == 1
    await guard.reset()
    assert (await guard.snapshot())["refused"]["spam"] == 0


# ============================================================================================ 8. admission payloads
def as_stored(payload: dict[str, Any]) -> dict[str, Any]:
    """What a job reads back from its JSON payload column."""
    return json.loads(json.dumps(payload))


async def test_a_job_gives_back_what_the_click_was_charged(g: AIGuard, store: str) -> None:
    who = owner(15)  # staff gen (6, 30, 100); a coach plan costs 3
    admission = await g.admit(who, "coach")
    payload = as_stored(admission.to_payload())
    assert {"charges", "backend", "dedupe_token", "settled", "ref", "user_id", "units"} <= set(payload)
    assert (payload["backend"], payload["settled"], payload["units"], len(payload["charges"])) == (store, False, 3, 3)
    assert Admission.from_payload(payload).charges == admission.charges
    assert (await g.status(15))["usage"]["gen"] == [3, 3, 3]
    assert await g.settle_payload(payload, False) is True  # the job fell back without AI
    assert payload["settled"] is True and (await g.status(15))["usage"]["gen"] == [0, 0, 0]
    await g.admit(who, "coach")  # another plan, charged
    assert await g.settle_payload(as_stored(admission.to_payload()), False) is False  # the first one: settled once
    await g.settle(admission, ok=False)  # the original object shares the claim
    assert await g.settle_payload(payload, False) is False  # marked settled in place
    assert (await g.status(15))["usage"]["gen"] == [3, 3, 3]


async def test_settling_copies_concurrently_refunds_once(g: AIGuard) -> None:
    who = owner(16)
    admission, other = await g.admit(who, "coach"), await g.admit(who, "coach")
    raw = json.dumps(admission.to_payload())
    results = await asyncio.gather(*(g.settle_payload(json.loads(raw), False) for _ in range(6)))
    assert results.count(True) == 1
    assert (await g.status(16))["usage"]["gen"] == [3, 3, 3]  # one refund, not six
    await g.settle(other, ok=True)


async def test_an_ok_settle_keeps_the_charge_and_blocks_a_later_refund(g: AIGuard) -> None:
    who = owner(17)
    admission = await g.admit(who, "coach")
    payload = admission.to_payload()
    assert await g.settle_payload(as_stored(payload), True) is True  # the job's AI answered
    assert await g.settle_payload(as_stored(payload), False) is False  # a retry that failed later: still charged
    await g.settle(admission, ok=False)
    assert (await g.status(17))["usage"]["gen"] == [3, 3, 3]


async def test_settling_the_original_first_makes_payloads_no_ops(g: AIGuard) -> None:
    who = owner(18)
    admission = await g.admit(who, "coach")
    payload = as_stored(admission.to_payload())
    await g.settle(admission, ok=False)  # e.g. the route could not queue the job after all
    assert (await g.status(18))["usage"]["gen"] == [0, 0, 0]
    await g.admit(who, "coach")
    assert await g.settle_payload(payload, False) is False
    assert (await g.status(18))["usage"]["gen"] == [3, 3, 3]


async def test_a_payload_refund_forgets_the_fingerprint(g: AIGuard) -> None:
    who, note = owner(19), "Priya Sharma, 31, wants to lose 5 kg before her wedding"
    admission = await g.admit(who, "intake", note)
    payload = as_stored(admission.to_payload())
    assert payload["dedupe_token"] == admission.dedupe_token and payload["dedupe_key"]
    assert (await refused(g, who, "intake", note)).code == "duplicate"
    assert await g.settle_payload(payload, False) is True
    assert await g.admit(who, "intake", note) is not None  # asked again at once: no 'duplicate'


async def test_invalid_payloads_are_ignored(g: AIGuard) -> None:
    who = owner(20)
    admission = await g.admit(who, "coach")
    good = as_stored(admission.to_payload())
    other_user = {**good, "user_id": 21}  # its charge keys belong to user 20
    other_namespace = {**good, "charges": [["ip:prod:live:ai:q:20:gen:60:1", 3, 9e12]]}
    for bad in (None, {}, "payload", [], {"v": 2, **{k: v for k, v in good.items() if k != "v"}},
                {**good, "ref": "not-hex!"}, {**good, "units": True}, {**good, "units": -3},
                {**good, "backend": "disk"}, {**good, "charges": [["k", 1]]}, {**good, "charges": "x"},
                {**good, "charges": [[good["charges"][0][0], 3, float("nan")]]}, other_user, other_namespace):
        assert await g.settle_payload(bad, False) is False, bad
    assert (await g.status(20))["usage"]["gen"] == [3, 3, 3]  # nothing was refunded by any of them
    assert await g.settle_payload(good, False) is True


async def test_a_shared_admission_without_a_claim_id_settles_on_its_own(g: AIGuard) -> None:
    """``shared`` without a ``ref`` (built by hand, never exported) cannot use a claim - it settles like any other."""
    who = owner(24)
    first, second = await g.admit(who, "coach"), await g.admit(who, "coach")
    for admission in (first, second):
        admission.shared = True
    await g.settle(first, ok=False)
    await g.settle(second, ok=False)  # not blocked by the first one's claim
    assert (await g.status(24))["usage"]["gen"] == [0, 0, 0]


async def test_an_in_memory_charge_of_another_process_is_not_refunded_here(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guard_mod, "_redis_client", _no_redis)
    g = AIGuard(clock=FakeClock())
    admission = await g.admit(owner(22), "coach")
    payload = as_stored(admission.to_payload())
    assert payload["backend"] == "memory"
    elsewhere = {**payload, "origin": "4242-cafe"}  # a job running in another process
    assert await g.settle_payload(elsewhere, False) is False
    assert (await g.status(22))["usage"]["gen"] == [3, 3, 3]
    assert await g.settle_payload(payload, False) is True  # in the process that took it: refunded
    assert (await g.status(22))["usage"]["gen"] == [0, 0, 0]


async def test_a_payload_is_settled_by_another_process_through_redis(redis_client: Any) -> None:
    clock = FakeClock()
    web, worker = AIGuard(clock=clock), AIGuard(clock=clock)  # the click in one process, the job in another
    admission = await web.admit(owner(23), "coach")
    payload = as_stored(admission.to_payload())
    assert await worker.settle_payload(payload, False) is True
    assert (await web.status(23))["usage"]["gen"] == [0, 0, 0]
    await web.settle(admission, ok=False)  # the original, in its own process: already settled
    assert (await web.status(23))["usage"]["gen"] == [0, 0, 0]
