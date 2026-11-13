"""AI security triage + IP-ban helpers (WP-G).

* ``@periodic("security_triage", every_seconds=300)``: up to 50 unreviewed events with severity >= 2,
  grouped by IP, are reviewed by the AI (``app.ai.triage``). Recommendations are applied only within policy:
  - ``extend_ban`` only for malicious/suspicious IPs. The duration always comes from
    ``app.security.bans.ban_duration`` (subscribers: max 24 h; non-subscriber repeat offenders: permanent).
    The AI may raise the observed severity by at most one step. Bans placed by a super-admin are never
    touched and whitelisted IPs are skipped (unless severity 5).
  - ``lift_ban`` only for *automatic*, temporary bans with severity <= 3, a ``benign`` verdict and no
    severity >= 4 event in the batch. Permanent bans are lifted by a super-admin only.
  Every reviewed event gets ``ai_reviewed`` + ``ai_verdict`` (JSON); super-admins are notified about new
  permanent bans; every ban change is audited.
* AI unavailable -> deterministic rule: >= 3 distinct severity >= 4 signatures from one IP -> escalate.
  Everything else stays unreviewed for the next AI run.
* Bounded backlog: a periodic run reviews up to PERIODIC_MAX_BATCHES batches (each its own transaction) within
  PERIODIC_BUDGET_S, after closing unreviewed events older than STALE_AFTER as "expired" in chunked UPDATEs; the
  "events left" figure is a capped count. A super-admin's "Run triage now" is one batch inside the request.
* Ban helpers shared with the super-admin console: ``manual_ban``, ``lift_ip_bans``, ``active_bans``.
  In-memory ban-registry changes are applied only after the database transaction commits; other processes
  pick lifts / extensions / replacements up from their audit rows (app/security/bans.py BAN_CHANGE_ACTIONS).
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import String, cast, event, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from app.ai.client import AIUnavailableError
from app.config import get_settings
from app.core.utils import utcnow
from app.db import session_scope
from app.models import AuditLog, IpBan, SecurityEvent, Subscription, User
from app.models.enums import NotificationCategory, Role, Severity, SubscriptionStatus
from app.security.bans import BanInfo, ban_duration, ban_registry, lift_ban_in_memory
from app.services.audit import audit
from app.services.batching import Chunks
from app.services.jobs import periodic
from app.services.notify import Note, notify_bulk

log = logging.getLogger("ironpulse.triage")

TRIAGE_NAME = "security_triage"
BATCH_LIMIT = 50
MIN_SEVERITY = 2
ESCALATE_SIGNATURES = 3  # deterministic rule: >= 3 distinct severity >= 4 signatures from one IP
ESCALATE_MIN_SEVERITY = 4
LIFT_MAX_SEVERITY = 3
RULES_MAX_IPS = 100
PERIODIC_AI_TIMEOUT = 45.0
# a bounded backlog: the gate can log events far faster than one batch per run reviews them
PERIODIC_MAX_BATCHES = 10  # batches of BATCH_LIMIT events per periodic run, each its own transaction ...
PERIODIC_BUDGET_S = 240.0  # ... within this time budget (the task runs every 300 s)
STALE_AFTER = timedelta(hours=48)  # unreviewed events older than this are closed as "expired", never triaged
EXPIRE_BATCH = 1000  # events closed per UPDATE (one short transaction each)
PENDING_COUNT_CAP = 10_000  # "events left" is a capped count (LIMIT), never a COUNT over the whole backlog

SOURCE_LABELS = {"auto": "Automatic", "ai": "AI triage", "superadmin": "Super-admin"}


# ===================================================================================== small helpers
def normalize_ip(value: str | None) -> str | None:
    """Canonical text form of an IPv4/IPv6 address, or None when it is not an IP address."""
    raw = (value or "").strip().strip("[]")
    if not raw or len(raw) > 45:
        return None
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError:
        return None


def lock_id(name: str) -> int:
    """Same advisory-lock id as ``app.services.jobs`` uses for periodic tasks."""
    return int.from_bytes(hashlib.blake2b(name.encode(), digest_size=8).digest(), "big", signed=True)


def _clip(value: str, n: int) -> str:
    value = " ".join((value or "").split())
    return value if len(value) <= n else value[: n - 1] + "…"


def hours_label(until: Any, now: Any = None) -> str:
    if until is None:
        return "permanently"
    delta = until - (now or utcnow())
    hours = max(1, round(delta.total_seconds() / 3600))
    return f"for {hours} h" if hours < 48 else f"for {round(hours / 24)} days"


def parse_verdict(raw: str | None) -> dict[str, Any] | None:
    """Decode the JSON stored in ``SecurityEvent.ai_verdict`` (older plain-text verdicts are wrapped)."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except ValueError:
        pass
    return {"verdict": "unknown", "rationale": raw[:400], "source": "legacy"}


