"""The AI router with the guard and the fair queue (app/ai/client.py, docs/ai-guard-and-queue.md section 4).

* the requester leaving stops provider work: HTTP disconnect -> AICancelled, a queue cancel -> AICancelled, the
  caller's own cancellation -> CancelledError that also cancels the provider call; slots are freed, nothing counts
  as a provider failure and a ``cancelled`` CallLog names the user;
* ``guard.admit`` / ``guard.settle`` bookkeeping (the guard is a black box here: a recording fake replaces it);
* waiting - in the queue or for a provider slot - never opens a circuit, and latency is provider time only;
* streaming: closing / cancelling the consumer aborts the provider stream, a queue cancel stops it between chunks,
  ``ProviderError.availability`` is honoured, ``total_timeout`` applies while no chunk arrives;
* the provider stream fixes (OpenAI AsyncStream closed, real Anthropic streaming);
* the AIRejected / AICancelled error handlers and the new ai_call_logs columns."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest

from app.ai import guard as guard_module
from app.ai import policy
from app.ai.client import (
    AnthropicProvider,
    CallLog,
    FakeProvider,
    OpenAICompatProvider,
    ProviderError,
    ai,
    call_log_queue,
    flush_ai_call_logs,
)
from app.ai.errors import AICancelled, AIRejected, AIUnavailableError
from app.ai.owner import AIOwner, owner_scope
from app.ai.scheduler import scheduler
from app.config import get_settings


# ------------------------------------------------------------------ fixtures + helpers
class RecordingGuard:
    """Stands in for ``app.ai.guard.guard``: admits everything (or raises ``reject``) and records the calls
    (``scopes`` holds the dedupe scope of each admit, in the same order)."""

    def __init__(self) -> None:
        self.admitted: list[tuple[Any, str, str | None]] = []
        self.scopes: list[str | None] = []
        self.settled: list[tuple[Any, bool]] = []
        self.reject: BaseException | None = None

    async def admit(self, owner: Any, feature: str, text: str | None = None, *, scope: str | None = None) -> Any:
        self.admitted.append((owner, feature, text))
        self.scopes.append(scope)
        if self.reject is not None:
            raise self.reject
        return SimpleNamespace(n=len(self.admitted), feature=feature)

    async def settle(self, admission: Any, *, ok: bool) -> None:
        self.settled.append((admission, ok))


@pytest.fixture(autouse=True)
def guard(monkeypatch):
    fake = RecordingGuard()
    monkeypatch.setattr(guard_module, "guard", fake)
    call_log_queue.clear()
    yield fake
    call_log_queue.clear()


def http_owner(uid: int = 42, *, flag: dict[str, bool] | None = None, page: str = "page-test-0001") -> AIOwner:
    """A signed-in HTTP request's owner; ``flag['gone']`` is what its disconnect probe reports."""
    state = flag if flag is not None else {"gone": False}

    async def disconnected() -> bool:
        return state["gone"]

    return AIOwner(user_id=uid, role="customer", session_hash="sess-x", page_id=page, disconnected=disconnected)


class SlowHandler:
    """FakeProvider handler that takes ``seconds`` and records whether it was cancelled."""

    def __init__(self, seconds: float = 30.0, answer: str = '{"ok": true}') -> None:
        self.seconds = seconds
        self.answer = answer
        self.started = asyncio.Event()
        self.calls = 0
        self.cancelled = 0

    async def __call__(self, **kwargs: Any) -> str:
        self.calls += 1
        self.started.set()
        try:
            await asyncio.sleep(self.seconds)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        return self.answer


async def drain(n: int = 10) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


async def collect(agen: Any) -> list[str]:
    return [chunk async for chunk in agen]


def llm_lane() -> dict[str, Any]:
    return scheduler.snapshot()["lanes"]["llm"]


# ------------------------------------------------------------------ the requester leaves (complete)
async def test_client_disconnect_stops_the_provider_call(guard):
    handler = SlowHandler()
    provider = FakeProvider("gateway", handler=handler, max_concurrency=1)
    ai.use_providers([provider])
    flag = {"gone": False}

    async def leave() -> None:
        await handler.started.wait()
        await asyncio.sleep(0.1)
        flag["gone"] = True

    leaver = asyncio.create_task(leave())
    started = time.monotonic()
    with pytest.raises(AICancelled) as err:
        await ai.complete(feature="assistant", system="s", prompt="p", user_text="hi", owner=http_owner(flag=flag))
    elapsed = time.monotonic() - started
    await leaver
    assert err.value.reason == "client_left" and elapsed < 1.5
    assert isinstance(err.value, AIUnavailableError)  # route fallbacks still catch it
    assert handler.cancelled == 1  # the provider call itself was stopped
    assert not provider.semaphore.locked()  # and its slot is free again
    st = provider.state
    assert (st.failures, st.consecutive_failures, provider.circuit_open()) == (0, 0, False)  # not a provider failure
    log = call_log_queue[-1]
    assert (log.feature, log.provider, log.ok, log.cancelled, log.user_id, log.owner_kind) == (
        "assistant", "gateway", False, True, 42, "user")
    assert log.error == "cancelled (client_left)" and log.queue_ms is not None
    snap = scheduler.snapshot()
    assert snap["lanes"]["llm"]["running"] == 0 and snap["counters"]["cancelled"] == {"client_left": 1}
    assert snap["saved"]["cancelled_running"] == 1
    await drain()
    # the provider was working on it: charged - only work that never reached a provider is free (dropping the
    # connection mid-answer must not be a way around the quotas)
    assert [ok for _, ok in guard.settled] == [True]


async def test_connected_client_gets_its_answer_and_the_watcher_stops(guard):
    ai.use_providers([FakeProvider("gateway", handler=SlowHandler(seconds=0.6, answer="done"))])
    res = await ai.complete(feature="assistant", system="s", prompt="p", owner=http_owner())
    assert res.text == "done"
    await drain()
    assert not [t for t in asyncio.all_tasks() if t.get_name().startswith(("ai-watch", "ai:"))]
    assert [ok for _, ok in guard.settled] == [True]


