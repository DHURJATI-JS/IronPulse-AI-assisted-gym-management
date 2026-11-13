"""Threat scoring, IP bans and security-event persistence.

Ban policy (as specified):
* offender WITH an active subscription  -> temporary ban, 2 h .. 24 h depending on severity;
* offender WITHOUT a subscription       -> temporary ban on the first offence, then a permanent IP ban
                                           for repeat offenders (lifted only by a super-admin).

The request path never waits for the database: events and ban decisions are queued in memory and
flushed by the background worker (``flush_security_queue``) every couple of seconds. A provisional
ban is applied in memory immediately so an attacker is stopped at once.

Bounded work (ip_bans only grows - permanent bans never expire - and attacks fill the queues):
* the flush finalises at most MAX_BANS_PER_FLUSH queued bans per call (one decision per address) with a
  constant number of set-based queries (``IN (...)`` lookups), never 4-6 queries per ban;
* ``sync_bans_from_db`` loads the whole active set only at start-up and as a rare safety net
  (FULL_RESYNC_SECONDS, streamed in SYNC_ROWS batches, three columns). Every other sync reads only what changed:
  new rows by id (re-reading the last SYNC_REREAD_SECONDS of ids, so a ban committed late is not missed) and the
  addresses of lifted / extended / replaced bans from the audit trail (``BAN_CHANGE_ACTIONS``, written in the
  same transaction by app/services/security_triage.py), whose current state is then reloaded.

Threat scores (``ThreatTracker``: severity points per address, halving every HALF_LIFE_SECONDS; at
``threat_ban_score`` the address is banned) are shared by every worker through Redis when REDIS_URL is set - the
store of app/security/ratelimit.py, one Lua script per update, keys ``ip:<env>:<db>:sec:{<ip>}:threat`` kept until the
score has decayed below 0.1 - so N workers reach a ban at the same total as one process. Without Redis, or while it
fails or is slow, each worker scores from its own (bounded) memory. As the workers see a shared score cross the ban
score together, only the first to claim the ban (``ThreatTracker.claim_ban``, key ``...{<ip>}:ban``, a minute)
queues it for the database; the others block the address in memory (``hold_ban``) until their ban sync loads it. The
synchronous reporters (CSRF and webhook-fraud reports, sign-in / re-auth lockouts, websocket floods, AI abuse) pass
their reason to ``ThreatTracker.add``: its background update bans from the SHARED total through the same claim (and
decides from this worker's view only without Redis), never from a stale local copy. As the backstop for Redis outages
and direct ``request_ban`` callers, ``_finalise_bans`` runs under a transaction-level advisory lock: two workers'
flushes for one address are serialised, so the second sees the first one's row instead of writing a "repeat offence".

An admin's Lift reaches every worker: the lifting worker forgets the address's shared score and claim at once
(``lift_ban_in_memory``); the others drop their held ban and their own copy of its score at their next ban sync.

A flush never loses what it took off the queues (hunt F17 / F37 / F49):
* every text is made storable when it is QUEUED (``storable``: a NUL byte - ``/x%00y``, which uvicorn decodes into the
  path - is kept as the four characters ``\\x00``); one such value used to fail the whole batch's INSERT and drop every
  event and ban decision of those 2 seconds - one anonymous request erased other attackers' bans and its own;
* the events are written inside a SAVEPOINT; a value the database still refuses (SQLSTATE 22 / 23) costs only that
  row - the batch is retried row by row;
* what a flush took off the queues goes back to their front unless the caller's transaction COMMITs (a statement
  timeout, a lost connection, a failure later in the same transaction): the next flush writes it again;
* a ban becomes a confirmed registry entry - and is logged as "banned" - only once its row is committed (before, a
  failed flush left a "confirmed" entry the next full ban sync dropped, so a 12 h ban lasted 30 minutes at most).

Evidence a third party can forge is never scored: the gate and the CSRF check pass ``attributable=False`` for what a
browser was made to send (app/security/net.py ``attributable``). Such events are stored already reviewed
(``NOT_ATTRIBUTABLE`` verdict), so the AI triage never bans on them either.
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections import OrderedDict, deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import event, func, or_, select, text
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.utils import utcnow
from app.models.enums import SubscriptionStatus, ThreatCategory
from app.security.ratelimit import SharedStore, address_key, lua_script, shared_store

log = logging.getLogger("ironpulse.security")

MAX_BANS_PER_FLUSH = 200  # queued ban decisions finalised per flush (every 2 s); the rest wait for the next one
BAN_FLUSH_LOCK_KEY = 0x49504246  # "IPBF": pg_advisory_xact_lock serialising every worker's _finalise_bans
# A held ban (another worker claimed and persists it) is dropped when the database says the address has no active ban
# (a Lift) - but only once it is older than this: a sync right after the hold may run before the claimer's flush commits.
HOLD_GRACE_SECONDS = 15.0
SYNC_ROWS = 5000  # ip_bans rows per query / streamed batch of a ban sync
SYNC_REREAD_SECONDS = 300.0  # new ban ids are re-read for this long (a long transaction may commit an older id late)
FULL_RESYNC_SECONDS = 1800.0  # safety net: rebuild the registry from the database every 30 minutes
# audit actions of every change to an EXISTING ip_bans row (app/services/security_triage.py writes them, detail.ip)
BAN_CHANGE_ACTIONS = ("security.ban_lift", "security.ban_manual", "security.triage_extended")

SEVERITY_POINTS = {1: 1.0, 2: 3.0, 3: 6.0, 4: 10.0, 5: 15.0}
SEVERITY_HOURS = {1: 2, 2: 2, 3: 6, 4: 12, 5: 24}
HALF_LIFE_SECONDS = 600.0  # threat score decays by half every 10 minutes

# security_events columns (app/models/system.py): every queued text is cut to these after ``storable``
EVENT_DETAIL_MAX, EVENT_METHOD_MAX, EVENT_PATH_MAX, EVENT_AGENT_MAX, BAN_REASON_MAX = 500, 10, 512, 300, 300
NOT_ATTRIBUTABLE = "not_attributable"  # the verdict of events stored already reviewed (never scored, never triaged)
NOT_ATTRIBUTABLE_NOTE = "sent by a browser - any web page or link can trigger it; refused, not counted against the address"


def storable(value: str | None, limit: int) -> str:
    """``value`` as PostgreSQL can store it in a text column, cut to ``limit`` characters: a NUL byte (``%00`` in a
    request path - uvicorn decodes it) becomes the four characters ``\\x00`` and an unpaired surrogate its ``\\udXXX``
    escape. The database refuses both, and one such value used to fail a whole security flush."""
    if not value:
        return ""
    if "\x00" in value:
        value = value.replace("\x00", "\\x00")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        value = value.encode("utf-8", "backslashreplace").decode("utf-8")
    return value[:limit]


def _not_attributable_verdict() -> str:
    """The ``ai_verdict`` of an event stored already reviewed: the security centre shows "Not attributable", and the
    triage (which reads only unreviewed events) never bans on it."""
    return json.dumps({"verdict": NOT_ATTRIBUTABLE, "action": "none", "source": "rules", "applied": "none",
                       "rationale": "A web page, an e-mail or a chat link can make any visitor's browser send this "
                                    "request, so it was refused but never counted against the address.",
                       "at": utcnow().isoformat(timespec="seconds")})


def ban_duration(severity: int, *, offense_count: int, has_subscription: bool) -> timedelta | None:
    """None means permanent."""
    hours = SEVERITY_HOURS.get(max(1, min(5, severity)), 2)
    if has_subscription:
        return timedelta(hours=hours)
    if offense_count >= 2:
        return None
    return timedelta(hours=hours)


@dataclass(slots=True)
class BanInfo:
    until: datetime | None  # None = permanent
    reason: str
    severity: int
    provisional: bool = False
    held: bool = False  # provisional, NOT queued here: another worker claimed the ban and persists it (hold_ban)
    since: float = field(default_factory=time.monotonic)

    def active(self, now: datetime | None = None) -> bool:
        return self.until is None or self.until > (now or utcnow())

    def held_for(self, now: float | None = None) -> float:
        return (time.monotonic() if now is None else now) - self.since


@dataclass
class _SyncState:
    """Where this process's registry stands against ip_bans (see ``sync_bans_from_db``)."""

    loaded: bool = False  # a full load happened (otherwise the next sync is one)
    full_at: float = 0.0  # time.monotonic() of the last full load
    max_id: int = 0  # highest ip_bans.id applied
    marks: deque[tuple[float, int]] = field(default_factory=deque)  # (monotonic, max_id) per sync, for re-reads

    def reread_floor(self, now: float) -> int:
        """``max_id`` as it was SYNC_REREAD_SECONDS ago: new rows are read from there."""
        while len(self.marks) > 1 and now - self.marks[1][0] >= SYNC_REREAD_SECONDS:
            self.marks.popleft()
        return self.marks[0][1] if self.marks else self.max_id