# ===================================================================== registry ops after COMMIT
_OPS_KEY = "ip_ban_registry_ops"


def after_commit(db: Any, fn: Callable[[], None]) -> None:
    """Run ``fn`` (an in-memory ban-registry change) only once the session's transaction commits."""
    db.info.setdefault(_OPS_KEY, []).append(fn)


@event.listens_for(Session, "after_commit")
def _apply_registry_ops(session: Session) -> None:
    for fn in session.info.pop(_OPS_KEY, None) or []:
        try:
            fn()
        except Exception:  # pragma: no cover - defensive: never break a commit
            log.exception("ban registry update failed")


@event.listens_for(Session, "after_soft_rollback")
def _drop_registry_ops(session: Session, previous_transaction: Any) -> None:
    session.info.pop(_OPS_KEY, None)


def _registry_set(ip: str, info: BanInfo) -> Callable[[], None]:
    def op() -> None:
        ban_registry.remove(ip)  # replace (even with a shorter ban) - the DB row is the source of truth
        ban_registry.put(ip, info)

    return op


def _registry_lift(ip: str) -> Callable[[], None]:
    return lambda: lift_ban_in_memory(ip)


# ===================================================================================== ban queries
def active_ban_clause(now: Any = None) -> list[Any]:
    now = now or utcnow()
    return [IpBan.lifted_at.is_(None), or_(IpBan.banned_until.is_(None), IpBan.banned_until > now)]


async def active_bans(db: AsyncSession, ip: str) -> list[IpBan]:
    stmt = select(IpBan).where(IpBan.ip == ip, *active_ban_clause()).order_by(IpBan.id.desc())
    return list((await db.execute(stmt)).scalars().all())


def strongest(bans: Sequence[IpBan]) -> IpBan | None:
    """The ban that decides the IP's state: a permanent one first, otherwise the latest expiry."""
    if not bans:
        return None
    permanent = [b for b in bans if b.banned_until is None]
    if permanent:
        return permanent[0]
    return max(bans, key=lambda b: b.banned_until)


async def _offenses_30d(db: AsyncSession, ip: str) -> int:
    since = utcnow() - timedelta(days=30)
    return int(await db.scalar(select(func.count(IpBan.id)).where(IpBan.ip == ip, IpBan.created_at > since)) or 0)


async def _active_superadmins(db: AsyncSession) -> list[User]:
    stmt = select(User).where(User.role == Role.SUPERADMIN, User.deleted_at.is_(None), User.is_active.is_(True))
    return list((await db.execute(stmt)).scalars().all())


async def notify_permanent_ban(db: AsyncSession, ban: IpBan, why: str, admins: list[User] | None = None) -> None:
    """Tell every active super-admin about a new permanent ban: ONE bulk send (``notify_bulk``), never one per admin."""
    admins = admins if admins is not None else await _active_superadmins(db)
    if admins:
        await db.flush()  # as notify() did: the caller's pending changes are written before the notices
        await notify_bulk(db, [
            Note(admin, f"Permanent IP ban: {ban.ip}",
                 f"{ban.ip} is now banned permanently ({why}). Reason: {ban.reason}. "
                 "Only a super-admin can lift a permanent ban - review it in the security centre.",
                 link=f"/superadmin/security/ip/{ban.ip}", category=NotificationCategory.SECURITY,
                 severity=Severity.DANGER)
            for admin in admins
        ])


# ===================================================================== super-admin actions (router)
async def manual_ban(
    db: AsyncSession, *, ip: str, duration: timedelta | None, reason: str, severity: int, actor: User,
    request: Any = None,
) -> tuple[IpBan, list[IpBan]]:
    """Ban an IP by hand (replaces any active ban for it). Returns (new ban, bans it replaced). Caller commits."""
    now = utcnow()
    replaced = await active_bans(db, ip)
    for old in replaced:
        old.lifted_at = now
        old.lifted_by_id = actor.id
    offenses = await _offenses_30d(db, ip) + 1
    until = None if duration is None else now + duration
    ban = IpBan(ip=ip, reason=_clip(reason, 300), severity=max(1, min(5, severity)), banned_until=until,
                created_by="superadmin", offense_count=offenses, had_subscription=False)
    db.add(ban)
    await db.flush()
    after_commit(db, _registry_set(ip, BanInfo(until=until, reason=ban.reason, severity=ban.severity)))
    await audit(db, actor, "security.ban_manual", "ip_ban", ban.id, request=request, detail={
        "ip": ip, "until": until.isoformat() if until else "permanent", "severity": ban.severity,
        "reason": ban.reason, "replaced": [b.id for b in replaced],
    })
    return ban, replaced


