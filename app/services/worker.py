"""In-process background runner started by the FastAPI lifespan (disabled in tests).

Loops: jobs (dispatch due jobs into free execution slots), job-watchdog (heartbeat + cross-process stop detection,
app/services/jobs.py), presence-leases (this process's Redis presence leases, app/services/presence.py),
security-flush, ban-sync, email, ai-health, plus one loop per ``@periodic`` task.

A loop whose round failed because the database is overloaded or out of reach (app.db ``db_unavailable``: pool timeout,
refused or lost connection) logs one warning a minute (no traceback) and waits longer before its next round - its
interval doubled per failure (jittered), at most max(interval, DB_BACKOFF_MAX_SECONDS) - until a round gets through.
Nothing is lost meanwhile: due jobs and e-mails stay queued in their tables, the security queue keeps its batch.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from app.config import get_settings
from app.core.overload import backoff_delay, warn_db_unavailable
from app.db import db_unavailable, session_scope

log = logging.getLogger("ironpulse.worker")
DB_BACKOFF_MAX_SECONDS = 30.0  # longest pause of a loop between two rounds while the database is unavailable


class BackgroundRunner:
    def __init__(self) -> None:
        self._tasks: list[asyncio.Task] = []
        self._stopping = asyncio.Event()
        self._last_ai_state: str | None = None

    async def _loop(self, name: str, every: float, fn: Callable[[], Awaitable[object]], initial_delay: float = 0.0) -> None:
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=initial_delay)
            return
        except TimeoutError:
            pass
        unavailable = 0  # rounds in a row the database could not serve
        while not self._stopping.is_set():
            pause = every
            try:
                await fn()
                unavailable = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                kind = db_unavailable(exc)
                if kind is None:
                    log.exception("background task %s failed", name)
                else:  # overload / outage, not a bug: back off, one warning a minute
                    unavailable += 1
                    pause = backoff_delay(unavailable + 1, every, max(every, DB_BACKOFF_MAX_SECONDS))
                    warn_db_unavailable(log, f"background task {name}", kind, exc,
                                        f"next round in {pause:.0f} s (failed {unavailable} time(s) in a row)")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=pause)
            except TimeoutError:
                continue

    # ---------------------------------------------------------------- built-in tasks
    @staticmethod
    async def _jobs() -> None:
        from app.services.jobs import dispatch_due_jobs

        await dispatch_due_jobs(window=2.0)  # starts jobs in their own tasks; never waits for them

    @staticmethod
    async def _job_watchdog() -> None:
        from app.services.jobs import watchdog_tick

        await watchdog_tick()

    @staticmethod
    async def _presence_leases() -> None:
        from app.services.presence import presence

        await presence.refresh_leases()

    @staticmethod
    async def _security() -> None:
        from app.ai.client import flush_ai_call_logs
        from app.security.bans import flush_security_queue

        # two transactions: a failure writing one (a DB hiccup, a refused value) never rolls back - or, retried every
        # 2 s, keeps holding back - the other; each puts its own uncommitted batch back in its own queue
        try:
            async with session_scope() as db:
                await flush_security_queue(db)
        finally:
            async with session_scope() as db:
                await flush_ai_call_logs(db)

    @staticmethod
    async def _bans() -> None:
        from app.security.bans import sync_bans_from_db

        async with session_scope() as db:
            await sync_bans_from_db(db)

    @staticmethod
    async def _email() -> None:
        from app.services.mailer import send_pending_emails

        async with session_scope() as db:
            await send_pending_emails(db)

    async def _ai_health(self) -> None:
        from app.ai.client import ai
        from app.services.realtime import hub

        status = await ai.refresh_health()
        if status["state"] != self._last_ai_state:
            if self._last_ai_state is not None:
                log.info("AI status changed: %s -> %s (%s)", self._last_ai_state, status["state"], status["active"])
                await hub.publish(hub.online_users(), {"type": "ai.status", "state": status["state"],
                                                       "active": status["active_label"], "available": status["available"]})
            self._last_ai_state = status["state"]

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> None:
        import app.services.retention  # noqa: F401 - registers @periodic("data_retention") (nightly batched deletes)
        from app.services.jobs import WATCHDOG_SECONDS, accept_jobs, registered_periodic, run_periodic
        from app.services.presence import LEASE_REFRESH_SECONDS

        s = get_settings()
        self._stopping.clear()
        accept_jobs()
        spec: list[tuple[str, float, Callable[[], Awaitable[object]], float]] = [
            ("jobs", 2.0, self._jobs, 1.0),
            ("job-watchdog", WATCHDOG_SECONDS, self._job_watchdog, WATCHDOG_SECONDS),
            ("presence-leases", LEASE_REFRESH_SECONDS, self._presence_leases, 0.0),
            ("security-flush", 2.0, self._security, 1.0),
            ("ban-sync", 30.0, self._bans, 0.0),
            ("email", 5.0, self._email, 2.0),
            ("ai-health", float(s.ai_health_interval_seconds), self._ai_health, 0.0),
        ]
        for task in registered_periodic().values():
            spec.append((f"periodic:{task.name}", task.every_seconds, (lambda t=task: run_periodic(t)), task.initial_delay))
        for name, every, fn, delay in spec:
            self._tasks.append(asyncio.create_task(self._loop(name, every, fn, delay), name=f"ironpulse:{name}"))
        log.info("background runner started (%s tasks)", len(self._tasks))

    async def stop(self) -> None:
        from app.services.jobs import shutdown_jobs

        self._stopping.set()
        # in-flight jobs go back to the queue (attempt restored) while the DB is still there - before the loops die
        try:
            await shutdown_jobs()
        except Exception:
            log.exception("could not put in-flight jobs back in the queue")
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        try:
            await shutdown_jobs(timeout=2.0)  # anything the jobs loop claimed while it was being stopped
        except Exception:
            log.exception("could not put in-flight jobs back in the queue")


runner = BackgroundRunner()
