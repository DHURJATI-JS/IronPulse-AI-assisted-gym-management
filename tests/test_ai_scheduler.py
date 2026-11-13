"""Fair AI queue (app/ai/scheduler.py, docs/ai-guard-and-queue.md section 4): lanes, round-robin fairness between
users, priorities with ageing, admission limits, wait limits, cancellation (every filter, cancel-safety races),
WebSocket ``ai.queue`` events, counters and reset. Pure in-process tests: no database or provider is involved."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from app.ai import policy
from app.ai import scheduler as sched_mod
from app.ai.errors import AICancelled, AIRejected, AIUnavailableError
from app.ai.owner import AIOwner
from app.ai.scheduler import Ticket, scheduler
from app.config import get_settings
from app.services.realtime import hub


# ------------------------------------------------------------------ helpers
def user(uid: int, *, page: str | None = None, session: str | None = None) -> AIOwner:
    return AIOwner(user_id=uid, role="customer", session_hash=session or f"sess-{uid}",
                   page_id=page or f"page-{uid:04d}-tab")


def job(uid: int | None, *, exit_policy: str | None = None, job_id: int = 7) -> AIOwner:
    return AIOwner(user_id=uid, kind="job", job_id=job_id, exit_policy=exit_policy)


async def settle(n: int = 5) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


class Recorder:
    """Starts acquire() calls as tasks and records the order in which they get their slot."""

    def __init__(self) -> None:
        self.order: list[str] = []
        self.tickets: dict[str, Ticket] = {}
        self.tasks: dict[str, asyncio.Task] = {}

    def start(self, owner: AIOwner | None, label: str, feature: str = "assistant", **kw: Any) -> asyncio.Task:
        async def run() -> Ticket:
            t = await scheduler.acquire(owner, feature, **kw)
            self.order.append(label)
            self.tickets[label] = t
            return t

        task = self.tasks[label] = asyncio.create_task(run(), name=f"acquire-{label}")
        return task

    def release(self, label: str, outcome: str = "done") -> None:
        scheduler.release(self.tickets[label], outcome=outcome)

    async def cleanup(self) -> None:
        for task in self.tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        for t in self.tickets.values():
            scheduler.release(t, outcome="done")


@pytest.fixture
async def rec():
    r = Recorder()
    yield r
    await r.cleanup()


@pytest.fixture
def capacity(monkeypatch):
    def set_(llm: int, tts: int | None = None) -> None:
        monkeypatch.setattr(get_settings(), "ai_queue_concurrency", llm)
        if tts is not None:
            monkeypatch.setattr(get_settings(), "ai_tts_concurrency", tts)

    return set_


@pytest.fixture
def events(monkeypatch):
    sent: list[tuple[list[int], dict[str, Any]]] = []
    monkeypatch.setattr(hub, "publish_nowait", lambda user_ids, evt: sent.append((list(user_ids), dict(evt))))
    return sent


def lane(name: str = "llm") -> dict[str, Any]:
    return scheduler.snapshot()["lanes"][name]


# ------------------------------------------------------------------ fairness
async def test_flooding_user_does_not_starve_a_single_request(rec, capacity):
    """A floods 4 requests; B's one request starts before A's 3rd (MAX_RUNNING_PER_USER=2, two slots)."""
    capacity(2)
    for i in range(1, 5):
        rec.start(user(1), f"a{i}")
    await settle()
    rec.start(user(2), "b1")
    await settle()
    assert rec.order == ["a1", "a2"]  # A runs its two allowed calls, the rest waits
    rec.release("a1")
    await settle()
    assert rec.order == ["a1", "a2", "b1"]  # B (never served) goes before A's 3rd
    rec.release("a2")
    await settle()
    rec.release("b1")
    await settle()
    assert rec.order == ["a1", "a2", "b1", "a3", "a4"]


async def test_round_robin_alternates_between_busy_users(rec, capacity):
    capacity(1)
    for i in range(1, 4):
        rec.start(user(1), f"a{i}")
    await settle()
    for i in range(1, 4):
        rec.start(user(2), f"b{i}")
    await settle()
    for _ in range(5):
        rec.release(rec.order[-1])
        await settle()
    assert rec.order == ["a1", "b1", "a2", "b2", "a3", "b3"]


async def test_fifo_within_one_user(rec, capacity):
    capacity(1)
    rec.start(None, "system-holder", feature="security_triage")
    await settle()
    for i in range(1, 4):
        rec.start(user(3), f"c{i}")
        await settle()
    for _ in range(3):
        rec.release(rec.order[-1])
        await settle()
    assert rec.order == ["system-holder", "c1", "c2", "c3"]


async def test_jobs_and_system_work_are_their_own_buckets(rec, capacity):
    capacity(1)
    # one priority for all four ('gear' is BACKGROUND, and so is every job): only the buckets decide the order
    rec.start(user(1), "u1", feature="gear")
    await settle()
    rec.start(user(1), "u2", feature="gear")
    rec.start(job(1), "j1", feature="gear")  # same user, but a job: a separate bucket
    rec.start(None, "s1", feature="gear")
    await settle()
    rec.release("u1")
    await settle()
    rec.release(rec.order[-1])
    await settle()
    rec.release(rec.order[-1])
    await settle()
    assert rec.order[0] == "u1" and set(rec.order[1:3]) == {"j1", "s1"} and rec.order[3] == "u2"
    assert rec.tickets["j1"].kind == "job" and rec.tickets["s1"].kind == "system"


async def test_max_running_per_user_lets_others_start(rec, capacity):
    capacity(4)
    assert policy.MAX_RUNNING_PER_USER == 2
    for i in range(1, 5):
        rec.start(user(1), f"a{i}")
    await settle()
    assert rec.order == ["a1", "a2"] and lane()["running"] == 2 and lane()["queued"] == 2  # free slots stay free
    rec.start(user(2), "b1")
    await settle()
    assert rec.order[-1] == "b1" and lane()["running"] == 3
    rec.release("a1")
    await settle()
    assert rec.order[-1] == "a3" and lane()["running"] == 3 and lane()["queued"] == 1


async def test_running_cap_is_read_from_policy(rec, capacity, monkeypatch):
    capacity(4)
    monkeypatch.setattr(policy, "MAX_RUNNING_PER_USER", 1)
    rec.start(user(1), "a1")
    rec.start(user(1), "a2")
    await settle()
    assert rec.order == ["a1"]


# ------------------------------------------------------------------ priorities + ageing
async def test_priorities_realtime_then_interactive_then_background(rec, capacity):
    capacity(1)
    rec.start(None, "holder", feature="security_triage")
    await settle()
    rec.start(user(1), "gear", feature="gear")  # BACKGROUND
    rec.start(user(2), "assistant", feature="assistant")  # INTERACTIVE
    rec.start(user(3), "live", feature="live_coach")  # REALTIME
    await settle()
    for _ in range(3):
        rec.release(rec.order[-1])
        await settle()
    assert rec.order == ["holder", "live", "assistant", "gear"]
    assert [rec.tickets[k].priority for k in ("live", "assistant", "gear")] == [
        policy.REALTIME, policy.INTERACTIVE, policy.BACKGROUND]


async def test_background_work_is_promoted_after_waiting(rec, capacity, monkeypatch):
    capacity(1)
    now = [1000.0]
    monkeypatch.setattr(scheduler, "clock", lambda: now[0])
    rec.start(None, "holder", feature="security_triage")
    await settle()
    rec.start(user(1), "gear", feature="gear")  # BACKGROUND, queued first
    rec.start(user(2), "ask1", feature="assistant")  # INTERACTIVE
    rec.start(user(3), "ask2", feature="assistant")
    await settle()
    rec.release("holder")
    await settle()
    assert rec.order[-1] == "ask1"  # not aged yet: interactive work first
    now[0] += sched_mod.AGEING_SECONDS + 1  # the background ticket has now waited > 60 s
    rec.release("ask1")
    await settle()
    assert rec.order[-1] == "gear"  # promoted to INTERACTIVE, oldest of its level -> before ask2
    rec.release("gear")
    await settle()
    assert rec.order == ["holder", "ask1", "gear", "ask2"]


async def test_ageing_threshold_is_a_module_setting(rec, capacity, monkeypatch):
    capacity(1)
    monkeypatch.setattr(sched_mod, "AGEING_SECONDS", -1.0)  # everything counts as aged
    rec.start(None, "holder", feature="security_triage")
    await settle()
    rec.start(user(1), "gear", feature="gear")
    rec.start(user(2), "ask", feature="assistant")
    await settle()
    rec.release("holder")
    await settle()
    assert rec.order[-1] == "gear"


async def test_jobs_run_at_background_priority_whatever_their_feature(rec, capacity, monkeypatch):
    """A job asking an interactive (or real-time, or safety) feature still waits behind people looking at a
    spinner - and is aged like any background work, so it never starves."""
    capacity(1)
    now = [5000.0]
    monkeypatch.setattr(scheduler, "clock", lambda: now[0])
    rec.start(None, "holder", feature="security_triage")
    await settle()
    rec.start(job(1), "job-live", feature="live_coach")  # REALTIME feature, queued first
    rec.start(job(2), "job-safety", feature="health_metrics")  # safety feature (a health note job's review)
    rec.start(user(3), "ask1", feature="assistant")
    rec.start(user(4), "ask2", feature="assistant")
    await settle()
    queued = {(t.kind, t.user_id): t for t in scheduler._tickets.values() if t.state == "queued"}
    assert queued[("job", 1)].priority == queued[("job", 2)].priority == policy.BACKGROUND
    assert queued[("user", 3)].priority == policy.INTERACTIVE
    rec.release("holder")
    await settle()
    assert rec.order[-1] == "ask1"
    now[0] += sched_mod.AGEING_SECONDS + 1  # the jobs have waited > 60 s: promoted to INTERACTIVE
    rec.release("ask1")
    await settle()
    assert rec.order[-1] == "job-live"  # oldest of the (now) interactive level
    for _ in range(2):
        rec.release(rec.order[-1])
        await settle()
    assert rec.order == ["holder", "ask1", "job-live", "job-safety", "ask2"]
    rec.release("ask2")
    # system work keeps its feature's priority
    s = await asyncio.wait_for(scheduler.acquire(None, "assistant"), 2)
    assert s.kind == "system" and s.priority == policy.INTERACTIVE
    scheduler.release(s, outcome="done")


async def test_realtime_is_never_overtaken_by_aged_background(rec, capacity, monkeypatch):
    capacity(1)
    monkeypatch.setattr(sched_mod, "AGEING_SECONDS", -1.0)
    rec.start(None, "holder", feature="security_triage")
    await settle()
    rec.start(user(1), "gear", feature="gear")
    rec.start(user(2), "live", feature="live_coach")
    await settle()
    rec.release("holder")
    await settle()
    assert rec.order[-1] == "live"


# ------------------------------------------------------------------ admission limits
async def test_too_many_per_user(rec, capacity):
    capacity(6)
    for i in range(1, 5):
        rec.start(user(1), f"a{i}")
    await settle()
    with pytest.raises(AIRejected) as err:
        await scheduler.acquire(user(1), "assistant")
    assert err.value.code == "too_many" and err.value.status == 429 and err.value.retry_after == 5
    assert isinstance(err.value, AIUnavailableError)  # existing fallbacks still catch it
    # other users, jobs of the same user and safety features are not limited by it
    rec.start(user(2), "b1")
    rec.start(job(1), "j1")
    rec.start(user(1), "safety", feature="health_metrics")
    await settle()
    assert {"b1", "j1", "safety"} <= set(rec.order)
    snap = scheduler.snapshot()
    assert snap["counters"]["rejected"] == {"too_many": 1} and snap["counters"]["submitted"] == 8


async def test_max_per_user_setting_is_read_on_each_acquire(rec, capacity, monkeypatch):
    capacity(4)
    monkeypatch.setattr(get_settings(), "ai_queue_max_per_user", 1)
    rec.start(user(1), "a1")
    await settle()
    with pytest.raises(AIRejected):
        await scheduler.acquire(user(1), "assistant")


async def test_total_queue_cap_answers_busy(rec, capacity, monkeypatch):
    capacity(1)
    monkeypatch.setattr(get_settings(), "ai_queue_max_total", 2)
    rec.start(None, "holder", feature="security_triage")
    await settle()
    rec.start(user(2), "u2")
    rec.start(user(3), "u3")
    await settle()
    with pytest.raises(AIRejected) as err:
        await scheduler.acquire(user(4), "assistant")
    assert err.value.code == "busy" and err.value.status == 503 and err.value.headers() == {"Retry-After": "5"}
    rec.start(None, "system")  # jobs and system work always wait their turn
    rec.start(job(9), "job")
    await settle()
    assert lane()["queued"] == 4
    assert scheduler.snapshot()["counters"]["rejected"] == {"busy": 1}


async def test_wait_limit_expires_with_busy_and_never_touches_providers(rec, capacity):
    from app.ai.client import FakeProvider, ai

    capacity(1)
    provider = FakeProvider("gateway")
    ai.use_providers([provider])
    rec.start(None, "holder", feature="security_triage")
    await settle()
    started = time.monotonic()
    with pytest.raises(AIRejected) as err:
        await scheduler.acquire(user(1), "assistant", max_wait=0.05)
    assert time.monotonic() - started < 1.0
    assert err.value.code == "busy" and err.value.message == "AI is busy right now - please try again in a moment."
    assert err.value.retry_after == 5
    assert lane()["queued"] == 0 and scheduler.snapshot()["counters"]["expired"] == 1
    st = provider.state
    assert (st.failures, st.consecutive_failures, st.healthy, provider.circuit_open()) == (0, 0, None, False)
    assert ai.status()["available"] is True


async def test_default_wait_limit_comes_from_the_policy(rec, capacity, monkeypatch):
    capacity(1)
    monkeypatch.setitem(policy.FEATURES, "quick", policy.FeaturePolicy("quick", "qa", max_wait=0.05))
    rec.start(None, "holder", feature="security_triage")
    await settle()
    with pytest.raises(AIRejected):
        await scheduler.acquire(user(1), "quick")  # user requests wait at most policy.max_wait
    waiting_job = rec.start(job(1), "job", feature="quick")  # jobs/system: no wait limit
    waiting_sys = rec.start(None, "sys", feature="quick")
    await asyncio.sleep(0.2)
    assert not waiting_job.done() and not waiting_sys.done()
    rec.release("holder")
    await settle()
    rec.release(rec.order[-1])
    await settle()
    # both got their slot in the end; system work keeps the feature's priority, a job runs as BACKGROUND
    assert rec.order == ["holder", "sys", "job"]


async def test_slot_granted_at_the_deadline_is_kept(rec, capacity):
    """The slot is granted and the wait limit fires in the same loop iteration: the waiter keeps the slot."""
    capacity(1)
    rec.start(None, "holder", feature="security_triage")
    await settle()
    task = asyncio.create_task(scheduler.acquire(user(1), "assistant", max_wait=0.3))
    await settle()
    asyncio.get_running_loop().call_later(0.25, rec.release, "holder")
    time.sleep(0.4)  # block the loop: the release (due first) and the deadline become due together
    t = await task
    assert t.state == "running" and scheduler.snapshot()["counters"]["expired"] == 0
    scheduler.release(t, outcome="done")
    assert lane()["running"] == 0


# ------------------------------------------------------------------ cancel-safety of acquire / release
async def test_cancelled_waiter_leaves_the_queue(rec, capacity):
    capacity(1)
    rec.start(None, "holder", feature="security_triage")
    await settle()
    waiter = rec.start(user(1), "w")
    await settle()
    assert lane()["queued"] == 1
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert lane()["queued"] == 0
    snap = scheduler.snapshot()
    assert snap["counters"]["cancelled"] == {"aborted": 1} and snap["saved"]["cancelled_queued"] == 1
    rec.release("holder")
    await settle()
    assert lane()["running"] == 0 and rec.order == ["holder"]  # nothing it did not hold was freed


async def test_slot_granted_while_the_waiter_is_cancelled_goes_to_the_next(rec, capacity):
    capacity(1)
    rec.start(None, "holder", feature="security_triage")
    await settle()
    first = rec.start(user(1), "first")
    second = rec.start(user(2), "second")
    await settle()
    rec.release("holder")  # grants 'first' ...
    first.cancel()  # ... which is cancelled before it could resume
    with pytest.raises(asyncio.CancelledError):
        await first
    await settle()
    assert rec.order == ["holder", "second"] and second.done()
    assert lane()["running"] == 1 and lane()["queued"] == 0


async def test_release_frees_exactly_once(rec, capacity):
    capacity(1)
    rec.start(user(1), "a")
    rec.start(user(2), "b")
    rec.start(user(3), "c")
    await settle()
    rec.release("a")
    rec.release("a")  # double release must not start a second ticket
    rec.release("a", outcome="failed")
    await settle()
    assert rec.order == ["a", "b"] and lane()["running"] == 1
    assert scheduler.snapshot()["counters"]["completed"] == 1 and scheduler.snapshot()["counters"]["failed"] == 0


async def test_release_of_a_queued_ticket_just_removes_it(capacity):
    capacity(1)
    holder = await scheduler.acquire(None, "security_triage")
    task = asyncio.create_task(scheduler.acquire(user(1), "assistant"))
    await settle()
    queued = next(t for t in scheduler._tickets.values() if t.state == "queued")
    scheduler.release(queued, outcome="cancelled")
    assert lane()["queued"] == 0 and queued.state == "cancelled"
    scheduler.release(holder, outcome="done")
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert lane()["running"] == 0


async def test_capacity_setting_is_read_live(rec, capacity, monkeypatch):
    capacity(1)
    rec.start(user(1), "a")
    rec.start(user(2), "b")
    await settle()
    assert rec.order == ["a"]
    monkeypatch.setattr(get_settings(), "ai_queue_concurrency", 2)
    rec.start(user(3), "c")
    await settle()
    assert rec.order == ["a", "b"]  # the oldest waiting ticket took the new slot


async def test_lanes_have_independent_capacity(rec, capacity):
    capacity(1, tts=1)
    rec.start(user(1), "llm")
    rec.start(user(2), "llm2")
    rec.start(user(1), "tts", feature="tts")
    await settle()
    assert rec.order == ["llm", "tts"] and rec.tickets["tts"].lane == "tts"
    assert lane("tts")["running"] == 1 and lane("llm")["queued"] == 1


# ------------------------------------------------------------------ cancellation
@pytest.mark.parametrize("filters", [
    {"user_id": 1}, {"page_id": "page-0001-tab"}, {"session_hash": "sess-1"}, "ticket",
])
async def test_cancel_queued_ticket_by_each_id_filter(rec, capacity, filters):
    capacity(1)
    rec.start(None, "holder", feature="security_triage")
    await settle()
    waiter = rec.start(user(1), "u1")
    other = rec.start(user(2), "u2")
    await settle()
    if filters == "ticket":
        ticket = next(t for t in scheduler._tickets.values() if t.user_id == 1)
        filters = {"ticket_id": ticket.id}
    assert scheduler.cancel(reason="page_closed", **filters) == 1
    with pytest.raises(AICancelled) as err:
        await waiter
    assert err.value.reason == "page_closed"
    assert not other.done() and lane()["queued"] == 1
    snap = scheduler.snapshot()
    assert snap["counters"]["cancelled"] == {"page_closed": 1} and snap["saved"]["cancelled_queued"] == 1


async def test_cancel_running_ticket_cancels_its_task(capacity):
    capacity(2)
    t = await scheduler.acquire(user(1), "assistant")
    work = asyncio.create_task(asyncio.sleep(30))
    scheduler.attach(t, work)
    assert scheduler.cancel(user_id=1, reason="left") == 1
    with pytest.raises(asyncio.CancelledError) as err:
        await work
    assert err.value.args == ("left",) and t.cancel_reason == "left"
    assert scheduler.cancel(user_id=1, reason="left") == 0  # already being stopped
    scheduler.release(t, outcome="cancelled")
    snap = scheduler.snapshot()
    assert snap["counters"]["cancelled"] == {"left": 1} and snap["saved"]["cancelled_running"] == 1
    assert lane()["running"] == 0


async def test_cancel_running_ticket_without_task_flags_it(capacity):
    """Streams have no child task: they watch ticket.cancel_reason between chunks."""
    t = await scheduler.acquire(user(1), "plans")
    assert scheduler.cancel(page_id="page-0001-tab", reason="page_closed") == 1
    assert t.cancel_reason == "page_closed" and t.state == "running"
    scheduler.release(t, outcome="cancelled")


async def test_attach_after_cancel_cancels_immediately(capacity):
    t = await scheduler.acquire(user(1), "assistant")
    scheduler.cancel(ticket_id=t.id, reason="client_left")
    work = asyncio.create_task(asyncio.sleep(30))
    scheduler.attach(t, work)
    with pytest.raises(asyncio.CancelledError):
        await work
    scheduler.release(t, outcome="cancelled")


async def test_cancel_filters_are_anded_and_respect_kinds_and_policies(rec, capacity):
    capacity(1)
    rec.start(None, "holder", feature="security_triage")
    await settle()
    rec.start(user(1), "ask")  # cancel policy
    rec.start(user(1), "coach", feature="coach")  # pause policy
    rec.start(user(1), "ocr", feature="ocr")  # keep: an obligation
    rec.start(user(1), "safety", feature="health_metrics")  # keep: a safety flow
    rec.start(job(1, exit_policy="pause"), "job-pause", feature="health_ac")
    rec.start(job(1, exit_policy="keep"), "job-keep", feature="payroll_explain")
    await settle()
    tickets = {label: t for t in scheduler._tickets.values() for label in [_label(t)] if label}
    assert tickets["job-pause"].exit_policy == "pause" and tickets["job-keep"].exit_policy == "keep"
    assert tickets["coach"].exit_policy == "pause" and tickets["ocr"].exit_policy == "keep"

    assert scheduler.cancel(user_id=2, reason="left") == 0  # another user
    assert scheduler.cancel(user_id=1, page_id="elsewhere", reason="left") == 0  # ANDed
    # the user left: their requests stop, but never 'keep' work and never their jobs
    assert scheduler.cancel(user_id=1, kinds={"user"}, reason="left") == 2
    await settle()
    assert rec.tasks["ask"].done() and rec.tasks["coach"].done()
    for label in ("ocr", "safety", "job-pause", "job-keep"):
        assert not rec.tasks[label].done(), label
    # pause-policy jobs of the user
    assert scheduler.cancel(user_id=1, kinds={"job"}, policies={"pause"}, reason="left") == 1
    await settle()
    assert rec.tasks["job-pause"].done() and not rec.tasks["job-keep"].done()
    # account deletion: everything, whatever its policy
    assert scheduler.cancel(user_id=1, policies={"keep", "pause", "cancel"}, reason="deleted") == 3
    results = await asyncio.gather(*(rec.tasks[k] for k in ("ocr", "safety", "job-keep")), return_exceptions=True)
    assert all(isinstance(r, AICancelled) and r.reason == "deleted" for r in results)
    assert lane()["queued"] == 0
    assert scheduler.snapshot()["counters"]["cancelled"] == {"left": 3, "deleted": 3}


def _label(t: Ticket) -> str | None:
    labels = {("user", "assistant"): "ask", ("user", "coach"): "coach", ("user", "ocr"): "ocr",
              ("user", "health_metrics"): "safety", ("job", "health_ac"): "job-pause",
              ("job", "payroll_explain"): "job-keep"}
    return labels.get((t.kind, t.feature))


async def test_cancel_requires_an_id_filter():
    with pytest.raises(ValueError):
        scheduler.cancel(reason="left")
    with pytest.raises(ValueError):
        scheduler.cancel(reason="left", kinds={"user"}, policies={"cancel"})


async def test_cancel_and_deadline_race_reports_the_cancel(rec, capacity):
    """cancel() and the wait limit fire in the same loop iteration (cancel first): the waiter reports the
    cancellation, not 'busy', and the ticket is counted once."""
    capacity(1)
    rec.start(None, "holder", feature="security_triage")
    await settle()
    task = asyncio.create_task(scheduler.acquire(user(1), "assistant", max_wait=0.1))
    await settle()
    asyncio.get_running_loop().call_later(0.05, lambda: scheduler.cancel(user_id=1, reason="logout"))
    time.sleep(0.2)  # block the loop so both become due together
    with pytest.raises(AICancelled) as err:
        await task
    assert err.value.reason == "logout"
    assert lane()["queued"] == 0
    counters = scheduler.snapshot()["counters"]
    assert counters["cancelled"] == {"logout": 1} and counters["expired"] == 0


async def test_cancel_before_the_waiter_runs_reports_the_cancel(rec, capacity):
    capacity(1)
    rec.start(None, "holder", feature="security_triage")
    await settle()
    task = asyncio.create_task(scheduler.acquire(user(1), "assistant", max_wait=0.05))
    await settle()
    time.sleep(0.1)  # the deadline is due, but the cancel below runs first
    scheduler.cancel(user_id=1, reason="logout")
    with pytest.raises(AICancelled):
        await task
    assert scheduler.snapshot()["counters"]["expired"] == 0


# ------------------------------------------------------------------ WebSocket queue events
async def test_queue_events_for_waiting_user_tickets(rec, capacity, events, monkeypatch):
    capacity(1)
    monkeypatch.setattr(sched_mod, "REFRESH_EVERY", 0.2)
    rec.start(None, "holder", feature="security_triage")
    await settle()
    rec.start(user(5, page="page-five-1234"), "u5")
    rec.start(user(6, page="page-six-12345"), "u6")
    await asyncio.sleep(0.5)
    assert events == []  # nothing before ~0.8 s
    await asyncio.sleep(0.6)
    queued = {e["ticket"]: (uids, e) for uids, e in events if e["state"] == "queued"}
    assert len(queued) == 2
    t5 = next(t for t in scheduler._tickets.values() if t.user_id == 5)
    t6 = next(t for t in scheduler._tickets.values() if t.user_id == 6)
    uids, e5 = queued[t5.id]
    assert uids == [5] and e5 == {"type": "ai.queue", "page_id": "page-five-1234", "ticket": t5.id,
                                  "feature": "assistant", "state": "queued", "position": 1, "eta": e5["eta"]}
    assert e5["eta"] >= 1 and queued[t6.id][1]["position"] == 2

    events.clear()
    scheduler.cancel(user_id=5, reason="page_closed")
    await asyncio.sleep(0.45)
    states = [(u, e["state"], e["position"]) for u, e in events]
    assert ([5], "cancelled", 0) in states and ([6], "queued", 1) in states  # u6 moved up
    assert next(e for u, e in events if e["state"] == "cancelled")["reason"] == "page_closed"

    events.clear()
    rec.release("holder")
    await settle()
    assert [(u, e["state"]) for u, e in events] == [([6], "running")]
    rec.release("u6")
    assert [(u, e["state"]) for u, e in events][-1] == ([6], "done")
    with pytest.raises(AICancelled):
        await rec.tasks["u5"]


async def test_no_queue_events_for_short_waits_jobs_or_system(rec, capacity, events):
    capacity(1)
    rec.start(None, "holder", feature="security_triage")
    await settle()
    rec.start(user(1), "quick")
    rec.start(job(2), "job")
    rec.start(None, "sys")
    await asyncio.sleep(0.3)
    rec.release("holder")  # 'quick' starts after 0.3 s: never announced
    await settle()
    await asyncio.sleep(0.7)
    assert all(e["ticket"] != rec.tickets["quick"].id for _, e in events)
    rec.release("quick")
    await asyncio.sleep(0.2)
    assert events == []  # jobs/system tickets never publish ai.queue events


async def test_expired_ticket_publishes_expired(rec, capacity, events, monkeypatch):
    capacity(1)
    monkeypatch.setattr(sched_mod, "ANNOUNCE_AFTER", 0.05)
    rec.start(None, "holder", feature="security_triage")
    await settle()
    with pytest.raises(AIRejected):
        await scheduler.acquire(user(4), "assistant", max_wait=0.3)
    assert [e["state"] for _, e in events] == ["queued", "expired"]


async def test_lost_queue_events_never_break_the_queue(rec, capacity, monkeypatch):
    capacity(1)
    monkeypatch.setattr(sched_mod, "ANNOUNCE_AFTER", 0.01)

    def broken(user_ids, evt):
        raise RuntimeError("hub down")

    monkeypatch.setattr(hub, "publish_nowait", broken)
    rec.start(None, "holder", feature="security_triage")
    await settle()
    rec.start(user(1), "u1")
    await asyncio.sleep(0.1)
    rec.release("holder")
    await settle()
    assert rec.order == ["holder", "u1"]


# ------------------------------------------------------------------ owners
async def test_ticket_fields_follow_owner_and_policy():
    t = await scheduler.acquire(user(1), "coach")
    assert (t.kind, t.user_id, t.page_id, t.session_hash, t.priority, t.lane, t.exit_policy) == (
        "user", 1, "page-0001-tab", "sess-1", policy.BACKGROUND, "llm", "pause")
    j = await scheduler.acquire(job(2, exit_policy="cancel", job_id=55), "payroll_explain")
    assert (j.kind, j.user_id, j.job_id, j.exit_policy) == ("job", 2, 55, "cancel")  # the job's own policy wins
    j2 = await scheduler.acquire(job(None), "payroll_explain")
    assert j2.exit_policy == "keep" and j2.user_id is None  # no job policy: the feature's
    s = await scheduler.acquire(None, "security_triage")
    assert (s.kind, s.exit_policy, s.queue_ms) == ("system", "keep", 0)
    for ticket in (t, j, j2, s):
        scheduler.release(ticket, outcome="done")
    assert scheduler.snapshot()["counters"]["completed"] == 4


async def test_safety_tickets_skip_the_per_user_running_cap(rec, capacity):
    capacity(4)
    rec.start(user(1), "a1")
    rec.start(user(1), "a2")
    rec.start(user(1), "a3")
    await settle()
    rec.start(user(1), "safety", feature="health_metrics")
    await settle()
    assert "safety" in rec.order and "a3" not in rec.order


# ------------------------------------------------------------------ snapshot, counters, reset
async def test_snapshot_and_counters(rec, capacity, monkeypatch):
    capacity(1)
    rec.start(None, "holder", feature="security_triage")
    await settle()
    with pytest.raises(AIRejected):  # expires at once: the only slot is taken
        await scheduler.acquire(user(3), "assistant", max_wait=0)
    rec.start(user(1), "a1")
    rec.start(user(1), "a2", feature="gear")
    rec.start(user(2), "b1", feature="live_coach")
    await asyncio.sleep(0.02)
    snap = scheduler.snapshot()
    assert snap["lanes"]["llm"]["capacity"] == 1 and snap["lanes"]["llm"]["running"] == 1
    assert snap["lanes"]["llm"]["queued"] == 3 and snap["lanes"]["llm"]["oldest_wait_ms"] >= 10
    assert snap["lanes"]["tts"]["capacity"] == get_settings().ai_tts_concurrency
    assert snap["by_priority"]["background"] == {"running": 1, "queued": 1}
    assert snap["by_priority"]["interactive"]["queued"] == 1 and snap["by_priority"]["realtime"]["queued"] == 1
    assert [(u["user_id"], u["queued"]) for u in snap["users"]] == [(1, 2), (2, 1)]

    rec.release("holder")
    await settle()
    rec.release("b1", outcome="failed")
    await settle()
    assert scheduler.cancel(user_id=1, reason="left") == 2  # a1 running (flagged), a2 queued (dropped)
    await asyncio.gather(rec.tasks["a2"], return_exceptions=True)
    rec.release("a1", outcome="cancelled")

    snap = scheduler.snapshot()
    c = snap["counters"]
    assert (c["submitted"], c["started"], c["completed"], c["failed"], c["expired"]) == (5, 3, 1, 1, 1)
    assert c["rejected"] == {}  # an expired wait is not an admission refusal
    assert c["cancelled"] == {"left": 2} and snap["saved"] == {"cancelled_running": 1, "cancelled_queued": 1}
    assert snap["avg_wait_ms"] >= 10
    assert snap["lanes"]["llm"]["running"] == 0 and snap["lanes"]["llm"]["queued"] == 0
    assert snap["lanes"]["llm"]["avg_service_ms"] is not None  # one completed call measured


async def test_max_wait_zero_expires_immediately_when_queued(capacity):
    capacity(1)
    holder = await scheduler.acquire(None, "security_triage")
    with pytest.raises(AIRejected):
        await scheduler.acquire(user(1), "assistant", max_wait=0)
    assert scheduler.snapshot()["counters"]["expired"] == 1
    scheduler.release(holder, outcome="done")
    t = await scheduler.acquire(user(1), "assistant", max_wait=0)  # a free slot needs no waiting
    scheduler.release(t, outcome="done")


async def test_reset_cancels_waiters_and_running_work(capacity):
    capacity(1)
    holder = await scheduler.acquire(user(1), "assistant")
    work = asyncio.create_task(asyncio.sleep(30))
    scheduler.attach(holder, work)
    waiter = asyncio.create_task(scheduler.acquire(user(2), "assistant"))
    await settle()
    scheduler.reset()
    results = await asyncio.gather(work, waiter, return_exceptions=True)
    assert all(isinstance(r, asyncio.CancelledError) for r in results)
    snap = scheduler.snapshot()
    assert snap["lanes"]["llm"]["running"] == 0 and snap["lanes"]["llm"]["queued"] == 0
    assert snap["counters"]["submitted"] == 0 and snap["counters"]["cancelled"] == {}
    scheduler.release(holder, outcome="done")  # a ticket from before the reset: no-op
    assert scheduler.snapshot()["counters"]["completed"] == 0
    fresh = await scheduler.acquire(user(3), "assistant")  # the queue works normally afterwards
    assert fresh.state == "running"
    scheduler.release(fresh, outcome="done")


async def test_reset_is_registered_with_the_state_registry():
    from app.core.state import _resets

    assert any(fn == scheduler.reset for fn in _resets.values())


async def test_queue_ms_and_unknown_outcome(capacity):
    capacity(1)
    holder = await scheduler.acquire(None, "security_triage")
    task = asyncio.create_task(scheduler.acquire(user(1), "assistant"))
    await asyncio.sleep(0.05)
    scheduler.release(holder, outcome="weird")  # unknown outcomes count as failed
    t = await task
    assert t.queue_ms >= 40
    assert scheduler.snapshot()["counters"]["failed"] == 1
    scheduler.release(t, outcome="done")