async def lift_ip_bans(db: AsyncSession, ip: str, *, actor: User | None, note: str = "", request: Any = None,
                       source: str = "superadmin") -> list[IpBan]:
    """Lift every active ban of ``ip`` (DB + in-memory after commit). Caller commits."""
    bans = await active_bans(db, ip)
    if not bans:
        return []
    now = utcnow()
    for b in bans:
        b.lifted_at = now
        b.lifted_by_id = actor.id if actor is not None else None
    after_commit(db, _registry_lift(ip))
    await audit(db, actor, "security.ban_lift", "ip_ban", bans[0].id, request=request, detail={
        "ip": ip, "bans": [b.id for b in bans], "note": _clip(note, 300), "by": source,
    })
    user_ids = sorted({b.user_id for b in bans if b.user_id})
    if user_ids:  # the accounts the lifted bans name: ONE bulk send (``notify_bulk``), never one notify per account
        await db.flush()  # as notify() did: the lifted bans and the audit row are written before the notices
        await notify_bulk(db, [
            Note(uid, "Access restored",
                 "The security block on the network address you used was lifted. You can use IronPulse again - "
                 "thank you for your patience.", link="/", category=NotificationCategory.SECURITY,
                 severity=Severity.SUCCESS)
            for uid in user_ids
        ])
    return bans


# ===================================================================================== triage
@dataclass(slots=True)
class TriageAction:
    ip: str
    action: str  # banned | extended | lifted | kept | skipped | none
    detail: str
    permanent: bool = False


@dataclass(slots=True)
class TriageReport:
    mode: str  # ai | rules | idle | busy
    provider: str | None = None
    latency_ms: int | None = None
    events_reviewed: int = 0
    ips_reviewed: int = 0
    events_pending: int = 0  # capped at PENDING_COUNT_CAP
    actions: list[TriageAction] = field(default_factory=list)
    error: str | None = None

    @property
    def changes(self) -> list[TriageAction]:
        return [a for a in self.actions if a.action in ("banned", "extended", "lifted")]

    @property
    def pending_label(self) -> str:
        return f"{self.events_pending:,}+" if self.events_pending >= PENDING_COUNT_CAP else str(self.events_pending)

    def summary(self) -> str:
        if self.mode == "busy":
            return "A triage run is already in progress - try again in a minute."
        if self.mode == "idle":
            return "Nothing to triage - every security event (severity 2+) has been reviewed."
        changes = self.changes
        counts: dict[str, int] = defaultdict(int)
        for a in changes:
            counts[a.action] += 1
        parts = [f"{n} {label}" for label, n in (("new ban(s)", counts["banned"]), ("ban(s) extended", counts["extended"]),
                                                  ("ban(s) lifted", counts["lifted"])) if n]
        outcome = ", ".join(parts) if parts else "no ban changes"
        if self.mode == "rules":
            return (f"AI unavailable - deterministic rules reviewed {self.events_reviewed} event(s) from "
                    f"{self.ips_reviewed} IP(s): {outcome}. {self.pending_label} event(s) wait for the AI.")
        return (f"AI triage ({self.provider}) reviewed {self.events_reviewed} event(s) from {self.ips_reviewed} IP(s): "
                f"{outcome}." + (f" {self.pending_label} event(s) left for the next run." if self.events_pending else ""))


@dataclass(slots=True)
class IpContext:
    ip: str
    events: list[SecurityEvent]
    active: list[IpBan] = field(default_factory=list)
    offenses_30d: int = 0
    subscriber: bool = False

    @property
    def max_severity(self) -> int:
        return max((e.severity for e in self.events), default=1)

    @property
    def ban(self) -> IpBan | None:
        return strongest(self.active)

    @property
    def critical_signatures(self) -> set[tuple[str, str]]:
        return {(e.category.value, e.detail or "") for e in self.events if e.severity >= ESCALATE_MIN_SEVERITY}

    @property
    def user_id(self) -> int | None:
        return next((e.user_id for e in self.events if e.user_id), None)