class BanRegistry:
    """Process-local view of active bans (refreshed from the DB by the worker)."""

    def __init__(self) -> None:
        self._bans: dict[str, BanInfo] = {}
        self.sync = _SyncState()

    def get(self, ip: str) -> BanInfo | None:
        info = self._bans.get(ip)
        if info and not info.active():
            self._bans.pop(ip, None)
            return None
        return info

    def put(self, ip: str, info: BanInfo) -> None:
        current = self._bans.get(ip)
        if current and current.active() and not current.provisional:
            # never shorten an existing confirmed ban
            if current.until is None or (info.until is not None and info.until <= current.until):
                return
        self._bans[ip] = info

    def remove(self, ip: str) -> None:
        self._bans.pop(ip, None)

    def replace_all(self, bans: dict[str, BanInfo]) -> None:
        """A full load: the database's active bans, plus this worker's provisional ones - those it queued itself, and
        the held ones (another worker persists them) younger than HOLD_GRACE_SECONDS (an older held ban the database
        does not have was lifted, or never persisted)."""
        now = time.monotonic()
        provisional = {ip: b for ip, b in self._bans.items()
                       if b.provisional and b.active() and not (b.held and b.held_for(now) > HOLD_GRACE_SECONDS)}
        provisional.update(bans)
        self._bans = provisional

    def set_confirmed(self, ip: str, info: BanInfo | None) -> None:
        """The database's current verdict on ``ip`` (None = no active ban): it replaces a confirmed entry, even with a
        shorter ban. A ban this worker queued itself stays until a confirmed one replaces it (as in ``replace_all``);
        a HELD one (another worker persists it) goes with None once older than HOLD_GRACE_SECONDS - an admin's Lift
        reaches this worker at its next ban sync instead of SEVERITY_HOURS later."""
        current = self._bans.get(ip)
        if info is not None:
            self._bans[ip] = info
        elif current is not None and (not current.provisional
                                      or (current.held and current.held_for() > HOLD_GRACE_SECONDS)):
            del self._bans[ip]

    def clear(self) -> None:
        self._bans.clear()
        self.sync = _SyncState()  # the next sync is a full load again

    def __len__(self) -> int:
        return len(self._bans)


