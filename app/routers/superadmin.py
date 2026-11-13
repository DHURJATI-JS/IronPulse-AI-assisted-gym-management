"""WP-G super-admin console.

Platform overview (aggregates only), security centre (events, per-IP drill-down, AI triage verdicts,
"Run AI triage now"), IP bans (manual ban / lift), the verification review queue with authorised document
previews, AI provider status + live provider test + the "AI usage & abuse" panel (AI queue, top AI users, paused
accounts with Lift, recent AI blocks, presence - docs/ai-guard-and-queue.md section 8), users (search, soft delete,
restore, unlock), the e-mail outbox (escaped text bodies only, retry), the platform audit log and the self-check.

Every route requires SUPERADMIN. List pages run a constant number of SQL queries (aggregates in SQL,
related rows eager-loaded or fetched with one ``IN (...)`` query), and every statement reads a bounded number of rows
however large the platform's tables grow:

* list counts stop at ``COUNT_CAP`` ("10,000+": ``_count_upto``), so a page COUNT and its OFFSET stay bounded;
* attacker-driven aggregates (security facets, top addresses, the 24 h chart, verdicts) read at most the newest
  ``SCAN_CAP`` matching events (``_newest_events`` / ``_recent_ids``) inside a time window (the security centre shows
  the last 7 days unless "All time" is chosen); the event lists are keyset windows (``?before=`` / ``?after=``);
* the audit log's area list is a loose index scan of the distinct actions, not a GROUP BY of the whole log.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from decimal import Decimal
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Path, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import Text, and_, case, cast, extract, func, literal, or_, select, text, union, update
from sqlalchemy.orm import joinedload, selectinload

from app.ai import guard as guard_module
from app.ai import scheduler as scheduler_module
from app.ai.client import AI_DISCLAIMER, AIUnavailableError, ai, flush_ai_call_logs
from app.ai.errors import AICancelled, AIRejected
from app.ai.owner import OWNER_JOB, clean_page_id, current_owner
from app.config import get_settings
from app.core.deps import DB, NotFound, SuperAdminUser, wants_json
from app.core.pagination import keyset, parse_keyset
from app.core.templating import humanize, redirect, render
from app.core.ttl_cache import TTLCache
from app.core.utils import is_safe_redirect, safe_zone, to_local, utcnow
from app.models import (
    AICallLog,
    AuditLog,
    Branch,
    BranchDocument,
    EmailOutbox,
    Employee,
    EmployeeDocument,
    Gym,
    IdentityDocument,
    IpBan,
    Job,
    Product,
    Refund,
    SecurityEvent,
    ShopOrder,
    Subscription,
    Transaction,
    User,
    VerificationRun,
    WorkoutSession,
    WorkoutSessionEvent,
)
from app.models.enums import (
    EmailStatus,
    EmploymentStatus,
    IdStatus,
    JobStatus,
    NotificationCategory,
    Role,
    Severity,
    SubscriptionStatus,
    ThreatCategory,
    TxCategory,
    TxDirection,
    TxStatus,
    VerificationStatus,
)
from app.models.shop import OrderStatus, RefundStatus
from app.security.bans import ban_registry, flush_security_queue
from app.security.net import client_ip
from app.services import security_triage as sectri  # registers @periodic("security_triage")
from app.services import jobs as jobs_service
from app.services import selfcheck as selfcheck_service
from app.services import storage
from app.services.accounts import AccountDeletionBlocked, soft_delete_user
from app.services.audit import audit
from app.services.identity_lookup import member_id
from app.services.notify import notify
from app.services.presence import presence

log = logging.getLogger("ironpulse.superadmin")
router = APIRouter()

MAX_ID = 2**63 - 1
IdParam = Annotated[int, Path(ge=1, le=MAX_ID)]
VS = VerificationStatus

SECTIONS: tuple[tuple[str, str, str, str], ...] = (
    ("overview", "Overview", "/superadmin", "home"),
    ("insights", "Site overview", "/superadmin/insights", "sparkles"),  # app/routers/superadmin_insights.py
    ("security", "Security", "/superadmin/security", "shield"),
    ("bans", "Bans", "/superadmin/bans", "lock"),
    ("trust", "Trust & safety", "/superadmin/trust", "flag"),  # app/routers/superadmin_trust.py
    ("governance", "Governance", "/superadmin/governance", "scale"),  # app/routers/superadmin_governance.py
    ("reviews", "Reviews", "/superadmin/reviews", "check-badge"),
    ("ai", "AI", "/superadmin/ai", "cpu"),
    ("users", "Users", "/superadmin/users", "users"),
    ("outbox", "Outbox", "/superadmin/outbox", "envelope"),
    ("audit", "Audit log", "/superadmin/audit", "document"),
    ("selfcheck", "Self-check", "/superadmin/selfcheck", "check-circle"),
    ("loadtest", "Load test", "/superadmin/load-test", "chart"),
)

BRANCH_QUEUE = (VS.NEEDS_REVIEW, VS.AI_UNAVAILABLE)
BRANCH_DECIDABLE = (VS.NEEDS_REVIEW, VS.AI_UNAVAILABLE, VS.REJECTED)
EVENTS_PER_PAGE = 40
IP_EVENTS_PER_PAGE = 30
BANS_PER_PAGE = 25
USERS_PER_PAGE = 25
OUTBOX_PER_PAGE = 30
AUDIT_PER_PAGE = 40
QUEUE_LIMIT = 50  # review-queue items per page
COUNT_CAP = 10_000  # list counts stop here and show "10,000+" (a COUNT reads at most COUNT_CAP + 1 rows)
SCAN_CAP = 50_000  # an aggregate over security events reads at most this many of the newest matching rows
EVENT_WINDOWS: tuple[tuple[str, str, timedelta | None], ...] = (
    ("24h", "Last 24 hours", timedelta(hours=24)),
    ("7d", "Last 7 days", timedelta(days=7)),
    ("30d", "Last 30 days", timedelta(days=30)),
    ("all", "All time", None),
)
EVENT_WINDOW_MAP = {key: (label, td) for key, label, td in EVENT_WINDOWS}
DEFAULT_EVENT_WINDOW = "7d"
AUDIT_MAX_ACTIONS = 500  # distinct audit actions read for the area filter (a loose index scan stops here)
TRIAGE_UI_TIMEOUT = 15.0
SELFTEST_TIMEOUT = 15.0
LIVE_ACTIVE_WINDOW = timedelta(hours=6)

BAN_DURATIONS: tuple[tuple[str, str, timedelta | None], ...] = (
    ("1h", "1 hour", timedelta(hours=1)),
    ("6h", "6 hours", timedelta(hours=6)),
    ("12h", "12 hours", timedelta(hours=12)),
    ("24h", "24 hours", timedelta(hours=24)),
    ("3d", "3 days", timedelta(days=3)),
    ("7d", "7 days", timedelta(days=7)),
    ("30d", "30 days", timedelta(days=30)),
    ("permanent", "Permanent", None),
)
BAN_DURATION_MAP = {key: (label, td) for key, label, td in BAN_DURATIONS}

CATEGORY_LABELS = {
    "sqli": "SQL injection", "xss": "Cross-site scripting", "traversal": "Path traversal",
    "command_injection": "Command injection", "scanner": "Scanner", "probe": "Probe", "bruteforce": "Brute force",
    "rate_limit": "Rate limit", "csrf": "CSRF", "upload": "Upload", "fraud": "Fraud", "ai_abuse": "AI abuse",
    "other": "Other",
}
ROLE_LABELS = {"superadmin": "Super-admins", "owner": "Gym owners", "staff": "Staff", "customer": "Members"}
ROLE_TONES = {"superadmin": "violet", "owner": "blue", "staff": "amber", "customer": "green"}
ROLE_NAMES = {"superadmin": "Super-admin", "owner": "Owner", "staff": "Staff", "customer": "Member"}

# AI usage & abuse panel (docs/ai-guard-and-queue.md section 8)
TOP_AI_USERS = 10
AI_COOLDOWNS_SHOWN = 50
AI_BLOCKS_SHOWN = 20
AI_RATE_LIMIT_DETAIL = "rate limit 'ai'"  # SecurityEvent.detail prefix of the gate's per-IP 'ai' bucket (security/gate.py)
# Load safety of the AI page: every aggregate reads a bounded window through an index, whatever the tables hold
AI_WINDOW = timedelta(hours=24)  # call-log aggregates (ix_ai_call_logs_created_at), cancelled jobs (ix_jobs_status_finished)
AI_BLOCKS_WINDOW = timedelta(days=7)  # "Recent AI blocks" (ix_security_events_category_created); older: security centre
AI_DEMO_COUNT_TTL = 600.0  # seconds the demo-account count is cached per process (its e-mail suffix match scans users)
AI_LANES = {"llm": ("Language model", "cpu"), "tts": ("Voice · Kokoro", "activity")}
AI_PRIORITIES = {"realtime": ("Realtime", "live coach and voice"), "interactive": ("Interactive", "someone waits on screen"),
                 "background": ("Background", "jobs nobody watches")}
AI_STOP_REASONS = {  # scheduler.cancel(reason=...) -> what the admin reads
    "page_closed": "Tab closed", "left": "Left the site", "client_left": "Connection dropped", "logout": "Signed out",
    "signed_out": "Signed out everywhere", "account_deleted": "Account deleted", "reset": "Server reset",
}
AI_QUEUE_REFUSALS = {"too_many": "Too many at once (one person)", "busy": "Queue full", "expired": "Waited too long"}
AI_GUARD_REFUSALS = {  # guard.snapshot()['refused'] codes -> what the admin reads
    "quota": "Over their limit", "duplicate": "Asked the same again", "spam": "Looked like spam",
    "cooldown": "Asked while paused",
}
_CANCELLED_CALL = re.compile(r"^cancelled \(([a-z_]{1,40})\)")
BRANCH_STATE_TONES = {
    "verified": "green", "needs_review": "amber", "ai_unavailable": "violet", "queued": "blue", "processing": "blue",
    "rejected": "red", "draft": "zinc",
}
USER_STATES = (("active", "Active"), ("locked", "Locked"), ("deleted", "Deleted"), ("all", "All"))
OUTBOX_STATES = ("queued", "sending", "sent", "logged", "failed")

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
PARTIAL_IP_RE = re.compile(r"^[0-9a-fA-F:.]{2,45}$")
AREA_RE = re.compile(r"^[a-z0-9_]{1,40}$")
SEARCH_RE = re.compile(r"^[a-z0-9_.\-]{1,80}$")


# ===================================================================================== helpers
def _clip(value: Any, limit: int) -> str:
    """One line of untrusted text: control characters removed, whitespace collapsed, truncated."""
    text_ = " ".join(_CONTROL.sub(" ", str(value or "")).split())
    return text_ if len(text_) <= limit else text_[: limit - 1].rstrip() + "…"


def _note(value: Any) -> str:
    """Multi-line note: control characters removed (newlines kept), trimmed. Length is validated by callers."""
    raw = _CONTROL.sub("", str(value or "").replace("\r\n", "\n"))
    return re.sub(r"\n{3,}", "\n\n", raw).strip()


def _form_str(form: Any, key: str) -> str:
    value = form.get(key)
    return value if isinstance(value, str) else ""


def _int_param(raw: Any, default: int, lo: int, hi: int) -> int:
    try:
        n = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _back(candidate: str | None, default: str) -> str:
    nxt = (candidate or "").strip()
    if is_safe_redirect(nxt) and nxt.startswith("/superadmin"):
        return nxt
    return default


def _count(entity: Any, *where: Any) -> Any:
    stmt = select(func.count()).select_from(entity)
    if where:
        stmt = stmt.where(*where)
    return stmt.scalar_subquery()


def _count_upto(entity: Any, *where: Any, cap: int | None = None) -> Any:
    """Scalar subquery: matching rows counted up to ``cap + 1`` (default COUNT_CAP; ``> cap`` means "cap+"), a bounded
    read where a plain COUNT would read every matching row of an unbounded (attacker-driven or log) table."""
    cap = COUNT_CAP if cap is None else cap
    inner = select(literal(1)).select_from(entity)
    if where:
        inner = inner.where(*where)
    return select(func.count()).select_from(inner.limit(cap + 1).subquery()).scalar_subquery()


def count_text(n: Any, cap: int | None = None) -> str:
    """A bounded count for people: "1,234", or "10,000+" once it passed the cap."""
    cap = COUNT_CAP if cap is None else cap
    n = int(n or 0)
    return f"{cap:,}+" if n > cap else f"{n:,}"


def _capped(n: Any, cap: int | None = None) -> tuple[int, bool]:
    """(count to show, capped?) of a ``_count_upto`` result."""
    cap = COUNT_CAP if cap is None else cap
    n = int(n or 0)
    return (cap, True) if n > cap else (n, False)


def _recent_ids(model: Any, n: int | None = None) -> Any:
    """``model.id`` among the newest ``n`` ids (default SCAN_CAP): a primary-key range read however large the table
    is (ids grow with time, so this is "the most recent n rows" for tables whose time column has no index)."""
    n = SCAN_CAP if n is None else n
    newest = select(func.coalesce(func.max(model.id), 0)).scalar_subquery()
    return model.id > newest - n


def _newest_events(*where: Any, cols: tuple[Any, ...] | None = None, cap: int | None = None) -> Any:
    """Subquery of the newest ``cap`` (default SCAN_CAP) security events matching ``where`` (a backward range read of a
    created_at index): every aggregate over it reads a bounded number of rows, however many events an attack logged."""
    columns = cols or (SecurityEvent.id, SecurityEvent.ip, SecurityEvent.category, SecurityEvent.severity,
                       SecurityEvent.action, SecurityEvent.ai_reviewed, SecurityEvent.created_at)
    return (select(*columns).where(*where).order_by(SecurityEvent.created_at.desc())
            .limit(SCAN_CAP if cap is None else cap).subquery())


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def day_bounds(day: date, tz_name: str | None) -> tuple[datetime, datetime]:
    """UTC bounds of a calendar day in the viewer's time zone."""
    tz = safe_zone(tz_name)
    start = datetime.combine(day, dtime.min, tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), dtime.min, tzinfo=tz)
    return start.astimezone(UTC), end.astimezone(UTC)