async def load_contexts(db: AsyncSession, by_ip: dict[str, list[SecurityEvent]]) -> dict[str, IpContext]:
    """Ban state, 30-day offence count and subscriber flag for every IP - 4 queries, any number of IPs."""
    ips = list(by_ip)
    contexts = {ip: IpContext(ip, evs) for ip, evs in by_ip.items()}
    if not ips:
        return contexts
    now = utcnow()
    bans = (await db.execute(
        select(IpBan).where(IpBan.ip.in_(ips), *active_ban_clause(now)).order_by(IpBan.id.desc())
    )).scalars().all()
    for b in bans:
        contexts[b.ip].active.append(b)
    for ip, n in (await db.execute(
        select(IpBan.ip, func.count(IpBan.id)).where(IpBan.ip.in_(ips), IpBan.created_at > now - timedelta(days=30))
        .group_by(IpBan.ip)
    )).all():
        contexts[ip].offenses_30d = int(n)
    sub_ips = set((await db.execute(
        select(User.last_login_ip).join(Subscription, Subscription.customer_id == User.id)
        .where(User.last_login_ip.in_(ips), Subscription.status == SubscriptionStatus.ACTIVE).distinct()
    )).scalars().all())
    user_ids = {e.user_id for evs in by_ip.values() for e in evs if e.user_id}
    sub_users: set[int] = set()
    if user_ids:
        sub_users = set((await db.execute(
            select(Subscription.customer_id).where(Subscription.customer_id.in_(user_ids),
                                                   Subscription.status == SubscriptionStatus.ACTIVE).distinct()
        )).scalars().all())
    for ctx in contexts.values():
        ctx.subscriber = (ctx.ip in sub_ips or any(e.user_id in sub_users for e in ctx.events if e.user_id)
                          or any(b.had_subscription for b in ctx.active))
    return contexts


def _whitelisted(ip: str) -> bool:
    return ip in get_settings().rate_limit_whitelist


@dataclass(slots=True)
class _Writes:
    """The writes of one triage pass, made at the end in a constant number of statements (``_apply_writes``) - never
    one per address or per event: a pass reviews up to BATCH_LIMIT events of many addresses at once."""

    marks: list[dict[str, Any]] = field(default_factory=list)  # security_events: ai_reviewed + ai_verdict
    new_bans: list[IpBan] = field(default_factory=list)
    ban_updates: list[dict[str, Any]] = field(default_factory=list)  # extended bans, by primary key
    audits: list[tuple[IpBan, str, dict[str, Any]]] = field(default_factory=list)  # (ban, action, detail)
    registry: list[tuple[str, IpBan]] = field(default_factory=list)  # set in this worker's memory after COMMIT
    permanent: list[tuple[IpBan, str]] = field(default_factory=list)  # (ban, why): the super-admins are told
    lifted: list[tuple[str, list[IpBan]]] = field(default_factory=list)  # (ip, its automatic bans lifted)

    def mark(self, events: Sequence[SecurityEvent], verdict: str) -> None:
        for e in events:
            self.marks.append({"id": e.id, "ai_reviewed": True, "ai_verdict": verdict})
            set_committed_value(e, "ai_reviewed", True)
            set_committed_value(e, "ai_verdict", verdict)


def plan_escalation(ctx: IpContext, *, severity: int, reason: str, created_by: str, writes: _Writes) -> TriageAction:
    """Extend the active ban or place a new one - durations strictly from ``ban_duration``. Decides in memory; the
    rows are written by ``_apply_writes`` with the rest of the pass."""
    severity = max(1, min(5, int(severity)))
    if _whitelisted(ctx.ip) and severity < 5:
        return TriageAction(ctx.ip, "skipped", "Whitelisted address (RATE_LIMIT_WHITELIST) - not banned.")
    now = utcnow()
    current = ctx.ban
    if current is not None:
        if any(b.created_by == "superadmin" for b in ctx.active):
            return TriageAction(ctx.ip, "kept", "Banned by a super-admin - triage leaves manual bans unchanged.")
        if current.banned_until is None:
            return TriageAction(ctx.ip, "none", "Already banned permanently.")
        duration = ban_duration(severity, offense_count=max(1, current.offense_count),
                                has_subscription=ctx.subscriber or current.had_subscription)
        new_until = None if duration is None else now + duration
        if new_until is not None and new_until <= current.banned_until:
            return TriageAction(ctx.ip, "none", f"Current ban already lasts {hours_label(current.banned_until, now)}.")
        values = {"banned_until": new_until, "severity": max(current.severity, severity),
                  "reason": _clip(f"{current.reason} · extended: {reason}", 300)}
        for name, value in values.items():  # the loaded row shows it now; one bulk UPDATE writes every extension
            set_committed_value(current, name, value)
        writes.ban_updates.append({"id": current.id, **values})
        ban, action = current, "extended"
    else:
        offenses = ctx.offenses_30d + 1
        duration = ban_duration(severity, offense_count=offenses, has_subscription=ctx.subscriber)
        until = None if duration is None else now + duration
        ban = IpBan(ip=ctx.ip, user_id=ctx.user_id, reason=_clip(reason, 300), severity=severity, banned_until=until,
                    created_by=created_by, offense_count=offenses, had_subscription=ctx.subscriber)
        writes.new_bans.append(ban)
        ctx.active.insert(0, ban)
        action = "banned"
    writes.registry.append((ctx.ip, ban))
    permanent = ban.banned_until is None
    who = "subscriber" if (ctx.subscriber or ban.had_subscription) else "non-subscriber"
    detail = (f"{'Banned' if action == 'banned' else 'Ban extended'} {hours_label(ban.banned_until, now)} "
              f"(severity {severity}, offence {ban.offense_count}, {who}).")
    writes.audits.append((ban, f"security.triage_{action}", {
        "ip": ctx.ip, "until": ban.banned_until.isoformat() if ban.banned_until else "permanent",
        "severity": severity, "by": created_by, "reason": _clip(reason, 200),
    }))
    if permanent:
        writes.permanent.append((ban, "repeat offender without an active subscription"))
    return TriageAction(ctx.ip, action, detail, permanent=permanent)