@dataclass(slots=True)
class _Score:
    value: float = 0.0
    updated: float = field(default_factory=time.monotonic)

    def at(self, now: float) -> float:
        return self.value * math.pow(0.5, (now - self.updated) / HALF_LIFE_SECONDS)


# KEYS[1]: one address's score {v = value, u = updated (Redis clock)}. ARGV: points to add (0 = only read), half-life (s).
# -> the score after decay + points. Kept until it has decayed below 0.1 (at most a day).
_THREAT_LUA = lua_script("threat", """
local points, half = tonumber(ARGV[1]), tonumber(ARGV[2])
local s = redis.call('HMGET', KEYS[1], 'v', 'u')
local v, u = tonumber(s[1]) or 0, tonumber(s[2]) or now
if now > u then v = v * math.pow(0.5, (now - u) / half) end
if points > 0 then
  v = v + points
  redis.call('HSET', KEYS[1], 'v', fmt(v), 'u', fmt(now))
  local keep = half * math.log(math.max(v, 0.1) / 0.1) / math.log(2)
  redis.call('PEXPIRE', KEYS[1], math.min(86400000, math.ceil(keep * 1000) + 1000))
end
return fmt(v)
""")

BAN_CLAIM_SECONDS = 60.0  # the first worker asking for an address's ban within this long persists it (claim_ban)

# KEYS[1]: one address's ban claim. ARGV: how long it holds (ms). -> 1 this caller claimed it, 0 another one holds it
_CLAIM_LUA = lua_script("ban_claim", """
if redis.call('SET', KEYS[1], '1', 'NX', 'PX', tonumber(ARGV[1])) then return 1 end
return 0
""")