async def test_keep_policy_work_is_not_stopped_by_a_disconnect():
    """Safety checks / obligations (exit policy 'keep') finish even if the browser is gone."""
    ai.use_providers([FakeProvider("gateway", handler=SlowHandler(seconds=0.7, answer="reviewed"))])
    res = await ai.complete(feature="health_metrics", system="s", prompt="p", owner=http_owner(flag={"gone": True}))
    assert res.text == "reviewed"


async def test_broken_disconnect_probe_is_ignored():
    async def probe() -> bool:
        raise RuntimeError("receive() failed")

    ai.use_providers([FakeProvider("gateway", handler=SlowHandler(seconds=0.7, answer="fine"))])
    res = await ai.complete(feature="assistant", system="s", prompt="p", owner=AIOwner(user_id=1, disconnected=probe))
    assert res.text == "fine"


async def test_caller_cancellation_propagates_to_the_provider_call(guard):
    handler = SlowHandler()
    provider = FakeProvider("gateway", handler=handler, max_concurrency=1)
    ai.use_providers([provider])
    task = asyncio.create_task(ai.complete(feature="assistant", system="s", prompt="p", owner=http_owner()))
    await handler.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):  # the caller asked for it: no AICancelled conversion
        await task
    assert handler.cancelled == 1 and not provider.semaphore.locked()
    assert llm_lane()["running"] == 0 and scheduler.snapshot()["counters"]["cancelled"] == {"aborted": 1}
    assert call_log_queue[-1].cancelled and call_log_queue[-1].error == "cancelled (aborted)"
    assert provider.state.failures == 0
    await drain()
    assert [ok for _, ok in guard.settled] == [True]  # the provider was working on it: charged


async def test_timeout_wrapper_around_complete_still_raises_timeout():
    """health._complete / live_coach wrap ai.complete in wait_for: their timeout cancels the provider call."""
    handler = SlowHandler()
    ai.use_providers([FakeProvider("gateway", handler=handler)])
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(ai.complete(feature="health_metrics", system="s", prompt="p"), timeout=0.2)
    assert handler.cancelled == 1 and llm_lane()["running"] == 0


async def test_queue_cancel_of_a_running_call_raises_aicancelled():
    handler = SlowHandler()
    ai.use_providers([FakeProvider("gateway", handler=handler)])
    owner = AIOwner(user_id=7, page_id="page-closing-01")
    task = asyncio.create_task(ai.complete(feature="assistant", system="s", prompt="p", owner=owner))
    await handler.started.wait()
    assert scheduler.cancel(page_id="page-closing-01", kinds={"user"}, reason="page_closed") == 1
    with pytest.raises(AICancelled) as err:
        await task
    assert err.value.reason == "page_closed" and handler.cancelled == 1
    assert call_log_queue[-1].error == "cancelled (page_closed)" and call_log_queue[-1].user_id == 7


async def test_call_cancelled_while_queued_never_reaches_a_provider(guard, monkeypatch):
    monkeypatch.setattr(get_settings(), "ai_queue_concurrency", 1)
    handler = SlowHandler()
    ai.use_providers([FakeProvider("gateway", handler=handler)])
    holder = asyncio.create_task(ai.complete(feature="assistant", system="s", prompt="1", owner=AIOwner(user_id=1)))
    await handler.started.wait()
    waiting = asyncio.create_task(ai.complete(feature="assistant", system="s", prompt="2", owner=AIOwner(user_id=2)))
    await drain()
    assert llm_lane()["queued"] == 1
    assert scheduler.cancel(user_id=2, reason="left") == 1
    with pytest.raises(AICancelled) as err:
        await waiting
    assert err.value.reason == "left" and handler.calls == 1
    holder.cancel()
    await asyncio.gather(holder, return_exceptions=True)
    await drain()
    # the queued call never reached a provider: refunded. The holder was cancelled while its provider worked: charged.
    assert sorted((a.n, ok) for a, ok in guard.settled) == [(1, True), (2, False)]


async def test_queue_wait_limit_refuses_busy_without_touching_providers(guard, monkeypatch):
    monkeypatch.setattr(get_settings(), "ai_queue_concurrency", 1)
    monkeypatch.setitem(policy.FEATURES, "quick", policy.FeaturePolicy("quick", "qa", max_wait=0.1))
    handler = SlowHandler()
    provider = FakeProvider("gateway", handler=handler)
    ai.use_providers([provider])
    holder = asyncio.create_task(ai.complete(feature="quick", system="s", prompt="1", owner=AIOwner(user_id=1)))
    await handler.started.wait()
    with pytest.raises(AIRejected) as err:
        await ai.complete(feature="quick", system="s", prompt="2", owner=AIOwner(user_id=2))
    assert err.value.code == "busy" and err.value.retry_after == 5
    assert provider.state.failures == 0 and not provider.circuit_open() and handler.calls == 1
    holder.cancel()
    await asyncio.gather(holder, return_exceptions=True)
    await drain()
    assert guard.settled[0][0].n == 2 and guard.settled[0][1] is False  # the refused call is refunded


# ------------------------------------------------------------------ guard bookkeeping
async def test_guard_admit_and_settle_arguments(guard, fake_ai):
    fake_ai(responses=["fine"])
    owner = http_owner(uid=5)
    res = await ai.complete(feature="assistant", system="s", prompt="p", user_text="raw words", owner=owner)
    assert res.text == "fine"
    assert guard.admitted == [(owner, "assistant", "raw words")]
    assert len(guard.settled) == 1 and guard.settled[0][0].n == 1 and guard.settled[0][1] is True


async def test_owner_defaults_to_the_current_owner(guard, fake_ai):
    fake_ai(responses=["fine"])
    owner = AIOwner(user_id=9, kind="job", job_id=3, exit_policy="keep")
    with owner_scope(owner):
        await ai.complete(feature="payroll_explain", system="s", prompt="p")
    assert guard.admitted[0][0] is owner and guard.admitted[0][2] is None
    log = call_log_queue[-1]
    assert (log.user_id, log.owner_kind, log.ok) == (9, "job", True)