@dataclass
class Pager:
    page: int
    per_page: int
    total: int
    path: str
    params: dict[str, str] = field(default_factory=dict)
    capped: bool = False  # ``total`` stopped at COUNT_CAP ("10,000+"): narrow the filters to reach older rows

    def __post_init__(self) -> None:
        self.page = max(1, min(self.page, self.pages))

    @property
    def pages(self) -> int:
        return max(1, math.ceil(self.total / self.per_page))

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.per_page

    @property
    def first(self) -> int:
        return 0 if not self.total else self.offset + 1

    @property
    def last(self) -> int:
        return min(self.total, self.offset + self.per_page)

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.pages

    def url(self, page: int | None = None) -> str:
        query = {k: v for k, v in self.params.items() if v not in (None, "")}
        if page and page > 1:
            query["page"] = str(page)
        return self.path + ("?" + urlencode(query) if query else "")


def link(path: str, params: dict[str, str]) -> str:
    return Pager(1, 1, 0, path, params).url()


def column_chart(points: list[tuple[str, int, str]], *, width: int = 720, height: int = 168, top: int = 10,
                 bottom: int = 24) -> dict[str, Any]:
    """SVG geometry for a single-series column chart (attributes only - CSP safe).

    ``points``: [(tick label or "", value, tooltip)]. Zero values get a 2 px stub so the axis reads."""
    n = max(1, len(points))
    peak = max((v for _, v, _ in points), default=0)
    plot_h = height - top - bottom
    slot = width / n
    bar_w = max(2.0, slot * 0.66)
    bars = []
    for i, (tick, value, tip) in enumerate(points):
        h = (plot_h * value / peak) if peak else 0.0
        h = max(h, 3.0) if value else 2.0
        bars.append({
            "x": round(i * slot + (slot - bar_w) / 2, 2), "y": round(top + plot_h - h, 2), "w": round(bar_w, 2),
            "h": round(h, 2), "zero": not value, "value": value, "tip": tip, "tick": tick,
            "cx": round(i * slot + slot / 2, 2), "slot_x": round(i * slot, 2), "slot_w": round(slot, 2),
        })
    return {"width": width, "height": height, "baseline": top + plot_h, "tick_y": height - 6, "bars": bars,
            "peak": peak, "total": sum(v for _, v, _ in points)}


async def _user_map(db: Any, ids: Any) -> dict[int, User]:
    wanted = {int(i) for i in ids if i}
    if not wanted:
        return {}
    rows = (await db.execute(select(User).where(User.id.in_(wanted)))).scalars().all()
    return {u.id: u for u in rows}


def _review_counts() -> tuple[Any, Any, Any]:
    return (
        _count(Branch, Branch.verification_status.in_(BRANCH_QUEUE)),
        _count(Employee, Employee.verification_status == VS.NEEDS_REVIEW, Employee.deleted_at.is_(None),
               Employee.status != EmploymentStatus.TERMINATED),
        _count(IdentityDocument, IdentityDocument.status == IdStatus.NEEDS_REVIEW),
    )


async def nav_counts(db: Any) -> dict[str, int]:
    """Badges of the section navigation - one query. The review queues are counted exactly (they are the work list);
    failed mail, active bans and untriaged events stop at COUNT_CAP + 1 (bounded reads of attacker-driven tables)."""
    now = utcnow()
    rb, re_, ri = _review_counts()
    row = (await db.execute(select(
        rb.label("rb"), re_.label("re"), ri.label("ri"),
        _count_upto(EmailOutbox, EmailOutbox.status == EmailStatus.FAILED).label("mail"),
        _count_upto(IpBan, *sectri.active_ban_clause(now)).label("bans"),
        _count_upto(SecurityEvent, SecurityEvent.ai_reviewed.is_(False),
                    SecurityEvent.severity >= sectri.MIN_SEVERITY).label("sec"),
        # governance: suspended gyms not yet archived - each has staff pay / members / orders to settle
        _count_upto(Gym, Gym.is_active.is_(False), Gym.archived_at.is_(None)).label("gov"),
    ))).one()
    return {"reviews": int(row.rb + row.re + row.ri), "outbox": int(row.mail), "bans": int(row.bans),
            "security": int(row.sec), "reviews_branches": int(row.rb), "reviews_staff": int(row.re),
            "reviews_identity": int(row.ri), "governance": int(row.gov)}


async def _page(request: Request, db: Any, template: str, section: str, ctx: dict[str, Any], *,
                status_code: int = 200, counts: dict[str, int] | None = None) -> Any:
    if counts is None:
        counts = await nav_counts(db)
    return render(request, f"superadmin/{template}",
                  {"sections": SECTIONS, "section": section, "counts": counts, "category_labels": CATEGORY_LABELS,
                   "count_cap": COUNT_CAP, **ctx},
                  status_code=status_code)


def _parse_verdicts(events: Any) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for e in events:
        if e.ai_verdict:
            v = sectri.parse_verdict(e.ai_verdict)
            if v:
                out[e.id] = v
    return out


async def latest_verdicts(db: Any, *, limit: int = 8) -> list[dict[str, Any]]:
    """Most recent triage verdict per IP (last 30 days, among the newest SCAN_CAP events) - one bounded query."""
    since = utcnow() - timedelta(days=30)
    sub = (
        select(SecurityEvent.ip, SecurityEvent.ai_verdict, SecurityEvent.id, SecurityEvent.created_at)
        .where(_recent_ids(SecurityEvent), SecurityEvent.ai_verdict.is_not(None), SecurityEvent.created_at >= since)
        .distinct(SecurityEvent.ip)
        .order_by(SecurityEvent.ip, SecurityEvent.id.desc())
        .subquery()
    )
    rows = (await db.execute(select(sub).order_by(sub.c.id.desc()).limit(limit))).all()
    out = []
    for r in rows:
        v = sectri.parse_verdict(r.ai_verdict) or {}
        out.append({**v, "ip": r.ip, "seen_at": r.created_at})
    return out


# ===================================================================================== overview
async def _hourly_events(db: Any, tz_name: str | None, now: datetime) -> dict[str, Any]:
    """Events per hour over the last 24 h, from the newest SCAN_CAP events (``sampled``: the cap was reached, so the
    earliest hours of a very busy day are incomplete)."""
    local_now = to_local(now, tz_name)
    start_local = (local_now - timedelta(hours=23)).replace(minute=0, second=0, microsecond=0)
    start = start_local.astimezone(UTC)
    recent = _newest_events(SecurityEvent.created_at >= start, cols=(SecurityEvent.created_at,))
    bucket = func.floor((extract("epoch", recent.c.created_at) - start.timestamp()) / 3600).label("b")
    rows = (await db.execute(select(bucket, func.count().label("n")).group_by(text("b")))).all()
    counts = {int(r.b): int(r.n) for r in rows if r.b is not None}
    points = []
    for i in range(24):
        at = start_local + timedelta(hours=i)
        n = counts.get(i, 0)
        tick = at.strftime("%H:%M") if i % 6 == 0 else ""
        points.append((tick, n, f"{at:%H:%M}–{(at + timedelta(hours=1)):%H:%M} · {n} event{'s' if n != 1 else ''}"))
    chart = column_chart(points)
    chart["sampled"] = sum(counts.values()) >= SCAN_CAP
    return chart


def _failed_calls(log: Any = AICallLog) -> Any:
    """SUM() of provider failures. Calls stopped because their requester left (``cancelled``: tab closed, signed
    out ...) are neither successes nor failures - people leaving pages must not look like a failing provider.
    ``log``: the call-log columns (the table, or a window of it)."""
    return func.sum(case((and_(log.ok.is_(False), log.cancelled.is_(False)), 1), else_=0))


def _cancelled_calls(log: Any = AICallLog) -> Any:
    return func.sum(case((log.cancelled.is_(True), 1), else_=0))


async def _provider_rows(db: Any, since: datetime) -> list[dict[str, Any]]:
    """In-memory provider state (ai.status) + 24 h aggregates from the persisted AI call log. ``calls_24h`` counts
    every attempt; the error rate is failures per *completed* call (cancelled ones are shown on their own)."""
    ok_latency = case((AICallLog.ok.is_(True), AICallLog.latency_ms))
    agg = {
        r.provider: r for r in (await db.execute(
            select(
                AICallLog.provider,
                func.count().label("calls"),
                _failed_calls().label("failures"),
                _cancelled_calls().label("cancelled"),
                func.avg(ok_latency).label("avg_ms"),
                func.percentile_cont(0.95).within_group(ok_latency).label("p95_ms"),
                func.max(AICallLog.created_at).label("last_at"),
            ).where(AICallLog.created_at >= since).group_by(AICallLog.provider)
        )).all()
    }
    rows = []
    for rank, p in enumerate(ai.status()["providers"], start=1):
        a = agg.get(p["name"])
        calls = int(a.calls) if a else 0
        failures = int(a.failures or 0) if a else 0
        cancelled = int(a.cancelled or 0) if a else 0
        completed = calls - cancelled
        rows.append({
            **p, "rank": rank, "calls_24h": calls, "failures_24h": failures, "cancelled_24h": cancelled,
            "completed_24h": completed,
            "avg_ms": int(a.avg_ms) if a and a.avg_ms is not None else None,
            "p95_ms": int(a.p95_ms) if a and a.p95_ms is not None else None,
            "last_at": a.last_at if a else None,
            "error_rate": round(100 * failures / completed) if completed else None,
            "last_ok": datetime.fromtimestamp(p["last_ok_at"], UTC) if p.get("last_ok_at") else None,
        })
    return rows