class ThreatTracker:
    """Threat score per address: SEVERITY_POINTS that halve every HALF_LIFE_SECONDS; callers request a ban when it
    reaches ``threat_ban_score``.

    Shared by every worker through Redis (app/security/ratelimit.py's store; one Lua script per update); this worker's
    memory without Redis or while it fails. ``add_async`` (the security gate) answers with the shared total of that
    one round trip - the shared total is the verdict whenever Redis answers; this worker's copy (raised to every
    shared total it sees) is only the fallback, so a copy left high by a score another worker's Lift reset never
    re-bans the address. The synchronous ``add`` (CSRF reports, account lockouts, websocket floods, AI abuse) answers at
    once from this worker's view and sends the points to the shared total in the background; given a ``reason``, that
    update bans the address when the SHARED total reaches the ban score (this worker's view only without Redis).
    ``ban`` (the gate and that update) persists a ban only from the worker that claims it. ``score`` is this worker's
    view. At most ``max_keys`` addresses are remembered here (least recently scored dropped first)."""

    def __init__(self, max_keys: int = 100_000, store: SharedStore | None = None) -> None:
        self._scores: OrderedDict[str, _Score] = OrderedDict()
        self._max_keys = max_keys
        self._store = store

    @property
    def store(self) -> SharedStore:
        return self._store if self._store is not None else shared_store

    def _raise(self, ip: str, points: float, floor: float = 0.0) -> float:
        """This worker's view of ``ip``: decayed to now, plus ``points``, at least ``floor`` (a shared total)."""
        now = time.monotonic()
        s = self._scores.get(ip)
        if s is None:
            s = self._scores[ip] = _Score(0.0, now)
            if len(self._scores) > self._max_keys:
                self._scores.popitem(last=False)
        else:
            self._scores.move_to_end(ip)
        s.value = max(s.at(now) + points, floor)
        s.updated = now
        return s.value

    async def _shared(self, ip: str, points: float) -> float | None:
        return await self.store.run(_THREAT_LUA, [address_key(ip, "threat")], [float(points), HALF_LIFE_SECONDS], float)

    def add(self, ip: str, severity: int, *, reason: str | None = None, session_hash: str | None = None) -> float:
        """Add ``severity``'s points and return this worker's view at once; the shared total follows in the background.
        With a ``reason`` that background update also bans the address (``ban``: the claim, one ip_bans row whichever
        worker reports) when the SHARED total reaches ``threat_ban_score`` - callers pass the reason instead of
        calling ``request_ban`` on the returned view, which may be stale (another worker's Lift reset the shared
        score) and bypasses the claim. Without a running event loop (or with too many updates in flight) this worker's
        view decides here, as one process does."""
        points = SEVERITY_POINTS.get(severity, 1.0)
        seen = self._raise(ip, points)
        if not self.store.spawn(self._push(ip, points, severity, seen, reason, session_hash)):
            if reason is not None and seen >= get_settings().threat_ban_score and ban_registry.get(ip) is None:
                request_ban(ip, severity, reason, session_hash)
        return seen

    async def add_async(self, ip: str, *severities: int) -> float:
        """Add the points of ``severities`` (one round trip for several findings) and return the shared total - this
        worker's view only when Redis is not there. This worker's copy is raised to the shared total (never the points
        added again: replies of one worker's simultaneous calls may be read in another order than Redis ran them) and
        is used only as the fallback."""
        points = sum(SEVERITY_POINTS.get(s, 1.0) for s in severities)
        shared = await self._shared(ip, points)
        if shared is None:
            return self._raise(ip, points)
        self._raise(ip, 0.0, shared)
        return shared

    async def score_async(self, ip: str) -> float:
        """The shared total (this worker's view without Redis)."""
        shared = await self._shared(ip, 0.0)
        if shared is None:
            return self.score(ip)
        self._raise(ip, 0.0, shared)
        return shared

    async def _push(self, ip: str, points: float, severity: int, seen: float, reason: str | None,
                    session_hash: str | None) -> None:
        try:
            shared = await self._shared(ip, points)
            if shared is None:  # no Redis (or it failed): this worker's view decides, as one process does - for a
                if reason is None:  # reporter that passed its reason (the others decide from add()'s answer)
                    return
                total = max(seen, self.score(ip))  # (the view this report reached, or more since)
            else:
                self._raise(ip, 0.0, shared)
                total = shared
            if total >= get_settings().threat_ban_score and ban_registry.get(ip) is None:
                await self.ban(ip, severity, reason or f"threat score {total:.0f} across workers", session_hash)
        except Exception:  # noqa: BLE001 - a background update must never surface
            log.exception("could not add threat points of %s to the shared score", ip)

    async def claim_ban(self, ip: str) -> bool:
        """True for the first worker that asks to ban ``ip`` within BAN_CLAIM_SECONDS: it persists the ban. Every
        worker sees a shared score reach the ban score at about the same moment; if each queued the ban, their
        flushes would race and write two ip_bans rows - the second one counts as a repeat offence, which makes a
        first offence a PERMANENT ban. True without Redis (each worker decides alone, as one process does)."""
        claimed = await self.store.run(_CLAIM_LUA, [address_key(ip, "ban")], [int(BAN_CLAIM_SECONDS * 1000)], int)
        return claimed != 0

    async def ban(self, ip: str, severity: int, reason: str, session_hash: str | None = None) -> None:
        """Ban ``ip`` because of its threat score: in this worker's memory at once, and queued for the database by the
        one worker that claims it (the others get it from their next ban sync)."""
        if ip in get_settings().rate_limit_whitelist and severity < 5:
            return  # (request_ban would not ban it either: no claim either)
        if await self.claim_ban(ip):
            request_ban(ip, severity, reason, session_hash)
        else:
            hold_ban(ip, severity, reason)

    def score(self, ip: str) -> float:
        s = self._scores.get(ip)
        return s.at(time.monotonic()) if s else 0.0

    def forget_local(self, ip: str) -> None:
        """Drop this worker's copy of ``ip``'s score (another worker's Lift already forgot the shared one)."""
        self._scores.pop(ip, None)

    def reset(self, ip: str | None = None) -> None:
        """Forget every score of this worker (tests), or one address's - here and in the shared store, its ban claim
        too (a lifted ban: a new offence is banned and persisted again at once)."""
        if ip is None:
            self._scores.clear()
        else:
            self._scores.pop(ip, None)
            self.store.spawn(self.store.forget(address_key(ip, "threat"), address_key(ip, "ban")))


