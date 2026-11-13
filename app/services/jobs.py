"""Durable background jobs + periodic tasks.

Register handlers in feature modules::

    from app.services.jobs import job_handler, periodic, enqueue

    @job_handler("verify_branch")  # exit policy "keep" (the default): an obligation, always finished
    async def verify_branch(db: AsyncSession, payload: dict) -> None: ...

    @job_handler("health_ai_note", on_exit="pause")  # deferred while its requester is away
    async def ai_note(db: AsyncSession, payload: dict) -> None: ...

    @periodic("bedtime_reminders", every_seconds=60)
    async def bedtime(db: AsyncSession) -> None: ...

    # caller commits; user_id = the person who asked for it (None for system work)
    await enqueue(db, "verify_branch", {"branch_id": 7}, dedupe_key="verify_branch:7", user_id=owner.id)

Jobs are claimed with ``SELECT ... FOR UPDATE SKIP LOCKED`` (safe with several app processes) and
retried with exponential back-off. Periodic tasks take a PostgreSQL advisory lock so only one
process runs each of them at a time.

Execution and exit policies (docs/ai-guard-and-queue.md section 6)
* Every claimed job runs in its own asyncio task (``running_jobs()``, by job id), at most ``settings.jobs_concurrency``
  at a time per process, with ``AIOwner(kind="job", user_id, job_id, exit_policy)`` set so its AI calls are
  attributed to the requester.
* ``locked_at`` is the claim's fencing token. The watchdog (``watchdog_tick``, every 2 s from the worker) heartbeats
  it for every job this process holds - a long job is never mistaken for a crashed one and run twice - and stops
  local tasks whose row another process changed. Every final status write is guarded by ``status == RUNNING AND
  locked_at == token``, so a cancelled or paused row is never overwritten by a late DONE or retry.
* When its requester leaves (app/services/presence.py), ``apply_exit_policy`` pauses ("pause") or cancels ("cancel")
  their queued and running jobs; "keep" always finishes. A stopped run gives its attempt back (a resume is not
  charged). ``resume_user_jobs`` re-queues paused jobs when they return. A claimed job whose requester is away is
  parked instead of run; once its PAUSED row is written presence is asked again, so a return noticed meanwhile (its
  resume found the row still RUNNING) never leaves it paused for someone who is present.
* Shutdown puts in-flight jobs back in the queue (attempt restored) instead of leaving them RUNNING for 15 minutes.
* The requester of a job hears about it over the WebSocket (``task.update``, docs section 7): paused, cancelled,
  resumed (``status: queued, resumed: true``), done and finally failed. A handler never publishes its own final
  event: it says how the runner should announce the outcome with ``set_result_event(...)`` ('done': ``title``,
  ``message``, ``link``, ``notify=False`` for no island note) and ``set_failure_event(...)`` (the last 'failed').
  ``current_run()`` tells a handler which attempt it is.
* An overloaded or unreachable database (app.db ``db_unavailable``: pool timeout, refused or lost connection) is not the
  job's fault: a run it stops goes back to the queue for DB_RETRY_SECONDS (jittered) with its attempt given back - it
  can never use up the job's attempts - and a final status write keeps trying with back-off for WRITE_RETRY_SECONDS
  before it leaves the row RUNNING to the stale-job reclaim. One warning a minute per call site, no traceback
  (app/core/overload.py); claims are retried by the worker loop with back-off (app/services/worker.py).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import traceback
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import String, any_, case, func, literal, or_, select, text, update
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.owner import OWNER_JOB, AIOwner, owner_scope
from app.ai.policy import EXIT_CANCEL, EXIT_KEEP, EXIT_PAUSE
from app.config import get_settings
from app.core.overload import backoff_delay, warn_db_unavailable
from app.core.state import register_reset
from app.core.utils import utcnow
from app.db import db_unavailable, session_scope
from app.models import Job
from app.models.enums import JobStatus

log = logging.getLogger("ironpulse.jobs")

JobFn = Callable[[AsyncSession, dict[str, Any]], Awaitable[None]]
PeriodicFn = Callable[[AsyncSession], Awaitable[Any]]

EXIT_POLICIES = (EXIT_KEEP, EXIT_CANCEL, EXIT_PAUSE)
# Exit policies fixed by docs/ai-guard-and-queue.md section 6. They apply whenever a module registers its handler
# without ``on_exit``, so every documented kind behaves as specified even before its module passes the policy.
DEFAULT_EXIT_POLICIES: dict[str, str] = {
    "health_ai_note": EXIT_PAUSE,
    "coach_plan": EXIT_PAUSE,
    "health_ac_note": EXIT_PAUSE,
    "payroll_explain": EXIT_KEEP,
    "verify_identity": EXIT_KEEP,
    "verify_branch": EXIT_KEEP,
    "verify_payment_proof": EXIT_KEEP,
    "staff_activation_check": EXIT_KEEP,
}
STALE_AFTER = timedelta(minutes=15)  # RUNNING without a heartbeat this long: its process died -> reclaim the job
WATCHDOG_SECONDS = 2.0  # worker loop interval of watchdog_tick()
SHUTDOWN_WAIT_SECONDS = 10.0  # how long shutdown waits for in-flight jobs to be written back to the queue
EXIT_WAIT_SECONDS = 10.0  # apply_exit_policy waits this long for stopped local jobs to write their rows
DB_RETRY_SECONDS = 30.0  # a run the database was unavailable for is queued again this far ahead (jittered)
WRITE_RETRY_SECONDS = 120.0  # a final status write keeps trying this long while the database is unavailable
WRITE_RETRY_MAX_PAUSE = 15.0  # ... waiting 0.5 s, 1 s, 2 s ... at most this long between two tries
_LOCK_TIMEOUT = "5s"  # a job-row write never waits longer for a row lock (it holds the process-wide write lock)

_handlers: dict[str, JobFn] = {}
_exit_policies: dict[str, str] = {}


@dataclass
class PeriodicTask:
    name: str
    every_seconds: float
    fn: PeriodicFn
    initial_delay: float = 5.0


_periodic: dict[str, PeriodicTask] = {}


def _check_policy(policy: str) -> str:
    if policy not in EXIT_POLICIES:
        raise ValueError(f"unknown exit policy {policy!r} (expected one of {', '.join(EXIT_POLICIES)})")
    return policy


def job_handler(kind: str, *, on_exit: str | None = None) -> Callable[[JobFn], JobFn]:
    """Register the handler of ``kind`` and its exit policy - what happens to queued/running work of this kind when
    the person who asked for it leaves: "keep" (finish anyway), "pause" (defer until they return) or "cancel".
    Without ``on_exit`` the documented policy of the kind applies (``DEFAULT_EXIT_POLICIES``), else "keep"."""
    policy = _check_policy(on_exit or DEFAULT_EXIT_POLICIES.get(kind, EXIT_KEEP))

    def deco(fn: JobFn) -> JobFn:
        _handlers[kind] = fn
        _exit_policies[kind] = policy
        return fn

    return deco


def exit_policy_for(kind: str) -> str:
    """The exit policy new jobs of ``kind`` get (an explicit ``enqueue(exit_policy=...)`` wins)."""
    return _exit_policies.get(kind) or DEFAULT_EXIT_POLICIES.get(kind, EXIT_KEEP)


def periodic(name: str, *, every_seconds: float, initial_delay: float = 5.0) -> Callable[[PeriodicFn], PeriodicFn]:
    def deco(fn: PeriodicFn) -> PeriodicFn:
        _periodic[name] = PeriodicTask(name, every_seconds, fn, initial_delay)
        return fn

    return deco


def registered_handlers() -> dict[str, JobFn]:
    return dict(_handlers)


def registered_periodic() -> dict[str, PeriodicTask]:
    return dict(_periodic)


# ------------------------------------------------------------------------------------------------ enqueue
async def enqueue(
    db: AsyncSession,
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    run_after: datetime | None = None,
    dedupe_key: str | None = None,
    max_attempts: int = 5,
    user_id: int | None = None,
    exit_policy: str | None = None,
) -> Job | None:
    """Queue a job (the caller commits). ``user_id`` is the person who asked for it (None = system work);
    ``exit_policy`` defaults to the kind's. With ``dedupe_key`` an unfinished job with the same key wins: a QUEUED or
    RUNNING one is returned as it is; a PAUSED one stays in flight too (re-queued, attempts kept, when a present user
    asks for it again); a DONE/FAILED/CANCELLED one is recycled. Two requests enqueueing the same new key at once
    never collide (INSERT ... ON CONFLICT DO NOTHING)."""
    policy = _check_policy(exit_policy or exit_policy_for(kind))
    now = utcnow()
    if dedupe_key:
        existing = await db.scalar(select(Job).where(Job.dedupe_key == dedupe_key))
        if existing is None:
            new_id = await db.scalar(
                pg_insert(Job)
                .values(kind=kind, payload=payload or {}, status=JobStatus.QUEUED, attempts=0, max_attempts=max_attempts,
                        run_after=run_after or now, dedupe_key=dedupe_key, user_id=user_id, exit_policy=policy)
                .on_conflict_do_nothing(index_elements=[Job.dedupe_key])
                .returning(Job.id)
            )
            if new_id is not None:
                return await db.get(Job, new_id)
            # a concurrent transaction inserted the key first (and committed): treat its row like any existing one
            existing = await db.scalar(select(Job).where(Job.dedupe_key == dedupe_key)
                                       .execution_options(populate_existing=True))
            if existing is None:  # pragma: no cover - the winner vanished again; fall back to a plain insert
                job = Job(kind=kind, payload=payload or {}, run_after=run_after or now, dedupe_key=dedupe_key,
                          max_attempts=max_attempts, status=JobStatus.QUEUED, user_id=user_id, exit_policy=policy)
                db.add(job)
                await db.flush()
                return job
        if existing.status in (JobStatus.QUEUED, JobStatus.RUNNING):
            return existing
        if existing.status == JobStatus.PAUSED:
            if user_id is not None and not _is_away(user_id):  # someone present wants it now: back in the queue
                existing.status = JobStatus.QUEUED
                existing.run_after = run_after or now
                existing.paused_at = None
                existing.cancel_reason = None
                existing.user_id = user_id
                existing.exit_policy = policy
            return existing
        # finished / failed / cancelled job with the same key -> recycle the row
        existing.status = JobStatus.QUEUED
        existing.payload = payload or {}
        existing.attempts = 0
        existing.last_error = None
        existing.finished_at = None
        existing.locked_at = None
        existing.paused_at = None
        existing.cancel_reason = None
        existing.run_after = run_after or now
        existing.max_attempts = max_attempts
        existing.user_id = user_id
        existing.exit_policy = policy
        return existing
    job = Job(kind=kind, payload=payload or {}, run_after=run_after or now, dedupe_key=None,
              max_attempts=max_attempts, status=JobStatus.QUEUED, user_id=user_id, exit_policy=policy)
    db.add(job)
    return job


async def enqueue_many(db: AsyncSession, kind: str, items: Sequence[tuple[dict[str, Any], str]], *,
                       run_after: datetime | None = None, max_attempts: int = 5) -> int:
    """``enqueue(db, kind, payload, dedupe_key=key)`` of SYSTEM work (no requester) for many keys at once - any number
    of them - in a constant number of statements: one multi-row ``INSERT ... ON CONFLICT (dedupe_key) DO NOTHING`` for
    the new keys (per 1000 keys: SQLAlchemy's insertmanyvalues pages the rows, so a statement's bind parameters stay
    far below asyncpg's 32,767) and one UPDATE recycling the finished / failed / cancelled rows of the others (the keys
    travel as ONE array parameter). Like ``enqueue``, a QUEUED, RUNNING or PAUSED job with the key stays as it is
    (system work never wakes a paused job). A key must always carry the same payload (``"<kind>:<id>"`` ->
    ``{"<x>_id": id}``): a recycled row keeps its own. Returns the keys given; the caller commits."""
    pairs = list(dict((key, payload) for payload, key in items if key).items())  # one row per key
    if not pairs:
        return 0
    policy = _check_policy(exit_policy_for(kind))
    when = run_after or utcnow()
    # rows as a parameter list + RETURNING: insertmanyvalues sends them as multi-row VALUES (1000 rows a statement);
    # the ids are not needed - without RETURNING the driver would run the INSERT once per row
    await db.execute(
        pg_insert(Job).on_conflict_do_nothing(index_elements=[Job.dedupe_key]).returning(Job.id),
        [{"kind": kind, "payload": payload or {}, "status": JobStatus.QUEUED, "attempts": 0,
          "max_attempts": max_attempts, "run_after": when, "dedupe_key": key, "user_id": None,
          "exit_policy": policy} for key, payload in pairs],
    )
    await db.execute(
        update(Job)
        .where(Job.dedupe_key == any_(literal([key for key, _ in pairs], ARRAY(String))),
               Job.status.in_([JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED]))
        .values(status=JobStatus.QUEUED, attempts=0, last_error=None, finished_at=None, locked_at=None, paused_at=None,
                cancel_reason=None, run_after=when, max_attempts=max_attempts, user_id=None, exit_policy=policy)
        .execution_options(synchronize_session=False)
    )
    return len(pairs)


def _is_away(user_id: int) -> bool:
    try:
        from app.services.presence import presence

        return presence.is_away(user_id)
    except Exception:  # noqa: BLE001
        return False


def _task_update(user_id: int | None, job_id: int, kind: str, status: str, *, policy: str | None = None,
                 fields: Mapping[str, Any] | None = None, **extra: Any) -> dict[str, Any] | None:
    """``task.update`` WebSocket event to the person who asked for a background job (docs section 7, read by IP.ai in
    app/static/src/js/01-ai.js): paused while they were away, resumed (``resumed: true``), cancelled, done, failed.
    Obligations (exit policy "keep": verification, payroll ...) announce their results with notifications of their
    own, so their done/failed updates say ``notify: false`` and the browser only tracks them. ``fields``: what the
    handler set for this outcome (``set_result_event`` / ``set_failure_event``). Returns the event (None without a
    requester). Never raises."""
    if user_id is None:
        return None
    evt: dict[str, Any] = {"type": "task.update", "task_id": job_id, "kind": kind, "status": status,
                           "progress": None, "message": None, "link": None}
    if status in (JobStatus.DONE.value, JobStatus.FAILED.value) and (policy or exit_policy_for(kind)) == EXIT_KEEP:
        evt["notify"] = False
    for name, value in (*(fields or {}).items(), *extra.items()):
        if name not in RUNNER_EVENT_KEYS:
            evt[name] = value
    try:
        from app.services.realtime import hub

        hub.publish_nowait([user_id], evt)
    except Exception:  # noqa: BLE001 - a lost progress note must never affect the job
        log.debug("task.update for job %s not delivered", job_id, exc_info=True)
    return evt


# ------------------------------------------------------------------------------------------------ the run in progress
RUNNER_EVENT_KEYS = frozenset({"type", "task_id", "kind", "status"})  # task.update keys only the runner sets


@dataclass(slots=True)
class JobRun:
    """The job run in progress in this task, as its handler sees it (``current_run()``)."""

    job_id: int
    kind: str
    user_id: int | None
    attempt: int  # 1 on the first run; a stopped run (pause, shutdown) gives its attempt back
    max_attempts: int
    done_event: dict[str, Any] = field(default_factory=dict)  # set_result_event()
    failed_event: dict[str, Any] = field(default_factory=dict)  # set_failure_event()
    # the runner is stopping this run (its requester left, shutdown, the row changed elsewhere): whatever the handler
    # raises now is not a failure - the job is paused, cancelled or queued again, not retried or failed
    stopping: bool = False

    @property
    def last_attempt(self) -> bool:
        """No retry follows if this run fails."""
        return self.attempt >= self.max_attempts


_current_run: ContextVar[JobRun | None] = ContextVar("ironpulse_job_run", default=None)


def current_run() -> JobRun | None:
    """The job run in progress in this task - None outside the runner (a handler called directly by a test or a
    script, ``run_job_inline``, requests, periodic tasks)."""
    return _current_run.get()


def _event_fields(fields: dict[str, Any]) -> dict[str, Any]:
    clash = RUNNER_EVENT_KEYS.intersection(fields)
    if clash:
        raise ValueError(f"task.update keys set by the job runner itself: {', '.join(sorted(clash))}")
    return fields


def set_result_event(**fields: Any) -> bool:
    """How the runner announces that this run succeeded: ``fields`` (``title``, ``message``, ``link``, ``notify``,
    ``progress`` or any other JSON-safe key) go into the ``task.update`` 'done' event it publishes after the DONE
    write, so a handler never publishes a 'done' of its own and the page only hears about it once the row is DONE.
    ``notify=False``: no island note (the page shows the outcome itself, or it is no news). Later calls add to or
    replace earlier ones. Works at any depth below the handler (ContextVar); outside the runner it does nothing and
    returns False. ``type``, ``task_id``, ``kind`` and ``status`` belong to the runner (ValueError)."""
    fields = _event_fields(fields)
    run = _current_run.get()
    if run is None:
        return False
    run.done_event.update(fields)
    return True


def set_failure_event(**fields: Any) -> bool:
    """``set_result_event`` for the 'failed' event the runner publishes when this run failed and no retry is left (a
    retry is not news), e.g. ``set_failure_event(notify=False)`` for a nicety whose failure changes nothing for the
    person. The 'done' fields never go with a failure."""
    fields = _event_fields(fields)
    run = _current_run.get()
    if run is None:
        return False
    run.failed_event.update(fields)
    return True


# ------------------------------------------------------------------------------------------------ local state
@dataclass(slots=True)
class _Claim:
    id: int
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    user_id: int | None
    exit_policy: str
    token: datetime  # the locked_at our claim wrote


@dataclass(eq=False, slots=True)
class _LocalJob:
    """A job this process claimed: waiting for a slot, running, or writing its final status."""

    id: int
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    user_id: int | None
    exit_policy: str
    token: datetime  # current locked_at fencing token (claim, then every heartbeat)
    task: asyncio.Task | None = None
    run: JobRun | None = None  # what the handler sees (current_run) and how it wants its outcome announced
    phase: str = "claimed"  # claimed -> running (handler) -> finishing (DONE write)
    stop: str | None = None  # pause | cancel (exit policy) | remote (row changed elsewhere) | shutdown
    stop_reason: str | None = None
    settled: bool = False  # the final row state is decided (written, being written, or left to someone else)


class _Runtime:
    """Process-local runner primitives, bound to the running event loop."""

    def __init__(self) -> None:
        self.accepting = True
        self.finalizers: set[asyncio.Task] = set()  # shielded final-status writes still in flight
        self.size = 0
        self._loop: Any = None
        self._lock: asyncio.Lock | None = None
        self._sem: asyncio.Semaphore | None = None
        self._slot_freed: asyncio.Event | None = None

    def _bind(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._loop, self._lock, self._sem, self._slot_freed = loop, None, None, None

    def write_lock(self) -> asyncio.Lock:
        """Serialises this process's job-row writes with the heartbeat, which moves the fencing token."""
        self._bind()
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def slots(self) -> asyncio.Semaphore:
        self._bind()
        size = concurrency()
        if self._sem is None or (size != self.size and not _running):
            self._sem, self.size = asyncio.Semaphore(size), size
        return self._sem

    def slot_freed(self) -> asyncio.Event:
        self._bind()
        if self._slot_freed is None:
            self._slot_freed = asyncio.Event()
        return self._slot_freed

    def notify_slot_freed(self) -> None:
        if self._slot_freed is not None:
            self._slot_freed.set()

    def reset(self) -> None:
        self.accepting = True
        self.finalizers.clear()
        self.size = 0
        self._loop = self._lock = self._sem = self._slot_freed = None