def plan_lift(ctx: IpContext, verdict: str, writes: _Writes) -> TriageAction:
    """An AI-recommended lift under the deterministic policy (``lift_blockers``) - of the bans loaded for this pass."""
    if not ctx.active:
        return TriageAction(ctx.ip, "none", "Not banned - nothing to lift.")
    blockers = lift_blockers(ctx, verdict)
    if blockers:
        return TriageAction(ctx.ip, "kept", "Lift refused: " + "; ".join(blockers) + ".")
    writes.lifted.append((ctx.ip, list(ctx.active)))
    ctx.active.clear()
    return TriageAction(ctx.ip, "lifted", "Automatic ban lifted (benign, severity ≤ 3).")


async def _apply_writes(db: AsyncSession, w: _Writes, *, actor: User | None, admins: list[User] | None = None) -> None:
    """Write one pass's decisions: extended bans (one bulk UPDATE), lifted bans (one UPDATE), new bans (one multi-row
    INSERT ... RETURNING - the ids the audit rows name), the audit rows, the in-memory registry after COMMIT, the
    notices (one ``notify_bulk``) and the reviewed events (one bulk UPDATE). The caller commits."""
    now = utcnow()
    if w.ban_updates:
        await db.execute(update(IpBan), w.ban_updates)
    lifted = [b for _ip, bans in w.lifted for b in bans]
    if lifted:
        await db.execute(update(IpBan).where(IpBan.id.in_([b.id for b in lifted]))
                         .values(lifted_at=now, lifted_by_id=None).execution_options(synchronize_session=False))
        for b in lifted:
            set_committed_value(b, "lifted_at", now)
            set_committed_value(b, "lifted_by_id", None)
    if w.new_bans:
        db.add_all(w.new_bans)
        await db.flush()
    actor_id = actor.id if actor is not None else None
    db.add_all([AuditLog(actor_id=actor_id, action=action[:80], entity_type="ip_ban", entity_id=ban.id, detail=detail)
                for ban, action, detail in w.audits])
    db.add_all([AuditLog(actor_id=None, action="security.ban_lift", entity_type="ip_ban", entity_id=bans[0].id,
                         detail={"ip": ip, "bans": [b.id for b in bans], "note": "AI triage: benign traffic", "by": "ai"})
                for ip, bans in w.lifted])
    for ip, ban in w.registry:
        after_commit(db, _registry_set(ip, BanInfo(until=ban.banned_until, reason=ban.reason, severity=ban.severity)))
    for ip, _bans in w.lifted:
        after_commit(db, _registry_lift(ip))
    notes: list[Note] = []
    if w.permanent:
        admins = admins if admins is not None else await _active_superadmins(db)
        notes += [Note(admin, f"Permanent IP ban: {ban.ip}",
                       f"{ban.ip} is now banned permanently ({why}). Reason: {ban.reason}. "
                       "Only a super-admin can lift a permanent ban - review it in the security centre.",
                       link=f"/superadmin/security/ip/{ban.ip}", category=NotificationCategory.SECURITY,
                       severity=Severity.DANGER)
                  for ban, why in w.permanent for admin in admins]
    restored = sorted({b.user_id for b in lifted if b.user_id})
    notes += [Note(uid, "Access restored",
                   "The security block on the network address you used was lifted. You can use IronPulse again - "
                   "thank you for your patience.", link="/", category=NotificationCategory.SECURITY,
                   severity=Severity.SUCCESS) for uid in restored]
    if notes:
        await notify_bulk(db, notes)
    if w.marks:
        await db.execute(update(SecurityEvent), w.marks)