async def test_system_work_is_logged_as_system(guard, fake_ai):
    fake_ai(responses=["fine"])
    await ai.complete(feature="security_triage", system="s", prompt="p")
    assert guard.admitted[0][0] is None
    assert (call_log_queue[-1].user_id, call_log_queue[-1].owner_kind) == (None, "system")


async def test_settle_reports_provider_failure(guard, ai_down):
    with pytest.raises(AIUnavailableError) as err:
        await ai.complete(feature="assistant", system="s", prompt="p", owner=AIOwner(user_id=5))
    assert not isinstance(err.value, (AIRejected, AICancelled))
    assert [ok for _, ok in guard.settled] == [False]
    assert [c.ok for c in call_log_queue] == [False, False] and all(c.user_id == 5 for c in call_log_queue)


async def test_guard_rejection_propagates_before_any_work(guard, fake_ai):
    provider = fake_ai(responses=["never"])
    guard.reject = AIRejected("quota", "AI limit reached for now.", retry_after=42, feature="assistant")
    with pytest.raises(AIRejected) as err:  # no image preparation either: these bytes are not an image
        await ai.complete(feature="assistant", system="s", prompt="p", images=[b"not an image"],
                          owner=AIOwner(user_id=5))
    assert err.value is guard.reject
    assert provider.prompts == [] and scheduler.snapshot()["counters"]["submitted"] == 0
    assert guard.settled == []  # nothing was admitted, nothing to settle
    agen = ai.stream(feature="plans", system="s", prompt="p", owner=AIOwner(user_id=5))
    with pytest.raises(AIRejected):
        await agen.__anext__()
    assert provider.prompts == []


async def test_queue_refusal_is_settled_as_not_ok(guard, monkeypatch, fake_ai):
    monkeypatch.setattr(get_settings(), "ai_queue_max_per_user", 1)
    handler = SlowHandler()
    fake_ai(handler=handler)
    owner = AIOwner(user_id=5)
    first = asyncio.create_task(ai.complete(feature="assistant", system="s", prompt="1", owner=owner))
    await handler.started.wait()
    with pytest.raises(AIRejected) as err:
        await ai.complete(feature="assistant", system="s", prompt="2", owner=owner)
    assert err.value.code == "too_many" and err.value.status == 429
    await drain()
    assert guard.settled == [(guard.settled[0][0], False)] and guard.settled[0][0].n == 2
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)


async def test_broken_guard_does_not_switch_ai_off(guard, fake_ai):
    fake_ai(responses=["ok"])
    guard.reject = RuntimeError("bug in the guard")
    res = await ai.complete(feature="assistant", system="s", prompt="p", owner=AIOwner(user_id=5))
    assert res.text == "ok" and guard.settled == []


async def test_images_are_prepared_after_admission(fake_ai):
    from tests.factories import png_bytes

    provider = fake_ai(responses=['{"ok": true}'])
    await ai.complete(feature="health_scan", system="s", prompt="p", images=[png_bytes()], owner=AIOwner(user_id=5))
    sent = provider.prompts[0]["images"]
    assert len(sent) == 1 and sent[0][:2] == b"\xff\xd8"  # re-encoded JPEG


async def test_no_configured_provider_fails_fast_without_admission(guard):
    ai.use_providers([])
    with pytest.raises(AIUnavailableError):
        await ai.complete(feature="assistant", system="s", prompt="p", owner=AIOwner(user_id=5))
    assert guard.admitted == [] and scheduler.snapshot()["counters"]["submitted"] == 0


async def test_all_circuits_open_fails_fast(guard):
    down = FakeProvider("gateway", fail=True)
    ai.use_providers([down])
    for _ in range(get_settings().ai_circuit_failures):
        with pytest.raises(AIUnavailableError):
            await ai.complete(feature="assistant", system="s", prompt="p")
    assert down.circuit_open()
    calls = len(guard.admitted)
    with pytest.raises(AIUnavailableError) as err:
        await ai.complete(feature="assistant", system="s", prompt="p")
    assert "temporarily disabled" in str(err.value) and len(guard.admitted) == calls


# ------------------------------------------------------------------ waiting is never a provider failure
async def test_queue_wait_longer_than_the_provider_timeout_is_no_failure(monkeypatch):
    monkeypatch.setattr(get_settings(), "ai_queue_concurrency", 1)

    async def handler(**kwargs: Any) -> str:
        if kwargs["prompt"] == "slow":
            await asyncio.sleep(0.5)
        return "answer"

    provider = FakeProvider("gateway", handler=handler)
    ai.use_providers([provider])
    slow = asyncio.create_task(ai.complete(feature="security_triage", system="s", prompt="slow"))
    await drain()
    started = time.monotonic()
    res = await ai.complete(feature="security_triage", system="s", prompt="fast", timeout=0.1)  # queues ~0.5 s
    assert time.monotonic() - started >= 0.4 and res.text == "answer"
    await slow
    st = provider.state
    assert (st.failures, st.consecutive_failures, st.healthy, provider.circuit_open()) == (0, 0, True, False)
    fast = call_log_queue[-1]
    assert fast.ok and fast.queue_ms >= 400 and fast.latency_ms < 200  # queue time is not provider latency


async def test_provider_slot_wait_is_busy_not_down():
    async def handler(**kwargs: Any) -> str:
        if kwargs["prompt"] == "slow":
            await asyncio.sleep(0.5)
        return "from gateway"

    gateway = FakeProvider("gateway", handler=handler, max_concurrency=1)
    docker = FakeProvider("docker", responses=["from docker"])
    ai.use_providers([gateway, docker])
    slow = asyncio.create_task(ai.complete(feature="security_triage", system="s", prompt="slow"))
    await drain()
    res = await ai.complete(feature="security_triage", system="s", prompt="fast", timeout=0.1)
    assert res.provider == "docker"  # the gateway's only slot stayed taken: next provider
    assert (await slow).provider == "gateway"
    st = gateway.state
    assert (st.failures, st.consecutive_failures, gateway.circuit_open()) == (0, 0, False)
    assert [c.provider for c in call_log_queue] == ["docker", "gateway"]  # a busy skip writes no failure row