@router.get("/superadmin")
async def overview(request: Request, db: DB, admin: SuperAdminUser):
    now = utcnow()
    day_ago, week_ago, month_ago = now - timedelta(hours=24), now - timedelta(days=7), now - timedelta(days=30)
    rb, re_, ri = _review_counts()
    open_orders = (OrderStatus.PAID, OrderStatus.PROCESSING, OrderStatus.READY_FOR_PICKUP, OrderStatus.SHIPPED)
    pending_refunds = (RefundStatus.REQUESTED, RefundStatus.APPROVED, RefundStatus.PROCESSING)
    k_row = (await db.execute(select(
        _count(Gym, Gym.is_active.is_(True)).label("gyms"),
        _count(Gym, Gym.online_enabled.is_(True), Gym.online_open.is_(True), Gym.is_active.is_(True)).label("gyms_online"),
        _count(Subscription, Subscription.status == SubscriptionStatus.ACTIVE).label("subs"),
        _count(Subscription, Subscription.status == SubscriptionStatus.ACTIVE,
               Subscription.activated_at >= month_ago).label("subs_new"),
        _count(User, User.deleted_at.is_not(None)).label("users_deleted"),
        _count(User, User.deleted_at.is_(None), User.created_at >= week_ago).label("users_new"),
        _count(User, User.deleted_at.is_(None), User.locked_until > now).label("users_locked"),
        # attacker-driven tables: bounded counts ("10,000+")
        _count_upto(IpBan, *sectri.active_ban_clause(now)).label("bans"),
        _count_upto(IpBan, IpBan.lifted_at.is_(None), IpBan.banned_until.is_(None)).label("bans_perm"),
        _count_upto(IpBan, _recent_ids(IpBan), IpBan.created_at >= day_ago).label("bans_new"),
        _count_upto(SecurityEvent, SecurityEvent.created_at >= day_ago).label("events"),
        _count_upto(SecurityEvent, SecurityEvent.created_at >= day_ago, SecurityEvent.action != "logged").label("blocked"),
        _count_upto(SecurityEvent, SecurityEvent.ai_reviewed.is_(False),
                    SecurityEvent.severity >= sectri.MIN_SEVERITY).label("untriaged"),
        _count_upto(SecurityEvent, SecurityEvent.ai_reviewed.is_(False),
                    SecurityEvent.severity >= 4).label("untriaged_critical"),
        rb.label("rev_branches"), re_.label("rev_staff"), ri.label("rev_ids"),
        _count(Product).label("products"),
        _count(Product, Product.is_active.is_(True)).label("products_active"),
        _count(Product, Product.is_active.is_(True), Product.stock <= Product.low_stock_threshold).label("low_stock"),
        _count(ShopOrder, ShopOrder.created_at >= month_ago).label("orders_30d"),
        _count(ShopOrder, ShopOrder.status.in_(open_orders)).label("orders_open"),
        _count(Refund, Refund.status.in_(pending_refunds)).label("refunds_pending"),
        _count(WorkoutSession, WorkoutSession.status.in_(("active", "paused")),
               WorkoutSession.started_at >= now - LIVE_ACTIVE_WINDOW).label("live_now"),
        _count(WorkoutSession, WorkoutSession.started_at >= week_ago).label("live_7d"),
        _count(WorkoutSession, WorkoutSession.started_at >= week_ago, WorkoutSession.status == "completed").label("live_done_7d"),
        _count(WorkoutSession, WorkoutSession.started_at >= month_ago,
               WorkoutSession.status == "stopped_for_safety").label("live_safety_30d"),
        _count(WorkoutSessionEvent, WorkoutSessionEvent.kind == "red_flag",
               WorkoutSessionEvent.created_at >= week_ago).label("live_red_flags_7d"),
    ))).one()
    k = {key: int(value or 0) for key, value in k_row._mapping.items()}
    k["reviews"] = k["rev_branches"] + k["rev_staff"] + k["rev_ids"]

    role_rows = dict((await db.execute(
        select(User.role, func.count()).where(User.deleted_at.is_(None)).group_by(User.role)
    )).all())
    roles = [(ROLE_LABELS[r.value], int(role_rows.get(r, 0)), ROLE_TONES[r.value], f"/superadmin/users?role={r.value}")
             for r in (Role.CUSTOMER, Role.OWNER, Role.STAFF, Role.SUPERADMIN)]
    branch_rows = dict((await db.execute(
        select(Branch.verification_status, func.count()).group_by(Branch.verification_status)
    )).all())
    branch_states = [("AI unavailable" if s == VS.AI_UNAVAILABLE else s.value.replace("_", " ").capitalize(),
                      int(branch_rows.get(s, 0)), BRANCH_STATE_TONES[s.value],
                      "/superadmin/reviews" if s in BRANCH_QUEUE else None)
                     for s in (VS.VERIFIED, VS.NEEDS_REVIEW, VS.AI_UNAVAILABLE, VS.QUEUED, VS.PROCESSING, VS.REJECTED, VS.DRAFT)]

    revenue: dict[str, dict[str, Decimal]] = {}
    for currency, category, total in (await db.execute(
        select(Transaction.currency, Transaction.category, func.sum(Transaction.amount))
        .where(Transaction.direction == TxDirection.INCOMING, Transaction.status == TxStatus.SUCCESS,
               func.coalesce(Transaction.completed_at, Transaction.created_at) >= month_ago)
        .group_by(Transaction.currency, Transaction.category)
    )).all():
        bucket = revenue.setdefault(currency, {"total": Decimal("0"), "subscriptions": Decimal("0"), "shop": Decimal("0"),
                                               "other": Decimal("0")})
        amount = Decimal(total or 0)
        bucket["total"] += amount
        key = {TxCategory.SUBSCRIPTION: "subscriptions", TxCategory.SHOP_SALE: "shop"}.get(category, "other")
        bucket[key] += amount
    revenue_rows = sorted(({"currency": c, **v} for c, v in revenue.items()), key=lambda r: -r["total"])

    paid_orders = (OrderStatus.PAID, OrderStatus.PROCESSING, OrderStatus.READY_FOR_PICKUP, OrderStatus.SHIPPED,
                   OrderStatus.COMPLETED, OrderStatus.PARTIALLY_REFUNDED)
    shop_revenue = [(c, Decimal(t or 0)) for c, t in (await db.execute(
        select(ShopOrder.currency, func.sum(ShopOrder.total - ShopOrder.refunded_total))
        .where(ShopOrder.status.in_(paid_orders), ShopOrder.paid_at >= month_ago)
        .group_by(ShopOrder.currency)
    )).all()]

    # the last 24 h by category / address: aggregated over the newest SCAN_CAP events of the window (bounded)
    recent = _newest_events(SecurityEvent.created_at >= day_ago)
    cat_rows = (await db.execute(
        select(recent.c.category, func.count().label("n")).group_by(recent.c.category).order_by(func.count().desc())
    )).all()
    events_by_cat = [(CATEGORY_LABELS.get(c.value, c.value), int(n), f"/superadmin/security?category={c.value}&range=24h")
                     for c, n in cat_rows]
    top_ips = (await db.execute(
        select(recent.c.ip, func.count().label("n"), func.max(recent.c.severity).label("sev"))
        .group_by(recent.c.ip).order_by(func.count().desc(), recent.c.ip).limit(6)
    )).all()
    critical = (await db.execute(
        select(SecurityEvent).where(_recent_ids(SecurityEvent), SecurityEvent.severity >= 4)
        .order_by(SecurityEvent.id.desc()).limit(5)
    )).scalars().all()
    chart = await _hourly_events(db, admin.timezone, now)

    # jobs / mail per status: bounded per-status counts (index reads of (status, ...)), one statement
    q_row = (await db.execute(select(
        *(_count_upto(Job, Job.status == s).label(f"job_{s.value}") for s in JobStatus),
        *(_count_upto(EmailOutbox, EmailOutbox.status == s).label(f"mail_{s.value}") for s in EmailStatus),
    ))).one()
    jobs = {s.value: int(getattr(q_row, f"job_{s.value}") or 0) for s in JobStatus}
    mails = {s.value: int(getattr(q_row, f"mail_{s.value}") or 0) for s in EmailStatus}

    providers = await _provider_rows(db, day_ago)

    attention: list[tuple[str, str, str, str]] = []
    if k["reviews"]:
        attention.append(("red", "check-badge", f"{k['reviews']} verification review{'s' if k['reviews'] != 1 else ''} waiting",
                          "/superadmin/reviews"))
    if k["untriaged_critical"]:
        attention.append(("red", "shield", f"{count_text(k['untriaged_critical'])} critical security event(s) not triaged yet",
                          "/superadmin/security?triage=pending&min_severity=4"))
    if mails["failed"]:
        attention.append(("amber", "envelope", f"{count_text(mails['failed'])} e-mail{'s' if mails['failed'] != 1 else ''} failed to send",
                          "/superadmin/outbox?status=failed"))
    if jobs["failed"]:
        attention.append(("amber", "alert", f"{count_text(jobs['failed'])} background job{'s' if jobs['failed'] != 1 else ''} failed",
                          "/superadmin/selfcheck"))
    if k["refunds_pending"]:
        attention.append(("blue", "receipt", f"{k['refunds_pending']} shop refund{'s' if k['refunds_pending'] != 1 else ''} pending",
                          "/superadmin/audit?area=shop"))
    if not ai.status()["available"]:
        attention.append(("amber", "cpu", "Every AI provider is offline - AI features show their fallbacks", "/superadmin/ai"))

    counts = {"reviews": k["reviews"], "outbox": mails["failed"], "bans": k["bans"], "security": k["untriaged"]}
    return await _page(request, db, "overview.html", "overview", {
        "k": k, "roles": roles, "users_total": sum(r[1] for r in roles), "branch_states": branch_states,
        "branches_total": sum(b[1] for b in branch_states), "revenue": revenue_rows, "shop_revenue": shop_revenue,
        "events_by_cat": events_by_cat, "events_max": max((n for _, n, _ in events_by_cat), default=0),
        "top_ips": top_ips, "critical": critical, "chart": chart, "jobs": jobs, "mails": mails,
        "providers": providers, "attention": attention, "ai_feature": True,
    }, counts=counts)


# ===================================================================================== security centre
@dataclass
class EventFilter:
    category: str = ""
    min_severity: int = 0
    ip: str = ""
    ip_exact: bool = True
    day: date | None = None
    day_raw: str = ""
    triage: str = ""
    window: str = DEFAULT_EVENT_WINDOW  # 24h | 7d | 30d | all (a chosen day replaces it)
    errors: dict[str, str] = field(default_factory=dict)

    def params(self) -> dict[str, str]:
        return {"category": self.category, "min_severity": str(self.min_severity or ""), "ip": self.ip,
                "day": self.day.isoformat() if self.day else "", "triage": self.triage,
                "range": "" if self.window == DEFAULT_EVENT_WINDOW else self.window}

    @property
    def active(self) -> bool:
        return bool(self.category or self.min_severity or self.ip or self.day or self.triage
                    or self.window != DEFAULT_EVENT_WINDOW)

    @property
    def window_label(self) -> str:
        return f"{self.day:%d %b %Y}" if self.day else EVENT_WINDOW_MAP[self.window][0]


def parse_event_filter(qp: Any) -> EventFilter:
    f = EventFilter()
    cat = (qp.get("category") or "").strip().lower()
    if cat:
        if cat in CATEGORY_LABELS:
            f.category = cat
        else:
            f.errors["category"] = "Unknown category."
    sev = (qp.get("min_severity") or "").strip()
    if sev:
        if sev.isdigit() and 1 <= int(sev) <= 5:
            f.min_severity = int(sev)
        else:
            f.errors["min_severity"] = "Severity goes from 1 to 5."
    ip = (qp.get("ip") or "").strip()
    if ip:
        norm = sectri.normalize_ip(ip)
        if norm:
            f.ip, f.ip_exact = norm, True
        elif PARTIAL_IP_RE.match(ip):
            f.ip, f.ip_exact = ip.lower(), False
        else:
            f.errors["ip"] = "Enter an IP address or the start of one (e.g. 203.0.113.)."
    day = (qp.get("day") or "").strip()
    if day:
        try:
            parsed = date.fromisoformat(day)
            if parsed.year < 2000 or parsed > date.today() + timedelta(days=1):
                raise ValueError(day)
            f.day = parsed
        except ValueError:
            f.errors["day"] = "Pick a valid date."
            f.day_raw = day[:20]
    tri = (qp.get("triage") or "").strip()
    if tri:
        if tri in ("pending", "reviewed"):
            f.triage = tri
        else:
            f.errors["triage"] = "Unknown triage filter."
    rng = (qp.get("range") or "").strip()
    if rng:
        if rng in EVENT_WINDOW_MAP:
            f.window = rng
        else:
            f.errors["range"] = "Choose a time range from the list."
    return f


def event_where(f: EventFilter, tz_name: str | None, *, skip_category: bool = False,
                now: datetime | None = None) -> list[Any]:
    where: list[Any] = []
    span = EVENT_WINDOW_MAP[f.window][1]
    if span is not None and not f.day:  # the time window bounds every count and aggregate of the page
        where.append(SecurityEvent.created_at >= (now or utcnow()) - span)
    if f.category and not skip_category:
        where.append(SecurityEvent.category == ThreatCategory(f.category))
    if f.min_severity:
        where.append(SecurityEvent.severity >= f.min_severity)
    if f.ip:
        where.append(SecurityEvent.ip == f.ip if f.ip_exact else SecurityEvent.ip.startswith(f.ip, autoescape=True))
    if f.day:
        start, end = day_bounds(f.day, tz_name)
        where += [SecurityEvent.created_at >= start, SecurityEvent.created_at < end]
    if f.triage == "pending":
        where += [SecurityEvent.ai_reviewed.is_(False), SecurityEvent.severity >= sectri.MIN_SEVERITY]
    elif f.triage == "reviewed":
        where.append(SecurityEvent.ai_reviewed.is_(True))
    return where


def _event_window_links(path: str, params: dict[str, str], window: Any) -> dict[str, str | None]:
    """Newer / Older / Newest links of a keyset window of events (newest first on screen)."""
    items = list(window.items) if window.newest_first else list(reversed(window.items))
    base = {k: v for k, v in params.items() if k not in ("before_id", "after_id", "page")}
    newer = older = None
    if items:
        if window.newest_first:
            older = items[-1].id if window.has_more else None
            newer = items[0].id if params.get("before_id") else None
        else:
            older = items[-1].id
            newer = items[0].id if window.has_more else None
    return {
        "items": items,
        "older_url": link(path, {**base, "before_id": str(older)}) if older else None,
        "newer_url": link(path, {**base, "after_id": str(newer)}) if newer else None,
        "newest_url": link(path, base) if (params.get("before_id") or params.get("after_id")) else None,
    }


async def _event_window(db: Any, request: Request, where: list[Any], path: str, params: dict[str, str],
                        per_page: int) -> dict[str, Any]:
    """One keyset window of matching events, newest first, ordered by (created_at, id): a backward range read of a
    created_at index whatever the depth (no OFFSET)."""
    k = parse_keyset(request, per_page, per_page)
    window = await keyset(db, select(SecurityEvent).where(*where), SecurityEvent.id, before_id=k.before_id,
                          after_id=k.after_id, limit=per_page, max_limit=per_page, order_col=SecurityEvent.created_at)
    cursor = {"before_id": str(k.before_id) if k.before_id else "", "after_id": str(k.after_id) if k.after_id else ""}
    return _event_window_links(path, {**params, **cursor}, window)