@dataclass(slots=True)
class PendingEvent:
    ip: str
    category: ThreatCategory
    severity: int
    detail: str
    method: str
    path: str
    user_agent: str | None
    action: str
    session_hash: str | None
    created_at: datetime
    reviewed: str | None = None  # an ai_verdict to store it with, already reviewed (NOT_ATTRIBUTABLE events)


@dataclass(slots=True)
class PendingBan:
    ip: str
    severity: int
    reason: str
    session_hash: str | None


class SecurityEventSink:
    """Collects events + ban requests in memory; ``flush_security_queue`` persists them."""

    def __init__(self) -> None:
        self.events: deque[PendingEvent] = deque(maxlen=20_000)
        self.bans: deque[PendingBan] = deque(maxlen=5_000)

    def record(
        self,
        ip: str,
        category: ThreatCategory,
        severity: int,
        detail: str,
        *,
        method: str = "",
        path: str = "",
        user_agent: str | None = None,
        action: str = "logged",
        session_hash: str | None = None,
        attributable: bool = True,
    ) -> None:
        """Queue an event (every text made storable now - see the module docstring). ``attributable=False``: evidence a
        third party can make a visitor's browser send - stored already reviewed (NOT_ATTRIBUTABLE), never triaged."""
        if severity < 2 and category not in (ThreatCategory.RATE_LIMIT, ThreatCategory.BRUTEFORCE):
            return  # severity-1 noise only affects the in-memory score
        reviewed = None
        if not attributable:
            detail = f"{storable(detail, EVENT_DETAIL_MAX - len(NOT_ATTRIBUTABLE_NOTE) - 3)} ({NOT_ATTRIBUTABLE_NOTE})"
            reviewed = _not_attributable_verdict()
        self.events.append(
            PendingEvent(ip, category, severity, storable(detail, EVENT_DETAIL_MAX), storable(method, EVENT_METHOD_MAX),
                         storable(path, EVENT_PATH_MAX), storable(user_agent, EVENT_AGENT_MAX) or None, action,
                         session_hash, utcnow(), reviewed)
        )

    def record_from_request(self, request: Any, category: ThreatCategory, severity: int, detail: str, *,
                            attributable: bool = True) -> None:
        """Queue an event about ``request`` and add its threat points to the address - unless it is not
        ``attributable`` (a browser was made to send it: a foreign Origin, a cross-site fetch): then it is only stored,
        already reviewed, and never counts toward a ban."""
        from app.security.net import client_ip
        from app.security.sessions import get_session_token
        from app.security.crypto import token_hash

        token = get_session_token(request)
        ip = client_ip(request)
        session_hash = token_hash(token) if token else None
        self.record(
            ip, category, severity, detail,
            method=request.method, path=request.url.path, user_agent=request.headers.get("user-agent"),
            session_hash=session_hash, attributable=attributable,
        )
        if not attributable:
            return
        # the SHARED total decides in the background and one claiming worker persists the ban (ThreatTracker.add);
        # banning here on this worker's own view raced the other workers into a second ip_bans row - a "repeat
        # offence", PERMANENT - and re-banned addresses whose score another worker's Lift had reset
        threat_tracker.add(ip, severity, reason=f"{category.value}: {detail}", session_hash=session_hash)

    def request_ban(self, ban: PendingBan) -> None:
        self.bans.append(ban)


ban_registry = BanRegistry()
threat_tracker = ThreatTracker()
shared_store.on_reset(threat_tracker.reset)  # tests: cleared together with the shared keys
security_events = SecurityEventSink()


def request_ban(ip: str, severity: int, reason: str, session_hash: str | None = None) -> None:
    """Apply a provisional in-memory ban now; the worker persists the final decision."""
    if ip in get_settings().rate_limit_whitelist and severity < 5:
        return
    hours = SEVERITY_HOURS.get(severity, 2)
    reason = storable(reason, BAN_REASON_MAX)  # e.g. "probe for <path>": the path may hold a NUL byte
    ban_registry.put(ip, BanInfo(until=utcnow() + timedelta(hours=hours), reason=reason, severity=severity, provisional=True))
    security_events.request_ban(PendingBan(ip, severity, reason, session_hash))


def hold_ban(ip: str, severity: int, reason: str) -> None:
    """``request_ban`` without queueing the ban for the database: blocks ``ip`` in this worker's memory at once while
    another worker, which claimed it (``ThreatTracker.claim_ban``), persists it; the next ban sync brings it here (or,
    after an admin's Lift, removes it: ``BanInfo.held``)."""
    if ip in get_settings().rate_limit_whitelist and severity < 5:
        return
    hours = SEVERITY_HOURS.get(severity, 2)
    ban_registry.put(ip, BanInfo(until=utcnow() + timedelta(hours=hours), reason=reason, severity=severity,
                                 provisional=True, held=True))


FLUSH_EVENTS_MAX = 1000  # queued events written per flush (every 2 s); the rest wait for the next one
_FLUSH_INFO = "ironpulse.security_flush"  # Session.info: the batches the session's open transaction is writing