async def test_only_busy_providers_means_unavailable_but_healthy():
    gateway = FakeProvider("gateway", handler=SlowHandler(seconds=0.5, answer="x"), max_concurrency=1)
    ai.use_providers([gateway])
    slow = asyncio.create_task(ai.complete(feature="security_triage", system="s", prompt="slow"))
    await drain()
    with pytest.raises(AIUnavailableError) as err:
        await ai.complete(feature="security_triage", system="s", prompt="fast", timeout=0.1)
    assert "busy" in str(err.value)
    await slow
    assert gateway.state.failures == 0 and ai.status()["available"] is True


async def test_provider_timeout_once_the_slot_is_held_still_counts():
    provider = FakeProvider("gateway", handler=SlowHandler(seconds=1.0))
    ai.use_providers([provider])
    with pytest.raises(AIUnavailableError):
        await ai.complete(feature="security_triage", system="s", prompt="p", timeout=0.05)
    assert provider.state.consecutive_failures == 1 and "no answer within" in provider.state.last_error


async def test_latency_is_provider_time_only():
    async def handler(**kwargs: Any) -> str:
        if kwargs["prompt"] == "slow":
            await asyncio.sleep(0.3)
        return "x"

    provider = FakeProvider("gateway", handler=handler, max_concurrency=1)
    ai.use_providers([provider])
    slow = asyncio.create_task(ai.complete(feature="security_triage", system="s", prompt="slow"))
    await drain()
    started = time.monotonic()
    res = await ai.complete(feature="security_triage", system="s", prompt="fast")  # waits for the slot
    assert time.monotonic() - started >= 0.25 and res.latency_ms < 150
    assert (await slow).latency_ms >= 250


async def test_json_repair_still_runs_on_the_same_provider():
    from pydantic import BaseModel

    class Out(BaseModel):
        n: int

    provider = FakeProvider("gateway", responses=["nope", '{"n": 4}'])
    ai.use_providers([provider])
    res = await ai.complete(feature="assistant", system="s", prompt="p", schema=Out, timeout=5)
    assert res.data.n == 4 and len(provider.prompts) == 2 and provider.prompts[1]["temperature"] == 0.0


# ------------------------------------------------------------------ streaming
async def test_closing_the_stream_consumer_aborts_the_provider_stream(guard):
    provider = FakeProvider("gateway", chunks=["Hel", "lo", " there"], delay=0.05, max_concurrency=1)
    ai.use_providers([provider])
    agen = ai.stream(feature="plans", system="s", prompt="p", user_text="q", owner=AIOwner(user_id=3))
    assert await agen.__anext__() == "Hel"
    assert provider.semaphore.locked()
    await agen.aclose()
    assert provider.streams_closed == 1 and not provider.semaphore.locked()
    snap = scheduler.snapshot()
    assert snap["lanes"]["llm"]["running"] == 0 and snap["counters"]["cancelled"] == {"aborted": 1}
    log = call_log_queue[-1]
    assert (log.ok, log.cancelled, log.user_id, log.owner_kind) == (False, True, 3, "user")
    await drain()
    # its first words went out before the reader left: charged (a stream stopped before its first words is free)
    assert guard.admitted[0][1:] == ("plans", "q") and [ok for _, ok in guard.settled] == [True]


async def test_cancelling_the_stream_consumer_task_aborts_the_provider_stream(guard):
    """An SSE disconnect: Starlette cancels the task iterating the stream while it waits for a chunk."""
    provider = FakeProvider("gateway", chunks=["a", "b", "c"], delay=0.3)
    ai.use_providers([provider])
    got: list[str] = []

    async def consume() -> None:
        async for chunk in ai.stream(feature="plans", system="s", prompt="p", owner=AIOwner(user_id=4)):
            got.append(chunk)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.45)  # 'a' arrived at 0.3 s; waiting for 'b'
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert got == ["a"] and provider.streams_closed == 1 and not provider.semaphore.locked()
    assert llm_lane()["running"] == 0 and call_log_queue[-1].cancelled
    await drain()
    assert [ok for _, ok in guard.settled] == [True]  # 'a' went out: charged


async def test_queue_cancel_stops_a_stream_between_chunks():
    provider = FakeProvider("gateway", chunks=["one ", "two ", "three"], delay=0.05)
    ai.use_providers([provider])
    meta: dict[str, Any] = {}
    agen = ai.stream(feature="plans", system="s", prompt="p", meta=meta,
                     owner=AIOwner(user_id=8, page_id="page-stream-01"))
    got = [await agen.__anext__()]
    assert scheduler.cancel(page_id="page-stream-01", reason="page_closed") == 1
    got += await collect(agen)
    assert got == ["one "]  # the stream simply ends - no error for the consumer ...
    assert meta["cancelled"] == "page_closed"  # ... which can still tell a cut answer from a complete one
    assert provider.streams_closed == 1
    snap = scheduler.snapshot()
    assert snap["counters"]["cancelled"] == {"page_closed": 1} and snap["lanes"]["llm"]["running"] == 0
    assert call_log_queue[-1].cancelled and call_log_queue[-1].error == "cancelled (page_closed)"


async def test_stream_cancelled_before_its_first_words_raises_aicancelled():
    provider = FakeProvider("gateway", chunks=["late"], delay=0.3)
    ai.use_providers([provider])
    agen = ai.stream(feature="plans", system="s", prompt="p", owner=AIOwner(user_id=8))

    async def first() -> str:
        return await agen.__anext__()

    task = asyncio.create_task(first())
    await asyncio.sleep(0.1)
    assert scheduler.cancel(user_id=8, reason="left") == 1
    with pytest.raises(AICancelled) as err:
        await task
    assert err.value.reason == "left" and provider.streams_closed == 1 and llm_lane()["running"] == 0