async def escalate(db: AsyncSession, ctx: IpContext, *, severity: int, reason: str, created_by: str,
                   actor: User | None = None, admins: list[User] | None = None) -> TriageAction:
    """Extend the active ban or place a new one (one address; a triage pass plans many and writes them together)."""
    writes = _Writes()
    action = plan_escalation(ctx, severity=severity, reason=reason, created_by=created_by, writes=writes)
    await _apply_writes(db, writes, actor=actor, admins=admins)
    return action


def lift_blockers(ctx: IpContext, verdict: str) -> list[str]:
    """Deterministic policy for AI-recommended lifts (empty list = allowed)."""
    reasons: list[str] = []
    if verdict != "benign":
        reasons.append("only a benign verdict can lift a ban")
    if any(b.created_by != "auto" for b in ctx.active):
        reasons.append("only automatic bans can be lifted by triage")
    if any(b.banned_until is None for b in ctx.active):
        reasons.append("permanent bans are lifted by a super-admin only")
    if any(b.severity > LIFT_MAX_SEVERITY for b in ctx.active):
        reasons.append(f"ban severity is above {LIFT_MAX_SEVERITY}")
    if ctx.max_severity > LIFT_MAX_SEVERITY:
        reasons.append(f"events with severity {ctx.max_severity} were logged")
    return reasons


async def ai_lift(db: AsyncSession, ctx: IpContext, verdict: str) -> TriageAction:
    """An AI-recommended lift for one address (a triage pass plans many and writes them together)."""
    writes = _Writes()
    action = plan_lift(ctx, verdict, writes)
    await _apply_writes(db, writes, actor=None)
    return action


def _verdict_json(**fields: Any) -> str:
    fields["at"] = utcnow().isoformat(timespec="seconds")
    return json.dumps(fields, ensure_ascii=False, default=str)[:4000]


async def _pending_count(db: AsyncSession) -> int:
    """Unreviewed events (severity >= 2), counted up to PENDING_COUNT_CAP only."""
    pending = (select(SecurityEvent.id).where(SecurityEvent.ai_reviewed.is_(False), SecurityEvent.severity >= MIN_SEVERITY)
               .limit(PENDING_COUNT_CAP).subquery())
    return int(await db.scalar(select(func.count()).select_from(pending)) or 0)


async def expire_stale_events(*, holder: Any = None, now: Any = None, batch: int | None = None,
                              budget_s: float | None = PERIODIC_BUDGET_S) -> int:
    """Close unreviewed events older than STALE_AFTER (any severity) as reviewed with an "expired" verdict, EXPIRE_BATCH
    per transaction (``UPDATE ... WHERE id IN (SELECT ... LIMIT n FOR UPDATE SKIP LOCKED)``). The unreviewed set - and
    with it every triage query, the backlog counters and the security pages' "pending" filter - then holds at most
    STALE_AFTER of events, however long an attack lasts. Returns how many were closed."""
    cutoff = (now or utcnow()) - STALE_AFTER
    size = max(1, int(batch or EXPIRE_BATCH))
    verdict = _verdict_json(verdict="expired", action="none", source="rules", applied="none",
                            rationale=f"Not reviewed within {int(STALE_AFTER.total_seconds() // 3600)} h - closed "
                                      "without action to keep the triage backlog bounded.")
    run = Chunks(holder=holder, budget_s=budget_s)
    total = 0
    while await run.next():
        async with session_scope() as s:
            doomed = (select(SecurityEvent.id)
                      .where(SecurityEvent.ai_reviewed.is_(False), SecurityEvent.created_at < cutoff)
                      .order_by(SecurityEvent.id).limit(size).with_for_update(skip_locked=True))
            n = (await s.execute(
                update(SecurityEvent).where(SecurityEvent.id.in_(doomed)).values(ai_reviewed=True, ai_verdict=verdict)
                .execution_options(synchronize_session=False)
            )).rowcount or 0
        total += n
        run.done(n, size)
    return total