@dataclass(slots=True)
class _FlushBatch:
    """What one flush took off the queues. It goes back to the FRONT of the queues unless the transaction writing it
    COMMITs (``_flush_not_committed``); ``confirmed`` - the registry entries of the bans it wrote, with what the log
    says about them - is applied only after that COMMIT (``_flush_committed``)."""

    events: list[PendingEvent] = field(default_factory=list)
    bans: list[PendingBan] = field(default_factory=list)
    confirmed: list[tuple[str, BanInfo, int, bool]] = field(default_factory=list)  # (ip, ban, offence, subscriber)


def _requeue(batches: Sequence[_FlushBatch]) -> None:
    events = [e for b in batches for e in b.events]
    pending = [p for b in batches for p in b.bans]
    if events:
        security_events.events.extendleft(reversed(events))  # oldest first again (a full queue drops its newest)
    if pending:
        security_events.bans.extendleft(reversed(pending))
    if events or pending:
        log.warning("security flush was not committed - %s events and %s ban decisions put back in the queue",
                    len(events), len(pending))


@event.listens_for(Session, "after_commit")
def _flush_committed(session: Session) -> None:
    if session.get_nested_transaction() is not None:  # a SAVEPOINT released - not the COMMIT
        return
    for batch in session.info.pop(_FLUSH_INFO, None) or []:
        for ip, info, offence, subscriber in batch.confirmed:
            try:
                ban_registry.put(ip, info)
                log.warning("IP %s banned until %s (severity %s, offence %s, subscriber=%s): %s",
                            ip, info.until or "PERMANENT", info.severity, offence, subscriber, info.reason)
            except Exception:  # noqa: BLE001 - never break a COMMIT that already happened
                log.exception("could not confirm the ban of %s in memory", ip)


@event.listens_for(Session, "after_transaction_end")
def _flush_not_committed(session: Session, transaction: Any) -> None:
    """The session's outermost transaction ended without the COMMIT above (rollback, failed COMMIT, closed session)."""
    if transaction.nested or transaction.parent is not None:
        return
    batches = session.info.pop(_FLUSH_INFO, None)
    if batches:
        try:
            _requeue(batches)
        except Exception:  # noqa: BLE001 - never break a rollback
            log.exception("could not put an uncommitted security flush back in the queue")


async def flush_security_queue(db: Any) -> int:
    """Persist queued events and finalise queued bans (the caller commits). Returns number of rows written.

    Per call: at most FLUSH_EVENTS_MAX events and MAX_BANS_PER_FLUSH ban decisions (one per address), each kind with a
    constant number of queries; whatever is left stays queued for the next flush (every 2 s). Nothing taken off the
    queues is lost: without the caller's COMMIT it goes back to their front (module docstring)."""
    batch = _FlushBatch()
    while security_events.events and len(batch.events) < FLUSH_EVENTS_MAX:
        batch.events.append(security_events.events.popleft())
    pending: dict[str, PendingBan] = {}
    while security_events.bans and len(pending) < MAX_BANS_PER_FLUSH:
        pb = security_events.bans.popleft()
        cur = pending.get(pb.ip)
        if cur is None:
            pending[pb.ip] = pb
        else:  # the same address queued again: one decision, at the higher severity
            top = pb if pb.severity > cur.severity else cur
            pending[pb.ip] = PendingBan(pb.ip, top.severity, top.reason, cur.session_hash or pb.session_hash)
    batch.bans = list(pending.values())
    if not batch.events and not batch.bans:
        return 0
    batches = db.info.setdefault(_FLUSH_INFO, [])
    batches.append(batch)
    try:
        written = await _insert_events(db, batch.events) if batch.events else 0
        if batch.bans:
            written += await _finalise_bans(db, batch.bans, batch)
        return written
    except BaseException:
        if not db.in_transaction():  # failed before any statement: no transaction will end and put it back
            if batch in batches:
                batches.remove(batch)
            _requeue([batch])
        raise


def _row_problem(exc: BaseException) -> bool:
    """A value the database refuses - SQLSTATE class 22 (data exception) or 23 (integrity), or one the driver can't
    encode: a problem of one row. Anything else (a statement timeout, a lost connection) is the database's."""
    seen: set[int] = set()
    stack: list[Any] = [exc]
    while stack:
        e = stack.pop()
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        if isinstance(e, (UnicodeError, DataError, IntegrityError)):
            return True
        code = getattr(e, "sqlstate", None) or getattr(e, "pgcode", None)
        if isinstance(code, str) and code[:2] in ("22", "23"):
            return True
        stack += [getattr(e, "orig", None), e.__cause__, e.__context__]
    return False