async def test_queue_cancel_ends_a_stalled_first_token_wait_at_once():
    """The provider has not said a word yet (slow first token): the cancel does not wait for it - the provider stream
    is aborted where it waits, its slot freed, and nothing counts as a provider failure."""
    provider = FakeProvider("gateway", chunks=["late"], delay=30.0, max_concurrency=1)
    ai.use_providers([provider])
    agen = ai.stream(feature="plans", system="s", prompt="p", owner=AIOwner(user_id=8), first_token_timeout=60)

    async def first() -> str:
        return await agen.__anext__()

    task = asyncio.create_task(first())
    await asyncio.sleep(0.05)
    started = time.monotonic()
    assert scheduler.cancel(user_id=8, reason="page_closed") == 1
    with pytest.raises(AICancelled) as err:
        await asyncio.wait_for(task, 2)
    assert err.value.reason == "page_closed" and time.monotonic() - started < 0.5
    assert provider.streams_closed == 1 and not provider.semaphore.locked() and llm_lane()["running"] == 0
    assert (provider.state.failures, provider.state.consecutive_failures) == (0, 0)
    assert call_log_queue[-1].cancelled and call_log_queue[-1].error == "cancelled (page_closed)"


async def test_queue_cancel_ends_a_stalled_wait_for_more_words_at_once():
    class SlowSecond(FakeProvider):
        async def stream_text(self, **kwargs: Any):
            async with self.slot():
                try:
                    yield "first "
                    await asyncio.sleep(30)
                    yield "never"  # pragma: no cover
                finally:
                    self.streams_closed += 1

    provider = SlowSecond("gateway", max_concurrency=1)
    ai.use_providers([provider])
    meta: dict[str, Any] = {}
    agen = ai.stream(feature="plans", system="s", prompt="p", meta=meta, owner=AIOwner(user_id=8, page_id="page-x-0001"))
    got = [await agen.__anext__()]
    rest = asyncio.create_task(collect(agen))
    await asyncio.sleep(0.05)
    started = time.monotonic()
    assert scheduler.cancel(page_id="page-x-0001", reason="page_closed") == 1
    got += await asyncio.wait_for(rest, 2)
    assert got == ["first "] and meta["cancelled"] == "page_closed" and time.monotonic() - started < 0.5
    assert provider.streams_closed == 1 and not provider.semaphore.locked() and provider.state.failures == 0
    assert call_log_queue[-1].cancelled


async def test_stream_cancel_is_never_a_provider_failure_whatever_the_provider_raises():
    """An SDK may turn the aborted request into an error of its own: the router still reports the cancel."""
    class Converting(FakeProvider):
        async def stream_text(self, **kwargs: Any):
            async with self.slot():
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    raise ProviderError("connection reset by peer") from None
                yield "never"  # pragma: no cover

    provider = Converting("gateway")
    ai.use_providers([provider, FakeProvider("docker", responses=["never asked"])])
    agen = ai.stream(feature="plans", system="s", prompt="p", owner=AIOwner(user_id=8))

    async def first() -> str:
        return await agen.__anext__()

    task = asyncio.create_task(first())
    await asyncio.sleep(0.05)
    scheduler.cancel(user_id=8, reason="left")
    with pytest.raises(AICancelled) as err:
        await asyncio.wait_for(task, 2)
    assert err.value.reason == "left" and provider.state.failures == 0 and not provider.circuit_open()
    assert ai.providers[1].prompts == []  # a cancel is not a reason to fail over


async def test_queue_refusal_too_many_is_reported_to_the_guard(guard, monkeypatch, fake_ai):
    """'too_many' (a user flooding the queue) earns guard strikes; the guard may turn it into 'cooldown'."""
    reported: list[tuple[Any, str, str]] = []
    replacement = AIRejected("cooldown", "AI is paused on your account for 2 min.", retry_after=120)

    async def refusal(owner: Any, feature: str, exc: AIRejected) -> AIRejected:
        reported.append((owner.user_id, feature, exc.code))
        return replacement if len(reported) > 1 else exc

    guard.refusal = refusal
    monkeypatch.setattr(get_settings(), "ai_queue_max_per_user", 1)
    handler = SlowHandler()
    fake_ai(handler=handler)
    owner = AIOwner(user_id=5)
    first = asyncio.create_task(ai.complete(feature="assistant", system="s", prompt="1", owner=owner))
    await handler.started.wait()
    with pytest.raises(AIRejected) as err:
        await ai.complete(feature="assistant", system="s", prompt="2", owner=owner)
    assert err.value.code == "too_many"
    agen = ai.stream(feature="assistant", system="s", prompt="3", owner=owner)
    with pytest.raises(AIRejected) as err:
        await agen.__anext__()
    assert err.value is replacement  # the account was paused by now: the real wait is shown
    assert reported == [(5, "assistant", "too_many"), (5, "assistant", "too_many")]
    # jobs and system work are never refused by the queue, 'busy' is not the user's fault: nothing to report
    monkeypatch.setattr(get_settings(), "ai_queue_max_total", 0)
    with pytest.raises(AIRejected) as err:
        await ai.complete(feature="assistant", system="s", prompt="4", owner=AIOwner(user_id=6))
    assert err.value.code == "busy" and len(reported) == 2
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    await drain()
    # every refused call was refunded; the first one was cancelled while its provider worked: charged
    assert sorted((a.n, ok) for a, ok in guard.settled) == [(1, True), (2, False), (3, False), (4, False)]


