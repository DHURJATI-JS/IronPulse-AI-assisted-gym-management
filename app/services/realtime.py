"""Real-time event hub (live chat, notifications, AI status, task updates) delivered over the ``/ws`` WebSocket.

* ``hub.subscribe(user_id, session_hash=..., page_id=..., conn_id=...)`` -> asyncio.Queue receiving dict events for that
  user. One queue per open socket; the optional metadata ties the socket to a session and a browser tab.
* ``publish_after_commit(db, user_ids, event)`` -> delivered only if the DB transaction commits.
* ``hub.close_sessions(session_hashes)`` -> closes every socket bound to those sessions (after logout / revocation).
  Close requests travel through the socket queues as private ``CloseRequest`` objects: control messages are never
  sent to a browser. A queue is a drop-oldest buffer for slow clients, so a closing socket's queue is emptied and
  then holds only its close request - nothing is delivered to it any more, so nothing can push the request out.
* ``hub.cancel_ai(user_id=..., reason=..., policies=...)`` -> stops a user's AI requests in every process.
* ``call_after_commit(db, fn)`` -> run ``fn()`` once the transaction really COMMITs (not when a SAVEPOINT is
  released); dropped on rollback.
* With ``REALTIME_PG_BRIDGE=true`` events, session close requests and AI cancels also travel through PostgreSQL
  LISTEN/NOTIFY so several uvicorn worker processes share one chat (off by default: one process needs no bridge). A
  process told that sessions ended elsewhere also stops their AI work and judges the user's exit
  (``presence.sessions_ended_elsewhere``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from collections import defaultdict
from collections.abc import Callable, Coroutine, Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.state import register_reset

log = logging.getLogger("ironpulse.realtime")

CHANNEL = "ironpulse_rt"
CONTROL_PREFIX = "_ctl"  # event types starting with this are internal and never forwarded to a browser
_PID = os.getpid()


@dataclass(frozen=True, slots=True)
class CloseRequest:
    """Queue item asking a socket's sender to close the connection (internal; never serialised to the browser)."""

    code: int = 4401
    reason: str = ""


@dataclass(slots=True)
class Connection:
    """One open /ws socket."""

    user_id: int
    conn_id: str
    queue: asyncio.Queue
    session_hash: str | None = None
    page_id: str | None = None
    opened_at: float = field(default_factory=time.monotonic)
    close_code: int | None = None  # set once the server asked this socket to close


def new_conn_id() -> str:
    return secrets.token_urlsafe(9)


def is_control_event(evt: Any) -> bool:
    """Internal hub messages that must never reach a browser."""
    if isinstance(evt, CloseRequest):
        return True
    return isinstance(evt, dict) and str(evt.get("type", "")).startswith(CONTROL_PREFIX)


def _put(q: asyncio.Queue, item: Any) -> None:
    try:
        q.put_nowait(item)
    except asyncio.QueueFull:  # slow consumer: drop oldest
        try:
            q.get_nowait()
            q.put_nowait(item)
        except Exception:
            pass


def _discard_pending(q: asyncio.Queue) -> None:
    while True:
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            return