async def _insert_events(db: Any, events: Sequence[PendingEvent]) -> int:
    """INSERT the events inside a SAVEPOINT: a value the database refuses (``storable`` should have made every value
    storable - this is the last line of defence) then costs only its own row, never the batch or the caller's
    transaction. Anything else is raised: the caller's transaction rolls back and the batch is queued again."""
    from app.models import SecurityEvent

    users = await _session_users(db, {e.session_hash for e in events if e.session_hash})

    def row(e: PendingEvent) -> Any:
        return SecurityEvent(
            ip=e.ip, user_id=users.get(e.session_hash or ""), method=e.method or "-", path=e.path or "-",
            user_agent=e.user_agent, category=e.category, severity=e.severity, detail=e.detail, action=e.action,
            created_at=e.created_at, ai_reviewed=e.reviewed is not None, ai_verdict=e.reviewed,
        )

    def forget(rows: Iterable[Any]) -> None:
        for r in rows:
            if r in db:
                db.expunge(r)

    rows = [row(e) for e in events]
    try:
        async with db.begin_nested():
            db.add_all(rows)
        return len(rows)
    except Exception as exc:  # noqa: BLE001 - only a refused value is handled here
        forget(rows)
        if not _row_problem(exc):
            raise
    written = 0
    for e in events:  # one at a time: only the refused rows are dropped
        r = row(e)
        try:
            async with db.begin_nested():
                db.add(r)
            written += 1
        except Exception as exc:  # noqa: BLE001
            forget([r])
            if not _row_problem(exc):
                raise
            log.error("security event of %s dropped - the database refused a value (%s): %s %r",
                      e.ip, exc.__class__.__name__, e.category.value, e.detail[:120])
    return written


async def _session_users(db: Any, hashes: Iterable[str]) -> dict[str, int]:
    hashes = set(hashes)
    if not hashes:
        return {}
    from app.models import UserSession

    rows = await db.execute(select(UserSession.id, UserSession.user_id).where(UserSession.id.in_(hashes)))
    return {sid: uid for sid, uid in rows.all()}


async def _finalise_bans(db: Any, pending: Sequence[PendingBan], batch: _FlushBatch | None = None) -> int:
    """Ban decisions for distinct addresses with set-based lookups (<= 5 queries for the whole batch): which already
    have an active ban, 30-day offence counts, the sessions' users, and which users / addresses are subscribers.

    Serialised across workers by a transaction-level advisory lock (released at the flush's COMMIT): two workers
    flushing a ban for the same address - a Redis outage, or a direct ``request_ban`` caller without the claim - no
    longer both pass the active-ban check, the second one then counted as offence 2 (PERMANENT for a non-subscriber).
    The second flush waits for the first one's commit and sees its row.

    The bans become confirmed registry entries (and are logged as banned) only after the caller's COMMIT; until then
    the provisional entries keep blocking the addresses on this worker."""
    from app.models import IpBan, Subscription, User

    if batch is None:  # a direct caller: the confirmations still wait for its COMMIT
        batch = _FlushBatch()
        db.info.setdefault(_FLUSH_INFO, []).append(batch)
    await db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": BAN_FLUSH_LOCK_KEY})
    now = utcnow()
    ips = [pb.ip for pb in pending]
    banned = set((await db.execute(
        select(IpBan.ip).where(IpBan.ip.in_(ips), *_active_clause(IpBan, now)).distinct()
    )).scalars().all())
    todo = [pb for pb in pending if pb.ip not in banned]  # already banned (another process got there first)
    if not todo:
        return 0
    ips = [pb.ip for pb in todo]
    offenses = {ip: int(n) for ip, n in (await db.execute(
        select(IpBan.ip, func.count(IpBan.id)).where(IpBan.ip.in_(ips), IpBan.created_at > now - timedelta(days=30))
        .group_by(IpBan.ip)
    )).all()}
    users = await _session_users(db, (pb.session_hash for pb in todo if pb.session_hash))
    member_ids: set[int] = set()
    if users:
        member_ids = set((await db.execute(
            select(Subscription.customer_id).where(Subscription.customer_id.in_(set(users.values())),
                                                   Subscription.status == SubscriptionStatus.ACTIVE).distinct()
        )).scalars().all())
    # also any subscriber who last signed in from the address (partial index ix_users_last_login_ip)
    member_ips = set((await db.execute(
        select(User.last_login_ip).join(Subscription, Subscription.customer_id == User.id)
        .where(User.last_login_ip.in_(ips), Subscription.status == SubscriptionStatus.ACTIVE).distinct()
    )).scalars().all())
    rows = []
    for pb in todo:
        user_id = users.get(pb.session_hash) if pb.session_hash else None
        has_sub = (user_id is not None and user_id in member_ids) or pb.ip in member_ips
        count = offenses.get(pb.ip, 0) + 1
        duration = ban_duration(pb.severity, offense_count=count, has_subscription=has_sub)
        until = None if duration is None else now + duration
        reason = storable(pb.reason, BAN_REASON_MAX)
        rows.append(IpBan(ip=pb.ip, user_id=user_id, reason=reason, severity=pb.severity, banned_until=until,
                          created_by="auto", offense_count=count, had_subscription=has_sub))
        batch.confirmed.append((pb.ip, BanInfo(until=until, reason=reason, severity=pb.severity), count, has_sub))
    db.add_all(rows)
    return len(rows)


