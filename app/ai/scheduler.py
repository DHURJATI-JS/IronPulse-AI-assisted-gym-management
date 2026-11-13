"""Fair AI queue with cancellation (docs/ai-guard-and-queue.md section 4).

Every AI call takes a ``Ticket`` from ``scheduler.acquire(owner, feature)`` before it may talk to a provider and
gives it back with ``scheduler.release(ticket, outcome=...)`` (app/ai/client.py does both). The queue:

* **bounds concurrency per lane** (per process): ``llm`` runs ``settings.ai_queue_concurrency`` calls at once and
  ``tts`` runs ``settings.ai_tts_concurrency``. Settings are read on every call, so they can be tuned live (and
  monkeypatched in tests).
* **is fair.** Inside a lane the best priority goes first (REALTIME < INTERACTIVE < BACKGROUND; BACKGROUND work that
  has waited longer than ``AGEING_SECONDS`` competes as INTERACTIVE, so it never starves). Inside a priority the
  *least recently served* bucket goes first - round-robin between users - and a bucket is FIFO. A bucket is one
  signed-in user's requests, or one user's background jobs, or the system's work (``(kind, user_id)``). A user runs
  at most ``policy.MAX_RUNNING_PER_USER`` requests per lane at once: their further tickets wait while other users'
  tickets start.
* **refuses early** (user requests only - jobs and system work always wait their turn): a user's queued + running
  requests in the lane reaching ``settings.ai_queue_max_per_user`` -> ``AIRejected('too_many')``; the lane's queue
  reaching ``settings.ai_queue_max_total`` -> ``AIRejected('busy')``. Safety features (``policy.safety``) are never
  refused and skip the per-user running cap.
* **bounds the wait**: ``max_wait`` (default ``policy.max_wait`` for user requests, unlimited for jobs/system) ->
  ``AIRejected('busy')``. Waiting is never a provider failure: this module never touches a provider or its circuit
  breaker.
* **runs background jobs as BACKGROUND** whatever feature they call (``AIOwner.kind == 'job'``): nobody waits on
  screen for them, and ageing still promotes them after ``AGEING_SECONDS``.
* **can be cancelled**: ``cancel(...)`` stops queued tickets (their waiter raises ``AICancelled``) and running ones
  (their attached task is cancelled with the reason; streams, which have no task of their own, watch
  ``ticket.cancel_reason`` and get their ``on_cancel`` hook called so a wait for the next words ends at once). Work whose
  exit policy is ``keep`` (safety checks, verification, payroll, security) is only cancelled when ``policies`` names
  'keep' explicitly - "never block or cancel safety flows".
* **tells the user**: tickets of a signed-in user that still wait after ``ANNOUNCE_AFTER`` seconds publish
  ``ai.queue`` WebSocket events (position + ETA, then running/done/cancelled/expired) to that user.
* **counts**: ``snapshot()`` feeds the admin panel (lanes, priorities, top waiting users, counters, average wait,
  compute saved by cancellations).

State is per process and only touched from the event loop thread; no method awaits while it mutates state.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import logging
import math
import secrets
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.ai import policy as _policy
from app.ai.errors import AICancelled, AIRejected
from app.ai.owner import OWNER_JOB, OWNER_SYSTEM, OWNER_USER, AIOwner
from app.ai.policy import BACKGROUND, EXIT_CANCEL, EXIT_KEEP, INTERACTIVE, LANE_LLM, LANE_TTS, REALTIME
from app.config import get_settings
from app.core.state import register_reset

log = logging.getLogger("ironpulse.ai.queue")

# Queue mechanics (the limits themselves live in app/ai/policy.py and the settings).
AGEING_SECONDS = 60.0  # BACKGROUND work waiting longer than this competes as INTERACTIVE
ANNOUNCE_AFTER = 0.8  # a user ticket still waiting after this long gets 'ai.queue' events
REFRESH_EVERY = 1.0  # position updates: at most one per ticket per second
BUSY_RETRY_AFTER = 5  # Retry-After (s) of queue refusals
DEFAULT_SERVICE_SECONDS = 5.0  # ETA basis until a lane has measured its own service times
BUSY_MESSAGE = "AI is busy right now - please try again in a moment."
TOO_MANY_MESSAGE = "You already have several AI requests in progress - please wait for them to finish."
OUTCOMES = ("done", "failed", "cancelled", "expired")
PRIORITY_NAMES = {REALTIME: "realtime", INTERACTIVE: "interactive", BACKGROUND: "background"}

_seq = itertools.count(1)  # start order (FIFO inside a bucket) - internal, never sent anywhere


def _new_id() -> str:
    """An opaque, random ticket id. It reaches the browser in ``ai.queue`` events, so it must carry nothing else - no
    process-wide counter, whose gaps would tell a member how much AI everybody else is using."""
    return f"t{secrets.token_hex(8)}"


@dataclass(slots=True, eq=False)
class Ticket:
    """One AI call's place in the queue. ``state``: queued -> running -> done | failed | cancelled, or
    queued -> cancelled | expired. ``cancel_reason`` is set once the call is being stopped."""

    id: str
    feature: str
    user_id: int | None = None
    page_id: str | None = None
    session_hash: str | None = None
    kind: str = OWNER_SYSTEM  # user | job | system
    priority: int = INTERACTIVE
    lane: str = LANE_LLM
    exit_policy: str = EXIT_CANCEL
    state: str = "queued"
    enqueued_at: float = field(default_factory=time.monotonic)
    started_at: float | None = None
    task: asyncio.Task | None = None
    cancel_reason: str | None = None
    # --- bookkeeping
    job_id: int | None = None
    safety: bool = False
    seq: int = 0
    finished_at: float | None = None
    waiter: asyncio.Future | None = field(default=None, repr=False)
    announced: bool = False
    last_position: int = 0
    last_published: float = 0.0
    announce_handle: asyncio.TimerHandle | None = field(default=None, repr=False)
    # Streams run in their consumer's task (no attached task to cancel): while one waits for its provider's next
    # words it sets this, and cancel() calls it so the wait ends at once instead of at the next chunk.
    on_cancel: Callable[[], None] | None = field(default=None, repr=False)

    @property
    def bucket(self) -> tuple[str, int | None]:
        """Fairness unit: one user's requests, one user's jobs, or the system's work."""
        return (self.kind, self.user_id)

    @property
    def queue_ms(self) -> int:
        """Time spent waiting for a slot (so far, for a ticket that still waits)."""
        if self.started_at is not None:
            end = self.started_at
        elif self.finished_at is not None:
            end = self.finished_at
        else:
            end = time.monotonic()
        return max(0, int((end - self.enqueued_at) * 1000))