@router.get("/superadmin/security")
async def security_page(request: Request, db: DB, admin: SuperAdminUser):
    """Events of the chosen window (default: the last 7 days). Every statement is bounded: the match count stops at
    COUNT_CAP, facets and top addresses aggregate the newest SCAN_CAP matching events, the list is a keyset window."""
    f = parse_event_filter(request.query_params)
    now = utcnow()
    where = event_where(f, admin.timezone, now=now)
    total, total_capped = _capped(await db.scalar(select(_count_upto(SecurityEvent, *where))))
    params = f.params()
    win = await _event_window(db, request, where, "/superadmin/security", params, EVENTS_PER_PAGE)
    events = win["items"]
    facet_sample = _newest_events(*event_where(f, admin.timezone, skip_category=True, now=now))
    facet_rows = (await db.execute(
        select(facet_sample.c.category, func.count()).group_by(facet_sample.c.category).order_by(func.count().desc())
    )).all()
    sample = _newest_events(*where)
    top_ips = (await db.execute(
        select(sample.c.ip, func.count().label("n"), func.max(sample.c.severity).label("sev"),
               func.max(sample.c.created_at).label("last"), func.sum(func.count()).over().label("seen"))
        .group_by(sample.c.ip).order_by(func.count().desc(), sample.c.ip).limit(8)
    )).all()
    verdicts = await latest_verdicts(db)
    users = await _user_map(db, (e.user_id for e in events))
    facets = [(c.value, CATEGORY_LABELS.get(c.value, c.value), int(n),
               link("/superadmin/security", {**params, "category": c.value})) for c, n in facet_rows]
    return await _page(request, db, "security.html", "security", {
        "f": f, "events": events, "total": total, "total_capped": total_capped,
        "older_url": win["older_url"], "newer_url": win["newer_url"], "newest_url": win["newest_url"],
        "verdict_of": _parse_verdicts(events), "users": users,
        "facets": facets, "facet_all": link("/superadmin/security", {**params, "category": ""}),
        "facets_sampled": sum(int(n) for _, _, n, _ in facets) >= SCAN_CAP, "scan_cap": SCAN_CAP,
        "top_sampled": bool(top_ips) and int(top_ips[0].seen or 0) >= SCAN_CAP,
        "top_ips": top_ips, "top_max": max((r.n for r in top_ips), default=0), "verdicts": verdicts,
        "categories": list(CATEGORY_LABELS.items()), "windows": [(k_, label) for k_, label, _ in EVENT_WINDOWS if k_ != DEFAULT_EVENT_WINDOW],
        "ai_feature": True, "whitelist": get_settings().rate_limit_whitelist,
    }, status_code=400 if f.errors else 200)


@router.post("/superadmin/security/triage")
async def run_triage_now(request: Request, db: DB, admin: SuperAdminUser):
    await flush_security_queue(db)  # include events the gate logged in the last couple of seconds
    report = await sectri.run_triage(db, actor=admin, ai_timeout=TRIAGE_UI_TIMEOUT)
    if report.mode in ("ai", "rules"):
        await audit(db, admin, "security.triage_run", "security_event", None, request=request, detail={
            "mode": report.mode, "provider": report.provider, "events": report.events_reviewed,
            "ips": report.ips_reviewed, "changes": [f"{a.ip}: {a.action}" for a in report.changes][:20],
        })
    await db.commit()
    if wants_json(request):
        return JSONResponse({
            "mode": report.mode, "provider": report.provider, "latency_ms": report.latency_ms,
            "events_reviewed": report.events_reviewed, "ips_reviewed": report.ips_reviewed,
            "events_pending": report.events_pending, "summary": report.summary(),
            "actions": [{"ip": a.ip, "action": a.action, "detail": a.detail, "permanent": a.permanent} for a in report.actions],
            "disclaimer": AI_DISCLAIMER,
        })
    tone = {"ai": "success", "rules": "warning", "busy": "warning"}.get(report.mode, "info")
    return redirect("/superadmin/security", report.summary(), tone)


@router.get("/superadmin/security/ip/{ip}")
async def ip_detail(ip: str, request: Request, db: DB, admin: SuperAdminUser):
    norm = sectri.normalize_ip(ip)
    if norm is None:
        raise NotFound("That is not a valid IP address.")
    if norm != ip:
        return redirect(f"/superadmin/security/ip/{norm}", status_code=307)
    now = utcnow()
    # figures over the address's newest SCAN_CAP events (one attacking address can log millions); first / last seen
    # are min / max reads of ix_security_events_ip_created (one index probe each)
    sample = _newest_events(SecurityEvent.ip == norm,
                            cols=(SecurityEvent.category, SecurityEvent.severity, SecurityEvent.action,
                                  SecurityEvent.ai_reviewed, SecurityEvent.ai_verdict, SecurityEvent.user_id,
                                  SecurityEvent.created_at))
    stats = (await db.execute(
        select(func.count().label("n"), func.max(sample.c.severity).label("sev"),
               select(func.min(SecurityEvent.created_at)).where(SecurityEvent.ip == norm).scalar_subquery().label("first"),
               func.max(sample.c.created_at).label("last"),
               func.sum(case((sample.c.action != "logged", 1), else_=0)).label("blocked"),
               func.sum(case((sample.c.ai_reviewed.is_(False), 1), else_=0)).label("pending"))
        .select_from(sample)
    )).one()
    total = int(stats.n or 0)
    total_capped = total >= SCAN_CAP
    win = await _event_window(db, request, [SecurityEvent.ip == norm], f"/superadmin/security/ip/{norm}", {},
                              IP_EVENTS_PER_PAGE)
    events = win["items"]
    cats = (await db.execute(
        select(sample.c.category, func.count()).group_by(sample.c.category).order_by(func.count().desc())
    )).all()
    # the gate bans an internet IPv6 client by its /64 (app/security/ratelimit.py address_bucket): list (and lift) those
    # bans here too, next to the address's own
    from app.security.ratelimit import address_bucket

    bucket = address_bucket(norm)
    ban_keys = [norm] if bucket == norm else [norm, bucket]
    bans = (await db.execute(select(IpBan).where(IpBan.ip.in_(ban_keys)).order_by(IpBan.id.desc()).limit(50))
            ).scalars().all()
    verdict_rows = (await db.execute(
        select(sample.c.ai_verdict, func.max(sample.c.created_at).label("at"), func.count().label("n"))
        .where(sample.c.ai_verdict.is_not(None))
        .group_by(sample.c.ai_verdict).order_by(text("at DESC")).limit(6)
    )).all()
    # accounts seen here: one IN over a UNION of two index lookups (an OR of the two would scan every user)
    seen_ids = union(
        select(User.id).where(User.last_login_ip == norm),
        select(sample.c.user_id).where(sample.c.user_id.is_not(None)),
    ).subquery()
    accounts = (await db.execute(
        select(User).where(User.id.in_(select(seen_ids.c.id))).order_by(User.id).limit(20)
    )).scalars().all()
    people = await _user_map(db, [b.lifted_by_id for b in bans] + [b.user_id for b in bans] + [e.user_id for e in events])
    active = [b for b in bans if b.lifted_at is None and (b.banned_until is None or b.banned_until > now)]
    current = sectri.strongest(active)
    return await _page(request, db, "security_ip.html", "security", {
        "ip": norm, "stats": stats, "total": total, "total_capped": total_capped, "events": events,
        "older_url": win["older_url"], "newer_url": win["newer_url"], "newest_url": win["newest_url"],
        "scan_cap": SCAN_CAP, "categories": cats,
        "bans": bans, "active_bans": active, "current": current,
        "registry": ban_registry.get(norm) or (ban_registry.get(bucket) if bucket != norm else None),
        "ban_bucket": bucket if bucket != norm else None,
        "verdicts": [{**(sectri.parse_verdict(r.ai_verdict) or {}), "seen_at": r.at, "events": int(r.n)} for r in verdict_rows],
        "verdict_of": _parse_verdicts(events), "accounts": accounts, "people": people,
        "durations": BAN_DURATIONS, "form": {"ip": norm, "duration": "24h", "severity": "3", "reason": ""}, "errors": {},
        "whitelisted": norm in get_settings().rate_limit_whitelist, "is_self": norm == client_ip(request),
        "ai_feature": True,
    })


# ===================================================================================== bans
async def _bans_view(request: Request, db: Any, admin: User, *, form: dict[str, str] | None = None,
                     errors: dict[str, str] | None = None, status_code: int = 200) -> Any:
    qp = request.query_params
    state = qp.get("state") if qp.get("state") in ("active", "past", "all") else "active"
    ipq = (qp.get("ip") or "").strip()[:45]
    now = utcnow()
    active_c = and_(*sectri.active_ban_clause(now))
    past_c = or_(IpBan.lifted_at.is_not(None), and_(IpBan.banned_until.is_not(None), IpBan.banned_until <= now))
    where: list[Any] = [active_c] if state == "active" else ([past_c] if state == "past" else [])
    ip_error = None
    if ipq:
        norm = sectri.normalize_ip(ipq)
        if norm:
            from app.security.ratelimit import address_bucket

            # an internet IPv6 address is banned by its /64 (the gate's key): find those bans too
            where.append(IpBan.ip.in_(sorted({norm, address_bucket(norm)})))
            ipq = norm
        elif PARTIAL_IP_RE.match(ipq):
            where.append(IpBan.ip.startswith(ipq.lower(), autoescape=True))
        else:
            ip_error = "Enter an IP address or the start of one."
    # bans are never purged and attackers create them: every count is bounded (COUNT_CAP, "10,000+")
    tabs = (await db.execute(select(
        _count_upto(IpBan, active_c).label("active"), _count_upto(IpBan, past_c).label("past"),
        _count_upto(IpBan).label("all"),
        _count_upto(IpBan, IpBan.lifted_at.is_(None), IpBan.banned_until.is_(None)).label("permanent"),
        _count_upto(IpBan, *where).label("total"),
    ))).one()
    total, capped = _capped(tabs.total)
    pager = Pager(_int_param(qp.get("page"), 1, 1, 100_000), BANS_PER_PAGE, total, "/superadmin/bans",
                  {"state": state, "ip": ipq if not ip_error else ""}, capped=capped)
    bans = (await db.execute(
        select(IpBan).where(*where).order_by(IpBan.id.desc()).offset(pager.offset).limit(pager.per_page)
    )).scalars().all()
    people = await _user_map(db, [b.lifted_by_id for b in bans] + [b.user_id for b in bans])
    defaults = {"ip": "", "duration": "24h", "severity": "3", "reason": ""}
    return await _page(request, db, "bans.html", "bans", {
        "bans": bans, "pager": pager, "state": state, "ipq": ipq, "ip_error": ip_error, "tabs": tabs,
        "people": people, "durations": BAN_DURATIONS, "form": {**defaults, **(form or {})}, "errors": errors or {},
        "my_ip": client_ip(request), "whitelist": get_settings().rate_limit_whitelist,
    }, status_code=400 if ip_error else status_code)


@router.get("/superadmin/bans")
async def bans_page(request: Request, db: DB, admin: SuperAdminUser):
    return await _bans_view(request, db, admin)


def validate_ban_form(form: Any, request: Request) -> tuple[dict[str, str], dict[str, str], str | None, int | None]:
    values = {
        "ip": _clip(_form_str(form, "ip"), 60), "duration": _form_str(form, "duration").strip(),
        "severity": _form_str(form, "severity").strip() or "3", "reason": _clip(_form_str(form, "reason"), 400),
    }
    errors: dict[str, str] = {}
    ip = sectri.normalize_ip(values["ip"])
    if not values["ip"]:
        errors["ip"] = "Enter the IP address to ban."
    elif ip is None:
        errors["ip"] = "That is not a valid IPv4 or IPv6 address."
    elif ip == client_ip(request):
        errors["ip"] = "This is the address you are using right now - banning it would lock you out."
    if values["duration"] not in BAN_DURATION_MAP:
        errors["duration"] = "Choose how long the ban lasts."
    sev = values["severity"]
    severity = int(sev) if sev.isdigit() and 1 <= int(sev) <= 5 else None
    if severity is None:
        errors["severity"] = "Severity goes from 1 (low) to 5 (critical)."
    if len(values["reason"]) < 5:
        errors["reason"] = "Give a short reason (at least 5 characters)."
    elif len(values["reason"]) > 300:
        errors["reason"] = "Keep the reason under 300 characters."
    return values, errors, ip, severity


@router.post("/superadmin/bans")
async def create_ban(request: Request, db: DB, admin: SuperAdminUser):
    form = await request.form()
    values, errors, ip, severity = validate_ban_form(form, request)
    if errors or ip is None or severity is None:
        return await _bans_view(request, db, admin, form=values, errors=errors, status_code=400)
    label, duration = BAN_DURATION_MAP[values["duration"]]
    _, replaced = await sectri.manual_ban(db, ip=ip, duration=duration, reason=values["reason"], severity=severity,
                                          actor=admin, request=request)
    await db.commit()
    msg = f"{ip} is banned {'permanently' if duration is None else 'for ' + label}."
    if replaced:
        msg += f" It replaces {len(replaced)} earlier active ban{'s' if len(replaced) != 1 else ''}."
    if ip in get_settings().rate_limit_whitelist:
        msg += " Note: this address is in RATE_LIMIT_WHITELIST - only rate limits are skipped, the ban still applies."
    return redirect(_back(_form_str(form, "next"), "/superadmin/bans"), msg, "success")