def _evidence(ctx: IpContext) -> Any:
    from app.ai.triage import IpEvidence, clean

    cats: dict[str, int] = defaultdict(int)
    sigs: list[str] = []
    paths: list[str] = []
    agents: list[str] = []
    for e in ctx.events:
        cats[e.category.value] += 1
        sig = clean(f"{e.category.value}: {e.detail or ''} (sev {e.severity})", 90)
        if sig not in sigs and len(sigs) < 6:
            sigs.append(sig)
        p = clean(f"{e.method} {e.path}", 80)
        if p not in paths and len(paths) < 4:
            paths.append(p)
        ua = clean(e.user_agent, 70)
        if ua and ua not in agents and len(agents) < 2:
            agents.append(ua)
    ban = ctx.ban
    if ban is None:
        ban_text = "none"
    else:
        until = "permanent" if ban.banned_until is None else ban.banned_until.strftime("%Y-%m-%d %H:%M UTC")
        ban_text = f"{ban.created_by} ban until {until}, severity {ban.severity}"
    times = sorted(e.created_at for e in ctx.events)
    return IpEvidence(
        ip=ctx.ip, events=len(ctx.events), max_severity=ctx.max_severity, categories=dict(cats), signatures=sigs,
        paths=paths, user_agents=agents, first_seen=times[0].strftime("%Y-%m-%d %H:%M"),
        last_seen=times[-1].strftime("%Y-%m-%d %H:%M"), ban=ban_text, subscriber=ctx.subscriber,
        prior_bans_30d=ctx.offenses_30d,
    )


def _rules_for(ctx: IpContext, *, note: str, writes: _Writes) -> tuple[TriageAction | None, dict[str, Any]]:
    sigs = ctx.critical_signatures
    if len(sigs) >= ESCALATE_SIGNATURES:
        action = plan_escalation(ctx, severity=ctx.max_severity, created_by="auto", writes=writes,
                                 reason=f"triage rules: {len(sigs)} distinct critical attack signatures")
        verdict = {"verdict": "malicious", "action": "extend_ban", "severity": ctx.max_severity, "source": "rules",
                   "applied": action.action, "applied_detail": action.detail,
                   "rationale": f"{len(sigs)} distinct severity ≥ {ESCALATE_MIN_SEVERITY} signatures from one IP. {note}".strip()}
        return action, verdict
    return None, {"verdict": "suspicious" if ctx.max_severity >= 3 else "benign", "action": "none",
                  "severity": ctx.max_severity, "source": "rules", "applied": "none",
                  "rationale": f"Fewer than {ESCALATE_SIGNATURES} distinct critical signatures. {note}".strip()}


async def deterministic_triage(db: AsyncSession, report: TriageReport, *, actor: User | None = None) -> None:
    """AI unavailable: escalate IPs with >= 3 distinct severity >= 4 signatures; leave the rest for the AI."""
    sig = func.concat(cast(SecurityEvent.category, String), ":", func.coalesce(SecurityEvent.detail, ""))
    distinct_sigs = func.count(func.distinct(sig))
    rows = (await db.execute(
        select(SecurityEvent.ip)
        .where(SecurityEvent.ai_reviewed.is_(False), SecurityEvent.severity >= ESCALATE_MIN_SEVERITY,
               SecurityEvent.created_at >= utcnow() - STALE_AFTER)  # older ones expire unreviewed
        .group_by(SecurityEvent.ip)
        .having(distinct_sigs >= ESCALATE_SIGNATURES)
        .order_by(func.max(SecurityEvent.id).desc())
        .limit(RULES_MAX_IPS)
    )).scalars().all()
    if rows:
        events = (await db.execute(
            select(SecurityEvent).where(SecurityEvent.ip.in_(rows), SecurityEvent.ai_reviewed.is_(False))
            .order_by(SecurityEvent.id).limit(5000)
        )).scalars().all()
        by_ip: dict[str, list[SecurityEvent]] = defaultdict(list)
        for e in events:
            by_ip[e.ip].append(e)
        contexts = await load_contexts(db, by_ip)
        writes = _Writes()  # decided per address in memory, written together below (never one statement per IP)
        for _ip, ctx in contexts.items():
            action, verdict = _rules_for(ctx, note="(AI unavailable)", writes=writes)
            if action is None:
                continue  # cannot happen (pre-filtered) - keep for the AI
            writes.mark(ctx.events, _verdict_json(**verdict))
            report.actions.append(action)
            report.events_reviewed += len(ctx.events)
            report.ips_reviewed += 1
        await _apply_writes(db, writes, actor=actor)
    await db.flush()
    report.events_pending = await _pending_count(db)