class _Lane:
    """Queued and running tickets of one lane plus its round-robin memory."""

    __slots__ = ("name", "queued", "n_queued", "running", "bucket_running", "served", "serve_seq", "service",
                 "refresh_handle")

    def __init__(self, name: str) -> None:
        self.name = name
        self.queued: dict[tuple[str, int | None], list[Ticket]] = {}  # bucket -> its queued tickets, FIFO
        self.n_queued = 0
        self.running: dict[str, Ticket] = {}
        self.bucket_running: defaultdict[tuple[str, int | None], int] = defaultdict(int)
        # bucket -> serve sequence number of its latest start; buckets that went idle are forgotten, so a user who
        # comes back is served before everyone who has been served since
        self.served: dict[tuple[str, int | None], int] = {}
        self.serve_seq = 0
        self.service: deque[float] = deque(maxlen=50)  # recent service times (s) of completed calls -> ETAs
        self.refresh_handle: asyncio.TimerHandle | None = None


class AIScheduler:
    def __init__(self) -> None:
        self.clock = time.monotonic  # replaceable in tests (ageing, queue times)
        self._lanes: dict[str, _Lane] = {}
        self._tickets: dict[str, Ticket] = {}  # every queued or running ticket
        self._reset_counters()

    # ------------------------------------------------------------------ public API
    async def acquire(self, owner: AIOwner | None, feature: str, *, max_wait: float | None = None,
                      on_queued: Callable[[Ticket], Any] | None = None) -> Ticket:
        """Wait for a slot in the feature's lane and return the running ticket (release it when done).

        Raises AIRejected('too_many' | 'busy') when refused or when the wait exceeds ``max_wait`` (default: the
        feature's ``max_wait`` for user requests, unlimited for jobs/system), and AICancelled when ``cancel()``
        stops the ticket while it waits. Cancelling the waiting task removes the ticket.

        ``on_queued(ticket)`` is called (synchronously, once) when the ticket has to wait - before the wait begins -
        so the caller can watch the requester while it waits (app/ai/client.py starts its HTTP disconnect watcher
        there, which cancels the queued ticket when the browser goes away). Not called when a slot is free at once."""
        pol = _policy.policy_for(feature)
        settings = get_settings()
        kind = (owner.kind or OWNER_SYSTEM) if owner is not None else OWNER_SYSTEM
        exit_policy = pol.on_exit
        priority = pol.priority
        if kind == OWNER_JOB:
            # a background job: nobody waits on screen for it, whatever feature it calls (a health note job runs
            # the 'health_metrics' review). Ageing still lifts it after AGEING_SECONDS, so it never starves.
            priority = max(priority, BACKGROUND)
            if owner is not None and owner.exit_policy:
                exit_policy = owner.exit_policy
        tid = _new_id()
        while tid in self._tickets:  # 64 random bits: practically never, but an id must name one ticket only
            tid = _new_id()
        t = Ticket(
            id=tid, feature=feature, user_id=owner.user_id if owner is not None else None,
            page_id=owner.page_id if owner is not None else None,
            session_hash=owner.session_hash if owner is not None else None, kind=kind, priority=priority,
            lane=pol.lane, exit_policy=exit_policy, state="queued", enqueued_at=self.clock(),
            job_id=owner.job_id if owner is not None else None, safety=pol.safety, seq=next(_seq),
        )
        lane = self._lane(t.lane)
        self._counters["submitted"] += 1
        if kind == OWNER_USER and t.user_id is not None and not t.safety:
            mine = len(lane.queued.get(t.bucket, ())) + lane.bucket_running.get(t.bucket, 0)
            if mine >= settings.ai_queue_max_per_user:
                self._reject(t, "too_many", TOO_MANY_MESSAGE)
            if lane.n_queued >= settings.ai_queue_max_total:
                self._reject(t, "busy", BUSY_MESSAGE)

        self._tickets[t.id] = t
        self._push(lane, t)
        self._dispatch(lane)
        if t.state == "running":
            return t

        # it has to wait
        loop = asyncio.get_running_loop()
        t.waiter = loop.create_future()
        if kind == OWNER_USER and t.user_id is not None:
            t.announce_handle = loop.call_later(ANNOUNCE_AFTER, self._announce, t)
        if on_queued is not None:
            try:
                on_queued(t)
            except Exception:  # noqa: BLE001 - a broken hook must not lose the ticket's place
                log.exception("AI queue: on_queued hook failed for ticket %s", t.id)
        wait = max_wait if max_wait is not None else (pol.max_wait if kind == OWNER_USER else None)
        try:
            if wait is None:
                await t.waiter
            else:
                async with asyncio.timeout(max(0.0, wait)):
                    await t.waiter
        except TimeoutError:
            if t.state == "running":  # a slot was granted right at the deadline: use it
                return t
            if t.state == "cancelled":  # cancel() won the race against the deadline
                _consume(t.waiter)
                raise AICancelled(t.cancel_reason or "cancelled") from None
            self._drop(t, "expired")
            log.info("AI queue: %s ticket %s of user %s expired after %.1fs", feature, t.id, t.user_id, wait)
            raise AIRejected("busy", BUSY_MESSAGE, retry_after=BUSY_RETRY_AFTER, feature=feature) from None
        except asyncio.CancelledError:
            _consume(t.waiter)
            self._abandon(t)
            raise
        return t

    def release(self, ticket: Ticket, *, outcome: str) -> None:
        """Give the slot back ('done' | 'failed' | 'cancelled' | 'expired') and start the next ticket. Exactly once:
        later calls, and calls for tickets from before a reset(), do nothing."""
        if self._tickets.get(ticket.id) is not ticket:
            return
        if ticket.state == "queued":  # never started (caller bug or an abandoned wait): just leave the queue
            self._drop(ticket, "expired" if outcome == "expired" else "cancelled", ticket.cancel_reason or "released")
            return
        if ticket.state == "running":
            self._finish(ticket, outcome)

    def attach(self, ticket: Ticket, task: asyncio.Task) -> None:
        """Register the child task doing the provider work, so cancel() can stop it. A cancel that arrived between
        acquire() and attach() takes effect immediately."""
        ticket.task = task
        if ticket.cancel_reason is not None and ticket.state == "running" and not task.done():
            task.cancel(msg=ticket.cancel_reason)

    def cancel(self, *, user_id: int | None = None, page_id: str | None = None, session_hash: str | None = None,
               ticket_id: str | None = None, reason: str, kinds: set[str] | None = None,
               policies: set[str] | None = None) -> int:
        """Stop matching tickets; returns how many were stopped. Filters are ANDed and at least one id filter
        (user_id, page_id, session_hash, ticket_id) is required. ``kinds`` filters the owner kind ('user', 'job',
        'system'); ``policies`` filters the exit policy. Without ``policies``, 'keep' work (safety, verification,
        payroll ...) is left alone - name 'keep' explicitly to stop it (e.g. account deletion).

        Queued tickets leave the queue and their waiter raises AICancelled(reason). Running tickets get
        ``cancel_reason = reason`` and their attached task is cancelled with ``msg=reason``."""
        if user_id is None and page_id is None and session_hash is None and ticket_id is None:
            raise ValueError("scheduler.cancel() needs user_id, page_id, session_hash or ticket_id")
        reason = (reason or "cancelled")[:40]
        stopped = 0
        for t in list(self._tickets.values()):
            if not _matches(t, user_id, page_id, session_hash, ticket_id, kinds, policies):
                continue
            if t.state == "queued":
                t.cancel_reason = reason
                self._drop(t, "cancelled", reason)
                if t.waiter is not None and not t.waiter.done():
                    t.waiter.set_exception(AICancelled(reason))
                stopped += 1
            elif t.state == "running" and t.cancel_reason is None:
                t.cancel_reason = reason
                if t.task is not None and not t.task.done():
                    t.task.cancel(msg=reason)
                _wake(t)
                stopped += 1
        if stopped:
            log.info("AI queue: stopped %d ticket(s) (%s; user=%s page=%s ticket=%s)", stopped, reason, user_id,
                     page_id, ticket_id)
        return stopped

    def snapshot(self) -> dict[str, Any]:
        """Cheap, synchronous view for the admin panel."""
        now = self.clock()
        lanes: dict[str, Any] = {}
        by_priority = {name: {"running": 0, "queued": 0} for name in PRIORITY_NAMES.values()}
        users: dict[int, dict[str, Any]] = {}

        def user_row(t: Ticket) -> dict[str, Any] | None:
            if t.kind != OWNER_USER or t.user_id is None:
                return None
            return users.setdefault(t.user_id, {"user_id": t.user_id, "queued": 0, "running": 0,
                                                "oldest_wait_ms": 0})

        names = [LANE_LLM, LANE_TTS] + sorted(set(self._lanes) - {LANE_LLM, LANE_TTS})
        for name in names:
            lane = self._lanes.get(name)
            queued = [t for bucket in lane.queued.values() for t in bucket] if lane else []
            running = list(lane.running.values()) if lane else []
            oldest = max((now - t.enqueued_at for t in queued), default=0.0)
            service = lane.service if lane else ()
            lanes[name] = {
                "capacity": self._capacity(name), "running": len(running), "queued": len(queued),
                "oldest_wait_ms": int(oldest * 1000),
                "avg_service_ms": int(sum(service) / len(service) * 1000) if service else None,
            }
            for t in running:
                by_priority.setdefault(PRIORITY_NAMES.get(t.priority, str(t.priority)),
                                       {"running": 0, "queued": 0})["running"] += 1
                row = user_row(t)
                if row is not None:
                    row["running"] += 1
            for t in queued:
                by_priority.setdefault(PRIORITY_NAMES.get(t.priority, str(t.priority)),
                                       {"running": 0, "queued": 0})["queued"] += 1
                row = user_row(t)
                if row is not None:
                    row["queued"] += 1
                    row["oldest_wait_ms"] = max(row["oldest_wait_ms"], int((now - t.enqueued_at) * 1000))
        top = sorted((u for u in users.values() if u["queued"]),
                     key=lambda u: (-u["queued"], -u["oldest_wait_ms"], u["user_id"]))[:10]
        counters: dict[str, Any] = dict(self._counters)
        counters["cancelled"] = dict(self._cancelled)
        counters["rejected"] = dict(self._rejected)
        return {
            "lanes": lanes,
            "by_priority": by_priority,
            "users": top,
            "counters": counters,
            "avg_wait_ms": int(sum(self._waits) / len(self._waits)) if self._waits else 0,
            "saved": dict(self._saved),
        }

    def reset(self) -> None:
        """Tests: forget everything. Waiting tasks are cancelled, running ones too (they belong to a finished
        test), timers are stopped and the counters start from zero."""
        for lane in self._lanes.values():
            if lane.refresh_handle is not None:
                lane.refresh_handle.cancel()
            for t in [t for bucket in lane.queued.values() for t in bucket]:
                t.state, t.cancel_reason = "cancelled", t.cancel_reason or "reset"
                if t.announce_handle is not None:
                    t.announce_handle.cancel()
                if t.waiter is not None and not t.waiter.done():
                    try:
                        t.waiter.cancel(msg="reset")
                    except RuntimeError:  # its event loop is gone
                        pass
            for t in list(lane.running.values()):
                t.state, t.cancel_reason = "cancelled", t.cancel_reason or "reset"
                if t.task is not None and not t.task.done():
                    try:
                        t.task.cancel(msg="reset")
                    except RuntimeError:
                        pass
                _wake(t)
        self._lanes.clear()
        self._tickets.clear()
        self._reset_counters()

    # ------------------------------------------------------------------ queue mechanics
    def _reset_counters(self) -> None:
        self._counters: dict[str, int] = {"submitted": 0, "started": 0, "completed": 0, "failed": 0, "expired": 0}
        self._cancelled: defaultdict[str, int] = defaultdict(int)
        self._rejected: defaultdict[str, int] = defaultdict(int)
        self._saved = {"cancelled_running": 0, "cancelled_queued": 0}
        self._waits: deque[int] = deque(maxlen=500)  # queue_ms of recently started tickets

    def _lane(self, name: str) -> _Lane:
        lane = self._lanes.get(name)
        if lane is None:
            lane = self._lanes[name] = _Lane(name)
        return lane

    @staticmethod
    def _capacity(name: str) -> int:
        settings = get_settings()
        value = settings.ai_tts_concurrency if name == LANE_TTS else settings.ai_queue_concurrency
        return max(1, int(value))

    def _reject(self, t: Ticket, code: str, message: str) -> None:
        self._rejected[code] += 1
        log.info("AI queue: %s refused for user %s (%s)", t.feature, t.user_id, code)
        raise AIRejected(code, message, retry_after=BUSY_RETRY_AFTER, feature=t.feature)

    def _effective_priority(self, t: Ticket, now: float) -> int:
        if t.priority >= BACKGROUND and now - t.enqueued_at > AGEING_SECONDS:
            return INTERACTIVE
        return t.priority

    def _push(self, lane: _Lane, t: Ticket) -> None:
        lane.queued.setdefault(t.bucket, []).append(t)  # seq grows, so every bucket stays in FIFO order
        lane.n_queued += 1

    def _unqueue(self, lane: _Lane, t: Ticket) -> None:
        tickets = lane.queued.get(t.bucket)
        if tickets is None or t not in tickets:
            return
        tickets.remove(t)
        lane.n_queued -= 1
        if not tickets:
            del lane.queued[t.bucket]
            self._forget_if_idle(lane, t.bucket)

    @staticmethod
    def _forget_if_idle(lane: _Lane, bucket: tuple[str, int | None]) -> None:
        if bucket not in lane.queued and not lane.bucket_running.get(bucket):
            lane.served.pop(bucket, None)
            lane.bucket_running.pop(bucket, None)

    def _pick(self, lane: _Lane) -> Ticket | None:
        """The next ticket to start: best effective priority, then the least recently served bucket, then FIFO.
        Tickets of a user already running MAX_RUNNING_PER_USER calls (safety tickets excepted) and tickets whose
        waiter is already gone are skipped."""
        now = self.clock()
        cap = _policy.MAX_RUNNING_PER_USER
        best: Ticket | None = None
        best_rank: tuple[int, int, int] | None = None
        for bucket, tickets in lane.queued.items():
            capped = bucket[0] == OWNER_USER and bucket[1] is not None and lane.bucket_running.get(bucket, 0) >= cap
            cand: Ticket | None = None
            cand_rank: tuple[int, int] | None = None
            for t in tickets:
                if (capped and not t.safety) or (t.waiter is not None and t.waiter.done()):
                    continue
                rank = (self._effective_priority(t, now), t.seq)
                if cand_rank is None or rank < cand_rank:
                    cand, cand_rank = t, rank
            if cand is None or cand_rank is None:
                continue
            rank3 = (cand_rank[0], lane.served.get(bucket, 0), cand_rank[1])
            if best_rank is None or rank3 < best_rank:
                best, best_rank = cand, rank3
        return best

    def _dispatch(self, lane: _Lane) -> None:
        capacity = self._capacity(lane.name)
        while len(lane.running) < capacity and lane.n_queued:
            t = self._pick(lane)
            if t is None:
                break
            self._start(lane, t)

    def _start(self, lane: _Lane, t: Ticket) -> None:
        self._unqueue(lane, t)
        t.state = "running"
        t.started_at = self.clock()
        lane.running[t.id] = t
        lane.bucket_running[t.bucket] += 1
        lane.serve_seq += 1
        lane.served[t.bucket] = lane.serve_seq
        self._counters["started"] += 1
        self._waits.append(t.queue_ms)
        if t.announce_handle is not None:
            t.announce_handle.cancel()
            t.announce_handle = None
        if t.waiter is not None and not t.waiter.done():
            try:
                t.waiter.set_result(None)
            except RuntimeError:  # the waiter's loop is closed: nobody will run this ticket
                self._finish(t, "cancelled")
                return
        if t.announced:
            self._publish(t, "running")

    def _finish(self, t: Ticket, outcome: str, *, saved: str = "cancelled_running") -> None:
        """Terminal transition of a running ticket: free its slot and start the next ticket."""
        lane = self._lanes.get(t.lane)
        self._tickets.pop(t.id, None)
        if outcome not in OUTCOMES:
            log.warning("AI queue: unknown outcome %r for ticket %s - counted as failed", outcome, t.id)
            outcome = "failed"
        now = self.clock()
        t.state, t.finished_at = outcome, now
        if outcome == "done":
            self._counters["completed"] += 1
        elif outcome == "failed":
            self._counters["failed"] += 1
        elif outcome == "expired":
            self._counters["expired"] += 1
        else:
            t.cancel_reason = t.cancel_reason or "aborted"
            self._cancelled[t.cancel_reason] += 1
            self._saved[saved] += 1
        if t.announced:
            self._publish(t, "done" if outcome in ("done", "failed") else outcome)
        if lane is None or lane.running.pop(t.id, None) is None:
            return
        if outcome == "done" and t.started_at is not None:
            lane.service.append(max(0.0, now - t.started_at))
        left = lane.bucket_running.get(t.bucket, 0) - 1
        if left > 0:
            lane.bucket_running[t.bucket] = left
        else:
            lane.bucket_running.pop(t.bucket, None)
            self._forget_if_idle(lane, t.bucket)
        self._dispatch(lane)

    def _drop(self, t: Ticket, state: str, reason: str | None = None) -> None:
        """Terminal transition of a queued ticket (cancelled / expired)."""
        lane = self._lanes.get(t.lane)
        if lane is not None:
            self._unqueue(lane, t)
        self._tickets.pop(t.id, None)
        if t.announce_handle is not None:
            t.announce_handle.cancel()
            t.announce_handle = None
        t.state, t.finished_at = state, self.clock()
        if state == "expired":
            self._counters["expired"] += 1
        else:
            t.cancel_reason = t.cancel_reason or reason or "cancelled"
            self._cancelled[t.cancel_reason] += 1
            self._saved["cancelled_queued"] += 1
        if t.announced:
            self._publish(t, state)

    def _abandon(self, t: Ticket) -> None:
        """The waiting task was cancelled (SSE disconnect, a caller's timeout, shutdown)."""
        if t.state == "queued":
            self._drop(t, "cancelled", "aborted")
        elif t.state == "running":  # granted at the very moment it was cancelled: hand the slot on
            t.cancel_reason = t.cancel_reason or "aborted"
            self._finish(t, "cancelled", saved="cancelled_queued")

    # ------------------------------------------------------------------ live queue positions (WebSocket)
    def _positions(self, lane: _Lane) -> dict[str, int]:
        """1-based start order of the queued tickets, simulating _pick() (per-user running caps ignored)."""
        now = self.clock()
        orders: dict[tuple[str, int | None], list[Ticket]] = {}
        heap: list[tuple[int, int, int, tuple[str, int | None]]] = []
        for bucket, tickets in lane.queued.items():
            if not tickets:
                continue
            order = sorted(tickets, key=lambda t: (self._effective_priority(t, now), t.seq))
            orders[bucket] = order
            heap.append((self._effective_priority(order[0], now), lane.served.get(bucket, 0), order[0].seq, bucket))
        heapq.heapify(heap)
        serve_seq = lane.serve_seq
        nxt = dict.fromkeys(orders, 0)
        positions: dict[str, int] = {}
        while heap:
            bucket = heapq.heappop(heap)[3]  # ticket seqs are unique, so buckets are never compared
            order, i = orders[bucket], nxt[bucket]
            positions[order[i].id] = len(positions) + 1
            nxt[bucket] = i = i + 1
            if i < len(order):
                serve_seq += 1
                heapq.heappush(heap, (self._effective_priority(order[i], now), serve_seq, order[i].seq, bucket))
        return positions

    def _eta(self, lane: _Lane, position: int) -> int:
        avg = sum(lane.service) / len(lane.service) if lane.service else DEFAULT_SERVICE_SECONDS
        return max(1, math.ceil(avg * position / self._capacity(lane.name)))

    def _announce(self, t: Ticket) -> None:
        t.announce_handle = None
        lane = self._lanes.get(t.lane)
        if t.state != "queued" or lane is None or self._tickets.get(t.id) is not t:
            return
        position = self._positions(lane).get(t.id, 1)
        t.announced = True
        self._publish(t, "queued", position, self._eta(lane, position))
        self._schedule_refresh(lane)

    def _schedule_refresh(self, lane: _Lane) -> None:
        if lane.refresh_handle is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        lane.refresh_handle = loop.call_later(REFRESH_EVERY, self._refresh, lane)

    def _refresh(self, lane: _Lane) -> None:
        lane.refresh_handle = None
        if self._lanes.get(lane.name) is not lane:  # reset() since
            return
        announced = [t for bucket in lane.queued.values() for t in bucket if t.announced]
        if not announced:
            return
        positions = self._positions(lane)
        now = time.monotonic()
        for t in announced:
            position = positions.get(t.id)
            if position and position != t.last_position and now - t.last_published >= REFRESH_EVERY * 0.9:
                self._publish(t, "queued", position, self._eta(lane, position))
        self._schedule_refresh(lane)

    def _publish(self, t: Ticket, state: str, position: int = 0, eta: int = 0) -> None:
        if t.user_id is None:
            return
        t.last_position, t.last_published = position, time.monotonic()
        evt: dict[str, Any] = {"type": "ai.queue", "page_id": t.page_id, "ticket": t.id, "feature": t.feature,
                               "state": state, "position": position, "eta": eta}
        if state == "cancelled" and t.cancel_reason:
            evt["reason"] = t.cancel_reason
        try:
            from app.services.realtime import hub

            hub.publish_nowait([t.user_id], evt)
        except Exception:  # noqa: BLE001 - a lost progress event must never break the queue
            log.debug("ai.queue event for ticket %s not delivered", t.id, exc_info=True)


