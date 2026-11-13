"""Presence - is anybody still looking? The "stop when you leave" signal (docs/ai-guard-and-queue.md section 5).

Inputs
* ``/ws`` sockets (app/routers/core.py): ``connected`` right after the hub subscription, ``disconnected(code=...)``
  when the socket ends. Every signed-in page holds one socket, so a page navigation is a close followed a moment
  later by a connect, and N tabs are N sockets.
* ``touch`` from authenticated HTTP requests - cheap: a few dict operations, never awaits; at most once per
  ``AWAY_RECHECK_SECONDS`` per user it starts a background Redis check (see Return).
* ``logout`` / ``sessions_revoked`` from the sign-out paths (immediate, no grace period).

What happens
* **Page closed** - the client closes its socket with **4001** on ``pagehide`` and ``/ws?page=<id>`` said which tab
  it was: that tab's queued/running interactive AI is cancelled at once (``scheduler.cancel(page_id=...,
  user_id=..., kinds={"user"})`` - the page id is client-made, so it only ever scopes the caller's own work).
* **User left** - their last socket closed (any code but 1012 = server restart, and not during shutdown) and
  nothing came back within ``settings.presence_grace_seconds`` - neither here nor, through Redis, in another
  process: ``on_left`` cancels the interactive AI they were waiting for, applies their background jobs' exit
  policies (``jobs.apply_exit_policy``) and marks them away. HTTP activity here during the grace period (``touch``)
  restarts it from that request: someone whose socket keeps failing while they keep browsing is not gone.
* **Logout / revocation** - that session's AI work stops now; with no other socket here and no other live session
  (the database knows every session, whichever process serves it) it is an exit right away (no grace period).
  Another process learns of it through the PG bridge (``sessions_ended_elsewhere``) and stops that session's AI work
  there too.
* **Return** - a socket (or HTTP activity) of an away user resumes their paused jobs. A *fresh* arrival - the first
  socket in this process with no grace timer running - always resumes: PAUSED rows are the durable record of an
  exit, so a restart or another process's exit can never strand them. HTTP activity of a user another process saw
  leave is noticed within ``AWAY_RECHECK_SECONDS``: ``touch`` asks Redis at most that often per user (per process),
  never on every request, and a set flag means they are back - it is cleared and their paused jobs resume. An exit
  that a return elsewhere overtook (the flag vanished while the exit was pausing their jobs) resumes them at once, and
  so does a job the runner parked while they came back (``resume_if_back``). A return noticed by an HTTP request
  comes before the page's socket: the 'resuming now' notes are kept for the user's next socket
  (``NOTE_KEEP_SECONDS``).

Cross-process (Redis, optional; keys ``ip:<env>:<db-name>:presence:...``)
* ``presence:<uid>`` - ZSET of process ids scored by lease expiry (unix time). Written when the user's first socket
  arrives here and refreshed every ``LEASE_REFRESH_SECONDS`` by the worker while this process holds sockets of the
  user. A grace timer only fires when no *other* process holds an unexpired lease.
* ``presence:away:<uid>`` - set by ``on_left``, deleted by the return: any process can tell that the user left or
  came back (the job runner asks ``away_now`` before it starts one of their jobs).
* ``presence:notes:<uid>`` - 'resuming now' notes waiting for the user's next socket, whichever process it lands in.
Without Redis one process judges by its own sockets. Several processes (``REALTIME_PG_BRIDGE``, set by ``manage.py
run --workers N``) without Redis, or while it is down, fail safe: a process that cannot see the others never treats
a closed tab as an exit (their next page may be connected elsewhere) and never parks a job on its own, possibly stale,
away mark - only sign-out and revocation end a user's AI work then (``startup_check`` says so in the log).

Every entry point swallows and logs its own errors: presence must never break a socket, a request or a sign-out.

Database overload (pool timeout, refused or lost connection; app.db ``db_unavailable``): the database step of a return
(``jobs.resume_user_jobs``), an exit (``jobs.apply_exit_policy``) or a sign-out (is another session of the user
alive?) is NOT dropped and NOT logged with a traceback. It is deferred - one warning a minute per step - and the
deferred-step sweeper (``retry_deferred``) runs it again after ~RETRY_FIRST_SECONDS, doubling (jittered) up to
RETRY_MAX_SECONDS while the database stays unavailable, one probe per round. Each retry is judged on the state of THEN,
under the user's lock: a resume only while the user is not gone again (an exit since owns their jobs), an exit only
while they are still away (a return since undid it), ended sessions are judged afresh. Meanwhile nothing is lost: PAUSED
rows stay paused (and a fresh arrival resumes them anyway), and the job runner parks a queued job of a user marked away.
The lease refresh (worker loop) re-arms the sweeper should it ever have stopped with work left.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import socket
import time
from collections import Counter, OrderedDict
from collections.abc import Coroutine, Iterable
from dataclasses import dataclass, field
from typing import Any

from app.ai.owner import OWNER_USER, clean_page_id
from app.config import get_settings
from app.core.overload import backoff_delay, warn_db_unavailable
from app.core.state import register_reset

log = logging.getLogger("ironpulse.presence")

PAGE_CLOSED = 4001  # close code the client sends on pagehide (tab closed / navigated away)
SERVICE_RESTART = 1012  # uvicorn closes every socket with this on shutdown/reload: never an exit
UNKNOWN_CLOSE = 1006  # what /ws reports when the close code is unknown
LEASE_TTL_SECONDS = 45.0  # "this process holds sockets of the user" - outlives two missed refreshes
LEASE_REFRESH_SECONDS = 15.0  # worker loop interval (app/services/worker.py)
AWAY_TTL_SECONDS = 7 * 24 * 3600  # the shared "they left" flag; PAUSED rows themselves never expire
AWAY_RECHECK_SECONDS = 30.0  # touch(): HTTP activity asks Redis "did another process see them leave?" this rarely
NOTE_KEEP_SECONDS = 60.0  # a 'resuming now' note waits this long for the returning user's next socket
RETRY_FIRST_SECONDS = 5.0  # a step the database could not run (pool timeout, lost connection) is retried after ~this
RETRY_MAX_SECONDS = 60.0  # ... doubling (jittered) up to this while the database stays unavailable
RETRY_ROUND_MAX = 100  # users whose deferred steps one round retries (the rest: the next round, RETRY_FIRST_SECONDS on)
_NOTES_MAX = 20  # notes kept per user
_LOCK_STRIPES = 64
_STEPS = {"resume": "resuming the paused jobs", "exit": "applying the exit policies",
          "sessions": "judging the ended sessions"}  # deferred steps, as the log names them


@dataclass(slots=True)
class _Conn:
    conn_id: str
    page_id: str | None
    session_hash: str | None
    since: float


@dataclass(slots=True)
class _Away:
    since: float  # unix time
    reason: str
    shared: bool = False  # mirrored to Redis - once that flag is gone another process saw them come back


@dataclass(slots=True)
class _Deferred:
    """What presence still owes a user because the database could not run it (``retry_deferred``). ``resume`` and
    ``exit`` are opposite wishes - the newer one wins; ``sessions`` collects ended sessions still to be judged."""

    resume: bool = False
    exit: str | None = None  # the exit's reason
    sessions: set[str] = field(default_factory=set)
    sessions_reason: str = "signed_out"

    def merge_older(self, older: _Deferred) -> None:
        """Fold in an entry recorded before this one (a retry that failed again while a newer one was recorded)."""
        if not self.resume and self.exit is None:
            self.resume, self.exit = older.resume, older.exit
        if older.sessions:
            if not self.sessions:
                self.sessions_reason = older.sessions_reason
            self.sessions |= older.sessions

    def __bool__(self) -> bool:
        return self.resume or self.exit is not None or bool(self.sessions)


def _db_trouble(exc: BaseException) -> str | None:
    """``db_unavailable`` for presence's database steps. Their statements are tiny (one UPDATE / SELECT by user), so a
    statement timeout can only mean overload or a lock as well: deferred like a pool timeout."""
    from app.db import db_unavailable

    return db_unavailable(exc, statement_timeouts=True)


_ns_cache: tuple[str, str, str] | None = None


def redis_namespace() -> str:
    """``ip:<env>:<db-name>:presence`` - the dev servers, the tests and each test database never share keys."""
    global _ns_cache
    s = get_settings()
    if _ns_cache is None or _ns_cache[0] != s.environment or _ns_cache[1] != s.database_url:
        try:
            from sqlalchemy.engine import make_url

            db_name = make_url(s.database_url).database or "db"
        except Exception:  # noqa: BLE001 - a malformed URL only affects the key name
            db_name = "db"
        _ns_cache = (s.environment, s.database_url, f"ip:{s.environment}:{db_name}:presence")
    return _ns_cache[2]


def _process_id() -> str:
    host = (socket.gethostname() or "host").split(".")[0][:40]
    return f"{host}:{os.getpid()}:{secrets.token_hex(3)}"


class Presence:
    """Per-process presence tracker (module singleton ``presence``)."""

    def __init__(self) -> None:
        self.process_id = _process_id()
        self._conns: dict[int, dict[str, _Conn]] = {}  # user -> open sockets in this process
        self._timers: dict[int, asyncio.Task] = {}  # user -> pending grace timer
        self._http_seen: dict[int, float] = {}  # user -> their last HTTP request (monotonic) while a timer runs
        self._away: dict[int, _Away] = {}  # users this process saw leave (and not come back yet)
        # user -> when touch() last asked Redis about them (monotonic), oldest first; only the last window is kept
        self._away_checked: OrderedDict[int, float] = OrderedDict()
        # user -> (deadline, 'resuming now' notes) for their next socket here, oldest first (without Redis)
        self._notes: OrderedDict[int, tuple[float, list[dict[str, Any]]]] = OrderedDict()
        self._tasks: set[asyncio.Task] = set()  # fire-and-forget callbacks (strong references)
        self._locks: list[asyncio.Lock] = []
        self._locks_loop: Any = None
        self._shutting_down = False
        # user -> the database steps still owed to them (oldest first), retried by the sweeper task
        self._deferred: dict[int, _Deferred] = {}
        self._sweeper: asyncio.Task | None = None
        self.stats: Counter[str] = Counter()

    # ------------------------------------------------------------------ socket events (sync, called by /ws)
    def connected(self, user_id: int, *, conn_id: str, page_id: str | None, session_hash: str | None) -> None:
        """A socket of ``user_id`` opened (after the hub subscription)."""
        try:
            conns = self._conns.setdefault(user_id, {})
            first_here = not conns
            conns[conn_id] = _Conn(conn_id, clean_page_id(page_id), session_hash, time.monotonic())
            timer = self._timers.pop(user_id, None)
            if timer is not None:
                timer.cancel()  # a navigation or a reconnect inside the grace period: they never left
            self._http_seen.pop(user_id, None)
            was_away = self._away.pop(user_id, None) is not None
            if was_away:
                self.stats["returns"] += 1
            if first_here or was_away:
                self._spawn(self._arrive(user_id, resume=was_away or timer is None, lease=first_here),
                            f"ironpulse:presence-arrive:{user_id}")
            self._publish(user_id, self._take_notes(user_id))  # 'resuming now' notes an HTTP return kept for it
        except Exception:  # noqa: BLE001
            log.exception("presence: connect of user %s failed", user_id)

    def disconnected(self, user_id: int, *, conn_id: str, page_id: str | None, code: int | None) -> None:
        """A socket of ``user_id`` ended with close ``code`` (None/1006 when unknown)."""
        try:
            conns = self._conns.get(user_id)
            conn = conns.pop(conn_id, None) if conns is not None else None
            if conn is None:
                return  # never registered here (or already gone): it says nothing about the user
            if not conns:
                del self._conns[user_id]
            page = clean_page_id(page_id) or conn.page_id
            if code == PAGE_CLOSED and page:
                self.page_left(page, user_id=user_id, reason="page_closed")
            if user_id in self._conns or user_id in self._away:
                return  # another tab is still open here / they already counted as gone (logout, revocation)
            if code == SERVICE_RESTART or self._shutting_down:
                return  # a restart is not an exit: the client reconnects and the lease covers the gap
            self._start_timer(user_id)
        except Exception:  # noqa: BLE001
            log.exception("presence: disconnect of user %s failed", user_id)

    def touch(self, user_id: int) -> None:
        """Authenticated HTTP activity. Runs on every request: a few dict operations, never an await.

        * An exit pending here (grace timer) is postponed: the grace period restarts from this request, so someone
          whose socket keeps failing while they keep browsing is not taken for gone.
        * A user this process saw leave is back: their paused jobs resume.
        * Otherwise, at most once per ``AWAY_RECHECK_SECONDS`` per user, a background check whether another process
          saw them leave (the shared Redis flag) - if it did, they are back: flag cleared, paused jobs resumed."""
        try:
            if user_id in self._timers:
                self._http_seen[user_id] = time.monotonic()
            if user_id in self._away:
                del self._away[user_id]
                self.stats["returns"] += 1
                self._away_check_due(user_id)  # this return clears the shared flag too: no check for a while
                self._spawn(self._arrive(user_id, resume=True, lease=False), f"ironpulse:presence-return:{user_id}")
            elif self._away_check_due(user_id):
                self._spawn(self._check_away_elsewhere(user_id), f"ironpulse:presence-recheck:{user_id}")
        except Exception:  # noqa: BLE001
            log.exception("presence: touch of user %s failed", user_id)

    def _away_check_due(self, user_id: int) -> bool:
        """True at most once per ``AWAY_RECHECK_SECONDS`` for a user (and records the check). The map is kept oldest
        first and entries leave it as they age out, so this is O(1) and holds only the users seen in the last
        window."""
        now = time.monotonic()
        checked = self._away_checked
        while checked:
            oldest, at = next(iter(checked.items()))
            if now - at < AWAY_RECHECK_SECONDS:
                break
            del checked[oldest]
        if user_id in checked:
            return False
        checked[user_id] = now
        return True

    async def _check_away_elsewhere(self, user_id: int) -> int:
        """``touch``'s cross-process check: did another process see this user leave? Their request here says they are
        back, so ``_arrive`` deletes the shared away flag and, if it was set, resumes their paused jobs. Nothing to
        ask without Redis (presence is per process then). Returns the number of jobs resumed."""
        if await self._redis() is None:
            return 0
        self.stats["away_checks"] += 1
        return await self._arrive(user_id, resume=False, lease=False)

    def page_left(self, page_id: str, *, user_id: int, reason: str = "page_closed") -> int:
        """A tab is gone: cancel the interactive AI it started, now. Returns how many AI tickets were cancelled."""
        page = clean_page_id(page_id)
        if page is None:
            return 0
        self.stats["pages_closed"] += 1
        return self._cancel_tickets(page_id=page, user_id=user_id, kinds={OWNER_USER}, reason=reason)

    def is_away(self, user_id: int) -> bool:
        """This process saw the user leave and has not seen them since (``away_now`` also asks Redis)."""
        return user_id in self._away

    def is_connected(self, user_id: int) -> bool:
        """The user has an open socket in this process."""
        return bool(self._conns.get(user_id))

    def begin_shutdown(self) -> None:
        """Lifespan shutdown: sockets closing from now on are a restart, not people leaving. Deferred steps stop being
        retried: PAUSED rows are resumed by the users' next arrival after the restart, and a queued job of a user
        flagged away is parked by the runner."""
        self._shutting_down = True
        for task in list(self._timers.values()):
            task.cancel()
        self._timers.clear()
        self._http_seen.clear()
        if self._sweeper is not None:
            self._sweeper.cancel()
            self._sweeper = None

    # ------------------------------------------------------------------ explicit exits
    async def logout(self, user_id: int, *, session_hash: str | None, reason: str = "logout") -> None:
        """POST /auth/logout, after COMMIT: stop that session's AI work; with no other socket or live session left
        the user is gone right away (their jobs follow their exit policies)."""
        self.stats["logouts"] += 1
        await self.sessions_ended(user_id, [session_hash], reason=reason)

    def sessions_revoked(self, user_id: int, session_hashes: Iterable[str | None], *, reason: str = "signed_out") -> None:
        """Sync form of ``sessions_ended`` for after-commit callbacks (revoke-all, a browser's session replaced by a
        sign-in, account deletion)."""
        hashes = [h for h in session_hashes if h]
        if hashes:
            self._spawn(self.sessions_ended(user_id, hashes, reason=reason), f"ironpulse:presence-revoked:{user_id}")

    def sessions_ended_elsewhere(self, user_id: int, session_hashes: Iterable[str | None], *, reason: str,
                                 sockets_closed: int = 0) -> int:
        """Another process ended these sessions of ``user_id`` (its close request came through the PG bridge): their
        AI work in this process stops now too. If this process held anything of them (``sockets_closed`` here, or
        AI work it just stopped), the user's exit is judged here as well - so those sockets closing are not taken
        for a page that may come back. Returns the number of AI requests stopped here."""
        hashes = sorted({h for h in session_hashes if h})
        stopped = 0
        try:
            for h in hashes:
                stopped += self._cancel_tickets(user_id=user_id, session_hash=h, kinds={OWNER_USER}, reason=reason)
            if hashes and (stopped or sockets_closed):
                self._spawn(self.sessions_ended(user_id, hashes, reason=reason),
                            f"ironpulse:presence-ended-elsewhere:{user_id}")
        except Exception:  # noqa: BLE001
            log.exception("presence: end of sessions of user %s (from another process) failed", user_id)
        return stopped

    async def sessions_ended(self, user_id: int, session_hashes: Iterable[str | None], *, reason: str) -> bool:
        """Sessions of ``user_id`` ended: cancel their interactive AI; if nothing else of the user is left (no socket
        of another session here, no other live session) it is an exit right away. Returns True when it counted as the
        user leaving. A database that cannot answer "is another session alive?" now defers the judgement
        (``retry_deferred``)."""
        hashes = {h for h in session_hashes if h}
        try:
            return await self._judge_sessions(user_id, hashes, reason)
        except Exception as exc:  # noqa: BLE001
            kind = _db_trouble(exc)
            if kind is not None:
                self._defer(user_id, "sessions", kind, exc, reason=reason, sessions=hashes)
                return False
            log.exception("presence: end of sessions of user %s failed", user_id)
            return False

    async def _judge_sessions(self, user_id: int, hashes: set[str], reason: str) -> bool:
        """``sessions_ended`` itself; raises while the database is unavailable."""
        for h in sorted(hashes):
            self._cancel_tickets(user_id=user_id, session_hash=h, kinds={OWNER_USER}, reason=reason)
        if self._shutting_down:
            return False
        if any(c.session_hash not in hashes for c in self._conns.get(user_id, {}).values()):
            return False  # another session still has a socket here: the normal grace logic decides later
        # No lease check: the database knows every live session, whichever process serves it. With none left,
        # nothing of theirs can still be looking - a socket in another process (its lease) belongs to an ended
        # session then, e.g. a second tab of the session that just signed out.
        if await self._other_session_alive(user_id, hashes):
            return False
        timer = self._timers.pop(user_id, None)
        if timer is not None:
            timer.cancel()
        self._http_seen.pop(user_id, None)
        return (await self.on_left(user_id, reason=reason, ended_sessions=hashes)) is not None

    async def on_left(self, user_id: int, reason: str = "left", *,
                      ended_sessions: Iterable[str] | None = None) -> dict[str, Any] | None:
        """The user is gone: cancel the interactive AI they were waiting for, apply their background jobs' exit
        policies and mark them away. ``ended_sessions``: sockets of these (signed-out) sessions don't count as
        presence. Idempotent; returns what was done (None when skipped or failed). An exit whose job step the database
        could not run now stands (they are marked away) and that step is deferred (``"deferred": True``)."""
        ended = set(ended_sessions) if ended_sessions is not None else None
        async with self._lock_for(user_id):
            mark: _Away | None = None
            cancelled = 0
            try:
                if self._shutting_down:
                    return None
                conns = self._conns.get(user_id, {}).values()
                if any(ended is None or c.session_hash not in ended for c in conns):
                    return None  # a socket (of a session still signed in) is open here: they are not gone
                mark = _Away(time.time(), reason)
                self._away[user_id] = mark  # synchronously after the socket check: a connect from now on sees it
                mark.shared = await self._flag_away(user_id)
                if self._away.get(user_id) is not mark:
                    return None  # they came back while we wrote the flag (the return resumes after us)
                cancelled = self._cancel_tickets(user_id=user_id, kinds={OWNER_USER}, reason=reason)
                result = await self._exit_jobs(user_id, mark, reason, f"{cancelled} AI request(s) stopped")
                return {"tickets": cancelled, "jobs": result}
            except Exception as exc:  # noqa: BLE001
                kind = _db_trouble(exc)
                if kind is not None and mark is not None and self._away.get(user_id) is mark:
                    self._defer(user_id, "exit", kind, exc, reason=reason)  # still gone: the job step follows later
                    return {"tickets": cancelled, "jobs": None, "deferred": True}
                if kind is not None:  # they came back while it failed: nothing of theirs is to be stopped any more
                    warn_db_unavailable(log, "presence (exit)", kind, exc,
                                        f"the exit of user {user_id} was overtaken by their return - nothing deferred")
                    return None
                log.exception("presence: exit of user %s failed", user_id)
                return None

    async def _exit_jobs(self, user_id: int, mark: _Away, reason: str, note: str) -> dict[str, int]:
        """The database step of an exit: the user's jobs follow their exit policies. Raises while the database is
        unavailable (the caller defers it). An exit that a return elsewhere overtook - the shared flag vanished while
        the jobs were being paused - resumes them at once: nothing may stay paused for someone who is present."""
        from app.services import jobs

        result = await jobs.apply_exit_policy(user_id, reason=reason)
        self.stats["exits"] += 1
        log.info("user %s left (%s): %s, jobs %s", user_id, reason, note, result)
        if mark.shared and (result.get("paused") or result.get("cancelled")) and await self._flag_gone(user_id):
            # another process saw them come back (and cleared the flag) while their jobs were being paused here: its
            # resume may have run before our pause landed
            if self._away.get(user_id) is mark:
                del self._away[user_id]
            self.stats["exits_overtaken"] += 1
            try:
                resumed = await self._resume(user_id)
            except Exception as exc:  # noqa: BLE001
                kind = _db_trouble(exc)
                if kind is None:
                    raise
                self._defer(user_id, "resume", kind, exc)
                return result
            log.info("user %s came back elsewhere during the exit: %s paused job(s) resumed", user_id, resumed)
        return result

    # ------------------------------------------------------------------ returns
    async def _arrive(self, user_id: int, *, resume: bool, lease: bool) -> int:
        """First socket here (``lease``) and/or a return (``resume``): write our lease, clear the shared away flag
        and re-queue the user's paused jobs. Serialised with ``on_left`` by the per-user lock, so a return always
        lands after an exit that was already running. With a socket of theirs here, the 'resuming now' notes an
        HTTP return kept (here, or in Redis by any process - read in the same round trip) are delivered: a return
        still being processed when the socket connected kept them after ``connected`` looked."""
        async with self._lock_for(user_id):
            try:
                flagged = False
                watching = bool(self._conns.get(user_id))
                if watching:
                    self._publish(user_id, self._take_notes(user_id))
                r = await self._redis()
                if r is not None:
                    try:
                        async with r.pipeline(transaction=False) as p:
                            if lease:
                                key = self._lease_key(user_id)
                                p.zadd(key, {self.process_id: time.time() + LEASE_TTL_SECONDS})
                                p.expire(key, self._lease_key_ttl())
                            p.delete(self._away_key(user_id))
                            if watching:
                                p.lrange(self._notes_key(user_id), 0, -1)
                                p.delete(self._notes_key(user_id))
                            res = await p.execute()
                        at = 2 if lease else 0
                        flagged = bool(res[at])
                        if watching:
                            self._publish(user_id, _decode_notes(res[at + 1]))
                    except Exception as exc:  # noqa: BLE001
                        self._redis_error(exc)
                if not (resume or flagged):
                    return 0
                resumed = await self._resume(user_id)
                if resumed:
                    log.info("user %s is back: %s paused job(s) resumed", user_id, resumed)
                return resumed
            except Exception as exc:  # noqa: BLE001
                kind = _db_trouble(exc)
                if kind is not None:  # the database is unavailable: the resume follows (retry_deferred)
                    self._defer(user_id, "resume", kind, exc)
                    return 0
                log.exception("presence: return of user %s failed", user_id)
                return 0

    async def resume_if_back(self, user_id: int) -> int:
        """The job runner parked a job of ``user_id`` because they looked away. A return noticed while it wrote the
        PAUSED row resumed nothing - the row was still RUNNING - so the runner asks again once the row is written:
        back by now -> their paused jobs resume (the same idea as an exit a return overtook). Under the per-user
        lock, so never in the middle of an exit or a return. Returns the number of jobs resumed."""
        async with self._lock_for(user_id):
            try:
                if await self.away_now(user_id):
                    return 0
                resumed = await self._resume(user_id)
                if resumed:
                    self.stats["parks_overtaken"] += 1
                    log.info("user %s came back while a job of theirs was parked: %s paused job(s) resumed",
                             user_id, resumed)
                return resumed
            except Exception as exc:  # noqa: BLE001
                kind = _db_trouble(exc)
                if kind is not None:  # judged again by the retry: resumed then unless they are away
                    self._defer(user_id, "resume", kind, exc)
                    return 0
                log.exception("presence: re-check of user %s after a parked job failed", user_id)
                return 0

    async def _resume(self, user_id: int) -> int:
        """Re-queue the user's paused jobs ('Paused while you were away - resuming now', ``task.update`` with
        ``resumed: true``). With no socket of theirs here - a return noticed by an HTTP request comes before the
        page's socket - those notes are kept for their next socket."""
        from app.services import jobs

        watching = bool(self._conns.get(user_id))
        notes: list[dict[str, Any]] = []
        resumed = await jobs.resume_user_jobs(user_id, announced=notes)
        if resumed:
            self.stats["jobs_resumed"] += resumed
        if notes and not watching:
            await self._keep_notes(user_id, notes)
        return resumed

    # ------------------------------------------------------------------ database steps deferred by an overload
    def _defer(self, user_id: int, step: str, kind: str, exc: BaseException, *, reason: str | None = None,
               sessions: Iterable[str] = ()) -> None:
        """The database could not run ``step`` ("resume" / "exit" / "sessions") for ``user_id`` now (``kind``: pool /
        statement timeout, refused or lost connection). Never dropped: remembered for the sweeper (one warning a minute
        per step, no traceback). The newer of a resume and an exit wins - they undo each other."""
        entry = self._deferred.setdefault(user_id, _Deferred())
        if step == "resume":
            entry.resume, entry.exit = True, None
        elif step == "exit":
            entry.exit, entry.resume = (reason or "left")[:80], False
        else:
            entry.sessions.update(h for h in sessions if h)
            entry.sessions_reason = reason or entry.sessions_reason
        self.stats["deferred"] += 1
        warn_db_unavailable(log, f"presence ({step})", kind, exc,
                            f"{_STEPS.get(step, step)} of user {user_id} is deferred, retried in the background")
        self._arm_sweeper()

    def _arm_sweeper(self) -> None:
        """Start the sweeper unless it is running already (it ends once nothing is deferred)."""
        if not self._deferred or self._shutting_down or (self._sweeper is not None and not self._sweeper.done()):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._sweeper = loop.create_task(self._sweep(), name="ironpulse:presence-deferred")

    async def _sweep(self) -> None:
        """Retry the deferred steps: first after about RETRY_FIRST_SECONDS, doubling up to RETRY_MAX_SECONDS while the
        database stays unavailable (jittered, so the worker processes that met the same overload do not all come back
        in the same second); back to the start after a round that got through."""
        failures = 0
        try:
            while self._deferred and not self._shutting_down:
                await asyncio.sleep(backoff_delay(failures + 1, RETRY_FIRST_SECONDS, RETRY_MAX_SECONDS))
                failures = 0 if await self.retry_deferred() else failures + 1
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the steps stay remembered; the next deferral or lease refresh re-arms
            log.exception("presence: the deferred-step sweeper failed")

    async def retry_deferred(self) -> bool:
        """One round over the deferred steps, oldest first, of at most RETRY_ROUND_MAX users (the sweeper's round;
        tests call it directly). Stops at the first step the database still cannot run - that one and the rest wait
        for the next round, so an overloaded database gets one probe a round, not one per user. True when the round
        got through."""
        for user_id in list(self._deferred)[:RETRY_ROUND_MAX]:
            if self._shutting_down:
                return False
            entry = self._deferred.pop(user_id, None)
            if entry is None:
                continue
            try:
                await self._run_deferred(user_id, entry)
            except Exception as exc:  # noqa: BLE001
                kind = _db_trouble(exc)
                if kind is None:  # a bug, not the database: retrying would only repeat the traceback
                    log.exception("presence: deferred step of user %s failed", user_id)
                    continue
                newer = self._deferred.pop(user_id, None)
                if newer is not None:  # deferred again while this ran: the newer wish wins
                    newer.merge_older(entry)
                    entry = newer
                if entry:
                    self._deferred[user_id] = entry
                self.stats["deferred_retry_failed"] += 1
                warn_db_unavailable(log, "presence (retry)", kind, exc,
                                    f"the deferred steps of {len(self._deferred)} user(s) wait for the next round")
                return False
            self.stats["deferred_done"] += 1
        return True

    async def _run_deferred(self, user_id: int, entry: _Deferred) -> None:
        """Run what is owed to ``user_id``, each step judged on the state of NOW. Raises while the database is still
        unavailable; ``entry`` then keeps only what is left."""
        if entry.sessions:
            await self._judge_sessions(user_id, set(entry.sessions), entry.sessions_reason)
            entry.sessions.clear()
        if entry.exit is not None:
            await self._exit_again(user_id, entry.exit)
            entry.exit = None
        if entry.resume:
            await self._resume_again(user_id)
            entry.resume = False

    async def _exit_again(self, user_id: int, reason: str) -> None:
        """A deferred exit: only while the user is still away (a return since - here, or elsewhere through the shared
        flag - undid it)."""
        async with self._lock_for(user_id):
            mark = self._away.get(user_id)
            if mark is None or self._shutting_down or not await self.away_now(user_id):
                return
            await self._exit_jobs(user_id, mark, reason, "exit policies applied on a retry")

    async def _resume_again(self, user_id: int) -> None:
        """A deferred resume: unless the user is away again (an exit since then owns their jobs)."""
        async with self._lock_for(user_id):
            if self._shutting_down or await self.away_now(user_id):
                return
            resumed = await self._resume(user_id)
            if resumed:
                log.info("user %s is back: %s paused job(s) resumed on a retry", user_id, resumed)

    # ------------------------------------------------------------------ 'resuming now' notes for the next socket
    async def _keep_notes(self, user_id: int, notes: list[dict[str, Any]]) -> None:
        """Keep the notes for the user's next socket for ``NOTE_KEEP_SECONDS``: in Redis when there is one (that
        socket may land in another process), else here."""
        r = await self._redis()
        if r is not None:
            try:
                key = self._notes_key(user_id)
                async with r.pipeline(transaction=False) as p:
                    p.rpush(key, *(json.dumps(n, default=str) for n in notes))
                    p.ltrim(key, -_NOTES_MAX, -1)
                    p.expire(key, max(1, int(NOTE_KEEP_SECONDS)))
                    await p.execute()
                self.stats["notes_kept"] += len(notes)
                return
            except Exception as exc:  # noqa: BLE001
                self._redis_error(exc)
        now = time.monotonic()
        kept = self._notes.pop(user_id, None)
        earlier = kept[1] if kept is not None and kept[0] > now else []
        self._notes[user_id] = (now + NOTE_KEEP_SECONDS, (earlier + notes)[-_NOTES_MAX:])
        self.stats["notes_kept"] += len(notes)
        while self._notes:  # entries leave as they expire (oldest first): only users of the last minute are kept
            oldest, (deadline, _) = next(iter(self._notes.items()))
            if deadline > now:
                break
            del self._notes[oldest]

    def _take_notes(self, user_id: int) -> list[dict[str, Any]]:
        """The notes kept here for the user's next socket (once; expired ones are dropped)."""
        if not self._notes:
            return []
        kept = self._notes.pop(user_id, None)
        if kept is None or kept[0] <= time.monotonic():
            return []
        return kept[1]

    def _publish(self, user_id: int, notes: list[dict[str, Any]]) -> None:
        if not notes:
            return
        try:
            from app.services.realtime import hub

            for note in notes:
                hub.publish_nowait([user_id], note)
            self.stats["notes_delivered"] += len(notes)
        except Exception:  # noqa: BLE001 - a lost note never matters more than the socket
            log.debug("presence: notes for user %s not delivered", user_id, exc_info=True)

    # ------------------------------------------------------------------ cross-process (Redis)
    async def refresh_leases(self) -> int:
        """Worker loop (every ``LEASE_REFRESH_SECONDS``): renew this process's lease for every user with a socket
        here, resume users another process declared away while they are connected here, and forget away marks whose
        shared flag another process cleared (they came back there). Returns the number of leases renewed. Also re-arms
        the sweeper of the deferred database steps, should it ever have stopped with work left."""
        self._arm_sweeper()
        # a user this process marked away (logout) may still have a socket of the revoked session closing: no lease
        users = [uid for uid in self._conns if uid not in self._away]
        shared_away = [uid for uid, mark in self._away.items() if mark.shared]
        if not users and not shared_away:
            return 0
        r = await self._redis()
        if r is None:
            return 0
        now = time.time()
        try:
            async with r.pipeline(transaction=False) as p:
                for uid in users:
                    key = self._lease_key(uid)
                    p.zremrangebyscore(key, "-inf", now)
                    p.zadd(key, {self.process_id: now + LEASE_TTL_SECONDS})
                    p.expire(key, self._lease_key_ttl())
                    p.exists(self._away_key(uid))
                for uid in shared_away:
                    p.exists(self._away_key(uid))
                res = await p.execute()
        except Exception as exc:  # noqa: BLE001
            self._redis_error(exc)
            return 0
        for i, uid in enumerate(users):
            if res[i * 4 + 3] and uid in self._conns and uid not in self._away:
                self.stats["away_while_connected"] += 1  # another process saw them leave, but they are here
                self._spawn(self._arrive(uid, resume=False, lease=False), f"ironpulse:presence-heal:{uid}")
        base = len(users) * 4
        for j, uid in enumerate(shared_away):
            mark = self._away.get(uid)
            if not res[base + j] and mark is not None and mark.shared and uid not in self._conns:
                del self._away[uid]  # they came back through another process
        return len(users)

    async def away_now(self, user_id: int) -> bool:
        """Is the user away right now - the shared view used by the job runner before it starts their job. With
        Redis the flag decides (another process may have seen them leave or come back); without it this process's
        own view (``_local_view``)."""
        mark = self._away.get(user_id)
        if mark is None and self._conns.get(user_id):
            return False  # a socket here right now (a lingering socket of a signed-out session does not count)
        r = await self._redis()
        if r is None:
            return await self._local_view(user_id, mark)
        try:
            flagged = bool(await r.exists(self._away_key(user_id)))
        except Exception as exc:  # noqa: BLE001
            self._redis_error(exc)
            return await self._local_view(user_id, mark)
        if flagged:
            return True
        if mark is not None and mark.shared:
            if self._away.get(user_id) is mark:
                del self._away[user_id]  # the flag was cleared elsewhere: they are back
            return False
        return mark is not None

    async def _local_view(self, user_id: int, mark: _Away | None) -> bool:
        """Away by this process's own mark (no Redis, or it failed). A single process sees every return, so its mark
        is the truth. With several processes the user may have come back through another one, which this one cannot
        see: then only a user signed in nowhere is certainly away (fail safe - a present user's job is never parked
        on a stale mark)."""
        if mark is None:
            return False
        if not self.multi_process():
            return True
        return not await self._other_session_alive(user_id, set())

    async def _held_elsewhere(self, user_id: int, *, release: bool = False) -> bool | None:
        """Does another process hold an unexpired lease for the user? ``release`` also drops this process's own
        (expired) member. None when this process cannot tell: no Redis, or it failed."""
        r = await self._redis()
        if r is None:
            return None
        key = self._lease_key(user_id)
        now = time.time()
        try:
            async with r.pipeline(transaction=False) as p:
                p.zremrangebyscore(key, "-inf", now)
                p.zrangebyscore(key, f"({now}", "+inf")
                if release:
                    p.zrem(key, self.process_id)
                res = await p.execute()
        except Exception as exc:  # noqa: BLE001
            self._redis_error(exc)
            return None
        return any(member != self.process_id for member in (res[1] or ()))

    @staticmethod
    def multi_process() -> bool:
        """Several app processes share the users (``REALTIME_PG_BRIDGE``, which ``manage.py run --workers N`` sets):
        one process's sockets are then not the whole picture."""
        return bool(getattr(get_settings(), "realtime_pg_bridge", False))

    async def startup_check(self) -> bool:
        """Startup of a process that shares its users with others (the realtime PG bridge starts): can presence see
        the other processes? Without Redis it cannot, and then fails safe - a user whose sockets closed here is never
        taken for gone after the grace period, because their next page may be connected to another process; only
        sign-out and revoked sessions (which the database decides) end their AI work. Logs a warning saying so.
        Returns True when presence is shared, or when there is only this process."""
        if not self.multi_process():
            return True
        r = await self._redis()
        shared = False
        if r is not None:
            try:
                shared = bool(await r.ping())
            except Exception as exc:  # noqa: BLE001
                self._redis_error(exc)
        if not shared:
            why = "is unreachable" if get_settings().redis_url else "is not set"
            log.warning(
                "presence: several worker processes share the users (REALTIME_PG_BRIDGE) but Redis (REDIS_URL) %s - "
                "a process cannot see the others' sockets, so closing every tab no longer counts as leaving: AI work "
                "is only stopped by sign-out or revoked sessions, and a page's own AI work when it closes. Set "
                "REDIS_URL (manage.py configure) for the full stop-when-you-leave behaviour.", why)
        return shared

    async def _flag_away(self, user_id: int) -> bool:
        r = await self._redis()
        if r is None:
            return False
        try:
            await r.set(self._away_key(user_id), str(int(time.time())), ex=AWAY_TTL_SECONDS)
            return True
        except Exception as exc:  # noqa: BLE001
            self._redis_error(exc)
            return False

    async def _flag_gone(self, user_id: int) -> bool:
        """Has the shared away flag (which this process set) been deleted - a return seen by another process? False
        when Redis can't tell."""
        r = await self._redis()
        if r is None:
            return False
        try:
            return not await r.exists(self._away_key(user_id))
        except Exception as exc:  # noqa: BLE001
            self._redis_error(exc)
            return False

    async def _redis(self) -> Any | None:
        """The shared Redis client, or None (not configured / recently unreachable)."""
        try:
            from app.security.sessions import get_redis

            return await get_redis()
        except Exception:  # noqa: BLE001
            return None

    def _redis_error(self, exc: Exception) -> None:
        self.stats["redis_errors"] += 1
        if self.stats["redis_errors"] % 50 == 1:  # don't flood the log during an outage
            log.warning("presence: Redis call failed (%s: %s) - judging by this process's sockets",
                        exc.__class__.__name__, exc)

    def _lease_key(self, user_id: int) -> str:
        return f"{redis_namespace()}:{user_id}"

    def _away_key(self, user_id: int) -> str:
        return f"{redis_namespace()}:away:{user_id}"

    def _notes_key(self, user_id: int) -> str:
        return f"{redis_namespace()}:notes:{user_id}"

    @staticmethod
    def _lease_key_ttl() -> int:
        return int(LEASE_TTL_SECONDS + max(0, int(get_settings().presence_grace_seconds)) + 60)

    # ------------------------------------------------------------------ plumbing
    def _start_timer(self, user_id: int) -> None:
        loop = asyncio.get_running_loop()
        old = self._timers.pop(user_id, None)
        if old is not None:
            old.cancel()
        self._http_seen.pop(user_id, None)  # the grace period starts now
        self._timers[user_id] = loop.create_task(self._grace(user_id), name=f"ironpulse:presence-grace:{user_id}")

    async def _grace(self, user_id: int) -> None:
        grace = max(0.0, float(get_settings().presence_grace_seconds))
        try:
            await asyncio.sleep(grace)
            postponed = False
            # HTTP requests during the grace period (touch) restart it: it ends `grace` after their last request
            while (seen := self._http_seen.get(user_id)) is not None and (wait := seen + grace - time.monotonic()) > 0:
                if not postponed:
                    postponed = True
                    self.stats["kept_by_http"] += 1
                await asyncio.sleep(wait)
        except asyncio.CancelledError:
            return  # they came back (or the server is stopping)
        me = asyncio.current_task()
        if self._timers.get(user_id) is not me:
            return  # superseded by a newer timer
        del self._timers[user_id]  # from here on a reconnect no longer cancels us: its resume runs after the exit
        self._http_seen.pop(user_id, None)
        if me is not None:
            self._tasks.add(me)
            me.add_done_callback(self._tasks.discard)
        try:
            if self._shutting_down or self._conns.get(user_id) or user_id in self._away:
                return
            held = await self._held_elsewhere(user_id, release=True)
            if held:
                self.stats["kept_by_lease"] += 1
                log.debug("user %s still has a socket in another process", user_id)
                return
            if held is None and self.multi_process():
                # several processes and no view of the others (no Redis, or it is down): their next page may well be
                # connected to another one - a navigation is not an exit (fail safe; sign-out still is)
                self.stats["kept_unseen"] += 1
                log.debug("user %s: no socket here, other processes unseen - not treated as gone", user_id)
                return
            await self.on_left(user_id, reason="left")
        except Exception:  # noqa: BLE001
            log.exception("presence: grace timer of user %s failed", user_id)

    async def _other_session_alive(self, user_id: int, ended: set[str]) -> bool:
        from sqlalchemy import select

        from app.core.utils import utcnow
        from app.db import get_sessionmaker
        from app.models import UserSession

        stmt = select(UserSession.id).where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None),
                                            UserSession.expires_at > utcnow())
        if ended:
            stmt = stmt.where(UserSession.id.not_in(sorted(ended)))
        async with get_sessionmaker()() as db:
            return (await db.scalar(stmt.limit(1))) is not None

    @staticmethod
    def _cancel_tickets(**filters: Any) -> int:
        """``scheduler.cancel(**filters)`` - imported lazily (the AI queue imports half the app)."""
        try:
            from app.ai.scheduler import scheduler

            return int(scheduler.cancel(**filters) or 0)
        except Exception:  # noqa: BLE001
            log.exception("presence: AI queue cancel %s failed", filters)
            return 0

    def _lock_for(self, user_id: int) -> asyncio.Lock:
        """Striped per-user lock: an exit and a return of the same user never interleave."""
        loop = asyncio.get_running_loop()
        if self._locks_loop is not loop or not self._locks:
            self._locks = [asyncio.Lock() for _ in range(_LOCK_STRIPES)]
            self._locks_loop = loop
        return self._locks[user_id % _LOCK_STRIPES]

    def _spawn(self, coro: Coroutine[Any, Any, Any], name: str | None = None) -> asyncio.Task | None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            coro.close()
            return None
        task = loop.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def drain(self, timeout: float = 5.0) -> None:
        """Wait for the callbacks already scheduled (tests, shutdown). Grace timers are not waited for."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        current = asyncio.current_task()
        while True:
            pending = [t for t in self._tasks if not t.done() and t is not current]
            remaining = deadline - loop.time()
            if not pending or remaining <= 0:
                return
            await asyncio.wait(pending, timeout=remaining)

    def snapshot(self) -> dict[str, Any]:
        return {
            "process": self.process_id,
            "users": len(self._conns),
            "sockets": sum(len(c) for c in self._conns.values()),
            "pages": len({c.page_id for conns in self._conns.values() for c in conns.values() if c.page_id}),
            "grace_timers": len(self._timers),
            "away": len(self._away),
            "deferred": len(self._deferred),
            "shutting_down": self._shutting_down,
            "stats": dict(self.stats),
        }

    async def reset(self) -> None:
        """Tests: forget every socket, timer, away mark and deferred step, plus this environment + database's Redis
        keys."""
        self._shutting_down = False
        sweeper, self._sweeper = self._sweeper, None
        self._deferred.clear()
        pending = [t for t in (*self._timers.values(), *self._tasks, *([sweeper] if sweeper else [])) if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            try:
                await asyncio.wait(pending, timeout=5)
            except Exception:  # noqa: BLE001 - tasks of another (closed) loop
                pass
        self._timers.clear()
        self._tasks.clear()
        self._conns.clear()
        self._away.clear()
        self._http_seen.clear()
        self._away_checked.clear()
        self._notes.clear()
        self._locks, self._locks_loop = [], None
        self.stats.clear()
        r = await self._redis()
        if r is not None:
            try:
                keys = [k async for k in r.scan_iter(match=f"{redis_namespace()}:*", count=500)]
                if keys:
                    await r.delete(*keys)
            except Exception as exc:  # noqa: BLE001
                log.warning("presence reset: Redis cleanup failed (%s)", exc.__class__.__name__)


def _decode_notes(raw: Any) -> list[dict[str, Any]]:
    """Notes kept in Redis (JSON strings) -> ``task.update`` events; anything else is dropped."""
    notes: list[dict[str, Any]] = []
    for item in raw or ():
        try:
            note = json.loads(item)
        except (TypeError, ValueError):
            continue
        if isinstance(note, dict) and note.get("type") == "task.update":
            notes.append(note)
    return notes


presence = Presence()
register_reset(presence.reset, name="presence")