@router.post("/superadmin/bans/{ban_id}/lift")
async def lift_ban(ban_id: IdParam, request: Request, db: DB, admin: SuperAdminUser):
    form = await request.form()
    ban = await db.get(IpBan, ban_id)
    if ban is None:
        raise NotFound("Ban not found.")
    back = _back(_form_str(form, "next"), "/superadmin/bans")
    note = _clip(_form_str(form, "note"), 300)
    lifted = await sectri.lift_ip_bans(db, ban.ip, actor=admin, note=note or "Lifted by the super-admin", request=request)
    if not lifted:
        return redirect(back, f"{ban.ip} has no active ban - nothing to lift.", "info")
    await db.commit()
    n = len(lifted)
    return redirect(back, f"Ban on {ban.ip} lifted ({n} active ban{'s' if n != 1 else ''}). The address can use the site again.",
                    "success")


# ===================================================================================== review queue
@router.get("/superadmin/reviews")
async def reviews_page(request: Request, db: DB, admin: SuperAdminUser):
    """One queue at a time (``?tab=``), QUEUE_LIMIT per page (``?page=``), oldest first. The tabs show each queue's
    true size (the nav-badge query already counts them)."""
    counts = await nav_counts(db)
    sizes = {"branches": counts["reviews_branches"], "staff": counts["reviews_staff"],
             "identity": counts["reviews_identity"]}
    tab = request.query_params.get("tab")
    if tab not in sizes:
        tab = "branches" if sizes["branches"] or not (sizes["staff"] or sizes["identity"]) else (
            "staff" if sizes["staff"] else "identity")
    pager = Pager(_int_param(request.query_params.get("page"), 1, 1, 100_000), QUEUE_LIMIT, sizes[tab],
                  "/superadmin/reviews", {"tab": tab})
    branches: list[Branch] = []
    employees: list[Employee] = []
    identities: list[IdentityDocument] = []
    if tab == "branches":
        branches = list((await db.execute(
            select(Branch).options(joinedload(Branch.gym).joinedload(Gym.owner))
            .where(Branch.verification_status.in_(BRANCH_QUEUE)).order_by(Branch.updated_at, Branch.id)
            .offset(pager.offset).limit(pager.per_page)
        )).unique().scalars().all())
    elif tab == "staff":
        employees = list((await db.execute(
            select(Employee).options(joinedload(Employee.gym), joinedload(Employee.user))
            .where(Employee.verification_status == VS.NEEDS_REVIEW, Employee.deleted_at.is_(None),
                   Employee.status != EmploymentStatus.TERMINATED)
            .order_by(Employee.updated_at, Employee.id).offset(pager.offset).limit(pager.per_page)
        )).unique().scalars().all())
    else:
        identities = list((await db.execute(
            select(IdentityDocument).options(joinedload(IdentityDocument.user), joinedload(IdentityDocument.employee))
            .where(IdentityDocument.status == IdStatus.NEEDS_REVIEW)
            .order_by(IdentityDocument.updated_at, IdentityDocument.id).offset(pager.offset).limit(pager.per_page)
        )).unique().scalars().all())
    branch_ids = [b.id for b in branches]
    emp_ids = [e.id for e in employees]
    doc_counts: dict[int, int] = {}
    run_info: dict[int, Any] = {}
    if branch_ids:
        doc_counts = dict((await db.execute(
            select(BranchDocument.branch_id, func.count()).where(BranchDocument.branch_id.in_(branch_ids))
            .group_by(BranchDocument.branch_id)
        )).all())
        run_info = {r.branch_id: r for r in (await db.execute(
            select(VerificationRun.branch_id, func.count().label("n"), func.max(VerificationRun.created_at).label("last"))
            .where(VerificationRun.branch_id.in_(branch_ids)).group_by(VerificationRun.branch_id)
        )).all()}
    emp_docs: dict[int, int] = {}
    if emp_ids:
        emp_docs = dict((await db.execute(
            select(EmployeeDocument.employee_id, func.count()).where(EmployeeDocument.employee_id.in_(emp_ids))
            .group_by(EmployeeDocument.employee_id)
        )).all())
    return await _page(request, db, "reviews.html", "reviews", {
        "branches": branches, "employees": employees, "identities": identities, "doc_counts": doc_counts,
        "run_info": run_info, "emp_docs": emp_docs, "tab": tab, "tab_sizes": sizes, "pager": pager, "ai_feature": True,
    }, counts=counts)


def _failing(checks: Any) -> int:
    n = 0
    for c in checks or []:
        if isinstance(c, dict) and (c.get("status") == "fail" or c.get("passed") is False):
            n += 1
    return n


def _decision_form(form: Any) -> tuple[dict[str, str], dict[str, str]]:
    values = {"decision": _form_str(form, "decision").strip(), "note": _note(_form_str(form, "note"))}
    errors: dict[str, str] = {}
    if values["decision"] not in ("approve", "reject"):
        errors["decision"] = "Choose whether to approve or reject."
    if len(values["note"]) < 3:
        errors["note"] = "Write a short note - it is sent to the person concerned (at least 3 characters)."
    elif len(values["note"]) > 2000:
        errors["note"] = "Keep the note under 2000 characters."
    return values, errors


async def _branch_review_view(request: Request, db: Any, branch_id: int, *, form: dict[str, str] | None = None,
                              errors: dict[str, str] | None = None, status_code: int = 200) -> Any:
    branch = (await db.execute(
        select(Branch)
        .options(joinedload(Branch.gym).joinedload(Gym.owner), selectinload(Branch.documents),
                 selectinload(Branch.photos), selectinload(Branch.hours))
        .where(Branch.id == branch_id)
    )).unique().scalar_one_or_none()
    if branch is None:
        raise NotFound("Branch not found.")
    runs = (await db.execute(
        select(VerificationRun).where(VerificationRun.branch_id == branch.id).order_by(VerificationRun.id.desc()).limit(20)
    )).scalars().all()
    distance_km = None
    if None not in (branch.latitude, branch.longitude, branch.geocoded_latitude, branch.geocoded_longitude):
        from app.core.geo import haversine_km

        distance_km = haversine_km(branch.latitude, branch.longitude, branch.geocoded_latitude, branch.geocoded_longitude)
    return await _page(request, db, "review.html", "reviews", {
        "kind": "branch", "branch": branch, "runs": runs, "reviewers": await _user_map(db, (r.reviewer_id for r in runs)),
        "decidable": branch.verification_status in BRANCH_DECIDABLE, "failing": _failing(branch.verification_checks),
        "distance_km": distance_km, "form": form or {"decision": "", "note": ""}, "errors": errors or {},
        "action": f"/superadmin/reviews/branches/{branch.id}/decide", "ai_feature": True,
    }, status_code=status_code)


@router.get("/superadmin/reviews/branches/{branch_id}")
async def branch_review(branch_id: IdParam, request: Request, db: DB, admin: SuperAdminUser):
    return await _branch_review_view(request, db, branch_id)


@router.post("/superadmin/reviews/branches/{branch_id}/decide")
async def branch_decide(branch_id: IdParam, request: Request, db: DB, admin: SuperAdminUser):
    form = await request.form()
    values, errors = _decision_form(form)
    branch = (await db.execute(
        select(Branch).options(joinedload(Branch.gym)).where(Branch.id == branch_id)
    )).unique().scalar_one_or_none()
    if branch is None:
        raise NotFound("Branch not found.")
    if branch.verification_status not in BRANCH_DECIDABLE:
        errors["decision"] = "This branch is not waiting for a review any more."
    if errors:
        return await _branch_review_view(request, db, branch_id, form=values, errors=errors, status_code=400)
    approve = values["decision"] == "approve"
    try:
        await _review_branch(db, branch, reviewer=admin, approve=approve, note=values["note"], request=request)
    except ValueError as exc:
        return await _branch_review_view(request, db, branch_id, form=values, errors={"note": str(exc)}, status_code=400)
    await db.commit()
    verb = "approved - it can now be published" if approve else "rejected"
    return redirect("/superadmin/reviews?tab=branches", f"{branch.name} ({branch.gym.name}) {verb}. The owner has been notified.",
                    "success")


async def _review_branch(db: Any, branch: Branch, *, reviewer: User, approve: bool, note: str, request: Any) -> None:
    """Manual decision through the branch-verification package (WP-B); minimal fallback if it is missing."""
    try:
        from app.services.branch_verification import review_branch
    except ImportError:  # pragma: no cover - WP-B ships review_branch
        review_branch = None
    if review_branch is not None:
        await review_branch(db, branch, reviewer=reviewer, approve=approve, note=note, request=request)
        return
    outcome = VS.VERIFIED if approve else VS.REJECTED  # pragma: no cover
    branch.verification_status = outcome
    branch.verification_summary = f"Manual review: {note}"
    branch.verified_at = utcnow() if approve else None
    db.add(VerificationRun(branch_id=branch.id, outcome=outcome, checks=branch.verification_checks, summary=note,
                           manual=True, reviewer_id=reviewer.id, score=1.0 if approve else 0.0))
    owner_id = await db.scalar(select(Gym.owner_id).where(Gym.id == branch.gym_id))
    if owner_id:
        await notify(db, owner_id, f"{branch.name} was {'verified' if approve else 'rejected'}",
                     f"The platform admin reviewed {branch.name}: {note}", category=NotificationCategory.VERIFICATION,
                     severity=Severity.SUCCESS if approve else Severity.WARNING, link=f"/owner/branches/{branch.id}")
    await audit(db, reviewer, "branch.review_approve" if approve else "branch.review_reject", "branch", branch.id,
                gym_id=branch.gym_id, detail={"note": note[:500]}, request=request)


async def _employee_review_view(request: Request, db: Any, employee_id: int, *, form: dict[str, str] | None = None,
                                errors: dict[str, str] | None = None, status_code: int = 200) -> Any:
    emp = (await db.execute(
        select(Employee)
        .options(joinedload(Employee.gym).joinedload(Gym.owner), joinedload(Employee.user), selectinload(Employee.documents))
        .where(Employee.id == employee_id)
    )).unique().scalar_one_or_none()
    if emp is None:
        raise NotFound("Staff member not found.")
    runs = (await db.execute(
        select(VerificationRun).where(VerificationRun.employee_id == emp.id).order_by(VerificationRun.id.desc()).limit(20)
    )).scalars().all()
    decidable = (emp.verification_status == VS.NEEDS_REVIEW and emp.deleted_at is None
                 and emp.status != EmploymentStatus.TERMINATED)
    return await _page(request, db, "review.html", "reviews", {
        "kind": "employee", "emp": emp, "runs": runs, "reviewers": await _user_map(db, (r.reviewer_id for r in runs)),
        "latest_checks": (runs[0].checks if runs else None) or [], "decidable": decidable,
        "documents": sorted(emp.documents, key=lambda d: (d.doc_type != "certification", d.id)),
        "form": form or {"decision": "", "note": ""}, "errors": errors or {},
        "action": f"/superadmin/reviews/employees/{emp.id}/decide", "ai_feature": True,
    }, status_code=status_code)


@router.get("/superadmin/reviews/employees/{employee_id}")
async def employee_review(employee_id: IdParam, request: Request, db: DB, admin: SuperAdminUser):
    return await _employee_review_view(request, db, employee_id)


@router.post("/superadmin/reviews/employees/{employee_id}/decide")
async def employee_decide(employee_id: IdParam, request: Request, db: DB, admin: SuperAdminUser):
    form = await request.form()
    values, errors = _decision_form(form)
    emp = (await db.execute(
        select(Employee).options(joinedload(Employee.gym), joinedload(Employee.user), selectinload(Employee.documents))
        .where(Employee.id == employee_id)
    )).unique().scalar_one_or_none()
    if emp is None:
        raise NotFound("Staff member not found.")
    if emp.verification_status != VS.NEEDS_REVIEW or emp.deleted_at is not None or emp.status == EmploymentStatus.TERMINATED:
        errors["decision"] = "This staff member is not waiting for a review any more."
    if errors:
        return await _employee_review_view(request, db, employee_id, form=values, errors=errors, status_code=400)
    approve = values["decision"] == "approve"
    await review_employee(db, emp, reviewer=admin, approve=approve, note=values["note"], request=request)
    await db.commit()
    verb = "approved - their staff access is re-checked now" if approve else "rejected"
    return redirect("/superadmin/reviews?tab=staff", f"{emp.full_name}'s certification {verb}. The gym owner has been notified.",
                    "success")