async def test_stream_waits_in_the_queue_and_can_be_cancelled_there(monkeypatch):
    monkeypatch.setattr(get_settings(), "ai_queue_concurrency", 1)
    provider = FakeProvider("gateway", chunks=["x"], delay=0.5)
    ai.use_providers([provider])
    holder = asyncio.create_task(collect(ai.stream(feature="plans", system="s", prompt="a")))
    await asyncio.sleep(0.05)
    agen = ai.stream(feature="plans", system="s", prompt="b", owner=AIOwner(user_id=11))

    async def first() -> str:
        return await agen.__anext__()

    waiting = asyncio.create_task(first())
    await asyncio.sleep(0.05)
    assert llm_lane()["queued"] == 1
    scheduler.cancel(user_id=11, reason="logout")
    with pytest.raises(AICancelled):
        await waiting
    assert await holder == ["x"] and len(provider.prompts) == 1


async def test_stream_honours_provider_error_availability():
    class Refusing(FakeProvider):
        def __init__(self, name: str, *, availability: bool) -> None:
            super().__init__(name)
            self.availability = availability

        async def stream_text(self, **kwargs: Any):
            async with self.slot():
                raise ProviderError("unusable output", availability=self.availability)
            yield ""  # pragma: no cover - makes this an async generator

    unusable = Refusing("gateway", availability=False)
    ai.use_providers([unusable, FakeProvider("docker", responses=["fallback words"])])
    for _ in range(3):
        assert await collect(ai.stream(feature="plans", system="s", prompt="p")) == ["fallback words"]
    st = unusable.state
    assert (st.failures, st.consecutive_failures, unusable.circuit_open()) == (3, 0, False)

    down = Refusing("gateway", availability=True)
    ai.use_providers([down, FakeProvider("docker", responses=["fallback words"])])
    for _ in range(get_settings().ai_circuit_failures):
        await collect(ai.stream(feature="plans", system="s", prompt="p"))
    assert down.circuit_open()


async def test_stream_total_timeout_applies_while_no_chunk_arrives():
    class Stalling(FakeProvider):
        async def stream_text(self, **kwargs: Any):
            async with self.slot():
                try:
                    yield "first words"
                    await asyncio.sleep(30)
                    yield "never"  # pragma: no cover
                finally:
                    self.streams_closed += 1

    provider = Stalling("gateway")
    ai.use_providers([provider])
    started = time.monotonic()
    got = await collect(ai.stream(feature="plans", system="s", prompt="p", total_timeout=0.3))
    assert got == ["first words", " …"] and time.monotonic() - started < 1.5
    assert provider.streams_closed == 1 and not provider.semaphore.locked()
    assert call_log_queue[-1].ok and not call_log_queue[-1].cancelled  # a cut answer is still an answer


async def test_stream_waiting_for_a_provider_slot_is_busy_not_down():
    gateway = FakeProvider("gateway", chunks=["slow ", "answer"], delay=0.4, max_concurrency=1)
    docker = FakeProvider("docker", responses=["docker answer"])
    ai.use_providers([gateway, docker])
    first = asyncio.create_task(collect(ai.stream(feature="plans", system="s", prompt="p")))
    await asyncio.sleep(0.05)  # the first stream holds the gateway's only slot
    got = await collect(ai.stream(feature="plans", system="s", prompt="q", first_token_timeout=0.1))
    assert got == ["docker answer"]
    assert await first == ["slow ", "answer"]
    assert gateway.state.failures == 0 and not gateway.circuit_open()


async def test_first_token_clock_starts_when_the_slot_is_held():
    provider = FakeProvider("gateway", chunks=["hello"], delay=0.35, max_concurrency=1)
    ai.use_providers([provider])
    holder = asyncio.create_task(collect(ai.stream(feature="plans", system="s", prompt="a")))
    await asyncio.sleep(0.02)
    # waits ~0.33 s for the slot, then ~0.35 s for its first chunk: 0.68 s > 0.5 s, but only 0.35 s after the slot
    got = await collect(ai.stream(feature="plans", system="s", prompt="b", first_token_timeout=0.5))
    assert got == ["hello"] and await holder == ["hello"]
    assert provider.state.failures == 0


async def test_stream_first_token_timeout_still_counts_as_a_failure():
    provider = FakeProvider("gateway", chunks=["late"], delay=1.0)
    ai.use_providers([provider])
    with pytest.raises(AIUnavailableError):
        await collect(ai.stream(feature="plans", system="s", prompt="p", first_token_timeout=0.1))
    assert provider.state.consecutive_failures == 1 and provider.streams_closed == 1
    assert not call_log_queue[-1].ok and not call_log_queue[-1].cancelled


async def test_stream_meta_and_success_log(guard):
    ai.use_providers([FakeProvider("gateway", chunks=["a", "b"])])
    meta: dict[str, Any] = {}
    assert await collect(ai.stream(feature="plans", system="s", prompt="p", meta=meta,
                                   owner=AIOwner(user_id=12))) == ["a", "b"]
    assert meta == {"provider": "gateway", "model": "fake-model", "label": "Fake (gateway)"}
    log = call_log_queue[-1]
    assert (log.ok, log.cancelled, log.user_id, log.feature) == (True, False, 12, "plans")
    await drain()
    assert [ok for _, ok in guard.settled] == [True]
    assert scheduler.snapshot()["counters"]["completed"] == 1


async def test_stream_cut_by_a_provider_failure_is_settled_as_not_ok(guard):
    """The words already said are kept and marked, but the guard settles the call as not ok: the person is refunded
    and may ask the same thing again at once (no 'duplicate'). Ending normally (above) settles ok."""
    class Breaks(FakeProvider):
        async def stream_text(self, **kwargs: Any):
            async with self.slot():
                yield "Online plans start at "
                raise ProviderError("connection reset by the gateway")

    ai.use_providers([Breaks("gateway")])
    got = await collect(ai.stream(feature="plans", system="s", prompt="p", user_text="q", owner=AIOwner(user_id=13)))
    assert got == ["Online plans start at ", " … (the answer was interrupted)"]
    await drain()
    assert [ok for _, ok in guard.settled] == [False]
    assert not scheduler.snapshot()["lanes"]["llm"]["running"]