def _active_clause(model: Any, now: datetime) -> list[Any]:
    return [model.lifted_at.is_(None), or_(model.banned_until.is_(None), model.banned_until > now)]


def _strongest(rows: Iterable[Any], into: dict[str, BanInfo] | None = None) -> dict[str, BanInfo]:
    """(ip, banned_until, severity) rows -> the deciding ban per address: a permanent one, else the latest expiry."""
    bans = {} if into is None else into
    for ip, until, severity in rows:
        cur = bans.get(ip)
        if cur is None or until is None or (cur.until is not None and until > cur.until):
            bans[ip] = BanInfo(until=until, reason="", severity=severity)
    return bans


async def sync_bans_from_db(db: Any) -> int:
    """Bring this process's ban registry up to date with ip_bans. Returns how many bans it applied.

    The first call (start-up) and one call every FULL_RESYNC_SECONDS rebuild the registry from every active ban,
    streamed in SYNC_ROWS batches. Every other call is incremental (see the module docstring)."""
    state = ban_registry.sync
    mono = time.monotonic()
    if not state.loaded or mono - state.full_at >= FULL_RESYNC_SECONDS:
        return await _full_ban_sync(db, mono)
    return await _incremental_ban_sync(db, mono)


async def _full_ban_sync(db: Any, mono: float) -> int:
    from app.models import IpBan

    now = utcnow()
    # the next incremental syncs re-read from the newest row whose transaction began SYNC_REREAD_SECONDS ago (a
    # backward primary-key scan): a ban still uncommitted while this load ran is picked up by them
    floor = int(await db.scalar(select(func.max(IpBan.id)).where(
        IpBan.created_at < func.now() - timedelta(seconds=SYNC_REREAD_SECONDS))) or 0)
    bans: dict[str, BanInfo] = {}
    result = await db.stream(
        select(IpBan.ip, IpBan.banned_until, IpBan.severity).where(*_active_clause(IpBan, now))
        .execution_options(yield_per=SYNC_ROWS)
    )
    async for part in result.partitions():
        _strongest(part, bans)
    ban_registry.replace_all(bans)
    state = ban_registry.sync
    state.loaded, state.full_at, state.max_id = True, mono, floor
    state.marks = deque([(mono, floor)])
    return len(bans)


async def _incremental_ban_sync(db: Any, mono: float) -> int:
    from app.models import AuditLog, IpBan

    state = ban_registry.sync
    now = utcnow()
    applied = 0
    changed: set[str] = set()
    last = state.reread_floor(mono)
    while True:  # rows added since the floor, SYNC_ROWS at a time
        rows = (await db.execute(
            select(IpBan.id, IpBan.ip, IpBan.banned_until, IpBan.severity, IpBan.lifted_at)
            .where(IpBan.id > last).order_by(IpBan.id).limit(SYNC_ROWS)
        )).all()
        for row in rows:
            if row.lifted_at is not None:
                changed.add(row.ip)  # its address's current state is reloaded below
            elif row.banned_until is None or row.banned_until > now:
                ban_registry.put(row.ip, BanInfo(until=row.banned_until, reason="", severity=row.severity))
                applied += 1
        if rows:
            last = rows[-1].id
        if len(rows) < SYNC_ROWS:
            break
    state.max_id = max(state.max_id, last)
    state.marks.append((mono, state.max_id))
    # older rows that were lifted, extended or replaced since: their addresses, from the audit trail
    since = now - timedelta(seconds=SYNC_REREAD_SECONDS)
    changed.update(ip for ip in (await db.execute(
        select(AuditLog.detail["ip"].astext)
        .where(AuditLog.created_at >= since, AuditLog.action.in_(BAN_CHANGE_ACTIONS)).distinct()
    )).scalars().all() if ip)
    if changed:
        applied += await _reload_addresses(db, sorted(changed), now)
    return applied


async def _reload_addresses(db: Any, ips: Sequence[str], now: datetime) -> int:
    """Set the registry entry of each address to its current database state (active bans only)."""
    from app.models import IpBan

    for start in range(0, len(ips), SYNC_ROWS):
        part = list(ips[start:start + SYNC_ROWS])
        best = _strongest((await db.execute(
            select(IpBan.ip, IpBan.banned_until, IpBan.severity).where(IpBan.ip.in_(part), *_active_clause(IpBan, now))
        )).all())
        for ip in part:
            info = best.get(ip)
            ban_registry.set_confirmed(ip, info)
            if info is None:  # lifted (or expired): this worker's copy of its score must not re-ban it
                threat_tracker.forget_local(ip)
    return len(ips)


def lift_ban_in_memory(ip: str) -> None:
    ban_registry.remove(ip)
    threat_tracker.reset(ip)