async def review_employee(db: Any, emp: Employee, *, reviewer: User, approve: bool, note: str,
                          request: Any = None) -> VerificationRun:
    """Manual decision on a trainer certification the automatic check could not confirm (caller commits).

    ``emp`` must have ``gym``, ``user`` and ``documents`` loaded."""
    from app.services.jobs import enqueue

    now = utcnow()
    outcome = VS.VERIFIED if approve else VS.REJECTED
    last_checks = await db.scalar(
        select(VerificationRun.checks).where(VerificationRun.employee_id == emp.id).order_by(VerificationRun.id.desc()).limit(1)
    )
    emp.verification_status = outcome
    emp.verified_at = now if approve else None
    emp.verification_summary = f"Manual review by the platform admin: {note}"
    for doc in emp.documents:
        if doc.doc_type == "certification":
            assessment = dict(doc.assessment or {})
            assessment["manual_review"] = {"approved": approve, "reviewer_id": reviewer.id, "at": now.isoformat(),
                                           "note": note[:300]}
            doc.assessment = assessment
    run = VerificationRun(employee_id=emp.id, outcome=outcome, checks=last_checks, summary=note, manual=True,
                          reviewer_id=reviewer.id, score=1.0 if approve else 0.0)
    db.add(run)
    if approve:  # WP-C1 re-evaluates the employment (it may become ACTIVE now)
        await enqueue(db, "staff_activation_check", {"employee_id": emp.id}, dedupe_key=f"staff_activation_check:{emp.id}")
    gym = emp.gym
    if approve:
        owner_title = f"{emp.full_name}'s certification was approved"
        owner_body = (f"The platform admin checked {emp.full_name}'s trainer certification for {gym.name} and approved it: "
                      f"{note}\nTheir staff access is re-checked automatically now.")
    else:
        owner_title = f"{emp.full_name}'s certification was rejected"
        owner_body = (f"The platform admin could not accept {emp.full_name}'s trainer certification for {gym.name}: {note}\n"
                      "Ask them to upload a clear, valid certificate in their name.")
    await notify(db, gym.owner_id, owner_title, owner_body, category=NotificationCategory.VERIFICATION,
                 severity=Severity.SUCCESS if approve else Severity.WARNING, link=f"/owner/staff/{emp.id}?tab=documents")
    user = emp.user
    if user is not None and user.deleted_at is None:
        if approve:
            title = "Your certification was approved"
            body = (f"Your trainer certification for {gym.name} was approved by the platform admin. "
                    "Your staff access updates in a moment.")
        else:
            title = "Your certification needs another look"
            body = (f"Your trainer certification for {gym.name} could not be accepted: {note}\n"
                    "Please upload a clear photo or PDF of a valid certificate in your name.")
        await notify(db, user, title, body, category=NotificationCategory.VERIFICATION,
                     severity=Severity.SUCCESS if approve else Severity.WARNING, link="/staff")
    await audit(db, reviewer, "staff.review_approve" if approve else "staff.review_reject", "employee", emp.id,
                gym_id=emp.gym_id, detail={"note": note[:500]}, request=request)
    return run


async def _identity_review_view(request: Request, db: Any, doc_id: int, *, form: dict[str, str] | None = None,
                                errors: dict[str, str] | None = None, status_code: int = 200) -> Any:
    doc = (await db.execute(
        select(IdentityDocument)
        .options(joinedload(IdentityDocument.user), joinedload(IdentityDocument.employee).joinedload(Employee.gym))
        .where(IdentityDocument.id == doc_id)
    )).unique().scalar_one_or_none()
    if doc is None:
        raise NotFound("Identity document not found.")
    duplicates: list[Any] = []
    if doc.id_number_hash:
        duplicates = list((await db.execute(
            select(IdentityDocument.id, IdentityDocument.user_id, IdentityDocument.employee_id, IdentityDocument.status,
                   IdentityDocument.created_at)
            .where(IdentityDocument.id_number_hash == doc.id_number_hash, IdentityDocument.id != doc.id)
            .order_by(IdentityDocument.id.desc()).limit(10)
        )).all())
    people = await _user_map(db, [doc.uploaded_by_id, doc.reviewed_by_id] + [d.user_id for d in duplicates])
    return await _page(request, db, "review.html", "reviews", {
        "kind": "identity", "doc": doc, "duplicates": duplicates, "people": people,
        "decidable": doc.status == IdStatus.NEEDS_REVIEW, "failing": _failing(doc.checks),
        "form": form or {"decision": "", "note": ""}, "errors": errors or {},
        "action": f"/superadmin/reviews/identity/{doc.id}/decide", "ai_feature": True,
    }, status_code=status_code)


@router.get("/superadmin/reviews/identity/{doc_id}")
async def identity_review(doc_id: IdParam, request: Request, db: DB, admin: SuperAdminUser):
    return await _identity_review_view(request, db, doc_id)


@router.post("/superadmin/reviews/identity/{doc_id}/decide")
async def identity_decide(doc_id: IdParam, request: Request, db: DB, admin: SuperAdminUser):
    form = await request.form()
    values, errors = _decision_form(form)
    doc = (await db.execute(
        select(IdentityDocument).options(joinedload(IdentityDocument.user), joinedload(IdentityDocument.employee))
        .where(IdentityDocument.id == doc_id)
    )).unique().scalar_one_or_none()
    if doc is None:
        raise NotFound("Identity document not found.")
    if doc.status != IdStatus.NEEDS_REVIEW:
        errors["decision"] = "This ID card is not waiting for a review any more."
    if errors:
        return await _identity_review_view(request, db, doc_id, form=values, errors=errors, status_code=400)
    approve = values["decision"] == "approve"
    back = f"/superadmin/reviews/identity/{doc_id}"
    subject_user_id, subject_employee_id = doc.user_id, doc.employee_id
    try:
        from app.services import id_verification

        await id_verification.review_identity_document(db, doc, reviewer=admin, approve=approve, note=values["note"])
    except NotImplementedError:  # nothing is committed: the request session rolls back when it closes
        return redirect(back, "Identity reviews are not available yet - the identity verification module is still "
                              "being installed. Nothing was changed.", "error")
    except ValueError as exc:
        return redirect(back, _clip(exc, 300) or "The decision could not be saved.", "error")
    await audit(db, admin, "identity.review_approve" if approve else "identity.review_reject", "identity_document", doc_id,
                request=request, detail={"note": values["note"][:500], "user_id": subject_user_id,
                                         "employee_id": subject_employee_id})
    await db.commit()
    return redirect("/superadmin/reviews?tab=identity",
                    f"ID card #{doc_id} {'approved' if approve else 'rejected'}. The account holder has been notified.",
                    "success")


# ---------------------------------------------------------------------- authorised private previews
PREVIEW_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "application/pdf": "pdf"}