_running: dict[int, _LocalJob] = {}
_rt = _Runtime()


def concurrency() -> int:
    """Jobs executed at once in this process (``settings.jobs_concurrency``, at least 1)."""
    try:
        return max(1, int(get_settings().jobs_concurrency or 1))
    except (TypeError, ValueError):
        return 1


def running_jobs() -> dict[int, asyncio.Task]:
    """Job id -> the local task running (or about to run) it."""
    return {job_id: lj.task for job_id, lj in _running.items() if lj.task is not None}


# ------------------------------------------------------------------------------------------------ claiming / running
async def _claim(limit: int) -> list[_Claim]:
    if limit <= 0 or not _rt.accepting:
        return []
    now = utcnow()
    async with session_scope() as db:
        rows = (
            await db.execute(
                select(Job)
                .where(
                    or_(
                        (Job.status == JobStatus.QUEUED) & (Job.run_after <= now),
                        (Job.status == JobStatus.RUNNING) & (Job.locked_at < now - STALE_AFTER),  # crashed process
                    )
                )
                .order_by(Job.run_after, Job.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).scalars().all()
        claimed = []
        for job in rows:
            if job.status == JobStatus.RUNNING:
                log.warning("job %s (%s) reclaimed: no heartbeat since %s", job.id, job.kind, job.locked_at)
            job.status = JobStatus.RUNNING
            job.locked_at = now
            job.attempts += 1
            policy = job.exit_policy if job.exit_policy in EXIT_POLICIES else exit_policy_for(job.kind)
            claimed.append(_Claim(job.id, job.kind, dict(job.payload or {}), job.attempts, job.max_attempts,
                                  job.user_id, policy, now))
        return claimed


def _spawn(claim: _Claim) -> asyncio.Task:
    lj = _LocalJob(claim.id, claim.kind, claim.payload, claim.attempts, claim.max_attempts, claim.user_id,
                   claim.exit_policy, claim.token)
    previous = _running.get(lj.id)
    if previous is not None:  # an older local run of the same row lost its claim (stale reclaim): it must stop
        _stop(previous, "remote", "claimed again")
    _running[lj.id] = lj
    lj.task = asyncio.get_running_loop().create_task(_run(lj), name=f"ironpulse:job:{lj.id}")
    lj.task.add_done_callback(lambda task, lj=lj: _task_done(lj, task))
    return lj.task


async def run_due_jobs(limit: int = 10) -> int:
    """Claim up to ``limit`` due jobs and run them - ``settings.jobs_concurrency`` at a time, each in its own task -
    returning (the number claimed) only once every one of them has finished. Tests (``run_jobs``) and scripts use
    this; the worker uses ``dispatch_due_jobs``."""
    claimed = await _claim(limit)
    if not claimed:
        return 0
    tasks = [_spawn(c) for c in claimed]
    try:
        await asyncio.wait(tasks)
    except asyncio.CancelledError:
        # our caller is going away (shutdown): give everything we claimed back to the queue before re-raising
        for c, task in zip(claimed, tasks, strict=True):
            lj = _running.get(c.id)
            if lj is not None and lj.task is task:
                _stop(lj, "shutdown", "runner cancelled")
        await _wait_shielded([*tasks, *_rt.finalizers], SHUTDOWN_WAIT_SECONDS)
        raise
    return len(claimed)


async def start_due_jobs() -> int:
    """Claim only as many due jobs as there are free slots and start them without waiting for them (a claimed job
    never sits in RUNNING waiting for a slot). Returns how many were started."""
    free = concurrency() - len(_running)
    if free <= 0 or not _rt.accepting:
        return 0
    claimed = await _claim(free)
    for c in claimed:
        _spawn(c)
    return len(claimed)


async def dispatch_due_jobs(window: float = 2.0) -> int:
    """Worker mode: keep the execution slots busy for up to ``window`` seconds - a freed slot is refilled at once
    while jobs are due - and return how many jobs were started. Never waits for the jobs themselves."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + window
    started = 0
    while _rt.accepting:
        freed = _rt.slot_freed()
        freed.clear()
        started += await start_due_jobs()
        if concurrency() - len(_running) > 0:
            break  # slots left over: nothing else is due right now
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        try:
            await asyncio.wait_for(freed.wait(), timeout=remaining)
        except TimeoutError:
            break
    return started


async def _run(lj: _LocalJob) -> None:
    try:
        async with _rt.slots():
            if not _rt.accepting:  # shutdown began after the claim: straight back to the queue
                lj.stop, lj.stop_reason = lj.stop or "shutdown", lj.stop_reason or "server shutdown"
                await _after_stop(lj)
                return
            if not await _still_ours(lj):
                lj.settled = True
                return
            if lj.user_id is not None and lj.exit_policy != EXIT_KEEP and await _requester_away(lj.user_id):
                lj.settled = True  # nobody will look at the result now: park it (pause) or drop it (cancel)
                await _shielded(_park(lj))
                return
            handler = _handlers.get(lj.kind)
            if handler is None:
                raise RuntimeError(f"no handler registered for job kind '{lj.kind}'")
            lj.phase = "running"
            lj.run = JobRun(lj.id, lj.kind, lj.user_id, lj.attempts, lj.max_attempts)
            owner = AIOwner(user_id=lj.user_id, kind=OWNER_JOB, job_id=lj.id, exit_policy=lj.exit_policy)
            run_token = _current_run.set(lj.run)
            try:
                with owner_scope(owner):
                    async with session_scope() as db:
                        await handler(db, lj.payload)
            finally:
                _current_run.reset(run_token)
            lj.phase = "finishing"
        lj.settled = True
        await _shielded(_write_done(lj))
    except asyncio.CancelledError:
        if await _after_stop(lj):
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                task.uncancel()  # we asked for this stop and handled it: the task ends normally
            return
        raise
    except Exception as exc:  # noqa: BLE001
        if lj.stop is not None:  # the handler turned our stop into an error (e.g. AICancelled): same outcome
            await _after_stop(lj)
            return
        if lj.settled:  # the final write itself failed (logged by the finalizer)
            return
        kind = db_unavailable(exc)
        if kind is not None:  # an overloaded / unreachable database, not the job: later, attempt given back
            lj.settled = True
            await _shielded(_write_deferred(lj, kind, exc))
            return
        error = f"{exc.__class__.__name__}: {exc}\n" + traceback.format_exc(limit=4)
        log.exception("job %s (%s) failed on attempt %s", lj.id, lj.kind, lj.attempts)
        lj.settled = True
        await _shielded(_write_failed(lj, error))


async def _after_stop(lj: _LocalJob) -> bool:
    """A run was cancelled: write what its stop means. Returns True when the stop was requested by this module."""
    ours = lj.stop is not None
    if lj.settled:
        return ours  # its final write is already on its way
    lj.settled = True
    action = lj.stop or "shutdown"  # a cancellation nobody asked for (event loop teardown) counts as shutdown
    if action in (EXIT_PAUSE, EXIT_CANCEL):
        await _shielded(_write_stopped(lj, action, lj.stop_reason or "left"))
    elif action == "remote":
        log.info("job %s (%s) stopped here: %s", lj.id, lj.kind, lj.stop_reason or "row changed elsewhere")
    else:
        await _shielded(_write_requeued(lj, lj.stop_reason or "cancelled"))
    return ours


def _task_done(lj: _LocalJob, task: asyncio.Task) -> None:
    if _running.get(lj.id) is lj:
        del _running[lj.id]
    _rt.notify_slot_freed()
    if not lj.settled:  # cancelled before its coroutine ever ran: settle the row from here
        lj.settled = True
        action = lj.stop or "shutdown"
        if action in (EXIT_PAUSE, EXIT_CANCEL):
            _track(_write_stopped(lj, action, lj.stop_reason or "left"))
        elif action != "remote":
            _track(_write_requeued(lj, lj.stop_reason or "cancelled"))
    if not task.cancelled() and task.exception() is not None:
        log.error("job task %s (%s) crashed", lj.id, lj.kind, exc_info=task.exception())


def _stop(lj: _LocalJob, action: str, reason: str | None = None) -> bool:
    """Ask a local job to stop (its task is cancelled; the run writes what the stop means). False when it is already
    stopping, finished, or writing its DONE status."""
    task = lj.task
    if lj.stop is not None or lj.settled or lj.phase == "finishing" or task is None or task.done():
        return False
    lj.stop, lj.stop_reason = action, reason
    if lj.run is not None:
        lj.run.stopping = True
    task.cancel(msg=f"job {action}" + (f": {reason}" if reason else ""))
    return True


async def _still_ours(lj: _LocalJob) -> bool:
    """Right before the handler: is the row still RUNNING under our claim? (paused/cancelled/reclaimed meanwhile)"""
    async with _rt.write_lock():
        async with session_scope() as db:
            row = (await db.execute(select(Job.status, Job.locked_at).where(Job.id == lj.id))).first()
    if row is None:
        log.info("job %s (%s) skipped: its row is gone", lj.id, lj.kind)
        return False
    if row.status != JobStatus.RUNNING or row.locked_at != lj.token:
        log.info("job %s (%s) skipped: it became %s after it was claimed", lj.id, lj.kind,
                 row.status.value if row.status != JobStatus.RUNNING else "claimed elsewhere")
        return False
    return True


async def _requester_away(user_id: int) -> bool:
    try:
        from app.services.presence import presence

        return await presence.away_now(user_id)
    except Exception as exc:  # noqa: BLE001
        kind = db_unavailable(exc, statement_timeouts=True)
        if kind is not None:  # cannot tell: fail safe - a present user's job is never parked on a guess
            warn_db_unavailable(log, "jobs (presence check)", kind, exc,
                                f"could not tell whether user {user_id} is away - their job runs")
            return False
        log.exception("presence check for user %s failed", user_id)
        return False


async def _park(lj: _LocalJob) -> bool:
    """Its requester is away: park the job (pause) or drop it (cancel) instead of running it. The away verdict was
    read before this write; a return noticed in between resumed nothing (the row was still RUNNING), so once the
    PAUSED row is written presence is asked again - back by now: their paused jobs resume
    (``presence.resume_if_back``). A return after that finds the row PAUSED and resumes it itself."""
    parked = await _write_stopped(lj, lj.exit_policy, "away")
    if parked and lj.exit_policy == EXIT_PAUSE and lj.user_id is not None:
        try:
            from app.services.presence import presence

            await presence.resume_if_back(lj.user_id)
        except Exception:  # noqa: BLE001
            log.exception("presence re-check after parking job %s failed", lj.id)
    return parked


# ------------------------------------------------------------------------------------------------ final writes
async def _write(lj: _LocalJob, values: dict[str, Any], what: str) -> bool:
    """Fenced final write: only while the row is still RUNNING under our token. Retried briefly on DB errors (3 tries);
    while the database is unavailable (pool timeout, refused or lost connection) with back-off for WRITE_RETRY_SECONDS,
    one warning a minute - then given up without a traceback: the row stays RUNNING and the stale-job reclaim
    (STALE_AFTER) runs the job again, nothing is lost."""
    got = None
    loop = asyncio.get_running_loop()
    give_up = loop.time() + WRITE_RETRY_SECONDS
    failures = unavailable = 0
    while True:
        try:
            async with _rt.write_lock():
                async with session_scope() as db:
                    await db.execute(text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'"))
                    got = await db.scalar(
                        update(Job)
                        .where(Job.id == lj.id, Job.status == JobStatus.RUNNING, Job.locked_at == lj.token)
                        .values(**values)
                        .returning(Job.id)
                        .execution_options(synchronize_session=False)
                    )
            break
        except Exception as exc:  # noqa: BLE001
            kind = db_unavailable(exc)
            if kind is None:
                failures += 1
                if failures >= 3:
                    raise
                await asyncio.sleep(0.25 * failures)
                continue
            unavailable += 1
            pause = backoff_delay(unavailable, 0.5, WRITE_RETRY_MAX_PAUSE)
            if loop.time() + pause > give_up:  # its own call site: never hidden behind the "trying again" lines
                warn_db_unavailable(log, "jobs (status write given up)", kind, exc, f"job {lj.id} ({lj.kind}) not "
                                    f"marked {what} - left RUNNING for the stale-job reclaim to run it again")
                return False
            warn_db_unavailable(log, "jobs (status write)", kind, exc,
                                f"job {lj.id} ({lj.kind}) not marked {what} yet - trying again")
            await asyncio.sleep(pause)
    if got is None:
        log.info("job %s (%s) not marked %s: its row changed meanwhile (cancelled, paused or reclaimed)",
                 lj.id, lj.kind, what)
    return got is not None


async def _write_done(lj: _LocalJob) -> bool:
    ok = await _write(lj, {"status": JobStatus.DONE, "finished_at": utcnow(), "last_error": None}, "done")
    if ok:  # after the DONE write: a page told "done" never finds the job still running
        _task_update(lj.user_id, lj.id, lj.kind, JobStatus.DONE.value, policy=lj.exit_policy,
                     fields=lj.run.done_event if lj.run is not None else None)
    return ok


async def _write_failed(lj: _LocalJob, error: str) -> bool:
    final = lj.attempts >= lj.max_attempts
    delay = timedelta(seconds=min(3600, 30 * (2 ** max(0, lj.attempts - 1))))
    now = utcnow()
    ok = await _write(lj, {
        "status": JobStatus.FAILED if final else JobStatus.QUEUED,
        "run_after": now + delay,
        "last_error": error[:4000],
        "finished_at": now if final else None,
        "locked_at": None,
    }, "failed" if final else "for a retry")
    if ok and final:  # a retry is not news: it runs again by itself
        _task_update(lj.user_id, lj.id, lj.kind, JobStatus.FAILED.value, policy=lj.exit_policy,
                     fields=lj.run.failed_event if lj.run is not None else None)
    return ok


async def _write_stopped(lj: _LocalJob, action: str, reason: str) -> bool:
    paused = action == EXIT_PAUSE
    now = utcnow()
    ok = await _write(lj, {
        "status": JobStatus.PAUSED if paused else JobStatus.CANCELLED,
        "attempts": func.greatest(Job.attempts - 1, 0),  # the interrupted run doesn't count
        "locked_at": None,
        "cancel_reason": reason[:80],
        "paused_at": now if paused else None,
        "finished_at": None if paused else now,
    }, "paused" if paused else "cancelled")
    if ok:
        log.info("job %s (%s) %s (%s)", lj.id, lj.kind, "paused" if paused else "cancelled", reason)
        _task_update(lj.user_id, lj.id, lj.kind, (JobStatus.PAUSED if paused else JobStatus.CANCELLED).value)
    return ok


async def _write_deferred(lj: _LocalJob, kind: str, exc: BaseException) -> bool:
    """The database was unavailable for this run (pool timeout, refused or lost connection) - the job's own code is not
    at fault. Back in the queue DB_RETRY_SECONDS from now (jittered), its attempt given back: an overload can never use
    up a job's attempts. One warning a minute, no traceback."""
    delay = backoff_delay(1, DB_RETRY_SECONDS, DB_RETRY_SECONDS)
    warn_db_unavailable(log, "jobs (run)", kind, exc,
                        f"job {lj.id} ({lj.kind}) queued again in {delay:.0f} s, its attempt given back")
    return await _write(lj, {
        "status": JobStatus.QUEUED,
        "attempts": func.greatest(Job.attempts - 1, 0),
        "locked_at": None,
        "run_after": utcnow() + timedelta(seconds=delay),
        "last_error": f"deferred: the database was unavailable ({kind})",
    }, "queued again")


async def _write_requeued(lj: _LocalJob, reason: str) -> bool:
    ok = await _write(lj, {
        "status": JobStatus.QUEUED,
        "attempts": func.greatest(Job.attempts - 1, 0),
        "locked_at": None,
        "run_after": utcnow(),
    }, "queued again")
    if ok:
        log.info("job %s (%s) put back in the queue (%s)", lj.id, lj.kind, reason)
    return ok


def _log_finalizer(task: asyncio.Task) -> None:
    _rt.finalizers.discard(task)
    if not task.cancelled() and task.exception() is not None:
        log.error("job status write failed", exc_info=task.exception())


def _track(coro: Coroutine[Any, Any, Any]) -> asyncio.Task | None:
    try:
        task = asyncio.get_running_loop().create_task(coro, name="ironpulse:job-status")
    except RuntimeError:  # no running loop (interpreter teardown)
        coro.close()
        return None
    _rt.finalizers.add(task)
    task.add_done_callback(_log_finalizer)
    return task


async def _shielded(coro: Coroutine[Any, Any, Any]) -> Any:
    """Run a final status write to completion even if the awaiting task is cancelled again (shutdown)."""
    task = _track(coro)
    if task is None:
        return None
    return await asyncio.shield(task)


async def _wait_shielded(tasks: list[asyncio.Task], timeout: float) -> None:
    current = asyncio.current_task()
    pending = [t for t in tasks if not t.done() and t is not current]
    if pending:
        try:
            await asyncio.shield(asyncio.ensure_future(asyncio.wait(pending, timeout=timeout)))
        except asyncio.CancelledError:
            pass


# ------------------------------------------------------------------------------------------------ watchdog
async def watchdog_tick() -> dict[str, int]:
    """Worker loop (every ``WATCHDOG_SECONDS``): heartbeat ``locked_at`` of every job this process holds - so a long
    job is never reclaimed and run twice - and stop local tasks whose row another process cancelled, paused or
    reclaimed (no status write: that process already decided)."""
    local = [lj for lj in _running.values() if lj.stop is None and not lj.settled]
    if not local:
        return {"held": 0, "lost": 0}
    alive = await asyncio.shield(asyncio.ensure_future(_heartbeat(local)))
    lost = [lj for lj in local
            if lj.id not in alive and _running.get(lj.id) is lj and lj.stop is None and not lj.settled
            and lj.phase != "finishing"]
    if lost:
        async with session_scope() as db:
            states = {row.id: row.status for row in
                      (await db.execute(select(Job.id, Job.status).where(Job.id.in_([lj.id for lj in lost])))).all()}
        for lj in lost:
            status = states.get(lj.id)
            if status in (JobStatus.CANCELLED, JobStatus.PAUSED):
                reason = f"{status.value} by another process"
            elif status == JobStatus.RUNNING:
                reason = "claimed by another process"
            else:
                reason = f"row is {status.value if status else 'gone'}"
            _stop(lj, "remote", reason)
    return {"held": len(local) - len(lost), "lost": len(lost)}


async def _heartbeat(local: list[_LocalJob]) -> set[int]:
    alive: set[int] = set()
    async with _rt.write_lock():
        now = utcnow()
        async with session_scope() as db:
            await db.execute(text("SET LOCAL lock_timeout = '2s'"))
            for lj in local:
                if lj.settled:
                    continue  # finished while we waited for the lock: its DONE write uses the current token
                got = await db.scalar(
                    update(Job)
                    .where(Job.id == lj.id, Job.status == JobStatus.RUNNING, Job.locked_at == lj.token)
                    .values(locked_at=now)
                    .returning(Job.id)
                    .execution_options(synchronize_session=False)
                )
                if got is not None:
                    alive.add(lj.id)
        for lj in local:  # only after COMMIT: the tokens the DB now holds
            if lj.id in alive:
                lj.token = now
    return alive


# ------------------------------------------------------------------------------------------------ exit policies
async def apply_exit_policy(user_id: int, *, reason: str = "left") -> dict[str, int]:
    """The user left: their QUEUED/RUNNING jobs with policy "pause" become PAUSED and those with "cancel" become
    CANCELLED; "keep" is untouched. Jobs running here are stopped and write their own row (attempt given back);
    rows running in another process are written here and that process's watchdog stops the task.
    Returns ``{"paused": n, "cancelled": n, "running_stopped": n}`` (the first two include the local runs stopped)."""
    reason = (reason or "left")[:80]
    now = utcnow()
    mine = [lj for lj in _running.values() if lj.user_id == user_id]
    out = {"paused": 0, "cancelled": 0, "running_stopped": 0}
    changed: list[tuple[int, str, str]] = []  # (job id, kind, new status) of the rows written here
    async with session_scope() as db:
        for policy, status, key in ((EXIT_PAUSE, JobStatus.PAUSED, "paused"),
                                    (EXIT_CANCEL, JobStatus.CANCELLED, "cancelled")):
            stmt = update(Job).where(Job.user_id == user_id, Job.exit_policy == policy,
                                     Job.status.in_((JobStatus.QUEUED, JobStatus.RUNNING)))
            if mine:
                stmt = stmt.where(Job.id.not_in([lj.id for lj in mine]))
            rows = (await db.execute(
                stmt.values(
                    status=status,
                    attempts=case((Job.status == JobStatus.RUNNING, func.greatest(Job.attempts - 1, 0)),
                                  else_=Job.attempts),
                    locked_at=None,
                    cancel_reason=reason,
                    paused_at=now if status == JobStatus.PAUSED else None,
                    finished_at=now if status == JobStatus.CANCELLED else None,
                ).returning(Job.id, Job.kind).execution_options(synchronize_session=False)
            )).all()
            out[key] += len(rows)
            changed += [(job_id, kind, status.value) for job_id, kind in rows]
    for job_id, kind, status_value in changed:  # after COMMIT
        _task_update(user_id, job_id, kind, status_value)
    stopping = []
    for lj in mine:
        if lj.exit_policy in (EXIT_PAUSE, EXIT_CANCEL) and _stop(lj, lj.exit_policy, reason):
            out["paused" if lj.exit_policy == EXIT_PAUSE else "cancelled"] += 1
            stopping.append(lj.task)
    out["running_stopped"] = len(stopping)
    current = asyncio.current_task()
    waiting = [t for t in stopping if t is not None and t is not current]
    if waiting:
        await asyncio.wait(waiting, timeout=EXIT_WAIT_SECONDS)
    if out["paused"] or out["cancelled"]:
        log.info("exit policy for user %s (%s): %s", user_id, reason, out)
    return out


async def resume_user_jobs(user_id: int, *, announced: list[dict[str, Any]] | None = None) -> int:
    """The user is back: their PAUSED jobs are queued again (run_after = now, attempts kept). Returns the count.
    ``announced``: the ``task.update`` events published for them are also appended to it (presence keeps them for a
    page whose socket connects a moment later)."""
    async with session_scope() as db:
        rows = (await db.execute(
            update(Job).where(Job.user_id == user_id, Job.status == JobStatus.PAUSED)
            .values(status=JobStatus.QUEUED, run_after=utcnow(), paused_at=None, cancel_reason=None, locked_at=None)
            .returning(Job.id, Job.kind).execution_options(synchronize_session=False)
        )).all()
    if rows:
        log.info("resumed %s paused job(s) of user %s", len(rows), user_id)
    for job_id, kind in rows:  # after COMMIT: "Paused while you were away - resuming now"
        evt = _task_update(user_id, job_id, kind, JobStatus.QUEUED.value, resumed=True)
        if announced is not None and evt is not None:
            announced.append(dict(evt))
    return len(rows)


async def cancel_user_jobs(user_id: int, *, reason: str, all_policies: bool = False,
                           db: AsyncSession | None = None) -> int:
    """Cancel the user's unfinished (queued, running, paused) jobs - only "pause"/"cancel" ones, or every one with
    ``all_policies`` (account deletion). With ``db`` the rows change in the caller's transaction and local tasks
    stop only after it COMMITs; otherwise at once. Returns the number of jobs cancelled."""
    reason = (reason or "cancelled")[:80]
    now = utcnow()
    stmt = update(Job).where(Job.user_id == user_id,
                             Job.status.in_((JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.PAUSED)))
    if not all_policies:
        stmt = stmt.where(Job.exit_policy != EXIT_KEEP)
    stmt = stmt.values(
        status=JobStatus.CANCELLED,
        attempts=case((Job.status == JobStatus.RUNNING, func.greatest(Job.attempts - 1, 0)), else_=Job.attempts),
        locked_at=None,
        paused_at=None,
        cancel_reason=reason,
        finished_at=now,
    ).returning(Job.id).execution_options(synchronize_session=False)

    def stop_local(ids: set[int]) -> None:
        for lj in list(_running.values()):
            if lj.id in ids:
                _stop(lj, "remote", reason)  # the row is already CANCELLED: the run must not write

    if db is None:
        async with session_scope() as own:
            ids = set((await own.execute(stmt)).scalars().all())
        stop_local(ids)
    else:
        from app.services.realtime import call_after_commit

        ids = set((await db.execute(stmt)).scalars().all())
        if ids:
            call_after_commit(db, lambda: stop_local(ids))
    if ids:
        log.info("cancelled %s job(s) of user %s (%s)", len(ids), user_id, reason)
    return len(ids)


# ------------------------------------------------------------------------------------------------ lifecycle
def accept_jobs() -> None:
    """(Re)open the runner after a shutdown - the worker calls this when it starts."""
    _rt.accepting = True


async def shutdown_jobs(timeout: float = SHUTDOWN_WAIT_SECONDS) -> int:
    """Stop claiming, give every in-flight job back to the queue (QUEUED, attempt restored, locked_at NULL) and wait
    for those writes. The worker calls this before it cancels its loops. Returns the number of jobs put back."""
    _rt.accepting = False
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    stopped = 0
    current = asyncio.current_task()
    while True:
        for lj in list(_running.values()):
            if _stop(lj, "shutdown", "server shutdown"):
                stopped += 1
        pending = [lj.task for lj in _running.values() if lj.task is not None and not lj.task.done()]
        pending += [t for t in _rt.finalizers if not t.done()]
        pending = [t for t in pending if t is not current]
        if not pending:
            break
        remaining = deadline - loop.time()
        if remaining <= 0:
            log.warning("shutdown: %s job task(s) still busy after %.0f s", len(pending), timeout)
            break
        await asyncio.wait(pending, timeout=remaining)
    if stopped:
        log.info("shutdown: %s in-flight job(s) put back in the queue", stopped)
    return stopped


async def _reset() -> None:
    """Tests: drop every local job task without status writes (the tables are truncated next)."""
    pending = []
    for lj in list(_running.values()):
        lj.settled = True
        lj.stop = lj.stop or "remote"
        if lj.task is not None and not lj.task.done():
            lj.task.cancel()
            pending.append(lj.task)
    pending += [t for t in _rt.finalizers if not t.done()]
    if pending:
        try:
            await asyncio.wait(pending, timeout=5)
        except Exception:  # noqa: BLE001 - tasks of another (closed) loop
            pass
    _running.clear()
    _rt.reset()


register_reset(_reset, name="jobs")


# ------------------------------------------------------------------------------------------------ inline / periodic
async def run_job_inline(kind: str, payload: dict[str, Any]) -> None:
    """Run a handler immediately in its own transaction (tests / scripts)."""
    handler = _handlers[kind]
    async with session_scope() as db:
        await handler(db, payload)


def _lock_id(name: str) -> int:
    return int.from_bytes(hashlib.blake2b(name.encode(), digest_size=8).digest(), "big", signed=True)


async def run_periodic(task: PeriodicTask) -> bool:
    """Run one periodic task under its advisory lock. Returns False if another process holds it.

    The lock is a SESSION-level ``pg_try_advisory_lock`` on a connection of its own in autocommit mode (the pattern of
    app/db.py ``startup_lock``): that connection is never "idle in transaction", and the task's own session may commit
    as often as it likes (payroll per gym, retention batches) without releasing the lock - an xact-level lock taken in
    the task's transaction went with its first COMMIT and let another process start the same task. If the explicit
    unlock fails, the connection is invalidated: closing it releases the lock."""
    from app.db import get_engine

    key = _lock_id(task.name)
    async with get_engine().connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        if not await conn.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": key}):
            return False
        try:
            async with session_scope() as db:
                await task.fn(db)
            return True
        finally:
            try:
                await conn.scalar(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
            except Exception:  # noqa: BLE001 - never hand a pooled connection back still holding the lock
                log.warning("periodic %s: could not release its lock explicitly - dropping the connection", task.name)
                await conn.invalidate()
