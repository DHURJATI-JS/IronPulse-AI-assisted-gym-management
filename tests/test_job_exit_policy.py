"""Background jobs and exit policies (docs/ai-guard-and-queue.md section 6): requester + policy on enqueue, pause /
cancel / keep for queued and running jobs (attempts given back), resume, fenced final writes (a late DONE or retry
never overwrites CANCELLED/PAUSED), concurrent execution, claimed-then-paused jobs skipped, away requesters parked,
the watchdog (heartbeat + cross-process stop), shutdown re-queueing, race-free dedupe inserts, the job's AI owner,
account deletion, and how a run's outcome is announced (``set_result_event`` / ``set_failure_event``: the runner's
own 'done' and last 'failed' ``task.update`` carry what the handler set; ``current_run()``).

"Another process" is simulated by changing the job row directly in the database, exactly what a second worker's
``apply_exit_policy`` / claim would do.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, update

from app import db as dbmod
from app.ai.owner import OWNER_JOB, current_owner
from app.config import get_settings
from app.core.utils import utcnow
from app.models import Job
from app.models.enums import JobStatus
from app.services import jobs as J
from app.services.presence import presence
from app.services.realtime import hub
from tests.conftest import run_jobs
from tests.factories import make_customer

QUEUED, RUNNING, DONE, FAILED = JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.DONE, JobStatus.FAILED
PAUSED, CANCELLED = JobStatus.PAUSED, JobStatus.CANCELLED


# ============================================================================================ helpers / fixtures
@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    async def none() -> None:
        return None

    monkeypatch.setattr(presence, "_redis", none)


@pytest.fixture
def registry():
    """Register test handlers; the previous registrations are restored afterwards."""
    saved: dict[str, tuple[Any, Any]] = {}

    def register(kind: str, fn: Callable[..., Any], *, on_exit: str | None = None) -> Callable[..., Any]:
        saved.setdefault(kind, (J._handlers.get(kind), J._exit_policies.get(kind)))
        J.job_handler(kind, on_exit=on_exit)(fn)
        return fn

    yield register
    for kind, (handler, policy) in saved.items():
        if handler is None:
            J._handlers.pop(kind, None)
        else:
            J._handlers[kind] = handler
        if policy is None:
            J._exit_policies.pop(kind, None)
        else:
            J._exit_policies[kind] = policy


@pytest.fixture
def concurrency(monkeypatch):
    def set_to(n: int) -> None:
        monkeypatch.setattr(get_settings(), "jobs_concurrency", n)

    return set_to


class Gate:
    """A handler that blocks until released and records what happened to it."""

    def __init__(self, *, fail_with: Exception | None = None) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.fail_with = fail_with
        self.calls = 0
        self.cancelled = False
        self.finished = False
        self.owner: Any = None

    async def __call__(self, db: Any, payload: dict[str, Any]) -> None:
        self.calls += 1
        self.owner = current_owner()
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.fail_with is not None:
            raise self.fail_with
        self.finished = True


class Counter:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, db: Any, payload: dict[str, Any]) -> None:
        self.calls.append(payload)


async def add(kind: str, *, user_id: int | None = None, policy: str | None = None, key: str | None = None,
              payload: dict[str, Any] | None = None, max_attempts: int = 5) -> int:
    async with dbmod.get_sessionmaker()() as s:
        job = await J.enqueue(s, kind, payload or {"n": 1}, user_id=user_id, exit_policy=policy,
                              dedupe_key=key or f"{kind}:{time.monotonic_ns()}", max_attempts=max_attempts)
        await s.commit()
        return job.id


async def row(job_id: int) -> Job:
    async with dbmod.get_sessionmaker()() as s:
        return await s.get(Job, job_id)


async def set_row(job_id: int, **values: Any) -> None:
    """What another worker process would write."""
    async with dbmod.get_sessionmaker()() as s:
        await s.execute(update(Job).where(Job.id == job_id).values(**values))
        await s.commit()


async def wait(event: asyncio.Event, timeout: float = 5.0) -> None:
    await asyncio.wait_for(event.wait(), timeout)


def start_runner() -> asyncio.Task:
    return asyncio.create_task(J.run_due_jobs(limit=10))


# ============================================================================================ enqueue / policies
async def test_enqueue_records_the_requester_and_exit_policy(db, registry):
    user = await make_customer(db)
    registry("t_pause", Counter(), on_exit="pause")
    paused_kind = await add("t_pause", user_id=user.id)
    unknown = await add("t_unregistered", user_id=user.id)
    explicit = await add("t_pause", user_id=user.id, policy="cancel")
    system = await add("t_pause")
    assert ((await row(paused_kind)).user_id, (await row(paused_kind)).exit_policy) == (user.id, "pause")
    assert (await row(unknown)).exit_policy == "keep"
    assert (await row(explicit)).exit_policy == "cancel"
    assert (await row(system)).user_id is None and (await row(system)).exit_policy == "pause"
    with pytest.raises(ValueError):
        await J.enqueue(db, "t_pause", {}, exit_policy="explode")
    with pytest.raises(ValueError):
        J.job_handler("t_bad", on_exit="sometimes")


async def test_documented_kinds_get_their_exit_policy_even_without_on_exit(registry):
    assert J.exit_policy_for("health_ai_note") == "pause"  # registered by app/services/health.py without on_exit
    for kind in ("coach_plan", "health_ac_note"):
        assert J.exit_policy_for(kind) == "pause"
    for kind in ("payroll_explain", "verify_identity", "verify_branch", "verify_payment_proof", "staff_activation_check"):
        assert J.exit_policy_for(kind) == "keep"
    assert J.exit_policy_for("anything_else") == "keep"
    registry("coach_plan", Counter())  # registering without on_exit keeps the documented policy ...
    assert J.exit_policy_for("coach_plan") == "pause"
    registry("coach_plan", Counter(), on_exit="cancel")  # ... an explicit one wins
    assert J.exit_policy_for("coach_plan") == "cancel"


async def test_enqueue_without_a_user_still_runs_as_before(db, registry):
    seen = Counter()
    registry("t_plain", seen)
    await J.enqueue(db, "t_plain", {"x": 1}, dedupe_key="plain:1")
    await J.enqueue(db, "t_plain", {"x": 2}, dedupe_key="plain:1")  # deduplicated (queued = in flight)
    await J.enqueue(db, "t_plain", {"x": 3})  # no key
    await db.commit()
    assert await run_jobs() == 2 and sorted(p["x"] for p in seen.calls) == [1, 3]
    assert await db.scalar(select(func.count(Job.id)).where(Job.status == DONE)) == 2


async def test_concurrent_first_enqueue_of_the_same_key_never_collides(db):
    inserted = asyncio.Event()

    async def first() -> int:
        async with dbmod.get_sessionmaker()() as s:
            job = await J.enqueue(s, "t_race", {"who": "first"}, dedupe_key="race:1")
            inserted.set()
            await asyncio.sleep(0.3)  # keep the transaction open while the second request inserts the same key
            await s.commit()
            return job.id

    async def second() -> int:
        await inserted.wait()
        async with dbmod.get_sessionmaker()() as s:
            job = await J.enqueue(s, "t_race", {"who": "second"}, dedupe_key="race:1")  # waits for the first COMMIT
            await s.commit()
            return job.id

    a, b = await asyncio.wait_for(asyncio.gather(first(), second()), 10)
    assert a == b
    assert await db.scalar(select(func.count(Job.id)).where(Job.dedupe_key == "race:1")) == 1
    assert (await row(a)).payload == {"who": "first"}


async def test_paused_dedupe_key_stays_in_flight(db, registry):
    user = await make_customer(db)
    registry("t_note", Counter(), on_exit="pause")
    job_id = await add("t_note", user_id=user.id, key="note:1", payload={"v": 1})
    await set_row(job_id, status=PAUSED, attempts=1, paused_at=utcnow(), cancel_reason="left")
    await J.enqueue(db, "t_note", {"v": 2}, dedupe_key="note:1")  # system re-enqueue: stays paused
    await db.commit()
    r = await row(job_id)
    assert (r.status, r.attempts, r.payload) == (PAUSED, 1, {"v": 1})
    await J.enqueue(db, "t_note", {"v": 3}, dedupe_key="note:1", user_id=user.id)  # the (present) user asks again
    await db.commit()
    r = await row(job_id)
    assert (r.status, r.attempts, r.payload, r.paused_at, r.cancel_reason) == (QUEUED, 1, {"v": 1}, None, None)
    # a finished row is recycled as before
    await set_row(job_id, status=DONE, attempts=2)
    await J.enqueue(db, "t_note", {"v": 4}, dedupe_key="note:1", user_id=user.id)
    await db.commit()
    r = await row(job_id)
    assert (r.status, r.attempts, r.payload) == (QUEUED, 0, {"v": 4})


# ============================================================================================ queued jobs
async def test_exit_policy_for_queued_jobs_and_resume(db, registry):
    user = await make_customer(db)
    other = await make_customer(db)
    seen = Counter()
    for kind, policy in (("t_p", "pause"), ("t_c", "cancel"), ("t_k", "keep")):
        registry(kind, seen, on_exit=policy)
    paused, cancelled, kept = (await add("t_p", user_id=user.id), await add("t_c", user_id=user.id),
                               await add("t_k", user_id=user.id))
    others = await add("t_p", user_id=other.id)
    out = await J.apply_exit_policy(user.id, reason="left")
    assert out == {"paused": 1, "cancelled": 1, "running_stopped": 0}
    p, c, k = await row(paused), await row(cancelled), await row(kept)
    assert (p.status, p.attempts, p.cancel_reason) == (PAUSED, 0, "left") and p.paused_at is not None
    assert (c.status, c.cancel_reason) == (CANCELLED, "left") and c.finished_at is not None
    assert k.status == QUEUED and (await row(others)).status == QUEUED
    assert await J.apply_exit_policy(user.id, reason="left") == {"paused": 0, "cancelled": 0, "running_stopped": 0}

    assert await J.resume_user_jobs(user.id) == 1
    p = await row(paused)
    assert (p.status, p.paused_at, p.cancel_reason) == (QUEUED, None, None)
    assert await run_jobs() == 3  # resumed + kept + the other user's
    assert (await row(paused)).status == DONE and (await row(cancelled)).status == CANCELLED


async def test_cancel_user_jobs_respects_policies_unless_told_otherwise(db):
    user = await make_customer(db)
    keep = await add("t_k2", user_id=user.id, policy="keep")
    pause = await add("t_p2", user_id=user.id, policy="pause")
    cancel = await add("t_c2", user_id=user.id, policy="cancel")
    parked = await add("t_p2", user_id=user.id, policy="pause")
    await set_row(parked, status=PAUSED)
    assert await J.cancel_user_jobs(user.id, reason="admin") == 3
    assert (await row(keep)).status == QUEUED
    assert {(await row(i)).status for i in (pause, cancel, parked)} == {CANCELLED}
    assert await J.cancel_user_jobs(user.id, reason="account_deleted", all_policies=True) == 1
    assert (await row(keep)).status == CANCELLED and (await row(keep)).cancel_reason == "account_deleted"


# ============================================================================================ running jobs
@pytest.mark.parametrize("policy,status", [("pause", PAUSED), ("cancel", CANCELLED)])
async def test_running_job_stopped_mid_handler_gives_its_attempt_back(db, registry, policy, status):
    user = await make_customer(db)
    gate = Gate()
    registry("t_live", gate, on_exit=policy)
    job_id = await add("t_live", user_id=user.id)
    runner = start_runner()
    await wait(gate.started)
    r = await row(job_id)
    assert (r.status, r.attempts) == (RUNNING, 1) and J.running_jobs().keys() == {job_id}

    out = await J.apply_exit_policy(user.id, reason="left")
    assert out[{"pause": "paused", "cancel": "cancelled"}[policy]] == 1 and out["running_stopped"] == 1
    assert gate.cancelled and not gate.finished
    r = await row(job_id)
    assert (r.status, r.attempts, r.cancel_reason, r.locked_at) == (status, 0, "left", None)
    assert (r.paused_at is not None) == (policy == "pause")
    assert await asyncio.wait_for(runner, 5) == 1 and J.running_jobs() == {}

    if policy == "pause":  # they come back: the job runs from scratch, uncharged for the interrupted run
        assert await J.resume_user_jobs(user.id) == 1
        gate.release.set()
        assert await run_jobs() == 1
        r = await row(job_id)
        assert (r.status, r.attempts) == (DONE, 1) and gate.finished


async def test_keep_job_finishes_when_its_requester_leaves(db, registry):
    user = await make_customer(db)
    gate = Gate()
    registry("t_obligation", gate, on_exit="keep")
    job_id = await add("t_obligation", user_id=user.id)
    runner = start_runner()
    await wait(gate.started)
    assert await J.apply_exit_policy(user.id) == {"paused": 0, "cancelled": 0, "running_stopped": 0}
    gate.release.set()
    assert await asyncio.wait_for(runner, 5) == 1
    assert (await row(job_id)).status == DONE and not gate.cancelled


async def test_a_late_done_never_overwrites_a_cancelled_row(db, registry):
    gate = Gate()
    registry("t_late", gate)
    job_id = await add("t_late")
    runner = start_runner()
    await wait(gate.started)
    await set_row(job_id, status=CANCELLED, cancel_reason="admin", locked_at=None)  # another process cancelled it
    gate.release.set()
    assert await asyncio.wait_for(runner, 5) == 1 and gate.finished
    r = await row(job_id)
    assert (r.status, r.cancel_reason) == (CANCELLED, "admin")


async def test_a_late_failure_never_requeues_a_paused_row(db, registry):
    gate = Gate(fail_with=RuntimeError("provider exploded"))
    registry("t_late_fail", gate)
    job_id = await add("t_late_fail")
    runner = start_runner()
    await wait(gate.started)
    await set_row(job_id, status=PAUSED, locked_at=None, cancel_reason="left")
    gate.release.set()
    assert await asyncio.wait_for(runner, 5) == 1
    r = await row(job_id)
    assert (r.status, r.last_error) == (PAUSED, None)


async def test_failures_still_retry_with_back_off(db, registry):
    async def boom(db: Any, payload: dict[str, Any]) -> None:
        raise ValueError("bad input")

    registry("t_boom", boom)
    retrying = await add("t_boom")
    final = await add("t_boom", max_attempts=1)
    assert await run_jobs() == 2
    r = await row(retrying)
    assert (r.status, r.attempts, r.locked_at) == (QUEUED, 1, None) and "ValueError: bad input" in r.last_error
    assert r.run_after > utcnow() + timedelta(seconds=20)
    r = await row(final)
    assert (r.status, r.attempts) == (FAILED, 1) and r.finished_at is not None


# ============================================================================================ concurrency / claim
async def test_two_jobs_run_at_once(db, registry, concurrency):
    concurrency(2)
    both = asyncio.Event()
    running = 0
    peak = 0

    async def handler(db: Any, payload: dict[str, Any]) -> None:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        if running == 2:
            both.set()
        await asyncio.wait_for(both.wait(), 5)  # would time out if the jobs ran one after another
        running -= 1

    registry("t_pair", handler)
    first, second = await add("t_pair"), await add("t_pair")
    assert await asyncio.wait_for(J.run_due_jobs(10), 10) == 2
    assert peak == 2 and {(await row(first)).status, (await row(second)).status} == {DONE}


async def test_concurrency_limit_is_respected(db, registry, concurrency):
    concurrency(1)
    running = 0
    peak = 0

    async def handler(db: Any, payload: dict[str, Any]) -> None:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1

    registry("t_single", handler)
    for _ in range(3):
        await add("t_single")
    assert await run_jobs() == 3 and peak == 1


async def test_claimed_job_paused_meanwhile_is_skipped(db, registry, concurrency):
    concurrency(1)
    gate, seen = Gate(), Counter()
    registry("t_first", gate)
    registry("t_second", seen)
    await add("t_first")
    waiting = await add("t_second")
    runner = start_runner()  # claims both; the second waits for the only slot
    await wait(gate.started)
    assert (await row(waiting)).status == RUNNING
    await set_row(waiting, status=PAUSED, locked_at=None, cancel_reason="left", attempts=0)  # another process
    gate.release.set()
    assert await asyncio.wait_for(runner, 5) == 2
    assert seen.calls == []
    r = await row(waiting)
    assert (r.status, r.attempts, r.cancel_reason) == (PAUSED, 0, "left")


async def test_job_of_an_away_requester_is_parked_instead_of_run(db, registry):
    user = await make_customer(db)
    seen = Counter()
    for kind, policy in (("t_ap", "pause"), ("t_ac", "cancel"), ("t_ak", "keep")):
        registry(kind, seen, on_exit=policy)
    await presence.on_left(user.id)  # they left (nothing of theirs was queued yet)
    assert presence.is_away(user.id)
    paused, cancelled, kept = (await add("t_ap", user_id=user.id), await add("t_ac", user_id=user.id),
                               await add("t_ak", user_id=user.id))  # enqueued afterwards (e.g. a slow request)
    assert await run_jobs() == 3
    assert len(seen.calls) == 1  # only the obligation ran
    p, c = await row(paused), await row(cancelled)
    assert (p.status, p.attempts, p.cancel_reason) == (PAUSED, 0, "away")
    assert (c.status, c.attempts, c.cancel_reason) == (CANCELLED, 0, "away")
    assert (await row(kept)).status == DONE
    presence.touch(user.id)  # back: the parked job is resumed and runs
    await presence.drain()
    assert await run_jobs() == 1 and len(seen.calls) == 2 and (await row(paused)).status == DONE


async def test_owner_context_is_visible_inside_a_handler(db, registry):
    user = await make_customer(db)
    gate = Gate()
    gate.release.set()
    registry("t_owned", gate, on_exit="pause")
    job_id = await add("t_owned", user_id=user.id)
    assert await run_jobs() == 1
    owner = gate.owner
    assert (owner.kind, owner.user_id, owner.job_id, owner.exit_policy) == (OWNER_JOB, user.id, job_id, "pause")
    assert not owner.is_user_request and current_owner() is None  # never leaks out of the job

    system = Gate()
    system.release.set()
    registry("t_system", system)
    await add("t_system")
    await run_jobs()
    assert (system.owner.kind, system.owner.user_id, system.owner.exit_policy) == (OWNER_JOB, None, "keep")


# ============================================================================================ watchdog
async def test_watchdog_heartbeats_running_jobs(db, registry):
    gate = Gate()
    registry("t_long", gate)
    job_id = await add("t_long")
    runner = start_runner()
    await wait(gate.started)
    before = (await row(job_id)).locked_at
    await asyncio.sleep(0.01)
    assert await J.watchdog_tick() == {"held": 1, "lost": 0}
    r = await row(job_id)
    assert r.status == RUNNING and r.locked_at > before  # never mistaken for a crashed process's job
    gate.release.set()
    assert await asyncio.wait_for(runner, 5) == 1
    assert (await row(job_id)).status == DONE  # the fencing token followed the heartbeat


async def test_watchdog_stops_a_job_paused_by_another_process(db, registry):
    user = await make_customer(db)
    gate = Gate()
    registry("t_remote", gate, on_exit="pause")
    job_id = await add("t_remote", user_id=user.id)
    runner = start_runner()
    await wait(gate.started)
    await set_row(job_id, status=PAUSED, attempts=0, locked_at=None, paused_at=utcnow(), cancel_reason="left")
    assert await J.watchdog_tick() == {"held": 0, "lost": 1}
    assert await asyncio.wait_for(runner, 5) == 1 and gate.cancelled
    r = await row(job_id)
    assert (r.status, r.attempts, r.cancel_reason) == (PAUSED, 0, "left")  # that process decided; nothing rewritten
    assert await J.watchdog_tick() == {"held": 0, "lost": 0}


async def test_watchdog_stops_a_job_reclaimed_elsewhere(db, registry):
    gate = Gate()
    registry("t_reclaimed", gate)
    job_id = await add("t_reclaimed")
    runner = start_runner()
    await wait(gate.started)
    theirs = utcnow() + timedelta(seconds=1)
    await set_row(job_id, locked_at=theirs, attempts=2)  # another worker claimed the row
    assert await J.watchdog_tick() == {"held": 0, "lost": 1}
    assert await asyncio.wait_for(runner, 5) == 1 and gate.cancelled
    r = await row(job_id)
    assert (r.status, r.locked_at, r.attempts) == (RUNNING, theirs, 2)


# ============================================================================================ shutdown / cancellation
async def test_shutdown_puts_in_flight_jobs_back_in_the_queue(db, registry, concurrency):
    concurrency(1)
    gate, seen = Gate(), Counter()
    registry("t_inflight", gate)
    registry("t_after", seen)
    running = await add("t_inflight")
    waiting = await add("t_after")
    runner = start_runner()
    await wait(gate.started)
    assert await J.shutdown_jobs() == 2
    assert await asyncio.wait_for(runner, 5) == 2 and gate.cancelled and seen.calls == []
    for job_id in (running, waiting):
        r = await row(job_id)
        assert (r.status, r.attempts, r.locked_at) == (QUEUED, 0, None), job_id
    assert await J.run_due_jobs() == 0  # nothing is claimed while shutting down
    J.accept_jobs()
    gate.release.set()
    assert await run_jobs() == 2 and (await row(running)).status == DONE and (await row(waiting)).status == DONE


async def test_cancelling_the_runner_requeues_what_it_claimed(db, registry):
    gate = Gate()
    registry("t_outer", gate)
    job_id = await add("t_outer")
    runner = start_runner()
    await wait(gate.started)
    runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(runner, 5)
    r = await row(job_id)
    assert (r.status, r.attempts, r.locked_at) == (QUEUED, 0, None) and gate.cancelled


async def test_a_job_stopped_before_its_task_ever_ran_is_still_settled(db):
    user = await make_customer(db)
    job_id = await add("t_never_started", user_id=user.id, policy="pause")
    [claim] = await J._claim(1)
    task = J._spawn(claim)
    assert J._stop(J._running[job_id], "pause", "left")  # cancelled before the event loop ever ran it
    await asyncio.wait([task])
    await asyncio.wait_for(asyncio.gather(*J._rt.finalizers), 5)
    r = await row(job_id)
    assert (r.status, r.attempts, r.cancel_reason) == (PAUSED, 0, "left") and J.running_jobs() == {}


async def test_dispatch_starts_jobs_only_into_free_slots_and_does_not_wait(db, registry, concurrency):
    concurrency(2)
    gates = [Gate() for _ in range(3)]
    ids = []
    for i, gate in enumerate(gates):
        registry(f"t_slot{i}", gate)
        ids.append(await add(f"t_slot{i}"))
    assert await J.dispatch_due_jobs(window=0.1) == 2  # returns while both jobs are still running
    await wait(gates[0].started)
    await wait(gates[1].started)
    assert (await row(ids[2])).status == QUEUED  # never claimed without a free slot
    gates[0].release.set()
    gates[2].release.set()
    assert await J.dispatch_due_jobs(window=3.0) == 1  # the freed slot is refilled at once
    await wait(gates[2].started)
    gates[1].release.set()
    for _ in range(100):
        if not J.running_jobs():
            break
        await asyncio.sleep(0.02)
    assert [(await row(i)).status for i in ids] == [DONE, DONE, DONE]


# ============================================================================================ account deletion
async def test_account_deletion_stops_a_running_job_without_it_writing_back(db, registry):
    from app.services.accounts import soft_delete_user

    user = await make_customer(db)
    gate = Gate()
    registry("t_verification", gate, on_exit="keep")
    job_id = await add("t_verification", user_id=user.id)
    runner = start_runner()
    await wait(gate.started)
    await soft_delete_user(db, user, reason="test deletion")
    assert not gate.cancelled  # nothing happens before COMMIT
    await db.commit()
    assert await asyncio.wait_for(runner, 5) == 1 and gate.cancelled
    r = await row(job_id)
    assert (r.status, r.cancel_reason) == (CANCELLED, "account_deleted")


# ============================================================================================ worker integration
async def test_worker_loops_dispatch_heartbeat_and_requeue_on_stop(db, registry):
    from app.services.worker import BackgroundRunner

    gate = Gate()
    registry("t_worker", gate)
    job_id = await add("t_worker")
    runner = BackgroundRunner()  # only the job loops (no AI health pings, no periodic tasks)
    runner._tasks.append(asyncio.create_task(runner._loop("jobs", 0.05, runner._jobs, 0.0)))
    runner._tasks.append(asyncio.create_task(runner._loop("job-watchdog", 0.05, runner._job_watchdog, 0.0)))
    await wait(gate.started)  # the jobs loop started it in its own task
    claimed_at = (await row(job_id)).locked_at
    for _ in range(100):  # the watchdog loop heartbeats it
        if (await row(job_id)).locked_at > claimed_at:
            break
        await asyncio.sleep(0.02)
    assert (await row(job_id)).locked_at > claimed_at
    await asyncio.wait_for(runner.stop(), 10)  # shutdown: back to the queue before the loops die
    assert gate.cancelled and runner._tasks == []
    r = await row(job_id)
    assert (r.status, r.attempts, r.locked_at) == (QUEUED, 0, None)


async def test_worker_starts_the_watchdog_and_presence_lease_loops(monkeypatch):
    from app.services.worker import BackgroundRunner

    names: list[str] = []

    async def fake_loop(self: Any, name: str, every: float, fn: Any, initial_delay: float = 0.0) -> None:
        names.append(name)
        await asyncio.Event().wait()  # parked until stop() cancels it

    monkeypatch.setattr(BackgroundRunner, "_loop", fake_loop)
    runner = BackgroundRunner()
    runner.start()
    await asyncio.sleep(0)
    assert {"jobs", "job-watchdog", "presence-leases"} <= set(names)
    await runner.stop()
    assert runner._tasks == []
    J.accept_jobs()


async def test_reclaiming_a_stale_job_here_stops_the_old_local_run(db, registry):
    old = Gate()
    registry("t_stale", old)
    job_id = await add("t_stale")
    first = start_runner()
    await wait(old.started)
    await set_row(job_id, locked_at=utcnow() - timedelta(minutes=20))  # no heartbeat for 20 minutes (watchdog off)
    new = Gate()
    new.release.set()
    registry("t_stale", new)
    assert await asyncio.wait_for(J.run_due_jobs(10), 5) == 1  # reclaimed: the old run here is stopped, not doubled
    assert await asyncio.wait_for(first, 5) == 1 and old.cancelled and new.finished
    r = await row(job_id)
    assert (r.status, r.attempts) == (DONE, 2) and J.running_jobs() == {}


# ============================================================================================ outcome events
def task_events(q: asyncio.Queue) -> list[dict[str, Any]]:
    out = []
    while not q.empty():
        evt = q.get_nowait()
        if isinstance(evt, dict) and evt.get("type") == "task.update":
            out.append(evt)
    return out


async def ticks(n: int = 30) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


async def test_the_handler_says_how_the_runner_announces_done(db, registry):
    user = await make_customer(db)
    seen: list[Any] = []

    async def deeper() -> None:  # any depth below the handler: the run travels in a ContextVar
        assert J.set_result_event(notify=False, message="Your plan is ready.")

    async def handler(db: Any, payload: dict[str, Any]) -> None:
        run = J.current_run()
        seen.append((run.job_id, run.kind, run.user_id, run.attempt, run.max_attempts, run.last_attempt, run.stopping))
        assert J.set_result_event(title="Plan ready", message="draft", link="/me/coach")
        await deeper()
        seen.append(len(task_events(q)))  # nothing is announced while the handler runs

    registry("t_outcome", handler, on_exit="pause")
    job_id = await add("t_outcome", user_id=user.id, max_attempts=3)
    q = hub.subscribe(user.id)
    try:
        assert await run_jobs() == 1
        await ticks()
        events = task_events(q)
    finally:
        hub.unsubscribe(user.id, q)
    assert seen == [(job_id, "t_outcome", user.id, 1, 3, False, False), 0]
    # one 'done' - the runner's, after the DONE write - carrying what the handler set (later calls win)
    assert events == [{"type": "task.update", "task_id": job_id, "kind": "t_outcome", "status": "done",
                       "progress": None, "message": "Your plan is ready.", "link": "/me/coach", "title": "Plan ready",
                       "notify": False}]
    assert (await row(job_id)).status == DONE and J.current_run() is None


async def test_obligations_stay_silent_and_a_plain_handler_keeps_the_default_done(db, registry):
    user = await make_customer(db)

    async def obligation(db: Any, payload: dict[str, Any]) -> None:
        J.set_result_event(link="/owner/payroll")

    registry("t_keep_outcome", obligation, on_exit="keep")
    registry("t_plain_outcome", Counter(), on_exit="pause")
    keep_id = await add("t_keep_outcome", user_id=user.id)
    plain_id = await add("t_plain_outcome", user_id=user.id)
    q = hub.subscribe(user.id)
    try:
        assert await run_jobs() == 2
        await ticks()
        got = {e["task_id"]: e for e in task_events(q)}
    finally:
        hub.unsubscribe(user.id, q)
    assert got[keep_id]["notify"] is False and got[keep_id]["link"] == "/owner/payroll"
    assert got[plain_id]["status"] == "done" and "notify" not in got[plain_id] and got[plain_id]["link"] is None


async def test_failure_fields_go_with_the_last_failed_event_only(db, registry):
    user = await make_customer(db)
    runs: list[tuple[int, bool]] = []

    async def flaky(db: Any, payload: dict[str, Any]) -> None:
        run = J.current_run()
        runs.append((run.attempt, run.last_attempt))
        J.set_result_event(message="never sent: this run fails")
        J.set_failure_event(notify=False, message="The wording stays as it was.")
        raise RuntimeError("provider exploded")

    registry("t_flaky", flaky, on_exit="pause")
    job_id = await add("t_flaky", user_id=user.id, max_attempts=2)
    q = hub.subscribe(user.id)
    try:
        assert await run_jobs() == 1
        await ticks()
        assert task_events(q) == []  # a retry is not news
        await set_row(job_id, run_after=utcnow())
        assert await run_jobs() == 1
        await ticks()
        events = task_events(q)
    finally:
        hub.unsubscribe(user.id, q)
    assert runs == [(1, False), (2, True)]
    assert [(e["status"], e["notify"], e["message"]) for e in events] == [
        ("failed", False, "The wording stays as it was.")]
    assert (await row(job_id)).status == FAILED


async def test_each_run_starts_with_a_clean_outcome(db, registry):
    user = await make_customer(db)

    async def second_time_lucky(db: Any, payload: dict[str, Any]) -> None:
        if J.current_run().attempt == 1:
            J.set_result_event(notify=False, message="from the failed run")
            raise RuntimeError("transient")
        J.set_result_event(link="/me/health")

    registry("t_lucky", second_time_lucky, on_exit="pause")
    job_id = await add("t_lucky", user_id=user.id, max_attempts=2)
    q = hub.subscribe(user.id)
    try:
        assert await run_jobs() == 1
        await set_row(job_id, run_after=utcnow())
        assert await run_jobs() == 1
        await ticks()
        events = task_events(q)
    finally:
        hub.unsubscribe(user.id, q)
    assert [(e["status"], e["link"], e["message"], e.get("notify")) for e in events] == [
        ("done", "/me/health", None, None)]


async def test_a_stopped_run_knows_it_is_stopping(db, registry):
    user = await make_customer(db)
    gate = Gate()
    seen: list[bool] = []

    async def handler(db: Any, payload: dict[str, Any]) -> None:
        try:
            await gate(db, payload)
        except asyncio.CancelledError:
            seen.append(J.current_run().stopping)  # paused because they left: not a failure of this attempt
            raise

    registry("t_stopping", handler, on_exit="pause")
    job_id = await add("t_stopping", user_id=user.id, max_attempts=1)
    runner = start_runner()
    await wait(gate.started)
    assert J.current_run() is None  # the run lives in the job's own task
    await J.apply_exit_policy(user.id, reason="left")
    assert await asyncio.wait_for(runner, 5) == 1
    assert seen == [True] and (await row(job_id)).status == PAUSED


async def test_outcome_fields_outside_the_runner_and_keys_the_runner_owns(db, registry):
    assert J.current_run() is None
    assert J.set_result_event(message="x") is False and J.set_failure_event(notify=False) is False
    for key in ("type", "task_id", "kind", "status"):
        with pytest.raises(ValueError):
            J.set_result_event(**{key: "x"})
        with pytest.raises(ValueError):
            J.set_failure_event(**{key: "x"})
    calls: list[Any] = []

    async def handler(db: Any, payload: dict[str, Any]) -> None:
        calls.append((J.current_run(), J.set_result_event(notify=False)))

    registry("t_inline", handler)
    await J.run_job_inline("t_inline", {})  # tests and scripts: no runner, nothing to announce
    assert calls == [(None, False)]