async def _private_file(key: str, content_type: str, name: str) -> Response:
    try:
        data = await storage.load_private(key)
    except Exception:  # missing file, bad key, decryption failure -> never a 500 for a preview
        raise NotFound("The file could not be opened.") from None
    ext = PREVIEW_TYPES.get(content_type)
    disposition = "inline" if ext else "attachment"
    return Response(
        content=data, media_type=content_type if ext else "application/octet-stream",
        headers={"Content-Disposition": f'{disposition}; filename="{name}.{ext or "bin"}"',
                 "Cache-Control": "no-store, private", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/superadmin/files/branch-doc/{doc_id}")
async def branch_doc_file(doc_id: IdParam, db: DB, admin: SuperAdminUser):
    doc = await db.get(BranchDocument, doc_id)
    if doc is None:
        raise NotFound("Document not found.")
    return await _private_file(doc.file_key, doc.content_type, f"branch-document-{doc.id}")


@router.get("/superadmin/files/employee-doc/{doc_id}")
async def employee_doc_file(doc_id: IdParam, db: DB, admin: SuperAdminUser):
    doc = await db.get(EmployeeDocument, doc_id)
    if doc is None:
        raise NotFound("Document not found.")
    return await _private_file(doc.file_key, doc.content_type, f"staff-document-{doc.id}")


# ===================================================================================== AI providers
def _wait_text(ms: Any) -> str:
    """'—', '340 ms', '2.4 s', '3.5 min': a queue or service time, for people."""
    if ms is None:
        return "—"
    ms = max(0, int(ms))
    if ms < 1000:
        return f"{ms} ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f} s"
    return f"{ms / 60_000:.1f} min"


def _ranked(counts: dict[str, Any], labels: dict[str, str]) -> list[tuple[str, int, None]]:
    """{key: n} -> [(label, n, None)], largest first and zeros dropped (rows for sa.ranked_bars)."""
    rows = [(labels.get(key) or humanize(key) or "Other", int(n or 0), None) for key, n in counts.items()]
    return sorted((r for r in rows if r[1] > 0), key=lambda r: (-r[1], r[0]))


def _stop_reason(error: str | None) -> str:
    """Why a cancelled AI call stopped: 'cancelled (page_closed)' -> 'Tab closed'."""
    m = _CANCELLED_CALL.match(error or "")
    reason = m.group(1) if m else ""
    return AI_STOP_REASONS.get(reason) or (humanize(reason) if reason else "Stopped")


def _user_link(uid: int) -> str:
    return link("/superadmin/users", {"q": member_id(int(uid)), "state": "all"})


async def _guard_today() -> dict[str, Any]:
    """The per-person guard's counters of today (UTC): refusals by code, pauses started, pauses escalated to the
    security system (``guard.snapshot()``; every process with Redis, this one without). Never fails the page."""
    try:
        stats = await guard_module.guard.snapshot()
    except Exception:  # noqa: BLE001 - the panel still shows everything else
        log.exception("AI guard counters unavailable for the admin panel")
        stats = {}
    raw = stats.get("refused") or {}
    refused = {code: max(0, int(raw.get(code) or 0)) for code in AI_GUARD_REFUSALS}
    refusals = _ranked(refused, AI_GUARD_REFUSALS)
    return {
        "refusals": refusals, "refusals_max": max((n for _, n, _ in refusals), default=0),
        "refused_total": sum(refused.values()),
        "cooldowns": max(0, int(stats.get("cooldowns_started_today") or 0)),
        "escalations": max(0, int(stats.get("escalations_today") or 0)),
        "day": stats.get("day"), "shared": stats.get("backend") == "redis", "available": bool(stats),
    }


async def _ai_usage(db: Any, since: datetime, *, now: datetime | None = None) -> dict[str, Any]:
    """The "AI usage & abuse" panel (docs/ai-guard-and-queue.md section 8). The queue, presence and running jobs are
    this process's memory; cooldowns and the guard's refusal counters of today come from the guard's shared store.
    Four SQL queries whatever the row counts, each reading a bounded window through an index: the top users read the
    call log since ``since`` first (a MATERIALIZED window: ix_ai_call_logs_created_at, whatever the number of people in
    the composite user index), aggregate it, and only then meet ``users`` (TOP_AI_USERS rows); the AI blocks are the
    last AI_BLOCKS_WINDOW (ix_security_events_category_created); the jobs are the paused ones plus those cancelled since
    ``since`` (ix_jobs_status_finished) - never a GROUP BY over every job ever run."""
    now = now or utcnow()
    snap = scheduler_module.scheduler.snapshot()
    cooldowns = await guard_module.guard.active_cooldowns(AI_COOLDOWNS_SHOWN)
    guard_today = await _guard_today()
    window = (select(AICallLog.user_id, AICallLog.owner_kind, AICallLog.ok, AICallLog.cancelled, AICallLog.feature,
                     AICallLog.created_at)
              .where(AICallLog.created_at >= since).cte("ai_window").prefix_with("MATERIALIZED"))
    busiest = (
        select(window.c.user_id.label("uid"), func.count().label("calls"),
               func.sum(case((window.c.owner_kind == OWNER_JOB, 1), else_=0)).label("background"),
               _failed_calls(window.c).label("failures"), _cancelled_calls(window.c).label("cancelled"),
               func.mode().within_group(window.c.feature).label("top_feature"),
               func.max(window.c.created_at).label("last_at"))
        .where(window.c.user_id.is_not(None))
        .group_by(window.c.user_id)
        .order_by(func.count().desc(), window.c.user_id)
        .limit(TOP_AI_USERS)
        .subquery("busiest")
    )
    top = (await db.execute(
        select(busiest, User.full_name, User.role, User.deleted_at)
        .outerjoin(User, User.id == busiest.c.uid)
        .order_by(busiest.c.calls.desc(), busiest.c.uid)
    )).all()
    blocks = (await db.execute(
        select(SecurityEvent, User)
        .outerjoin(User, User.id == SecurityEvent.user_id)
        .where(SecurityEvent.created_at >= now - AI_BLOCKS_WINDOW,
               or_(SecurityEvent.category == ThreatCategory.AI_ABUSE,
                   and_(SecurityEvent.category == ThreatCategory.RATE_LIMIT,
                        SecurityEvent.detail.startswith(AI_RATE_LIMIT_DETAIL, autoescape=True))))
        .order_by(SecurityEvent.created_at.desc(), SecurityEvent.id.desc()).limit(AI_BLOCKS_SHOWN)
    )).all()
    paused = select(func.count()).select_from(Job).where(Job.status == JobStatus.PAUSED).scalar_subquery()
    cancelled = (select(func.count()).select_from(Job)
                 .where(Job.status == JobStatus.CANCELLED, Job.finished_at >= since).scalar_subquery())
    jobs_row = (await db.execute(select(paused.label("paused"), cancelled.label("cancelled")))).one()
    jobs = {"paused": int(jobs_row.paused or 0), "cancelled": int(jobs_row.cancelled or 0)}
    # names for the cooldown list and the people waiting in the queue: always ONE query, even when nobody is listed
    wanted = sorted({int(c["user_id"]) for c in cooldowns} | {int(u["user_id"]) for u in snap["users"]})
    people = {u.id: u for u in (await db.execute(select(User).where(User.id.in_(wanted)))).scalars().all()}

    def name_of(uid: int) -> str:
        person = people.get(int(uid))
        return person.full_name if person is not None else f"Account #{uid}"

    lanes = []
    for key, lane in snap["lanes"].items():
        label, ico = AI_LANES.get(key, (humanize(key), "cpu"))
        capacity = max(1, int(lane.get("capacity") or 1))
        lanes.append({**lane, "key": key, "label": label, "icon": ico,
                      "pct": round(min(100.0, 100.0 * lane["running"] / capacity), 1),
                      "oldest": _wait_text(lane["oldest_wait_ms"]) if lane["queued"] else None,
                      "service": _wait_text(lane["avg_service_ms"]) if lane.get("avg_service_ms") is not None else None})
    priorities = [{"label": AI_PRIORITIES.get(name, (humanize(name), ""))[0],
                   "hint": AI_PRIORITIES.get(name, (humanize(name), ""))[1], **row}
                  for name, row in snap["by_priority"].items()]
    counters = snap["counters"]
    refused = {**(counters.get("rejected") or {}), "expired": counters.get("expired", 0)}
    stopped = _ranked(counters.get("cancelled") or {}, AI_STOP_REASONS)
    refusals = _ranked(refused, AI_QUEUE_REFUSALS)
    saved = {key: int(n or 0) for key, n in (snap.get("saved") or {}).items()}
    top_users = [{
        "user_id": int(r.uid), "name": r.full_name or f"Account #{r.uid}", "deleted": r.deleted_at is not None,
        "role": r.role.value if r.role is not None else None, "calls": int(r.calls),
        "background": int(r.background or 0), "failures": int(r.failures or 0), "cancelled": int(r.cancelled or 0),
        "top_feature": r.top_feature, "last_at": r.last_at, "href": _user_link(r.uid),
    } for r in top]
    return {
        "lanes": lanes, "priorities": priorities, "counters": counters,
        "avg_wait": _wait_text(snap.get("avg_wait_ms")) if counters.get("started") else None,
        "waiting": [{**u, "name": name_of(u["user_id"]), "wait": _wait_text(u["oldest_wait_ms"]),
                     "href": _user_link(u["user_id"])} for u in snap["users"]],
        "saved": saved, "saved_total": saved.get("cancelled_running", 0) + saved.get("cancelled_queued", 0),
        "stopped": stopped, "stopped_max": max((n for _, n, _ in stopped), default=0),
        "refusals": refusals, "refusals_max": max((n for _, n, _ in refusals), default=0),
        "guard": guard_today,  # the per-person guard's refusals, pauses and escalations today - next to the queue's
        "top_users": top_users, "top_max": max((u["calls"] for u in top_users), default=0),
        "cooldowns": [{**c, "person": people.get(int(c["user_id"])), "name": name_of(c["user_id"]),
                       "left": guard_module.human_wait(c["seconds_left"]), "href": _user_link(c["user_id"])}
                      for c in cooldowns],
        "blocks": [{"event": e, "person": person} for e, person in blocks],
        "blocks_days": max(1, AI_BLOCKS_WINDOW.days),
        "window_hours": max(1, round((now - since).total_seconds() / 3600)),
        "presence": presence.snapshot(), "jobs": jobs, "jobs_here": len(jobs_service.running_jobs()),
        "job_slots": jobs_service.concurrency(),
    }


_AI_DEMO_ACCOUNTS = TTLCache("superadmin.ai.demo_accounts", ttl=AI_DEMO_COUNT_TTL, maxsize=4)


async def _ai_services(db: Any) -> dict[str, Any]:
    """``selfcheck.platform_services`` for the AI page: the same live probes (Redis, Kokoro, the gateway's status page),
    but the demo-account count - an e-mail suffix match no index can serve, so a scan of ``users`` - comes from a
    per-process cache (AI_DEMO_COUNT_TTL): one scan per worker every few minutes, not one per page view. It only
    changes when scripts/seed_demo.py runs. The self-check page still counts live."""
    import asyncio

    redis, tts, gateway = await asyncio.gather(selfcheck_service.probe_redis(), selfcheck_service.probe_tts(),
                                               selfcheck_service.probe_gateway_status())
    domain = selfcheck_service.demo_domain()
    try:
        demo = await _AI_DEMO_ACCOUNTS.get_or_compute(domain, lambda: selfcheck_service.demo_accounts(db))
    except Exception:  # noqa: BLE001 - "Unknown" on the card, like platform_services
        demo = None
    return {"redis": redis, "tts": tts, "gateway": gateway,
            "demo": {"accounts": demo, "domain": domain, "production": get_settings().is_production}}


@router.get("/superadmin/ai")
async def ai_page(request: Request, db: DB, admin: SuperAdminUser):
    """Provider health, features, recent calls, services and the usage panel: a constant number of statements, every
    aggregate over the last AI_WINDOW of the call log (ix_ai_call_logs_created_at) - never the whole table."""
    now = utcnow()
    day_ago = now - AI_WINDOW
    providers = await _provider_rows(db, day_ago)
    features = (await db.execute(
        select(AICallLog.feature, func.count().label("calls"), _failed_calls().label("failures"),
               _cancelled_calls().label("cancelled"),
               func.avg(case((AICallLog.ok.is_(True), AICallLog.latency_ms))).label("avg_ms"),
               func.avg(AICallLog.queue_ms).label("queue_ms"))
        .where(AICallLog.created_at >= day_ago).group_by(AICallLog.feature).order_by(func.count().desc()).limit(12)
    )).all()
    recent = (await db.execute(select(AICallLog).order_by(AICallLog.id.desc()).limit(40))).scalars().all()
    services = await _ai_services(db)
    usage = await _ai_usage(db, day_ago, now=now)
    return await _page(request, db, "ai.html", "ai", {
        "providers": providers, "features": features, "recent": recent, "services": services,
        "calls_24h": sum(p["calls_24h"] for p in providers), "failures_24h": sum(p["failures_24h"] for p in providers),
        "cancelled_24h": sum(p["cancelled_24h"] for p in providers),
        "features_max": max((int(r.calls) for r in features), default=0), "ai_feature": True,
        "settings": get_settings(), "usage": usage, "role_tones": ROLE_TONES, "role_names": ROLE_NAMES,
        "stop_reasons": {c.id: _stop_reason(c.error) for c in recent if c.cancelled}, "wait_text": _wait_text,
    })


@router.post("/superadmin/ai/cooldowns/{user_id}/lift")
async def ai_cooldown_lift(user_id: IdParam, request: Request, db: DB, admin: SuperAdminUser):
    """Clear an account's AI pause and strike points (app/ai/guard.py ``lift``); quota usage is kept."""
    form = await request.form()
    back = _back(_form_str(form, "next"), "/superadmin/ai#usage")
    person = await db.get(User, user_id)
    name = person.full_name if person is not None else f"Account #{user_id}"
    guard = guard_module.guard
    before = await guard.status(user_id)
    if not await guard.lift(user_id):
        return redirect(back, f"{name} has no AI pause or strikes to lift.", "info")
    paused = before.get("cooldown") or {}
    await audit(db, admin, "ai.cooldown_lift", "user", user_id, request=request, detail={
        "cooldown_until": before.get("cooldown_until"), "strikes": before.get("strikes"), "level": paused.get("level"),
        "reason": paused.get("reason"), "repeat": paused.get("repeat"),
    })
    await db.commit()
    if before.get("cooldown_until"):
        return redirect(back, f"AI pause of {name} lifted - they can use AI again now (their strikes were cleared too).",
                        "success")
    return redirect(back, f"Strikes of {name} cleared - their AI was not paused.", "success")


@router.post("/superadmin/ai/test")
async def ai_test(request: Request, db: DB, admin: SuperAdminUser):
    """Ping every provider, then send one tiny completion through the chain (queue included) and record the outcome.
    No pooled DB connection is held while the providers are pinged and the AI answers: the transaction that loaded the
    admin is ended first (nothing is pending), and the call log + audit row are written in a new one afterwards."""
    form = await request.form()
    owner = current_owner()
    if owner is not None:  # a plain form post names its tab in a field (IP.ai sends the X-Page-Id header)
        owner.page_id = clean_page_id(form.get("page_id")) or owner.page_id
    await db.commit()  # gives the request's connection back to the pool for the pings, the queue wait and the answer
    started = time.perf_counter()
    status = await ai.refresh_health()
    pings = [{"name": p["name"], "label": p["label"], "status": p["status"], "model": p["model"],
              "error": p["last_error"]} for p in status["providers"]]
    outcome: dict[str, Any]
    try:
        res = await ai.complete(
            feature="selftest", system="You are a health check for the IronPulse platform. Reply with the single word OK.",
            prompt="Reply with OK", max_tokens=8, temperature=0.0, timeout=SELFTEST_TIMEOUT,
        )
        label = next((p["label"] for p in status["providers"] if p["name"] == res.provider), res.provider)
        reply = _clip(res.text, 60)
        outcome = {"ok": True, "provider": res.provider, "label": label, "model": res.model, "latency_ms": res.latency_ms,
                   "reply": reply, "replied_ok": "ok" in reply.lower()}
    except AIRejected as exc:  # the AI queue refused (full, or too many of your own requests): nothing was tested
        if wants_json(request):
            raise  # 429/503 + code + Retry-After (app/core/errors.py) -> IP.ai.describeError in the console
        return redirect("/superadmin/ai", f"The provider test did not run: {exc.message}", "warning")
    except AICancelled:  # the admin left mid-test: nobody reads the result, and it is not a provider failure
        raise
    except AIUnavailableError as exc:
        reason = str(exc).replace("(None)", "(health check failed)")
        outcome = {"ok": False, "error": _clip(reason, 400) or "no provider answered"}
    outcome["total_ms"] = int((time.perf_counter() - started) * 1000)
    await flush_ai_call_logs(db)  # the call log on the AI page updates right away
    await audit(db, admin, "ai.selftest", "ai", None, request=request,
                detail={k: v for k, v in outcome.items() if k != "reply"})
    await db.commit()
    if wants_json(request):
        return JSONResponse({**outcome, "providers": pings, "state": ai.status()["state"], "disclaimer": AI_DISCLAIMER})
    if outcome["ok"]:
        return redirect("/superadmin/ai", f"{outcome['label']} answered “{outcome['reply']}” in {outcome['latency_ms']} ms "
                                          f"({outcome['model']}).", "success")
    return redirect("/superadmin/ai", "AI unavailable - every provider failed the test: " + outcome["error"], "error")


# ===================================================================================== users
def _user_search(q: str) -> list[Any]:
    from app.services.identity_lookup import parse_member_id

    like = f"%{_escape_like(q)}%"
    # e-mail / username are citext, whose own ILIKE no trigram index serves: compared as text they use
    # ix_users_email_trgm / ix_users_username_trgm (same matches - ILIKE ignores case anyway)
    conds: list[Any] = [cast(User.email, Text).ilike(like, escape="\\"), User.full_name.ilike(like, escape="\\"),
                        cast(User.username, Text).ilike(like, escape="\\")]
    digits = re.sub(r"\D", "", q)
    if len(digits) >= 4:
        conds.append(User.phone_normalized.contains(digits, autoescape=True))
    member = parse_member_id(q)
    if member is not None and 0 < member <= MAX_ID:
        conds.append(User.id == member)
    return conds


async def _users_view(request: Request, db: Any, admin: User, params: Any = None, *,
                      alert: dict[str, Any] | None = None, delete_target: int | None = None,
                      delete_error: str | None = None, delete_value: str = "", status_code: int = 200) -> Any:
    qp = params if params is not None else request.query_params
    q = _clip(qp.get("q"), 120)
    role = qp.get("role") if qp.get("role") in {r.value for r in Role} else ""
    state = qp.get("state") if qp.get("state") in {s for s, _ in USER_STATES} else "active"
    now = utcnow()
    where: list[Any] = []
    if state == "active":
        where.append(User.deleted_at.is_(None))
    elif state == "deleted":
        where.append(User.deleted_at.is_not(None))
    elif state == "locked":
        where += [User.deleted_at.is_(None), User.locked_until > now]
    if q:
        from app.services.identity_lookup import find_user_by_identifier

        conds = _user_search(q)
        found = await find_user_by_identifier(db, q)
        if found is not None:
            conds.append(User.id == found.id)
        where.append(or_(*conds))
    role_counts = dict((await db.execute(select(User.role, func.count()).where(*where).group_by(User.role))).all())
    if role:
        where.append(User.role == Role(role))
    # the total comes from the role counts (no second COUNT); paging stops at COUNT_CAP rows (OFFSET <= 10,000)
    total = int(role_counts.get(Role(role), 0)) if role else int(sum(role_counts.values()))
    pager = Pager(_int_param(qp.get("page"), 1, 1, 100_000), USERS_PER_PAGE, min(total, COUNT_CAP), "/superadmin/users",
                  {"q": q, "role": role, "state": state}, capped=total > COUNT_CAP)
    users = (await db.execute(
        select(User).where(*where).order_by(User.id.desc()).offset(pager.offset).limit(pager.per_page)
    )).scalars().all()
    return await _page(request, db, "users.html", "users", {
        "users": users, "pager": pager, "q": q, "role": role, "state": state, "states": USER_STATES,
        "role_counts": {r.value: int(n) for r, n in role_counts.items()}, "roles": ROLE_LABELS, "role_tones": ROLE_TONES,
        "alert": alert, "delete_target": delete_target, "delete_error": delete_error, "delete_value": delete_value,
        "retention_days": get_settings().account_retention_days, "now": now, "back": pager.url(pager.page),
    }, status_code=status_code)


@router.get("/superadmin/users")
async def users_page(request: Request, db: DB, admin: SuperAdminUser):
    return await _users_view(request, db, admin)


async def _target_user(db: Any, user_id: int) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise NotFound("User not found.")
    return user


@router.post("/superadmin/users/{user_id}/delete")
async def user_delete(user_id: IdParam, request: Request, db: DB, admin: SuperAdminUser):
    form = await request.form()
    user = await _target_user(db, user_id)
    reason = _clip(_form_str(form, "reason"), 400)
    focus = {"q": user.email or f"IP-{user.id:06d}", "state": "all"}
    back = _back(_form_str(form, "next"), "/superadmin/users")
    if user.deleted_at is not None:
        return redirect(back, f"{user.full_name}'s account is already deleted.", "info")
    if user.id == admin.id or user.role == Role.SUPERADMIN:
        return await _users_view(request, db, admin, focus, status_code=400, alert={
            "tone": "danger", "title": "Super-admin accounts cannot be deleted here.",
            "items": ["The platform needs at least one super-admin; remove the role on the server instead."]})
    error = None
    if len(reason) < 5:
        error = "Give a reason (at least 5 characters) - it is included in the e-mail to the person."
    elif len(reason) > 255:
        error = "Keep the reason under 255 characters."
    if error:
        return await _users_view(request, db, admin, focus, delete_target=user.id, delete_error=error,
                                 delete_value=reason, status_code=400)
    name = user.full_name
    try:
        await soft_delete_user(db, user, reason=reason, actor=admin, request=request)
    except AccountDeletionBlocked as exc:  # raised before anything changes; the session is never committed
        return await _users_view(request, db, admin, focus, status_code=400, alert={
            "tone": "danger", "title": f"{name}'s account can't be deleted yet", "items": exc.reasons})
    await db.commit()
    return redirect(back, f"{name}'s account was deleted. Data is kept for {get_settings().account_retention_days} days "
                          "and can be restored until then.", "success")


@router.post("/superadmin/users/{user_id}/restore")
async def user_restore(user_id: IdParam, request: Request, db: DB, admin: SuperAdminUser):
    form = await request.form()
    user = await _target_user(db, user_id)
    back = _back(_form_str(form, "next"), "/superadmin/users?state=deleted")
    now = utcnow()
    if user.deleted_at is None:
        return redirect(back, f"{user.full_name}'s account is not deleted.", "info")
    if user.purge_after is None or user.purge_after <= now:
        return redirect(back, "The retention window has ended - this account can no longer be restored.", "error")
    from app.services.trust_safety import active_ban

    if await active_ban(db, user.id) is not None:  # restoring must never lift a trust-and-safety ban on the side
        return redirect(back, f"{user.full_name}'s account is banned. Lift the ban on the Trust & safety page first "
                              "(/superadmin/trust), then restore the account.", "error")
    previous = user.deletion_reason
    user.deleted_at = None
    user.purge_after = None
    user.deletion_reason = None
    user.is_active = True
    user.failed_login_count = 0
    user.locked_until = None
    await notify(db, user, "Your account has been restored",
                 "The IronPulse platform admin restored your account. You can sign in again with your usual details. "
                 "Memberships or staff employments that ended when the account was deleted are not reactivated "
                 "automatically - contact your gym if you need them back.",
                 category=NotificationCategory.ACCOUNT, severity=Severity.SUCCESS, link="/auth/login")
    await audit(db, admin, "account.restore", "user", user.id, request=request, detail={"previous_reason": previous})
    await db.commit()
    return redirect(back, f"{user.full_name}'s account was restored and they were notified.", "success")


@router.post("/superadmin/users/{user_id}/unlock")
async def user_unlock(user_id: IdParam, request: Request, db: DB, admin: SuperAdminUser):
    form = await request.form()
    user = await _target_user(db, user_id)
    back = _back(_form_str(form, "next"), "/superadmin/users")
    if user.deleted_at is not None:
        return redirect(back, "Deleted accounts cannot be unlocked - restore the account first.", "error")
    was_locked = user.locked_until is not None and user.locked_until > utcnow()
    if not was_locked and not user.failed_login_count:
        return redirect(back, f"{user.full_name}'s account is not locked.", "info")
    user.locked_until = None
    user.failed_login_count = 0
    await notify(db, user, "Your account was unlocked",
                 "The platform admin removed the temporary sign-in lock on your account. If you did not try to sign in "
                 "recently, change your password straight away.",
                 category=NotificationCategory.SECURITY, severity=Severity.INFO, link="/account")
    await audit(db, admin, "account.unlock", "user", user.id, request=request, detail={"was_locked": was_locked})
    await db.commit()
    return redirect(back, f"{user.full_name}'s account was unlocked.", "success")


# ===================================================================================== e-mail outbox
@router.get("/superadmin/outbox")
async def outbox_page(request: Request, db: DB, admin: SuperAdminUser):
    qp = request.query_params
    status = qp.get("status") if qp.get("status") in OUTBOX_STATES else ""
    q = _clip(qp.get("q"), 120)
    where: list[Any] = []
    if status:
        where.append(EmailOutbox.status == EmailStatus(status))
    if q:
        like = f"%{_escape_like(q)}%"
        where.append(or_(EmailOutbox.to_email.ilike(like, escape="\\"), EmailOutbox.subject.ilike(like, escape="\\")))
    # per-status chips and the match count in one statement, each a bounded read ((status, id) index; "10,000+")
    row = (await db.execute(select(
        *(_count_upto(EmailOutbox, EmailOutbox.status == EmailStatus(s)).label(s) for s in OUTBOX_STATES),
        _count_upto(EmailOutbox, *where).label("total"),
    ))).one()
    by_status = {s: int(getattr(row, s) or 0) for s in OUTBOX_STATES}
    total, capped = _capped(row.total)
    pager = Pager(_int_param(qp.get("page"), 1, 1, 100_000), OUTBOX_PER_PAGE, total, "/superadmin/outbox",
                  {"status": status, "q": q}, capped=capped)
    mails = (await db.execute(
        select(EmailOutbox).where(*where).order_by(EmailOutbox.id.desc()).offset(pager.offset).limit(pager.per_page)
    )).scalars().all()
    return await _page(request, db, "outbox.html", "outbox", {
        "mails": mails, "pager": pager, "status": status, "q": q, "by_status": by_status, "states": OUTBOX_STATES,
        "smtp": get_settings().smtp_enabled, "back": pager.url(pager.page),
    })


@router.post("/superadmin/outbox/retry-failed")
async def outbox_retry_all(request: Request, db: DB, admin: SuperAdminUser):
    result = await db.execute(
        update(EmailOutbox).where(EmailOutbox.status == EmailStatus.FAILED)
        .values(status=EmailStatus.QUEUED, attempts=0, next_attempt_at=None)
    )
    n = int(result.rowcount or 0)
    if not n:
        return redirect("/superadmin/outbox", "There are no failed e-mails to retry.", "info")
    await audit(db, admin, "outbox.retry_all", "email", None, request=request, detail={"count": n})
    await db.commit()
    return redirect("/superadmin/outbox?status=queued", f"{n} failed e-mail{'s' if n != 1 else ''} queued again.", "success")


@router.get("/superadmin/outbox/{email_id}")
async def outbox_view(email_id: IdParam, request: Request, db: DB, admin: SuperAdminUser):
    mail = await db.get(EmailOutbox, email_id)
    if mail is None:
        raise NotFound("E-mail not found.")
    recipient = await db.get(User, mail.user_id) if mail.user_id else None
    return await _page(request, db, "outbox_view.html", "outbox", {
        "mail": mail, "recipient": recipient, "html_chars": len(mail.html_body or ""),
    })


@router.post("/superadmin/outbox/{email_id}/retry")
async def outbox_retry(email_id: IdParam, request: Request, db: DB, admin: SuperAdminUser):
    form = await request.form()
    back = _back(_form_str(form, "next"), "/superadmin/outbox?status=failed")
    mail = await db.get(EmailOutbox, email_id)
    if mail is None:
        raise NotFound("E-mail not found.")
    if mail.status != EmailStatus.FAILED:
        return redirect(back, "Only failed e-mails can be retried.", "info")
    mail.status = EmailStatus.QUEUED
    mail.attempts = 0
    mail.next_attempt_at = None
    await audit(db, admin, "outbox.retry", "email", mail.id, request=request, detail={"to": mail.to_email})
    await db.commit()
    return redirect(back, f"E-mail to {mail.to_email} queued again - it is sent within a few seconds.", "success")


# ===================================================================================== audit log
_DISTINCT_ACTIONS = text(
    """
    WITH RECURSIVE actions(action, depth) AS (
        (SELECT action, 1 FROM audit_logs ORDER BY action LIMIT 1)
        UNION ALL
        SELECT (SELECT a.action FROM audit_logs a WHERE a.action > actions.action ORDER BY a.action LIMIT 1), depth + 1
        FROM actions WHERE actions.action IS NOT NULL AND depth < :max_actions
    )
    SELECT action FROM actions WHERE action IS NOT NULL
    """
)


async def _audit_areas(db: Any) -> list[str]:
    """The area filter's options (``security`` of ``security.ban_manual``): a loose index scan of ix_audit_logs_action
    - one index probe per distinct action (at most AUDIT_MAX_ACTIONS), never a GROUP BY over the whole log."""
    actions = (await db.execute(_DISTINCT_ACTIONS, {"max_actions": AUDIT_MAX_ACTIONS})).scalars().all()
    return sorted({a.split(".", 1)[0] for a in actions if a})


@router.get("/superadmin/audit")
async def audit_page(request: Request, db: DB, admin: SuperAdminUser):
    qp = request.query_params
    area = (qp.get("area") or "").strip().lower()
    area = area if AREA_RE.match(area) else ""
    q = (qp.get("q") or "").strip().lower()
    q_error = None
    if q and not SEARCH_RE.match(q):
        q_error, q = "Use letters, digits, dots, dashes or underscores (e.g. security.ban).", ""
    actor_raw = (qp.get("actor") or "").strip()
    actor_id = int(actor_raw) if actor_raw.isdigit() and 0 < int(actor_raw) <= MAX_ID else None
    where: list[Any] = []
    if area:
        where.append(AuditLog.action.startswith(area + ".", autoescape=True))
    if q:
        where.append(AuditLog.action.contains(q, autoescape=True))
    if actor_id:
        where.append(AuditLog.actor_id == actor_id)
    areas = await _audit_areas(db)
    total, capped = _capped(await db.scalar(select(_count_upto(AuditLog, *where))))
    pager = Pager(_int_param(qp.get("page"), 1, 1, 100_000), AUDIT_PER_PAGE, total, "/superadmin/audit",
                  {"area": area, "q": q, "actor": str(actor_id or "")}, capped=capped)
    rows = (await db.execute(
        select(AuditLog).where(*where).order_by(AuditLog.id.desc()).offset(pager.offset).limit(pager.per_page)
    )).scalars().all()
    people = await _user_map(db, [r.actor_id for r in rows] + ([actor_id] if actor_id else []))
    gym_ids = {r.gym_id for r in rows if r.gym_id}
    gyms = dict((await db.execute(select(Gym.id, Gym.name).where(Gym.id.in_(gym_ids)))).all()) if gym_ids else {}
    details = {r.id: json.dumps(r.detail or {}, indent=2, ensure_ascii=False, default=str)[:4000] for r in rows}
    return await _page(request, db, "audit.html", "audit", {
        "rows": rows, "pager": pager, "area": area, "q": q, "q_error": q_error, "areas": areas, "people": people,
        "gyms": gyms, "details": details, "actor_id": actor_id,
    }, status_code=400 if q_error else 200)


# ===================================================================================== self-check
@router.get("/superadmin/selfcheck")
async def selfcheck_page(request: Request, db: DB, admin: SuperAdminUser):
    probe = request.query_params.get("probe") == "1"
    started = time.perf_counter()
    results = await selfcheck_service.run_selfcheck(db, probe=probe)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    groups = [(g, [r for r in results if r.group == g]) for g in selfcheck_service.GROUPS]
    groups = [(g, rows) for g, rows in groups if rows]
    jobs = {s.value: 0 for s in JobStatus}
    for status_, n in (await db.execute(select(Job.status, func.count()).group_by(Job.status))).all():
        jobs[status_.value] = int(n)
    failed_jobs = (await db.execute(
        select(Job).where(Job.status == JobStatus.FAILED).order_by(Job.id.desc()).limit(8)
    )).scalars().all()
    return await _page(request, db, "selfcheck.html", "selfcheck", {
        "groups": groups, "summary": selfcheck_service.summarize(results), "probe": probe, "elapsed_ms": elapsed_ms,
        "jobs": jobs, "failed_jobs": failed_jobs, "checked_at": utcnow(),
    })