async def run_triage(db: AsyncSession, *, actor: User | None = None, ai_timeout: float | None = None,
                     limit: int = BATCH_LIMIT, lock: bool = True) -> TriageReport:
    """One triage pass over at most ``limit`` events (the caller commits). Safe to call concurrently: guarded by an
    advisory lock - ``lock=False`` only for a caller that already holds it (the periodic task's batches)."""
    if lock:
        got = await db.scalar(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": lock_id(TRIAGE_NAME)})
        if not got:
            return TriageReport(mode="busy")
    events = (await db.execute(
        select(SecurityEvent)
        .where(SecurityEvent.ai_reviewed.is_(False), SecurityEvent.severity >= MIN_SEVERITY)
        .order_by(SecurityEvent.id)
        .limit(max(1, min(limit, 500)))
    )).scalars().all()
    if not events:
        return TriageReport(mode="idle")
    by_ip: dict[str, list[SecurityEvent]] = defaultdict(list)
    for e in events:
        by_ip[e.ip].append(e)
    contexts = await load_contexts(db, by_ip)
    # most severe first, so the prompt budget is spent on what matters
    ordered = sorted(contexts.values(), key=lambda c: (-c.max_severity, -len(c.events), c.ip))
    from app.ai.triage import triage_with_ai

    try:
        verdicts, result, included = await triage_with_ai([_evidence(c) for c in ordered], timeout=ai_timeout)
    except AIUnavailableError as exc:
        report = TriageReport(mode="rules", error=str(exc)[:300])
        await deterministic_triage(db, report, actor=actor)
        return report

    report = TriageReport(mode="ai", provider=result.provider, latency_ms=result.latency_ms)
    by_verdict = {v.ip: v for v in verdicts}
    writes = _Writes()  # decided per address in memory, written together below (never one statement per IP)
    for ip in included:
        ctx = contexts[ip]
        v = by_verdict.get(ip)
        if v is None:  # the model skipped this IP -> deterministic rules decide, AI gets no second chance
            action, verdict = _rules_for(ctx, note="(no AI verdict for this IP)", writes=writes)
            if action is not None:
                report.actions.append(action)
            writes.mark(ctx.events, _verdict_json(**verdict))
        else:
            if v.recommended_action == "extend_ban":
                if v.verdict == "benign":
                    action = TriageAction(ip, "kept", "Ban not extended: the verdict was benign.")
                else:
                    severity = min(v.severity, ctx.max_severity + 1)  # AI may raise severity by one step at most
                    action = plan_escalation(ctx, severity=severity, created_by="ai", writes=writes,
                                             reason=f"AI triage ({v.verdict}): {_clip(v.rationale, 160)}")
            elif v.recommended_action == "lift_ban":
                action = plan_lift(ctx, v.verdict, writes)
            else:
                action = TriageAction(ip, "none", "No action recommended.")
            report.actions.append(action)
            writes.mark(ctx.events, _verdict_json(
                verdict=v.verdict, action=v.recommended_action, severity=v.severity, source=result.provider,
                applied=action.action, applied_detail=action.detail, rationale=v.rationale,
            ))
        report.events_reviewed += len(ctx.events)
        report.ips_reviewed += 1
    await _apply_writes(db, writes, actor=actor)
    await db.flush()
    report.events_pending = await _pending_count(db)
    return report


@periodic(TRIAGE_NAME, every_seconds=300, initial_delay=90)
async def _periodic_triage(db: AsyncSession) -> int:
    """Close stale events, then review the backlog in up to PERIODIC_MAX_BATCHES batches of BATCH_LIMIT events within
    PERIODIC_BUDGET_S. Every batch is its own transaction (``db`` holds this task's advisory lock - the same lock
    ``run_triage`` takes - for the whole run). Returns the events reviewed."""
    expired = await expire_stale_events(holder=db)
    if expired:
        log.info("security triage: %s stale event(s) closed unreviewed", expired)
    run = Chunks(holder=db, budget_s=PERIODIC_BUDGET_S, max_chunks=PERIODIC_MAX_BATCHES)
    reviewed = 0
    while await run.next():
        async with session_scope() as s:
            report = await run_triage(s, ai_timeout=PERIODIC_AI_TIMEOUT, limit=BATCH_LIMIT, lock=False)
        reviewed += report.events_reviewed
        if report.mode in ("ai", "rules") and report.events_reviewed:
            log.info("security triage: %s", report.summary())
        more = report.mode == "ai" and report.events_pending > 0
        more = more or (report.mode == "rules" and report.ips_reviewed >= RULES_MAX_IPS)  # the rules have more IPs
        if not more:
            run.finish()
    return reviewed