def _user_id(value: Any) -> int | None:
    """A user id from a bridge message (JSON): a positive int, never a bool."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


class Hub:
    def __init__(self) -> None:
        self._queues: dict[int, set[asyncio.Queue]] = defaultdict(set)
        self._conns: dict[asyncio.Queue, Connection] = {}
        self._bridge_conn: Any = None
        self._bridge_lock: asyncio.Lock | None = None
        self._bridge_loop: Any = None
        self._tasks: set[asyncio.Task] = set()

    # ---------------------------------------------------------------- subscriptions
    def subscribe(self, user_id: int, *, session_hash: str | None = None, page_id: str | None = None,
                  conn_id: str | None = None) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._queues[user_id].add(q)
        self._conns[q] = Connection(user_id, conn_id or new_conn_id(), q, session_hash, page_id)
        return q

    def unsubscribe(self, user_id: int, q: asyncio.Queue, *, conn_id: str | None = None) -> None:
        self._conns.pop(q, None)
        qs = self._queues.get(user_id)
        if qs:
            qs.discard(q)
            if not qs:
                self._queues.pop(user_id, None)

    def is_online(self, user_id: int) -> bool:
        """An open /ws socket of the user in THIS process (``online_anywhere`` also sees the other workers)."""
        return bool(self._queues.get(user_id))

    async def online_anywhere(self, user_ids: Iterable[int]) -> set[int]:
        """Which of ``user_ids`` hold an open /ws socket: here (``is_online``) or - with several worker processes
        sharing the users (``REALTIME_PG_BRIDGE``: live events reach every process) - in another one, by presence's
        cross-process view: an unexpired Redis lease of another process (``presence:<uid>``), all users in ONE
        pipelined round trip, and only for those not connected here. Without Redis (or while it fails), or with a
        single process, this process's sockets decide - the safe side for chat: a notification too many, never one
        missing. A lease outlives its process's last socket by up to the presence grace period, as in presence's own
        view of who is still here."""
        wanted = sorted({int(u) for u in user_ids if u})
        online = {u for u in wanted if self.is_online(u)}
        elsewhere = [u for u in wanted if u not in online]
        if not elsewhere:
            return online
        from app.services.presence import presence  # lazily: presence imports this module

        if not presence.multi_process():
            return online  # one process: its sockets are all there is (and another process's events never reach it)
        r = await presence._redis()
        if r is None:
            return online
        now = time.time()
        try:
            async with r.pipeline(transaction=False) as p:
                for uid in elsewhere:
                    p.zrangebyscore(presence._lease_key(uid), f"({now}", "+inf")
                leases = await p.execute()
        except Exception as exc:  # noqa: BLE001 - Redis trouble: judge by this process's sockets
            presence._redis_error(exc)
            return online
        for uid, holders in zip(elsewhere, leases, strict=False):
            if any(holder != presence.process_id for holder in (holders or ())):
                online.add(uid)
        return online

    def online_users(self) -> set[int]:
        return set(self._queues)

    def connection(self, q: asyncio.Queue) -> Connection | None:
        return self._conns.get(q)

    def connections(self, user_id: int) -> list[Connection]:
        return [self._conns[q] for q in self._queues.get(user_id, ()) if q in self._conns]

    def connection_count(self, user_id: int) -> int:
        return len(self._queues.get(user_id, ()))

    # ---------------------------------------------------------------- control
    def close_sessions(self, session_hashes: Iterable[str | None], *, code: int = 4401, reason: str = "signed out",
                       user_id: int | None = None, exit_reason: str | None = None) -> int:
        """Close every socket bound to these sessions - here, and through the PG bridge in the other processes.
        ``user_id`` / ``exit_reason`` (whose sessions, why: "logout", "signed_out") let the other processes stop the
        sessions' AI work too (``presence.sessions_ended_elsewhere``); the caller handles this process. Returns how
        many local sockets were asked to close."""
        hashes = {h for h in session_hashes if h}
        if not hashes:
            return 0
        closed = self._close_local(hashes, code, reason)
        if self._bridge_conn is not None:
            ctl: dict[str, Any] = {"op": "close_sessions", "sessions": sorted(hashes), "code": code, "reason": reason}
            if _user_id(user_id) is not None:
                ctl["user_id"] = user_id
            if exit_reason:
                ctl["exit"] = str(exit_reason)[:40]
            self._spawn(self._notify({"pid": _PID, "ctl": ctl}))
        return len(closed)

    def _close_local(self, hashes: set[str], code: int, reason: str) -> list[Connection]:
        """Ask this process's sockets of these sessions to close. Events still waiting for them are dropped (they
        belong to a session that is over) and nothing more is delivered to them, so the close request - the only
        item left in the drop-oldest queue - can never be pushed out by later events for the account."""
        closing = []
        for conn in list(self._conns.values()):
            if conn.session_hash in hashes and conn.close_code is None:
                conn.close_code = code
                _discard_pending(conn.queue)
                _put(conn.queue, CloseRequest(code, reason))
                closing.append(conn)
        return closing

    def _on_control(self, ctl: Any) -> None:
        """A control message another process sent through the PG bridge."""
        if not isinstance(ctl, dict):
            return
        op = ctl.get("op")
        if op == "close_sessions":
            hashes = {str(h) for h in ctl.get("sessions") or () if h}
            try:
                code = int(ctl.get("code") or 4401)
            except (TypeError, ValueError):
                code = 4401
            closed = self._close_local(hashes, code, str(ctl.get("reason") or "signed out")[:100])
            users: dict[int, int] = {}
            for conn in closed:
                users[conn.user_id] = users.get(conn.user_id, 0) + 1
            named = _user_id(ctl.get("user_id"))
            if named is not None:
                users.setdefault(named, 0)
            if hashes and users:
                self._end_sessions_here(users, hashes, str(ctl.get("exit") or "signed_out")[:40])
        elif op == "cancel_ai":
            self._cancel_ai_here(ctl.get("user_id"), ctl.get("reason"), ctl.get("policies"))

    @staticmethod
    def _end_sessions_here(users: dict[int, int], hashes: set[str], reason: str) -> None:
        """Sessions ended in another process: their AI work here stops too and, if this process held something of
        the user, their exit is judged here as well (app/services/presence.py)."""
        try:
            from app.services.presence import presence

            for uid, sockets in users.items():
                presence.sessions_ended_elsewhere(uid, hashes, reason=reason, sockets_closed=sockets)
        except Exception:  # noqa: BLE001 - the sockets are closed either way
            log.exception("realtime: ending sessions signed out elsewhere failed")

    # ---------------------------------------------------------------- AI work in every process
    def cancel_ai(self, *, user_id: int, reason: str, policies: Iterable[str] | None = None) -> int:
        """Stop a user's queued and running AI requests here (``scheduler.cancel``) and, through the PG bridge, in the
        other processes - e.g. account deletion with every policy (``policies={'keep', 'pause', 'cancel'}``; without
        ``policies`` 'keep' work is left alone, as in the scheduler). Returns how many were stopped here."""
        names = sorted({str(p) for p in policies}) if policies else None
        stopped = self._cancel_ai_here(user_id, reason, names)
        if self._bridge_conn is not None and _user_id(user_id) is not None:
            self._spawn(self._notify({"pid": _PID, "ctl": {"op": "cancel_ai", "user_id": user_id,
                                                           "reason": str(reason or "cancelled")[:40],
                                                           "policies": names}}))
        return stopped

    @staticmethod
    def _cancel_ai_here(user_id: Any, reason: Any, policies: Any) -> int:
        uid = _user_id(user_id)
        if uid is None:
            return 0
        try:
            from app.ai.policy import EXIT_CANCEL, EXIT_KEEP, EXIT_PAUSE
            from app.ai.scheduler import scheduler

            filters: dict[str, Any] = {"user_id": uid, "reason": str(reason or "cancelled")[:40]}
            if policies:
                names = set(policies) if isinstance(policies, (list, tuple, set, frozenset)) else None
                if not names or not names <= {EXIT_KEEP, EXIT_PAUSE, EXIT_CANCEL}:
                    return 0  # malformed: never widen a cancel
                filters["policies"] = names
            return int(scheduler.cancel(**filters) or 0)
        except Exception:  # noqa: BLE001
            log.exception("realtime: AI cancel for user %s failed", uid)
            return 0

    # ---------------------------------------------------------------- publishing
    def _deliver_local(self, user_ids: Iterable[int], evt: dict[str, Any]) -> None:
        for uid in set(user_ids):
            for q in list(self._queues.get(uid, ())):
                conn = self._conns.get(q)
                if conn is not None and conn.close_code is not None:
                    continue  # closing (signed out): nothing more for it - nor anything that could evict its close
                _put(q, evt)

    async def publish(self, user_ids: Iterable[int], evt: dict[str, Any]) -> None:
        ids = [int(u) for u in set(user_ids) if u is not None]
        if not ids:
            return
        self._deliver_local(ids, evt)
        if self._bridge_conn is not None:
            payload = json.dumps({"pid": _PID, "users": ids, "event": evt}, default=str)
            if len(payload.encode()) > 7500:  # NOTIFY payload limit: send a light reference instead
                payload = json.dumps({"pid": _PID, "users": ids, "event": {"type": evt.get("type", "") + ".ref",
                                                                             "id": evt.get("id"), "thread_id": evt.get("thread_id")}})
            await self._notify(payload)

    def publish_nowait(self, user_ids: Iterable[int], evt: dict[str, Any]) -> None:
        self._spawn(self.publish(user_ids, evt))

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            coro.close()
            return
        task = loop.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ---------------------------------------------------------------- multi-process bridge
    async def _notify(self, payload: str | dict[str, Any]) -> None:
        conn = self._bridge_conn
        if conn is None:
            return
        data = payload if isinstance(payload, str) else json.dumps(payload, default=str)
        loop = asyncio.get_running_loop()
        if self._bridge_lock is None or self._bridge_loop is not loop:
            self._bridge_lock, self._bridge_loop = asyncio.Lock(), loop
        try:
            async with self._bridge_lock:  # one asyncpg connection: one operation at a time
                await conn.execute("SELECT pg_notify($1, $2)", CHANNEL, data)
        except Exception as exc:
            log.warning("realtime bridge publish failed: %s", exc)

    async def start_bridge(self, dsn: str) -> None:
        import asyncpg

        dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")
        self._bridge_conn = await asyncpg.connect(dsn)

        def _on_notify(conn: Any, pid: int, channel: str, payload: str) -> None:
            try:
                data = json.loads(payload)
                if data.get("pid") == _PID:
                    return
                if "ctl" in data:
                    self._on_control(data.get("ctl"))
                    return
                self._deliver_local(data.get("users", []), data.get("event", {}))
            except Exception:
                log.exception("bad realtime payload")

        await self._bridge_conn.add_listener(CHANNEL, _on_notify)
        log.info("realtime: PostgreSQL LISTEN/NOTIFY bridge active")
        try:  # several processes: can presence see the others (Redis)? It says so in the log when it cannot
            from app.services.presence import presence

            await presence.startup_check()
        except Exception:  # noqa: BLE001 - a log line must never stop the start
            log.exception("presence startup check failed")

    async def stop_bridge(self) -> None:
        if self._bridge_conn is not None:
            try:
                await self._bridge_conn.close()
            finally:
                self._bridge_conn = None

    def reset(self) -> None:
        """Tests: forget every subscription (ids restart after TRUNCATE, so a leftover queue would make a later
        test's user look online)."""
        self._queues.clear()
        self._conns.clear()


hub = Hub()
register_reset(hub.reset, name="realtime")

_KEY = "ip_after_commit_events"
_CALLBACKS_KEY = "ip_after_commit_callbacks"


def publish_after_commit(db: Any, user_ids: Iterable[int], evt: dict[str, Any]) -> None:
    """Queue an event on the session; it is published only after a successful COMMIT."""
    db.info.setdefault(_KEY, []).append((list(user_ids), evt))


def call_after_commit(db: Any, fn: Callable[[], Any]) -> None:
    """Run ``fn()`` (synchronous - schedule async work yourself) right after the transaction COMMITs, e.g. closing
    the sockets of sessions it revoked. Releasing a SAVEPOINT does not count as the commit; rolling back drops
    ``fn`` (a SAVEPOINT rollback only drops what was registered inside it)."""
    sync = getattr(db, "sync_session", db)
    try:
        trans = sync.get_nested_transaction() or sync.get_transaction()
    except Exception:  # noqa: BLE001
        trans = None
    db.info.setdefault(_CALLBACKS_KEY, []).append((trans, fn))


def _within(trans: Any, ancestor: Any) -> bool:
    while trans is not None:
        if trans is ancestor:
            return True
        trans = getattr(trans, "parent", None)
    return False


@event.listens_for(Session, "after_commit")
def _after_commit(session: Session) -> None:
    pending = session.info.pop(_KEY, None)
    if pending:
        for user_ids, evt in pending:
            hub.publish_nowait(user_ids, evt)
    if session.info.get(_CALLBACKS_KEY) and session.get_nested_transaction() is None:  # the real COMMIT
        for _trans, fn in session.info.pop(_CALLBACKS_KEY, []):
            try:
                fn()
            except Exception:  # noqa: BLE001 - never break a commit
                log.exception("after-commit callback failed")


@event.listens_for(Session, "after_soft_rollback")
def _after_rollback(session: Session, previous_transaction: Any) -> None:
    session.info.pop(_KEY, None)
    callbacks = session.info.get(_CALLBACKS_KEY)
    if not callbacks:
        return
    if not getattr(previous_transaction, "nested", False):
        session.info.pop(_CALLBACKS_KEY, None)
        return
    # a SAVEPOINT rolled back: forget only what was registered inside it
    session.info[_CALLBACKS_KEY] = [(t, fn) for t, fn in callbacks if not _within(t, previous_transaction)]