# ------------------------------------------------------------------ provider stream fixes
class FakeOpenAIStream:
    """Mimics openai.AsyncStream: async-iterable chunks, ``close()`` and ``async with``."""

    def __init__(self, pieces: list[str]) -> None:
        self.pieces = pieces
        self.closed = False

    async def __aenter__(self) -> FakeOpenAIStream:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self):
        return self._events()

    async def _events(self):
        for piece in self.pieces:
            await asyncio.sleep(0)
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=piece))])


def openai_provider(stream: FakeOpenAIStream) -> tuple[OpenAICompatProvider, list[dict[str, Any]]]:
    provider = OpenAICompatProvider(get_settings(), name="gateway", label="Gemma (gateway)",
                                    base_url="http://gateway.test/v1", api_key="k", model="gemma", timeout=5,
                                    max_concurrency=1)
    calls: list[dict[str, Any]] = []

    async def create(**kwargs: Any) -> FakeOpenAIStream:
        calls.append(kwargs)
        return stream

    provider._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return provider, calls


async def test_openai_stream_is_closed_when_the_consumer_stops():
    stream = FakeOpenAIStream(["a", "b", "c"])
    provider, calls = openai_provider(stream)
    agen = provider.stream_text(system="s", prompt="p", max_tokens=10, temperature=0.1)
    assert await agen.__anext__() == "a"
    assert provider.semaphore.locked() and not stream.closed
    await agen.aclose()  # closed at a yield: the HTTP response must be closed now, not at garbage collection
    assert stream.closed and not provider.semaphore.locked()
    assert calls[0]["stream"] is True and calls[0]["model"] == "gemma" and calls[0]["max_tokens"] == 10


async def test_openai_stream_is_closed_after_the_router_cuts_it():
    stream = FakeOpenAIStream(["a"] * 50)
    provider, _ = openai_provider(stream)
    ai.use_providers([provider])
    got = await collect(ai.stream(feature="plans", system="s", prompt="p", total_timeout=0.0001))
    assert got[-1] == " …" and stream.closed and not provider.semaphore.locked()


async def test_openai_stream_fully_read_is_closed():
    stream = FakeOpenAIStream(["a", "b"])
    provider, _ = openai_provider(stream)
    ai.use_providers([provider])
    assert await collect(ai.stream(feature="plans", system="s", prompt="p")) == ["a", "b"]
    assert stream.closed


class FakeAnthropicStream:
    """Mimics the BetaAsyncMessageStreamManager / BetaAsyncMessageStream pair."""

    def __init__(self, pieces: list[str], stop_reason: str | None = "end_turn") -> None:
        self.pieces = pieces
        self.stop_reason = stop_reason  # None: the stream never started a message (the SDK then asserts)
        self.closed = False
        self.text_stream = self._text()

    async def __aenter__(self) -> FakeAnthropicStream:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.closed = True

    async def _text(self):
        for piece in self.pieces:
            await asyncio.sleep(0)
            yield piece

    async def get_final_message(self) -> Any:
        assert self.stop_reason is not None  # like the SDK: no message snapshot without message_start
        return SimpleNamespace(stop_reason=self.stop_reason)


def anthropic_provider(stream: FakeAnthropicStream) -> tuple[AnthropicProvider, list[dict[str, Any]]]:
    provider = AnthropicProvider(get_settings())
    calls: list[dict[str, Any]] = []

    def open_stream(**kwargs: Any) -> FakeAnthropicStream:
        calls.append(kwargs)
        return stream

    provider._client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=open_stream)))
    return provider, calls


async def test_anthropic_streams_text_deltas_with_the_completion_request():
    stream = FakeAnthropicStream(["Hi", " there"])
    provider, calls = anthropic_provider(stream)
    got = await collect(provider.stream_text(system="sys", prompt="question", max_tokens=260, temperature=0.3))
    assert got == ["Hi", " there"] and stream.closed and not provider.semaphore.locked()
    kw = calls[0]
    assert kw["model"] == provider.model and kw["max_tokens"] == 2048 and kw["system"] == "sys"
    assert kw["messages"] == [{"role": "user", "content": [{"type": "text", "text": "question"}]}]
    assert kw["output_config"] == {"effort": get_settings().anthropic_effort}
    assert kw["betas"] == ["server-side-fallback-2026-07-01"] and kw["fallbacks"] == "default"
    assert provider.state.calls == 1


async def test_anthropic_stream_is_closed_when_the_consumer_stops():
    stream = FakeAnthropicStream(["a", "b", "c"])
    provider, _ = anthropic_provider(stream)
    agen = provider.stream_text(system="s", prompt="p", max_tokens=10, temperature=0)
    assert await agen.__anext__() == "a"
    await agen.aclose()
    assert stream.closed and not provider.semaphore.locked()


async def test_anthropic_stream_refusal_is_unusable_output_not_an_outage():
    provider, _ = anthropic_provider(FakeAnthropicStream([], stop_reason="refusal"))
    with pytest.raises(ProviderError) as err:
        await collect(provider.stream_text(system="s", prompt="p", max_tokens=10, temperature=0))
    assert err.value.availability is False


async def test_anthropic_stream_without_a_message_snapshot_just_ends():
    stream = FakeAnthropicStream([], stop_reason=None)
    provider, _ = anthropic_provider(stream)
    assert await collect(provider.stream_text(system="s", prompt="p", max_tokens=10, temperature=0)) == []
    assert stream.closed and not provider.semaphore.locked()


async def test_anthropic_stream_through_the_router(monkeypatch):
    monkeypatch.setattr(get_settings(), "anthropic_api_key", "test-key")
    stream = FakeAnthropicStream(["Claude ", "answers"])
    provider, _ = anthropic_provider(stream)
    ai.use_providers([FakeProvider("gateway", fail=True), provider])
    meta: dict[str, Any] = {}
    assert await collect(ai.stream(feature="plans", system="s", prompt="p", meta=meta)) == ["Claude ", "answers"]
    assert meta["provider"] == "anthropic" and stream.closed