def _matches(t: Ticket, user_id: int | None, page_id: str | None, session_hash: str | None, ticket_id: str | None,
             kinds: set[str] | None, policies: set[str] | None) -> bool:
    if ticket_id is not None and t.id != ticket_id:
        return False
    if user_id is not None and t.user_id != user_id:
        return False
    if page_id is not None and t.page_id != page_id:
        return False
    if session_hash is not None and t.session_hash != session_hash:
        return False
    if kinds is not None and t.kind not in kinds:
        return False
    if policies is not None:
        return t.exit_policy in policies
    return t.exit_policy != EXIT_KEEP


def _consume(fut: asyncio.Future | None) -> None:
    """Mark a finished waiter's exception as retrieved (no 'exception was never retrieved' noise)."""
    if fut is not None and fut.done() and not fut.cancelled():
        fut.exception()


def _wake(t: Ticket) -> None:
    """Tell a running stream (no attached task) that it was cancelled, so a wait for its next words ends now."""
    hook, t.on_cancel = t.on_cancel, None
    if hook is None:
        return
    try:
        hook()
    except Exception:  # noqa: BLE001 - the stream still sees cancel_reason at its next chunk
        log.debug("cancel hook of ticket %s failed", t.id, exc_info=True)


scheduler = AIScheduler()
register_reset(scheduler.reset, name="app.ai.scheduler")