# ------------------------------------------------------------------ error handlers
@pytest.fixture
def raising_route(app):
    """A throw-away route (first in the table, removed afterwards) that raises the exception put in the box."""
    box: dict[str, BaseException] = {}

    async def raise_ai_error() -> None:
        raise box["exc"]

    app.add_api_route("/test-ai-errors/raise", raise_ai_error, methods=["GET"])
    route = app.router.routes.pop()
    app.router.routes.insert(0, route)
    yield box
    app.router.routes.remove(route)


JSON = {"Accept": "application/json"}
HTML = {"Accept": "text/html,application/xhtml+xml"}


async def test_ai_rejected_json_carries_code_and_retry_after(client, raising_route):
    raising_route["exc"] = AIRejected("quota", "You reached today's AI limit.", retry_after=30, feature="assistant")
    r = await client.get("/test-ai-errors/raise", headers=JSON)
    assert r.status_code == 429 and r.headers["retry-after"] == "30"
    assert r.json() == {"error": "AI limit reached", "detail": "You reached today's AI limit.", "code": "quota",
                        "retry_after": 30, "ai_available": True}


@pytest.mark.parametrize("code,status", [("cooldown", 429), ("too_many", 429), ("duplicate", 409), ("spam", 422),
                                         ("busy", 503), ("disabled", 403)])
async def test_ai_rejected_status_per_code(client, raising_route, code, status):
    raising_route["exc"] = AIRejected(code, "nope", retry_after=5)
    r = await client.get("/test-ai-errors/raise", headers=JSON)
    assert r.status_code == status and r.json()["code"] == code and r.headers["retry-after"] == "5"


async def test_ai_rejected_without_retry_after_has_no_header(client, raising_route):
    raising_route["exc"] = AIRejected("spam", "Message blocked.")
    r = await client.get("/test-ai-errors/raise", headers=JSON)
    assert r.status_code == 422 and "retry-after" not in r.headers and r.json()["retry_after"] == 0


async def test_ai_rejected_html_page(client, raising_route):
    raising_route["exc"] = AIRejected("cooldown", "AI is paused for your account for 2 minutes.", retry_after=120)
    r = await client.get("/test-ai-errors/raise", headers=HTML)
    assert r.status_code == 429 and r.headers["retry-after"] == "120"
    assert r.headers["content-type"].startswith("text/html")
    assert "AI paused" in r.text and "2 minutes" in r.text


async def test_ai_cancelled_json_and_html(client, raising_route):
    raising_route["exc"] = AICancelled("client_left")
    r = await client.get("/test-ai-errors/raise", headers=JSON)
    assert r.status_code == 503
    body = r.json()
    assert body["error"] == "Cancelled" and body["code"] == "cancelled" and body["detail"]
    r = await client.get("/test-ai-errors/raise", headers=HTML)
    assert r.status_code == 503 and r.headers["content-type"].startswith("text/html") and "stopped" in r.text


async def test_plain_ai_unavailable_keeps_its_handler(client, raising_route):
    raising_route["exc"] = AIUnavailableError("everything is down")
    r = await client.get("/test-ai-errors/raise", headers=JSON)
    assert r.status_code == 503 and r.json()["error"] == "AI unavailable" and r.json()["ai_available"] is False


# ------------------------------------------------------------------ end to end + persistence
async def test_signed_in_stream_is_attributed_and_guard_refusals_are_blocked_events(client, db, guard):
    from tests.conftest import api, login_as
    from tests.factories import make_branch, make_customer, make_gym, make_owner, make_plans

    gym = await make_gym(db, await make_owner(db), name="Pulse Central")
    await make_branch(db, gym)
    await make_plans(db, gym)
    user = await make_customer(db)
    await login_as(client, user)
    ai.use_providers([FakeProvider("gateway", chunks=["Online ", "is best."])])
    r = await api(client, "POST", "/api/plans/ask", json={"question": "Which plan?", "gym_slug": gym.slug})
    assert r.status_code == 200 and "is best." in r.text
    log = [c for c in call_log_queue if c.feature == "plans"][-1]
    assert log.ok and log.user_id == user.id and log.owner_kind == "user"
    assert guard.admitted[-1][0].user_id == user.id and guard.admitted[-1][1] == "plans"

    # a guard refusal is a 'blocked' SSE event (the widget shows why and starts its cooldown) - never an answer
    guard.reject = AIRejected("quota", "AI limit reached.", retry_after=60)
    r = await api(client, "POST", "/api/plans/ask", json={"question": "Cheapest plan?", "gym_slug": gym.slug})
    assert r.status_code == 200 and '"type": "blocked"' in r.text and '"code": "quota"' in r.text
    assert '"fallback": true' not in r.text
    # the member's words, about this gym: tagged (the old form) or, once plans pass dedupe_scope, raw + scoped
    assert (guard.admitted[-1][2], guard.scopes[-1]) in {(f"[gym {gym.id}] Cheapest plan?", None),
                                                          ("Cheapest plan?", f"gym:{gym.id}")}


async def test_flush_writes_owner_queue_and_cancel_columns(db):
    from sqlalchemy import select

    from app.models import AICallLog

    call_log_queue.append(CallLog("assistant", "gateway", "gemma", False, 120, "cancelled (left)", user_id=77,
                                  owner_kind="user", queue_ms=340, cancelled=True))
    call_log_queue.append(CallLog("ocr", "docker", "gemma3", True, 900, None))  # the old positional form
    assert await flush_ai_call_logs(db) == 2
    await db.commit()
    rows = (await db.execute(select(AICallLog).order_by(AICallLog.id))).scalars().all()
    assert [(r.feature, r.provider, r.user_id, r.owner_kind, r.queue_ms, r.cancelled, r.ok) for r in rows] == [
        ("assistant", "gateway", 77, "user", 340, True, False), ("ocr", "docker", None, None, None, False, True)]
