"""WP-C2 ledger, payroll statements, payouts, proofs, final settlement + termination.

Routes::

    GET  /owner/finance                      income / outgoing / net, pending salaries, 12-month chart, recent ledger
    GET  /owner/finance/transactions         every incoming + outgoing transaction (filters, search, pagination)
    GET  /owner/finance/transactions.csv     streaming CSV export (formula-injection safe)
    GET  /owner/payroll                      statements (filters gym / status / employee) + "run payroll"
    POST /owner/payroll/generate             generate a month's statements (idempotent)
    GET  /owner/payroll/{id}                 breakdown, rules, explanation, payout instructions, actions
    POST /owner/payroll/{id}/pay             Cashfree Payouts (INR) or TEST-MODE simulation
    POST /owner/payroll/{id}/proof           manual payment proof (file + UTR + amount) -> OCR verification
    POST /owner/payroll/{id}/confirm         confirm despite a proof mismatch (typed justification, audited)
    POST /owner/payroll/{id}/adjust          unpaid leave days on a due statement
    POST /owner/payroll/{id}/explain         queue the AI explanation
    GET  /owner/payroll/{id}/proof           the stored proof (authorised, no-store)
    GET  /owner/payroll/{id}/status          JSON status (polled while the proof is being verified)
    GET  /owner/staff/{id}/settlement        dues + final-settlement preview + termination
    POST /owner/staff/{id}/settlement        create the final settlement
    POST /owner/staff/{id}/terminate         hands over to /owner/staff/{id}/remove (an employment always ends with
                                             a reason from the trust-and-safety list)
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import math
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Path, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy import Select, and_, case, func, literal, or_, select, true, tuple_
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import aliased, joinedload

from app.core.deps import DB, NotFound, OwnerUser, get_owned_employee
from app.core.templating import redirect, render
from app.core.utils import local_today, money, safe_zone
from app.db import get_sessionmaker, long_statement
from app.models import Employee, Gym, Job, SalaryStatement, Transaction
from app.models.enums import (
    EmploymentStatus,
    JobStatus,
    StatementStatus,
    TxCategory,
    TxDirection,
    TxStatus,
)
from app.services import payroll as pr  # registers payroll jobs + periodic tasks on import
from app.services import storage
from app.services.audit import audit
from app.services.jobs import enqueue
from app.services.payroll_rules import TERMINATION_REASONS, rules_for

log = logging.getLogger("ironpulse.owner_finance")
router = APIRouter()
MAX_ID = 2**63 - 1  # BIGINT ids: a larger path id is a 422 page, never a driver error (a 500)
IdParam = Annotated[int, Path(ge=1, le=MAX_ID)]

PAGE_SIZE = 25
STATEMENT_PAGE_SIZE = 30
MAX_SEARCH = 80
# what the ledger shows, filters and sorts on - the expression ix_transactions_gym_booked indexes (models/billing.py)
BOOKED_AT = func.coalesce(Transaction.completed_at, Transaction.created_at)
# hard caps of the overview's lists (the full history lives on the paginated pages)
RECENT_TX_LIMIT = 8
PENDING_STATEMENT_LIMIT = 6
EMPLOYEE_OPTIONS_LIMIT = 300  # staff offered in the payroll filter (current staff first)
FILTER_DATE_MIN = date(2000, 1, 1)  # list/CSV date filters are clamped: date + 1 day must never overflow
# The transactions page (and its CSV) shows one period, never the owner's whole history by default: the last 12 months
# unless the owner picks another period or dates. "All time" is still bounded: its totals read at most
# TOTALS_ROW_CAP transactions (newest-first index scans that stop there) and say so when there are more.
PERIODS: dict[str, tuple[str, int | None]] = {  # key -> (label, days back incl. today; None = all time)
    "30d": ("Last 30 days", 30),
    "90d": ("Last 90 days", 90),
    "12m": ("Last 12 months", 365),
    "all": ("All time", None),
}
DEFAULT_PERIOD = "12m"
TOTALS_ROW_CAP = 50_000  # transactions counted + summed per page view; above it: "50,000+" and no partial sums
MAX_TX_PAGE = 10_000  # ?page= is clamped here (the capped total keeps real pages far below it)
# CSV export: keyset batches, each in its own short transaction (no pooled connection or snapshot is held while a
# slow client downloads), at most EXPORT_MAX_ROWS per file (a larger export asks for a narrower date range)
EXPORT_BATCH = 2000
EXPORT_MAX_ROWS = 100_000
EXPORT_STATEMENT_MS = 60_000  # statement_timeout of one export batch (long_statement)
EXPORT_CONCURRENCY = 2  # export batch queries running at once in this process
_export_slots = asyncio.Semaphore(EXPORT_CONCURRENCY)


# ============================================================================================ small helpers
def _int(value: Any) -> int | None:
    """A whole number from a form / query value - None when it isn't one or lies beyond BIGINT (no such row: never a
    driver error)."""
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return n if -MAX_ID <= n <= MAX_ID else None


def _date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def _filter_date(value: Any) -> date | None:
    """A list / export filter date clamped to FILTER_DATE_MIN .. a year from now (?to=9999-12-31 or
    ?from=0001-01-01 must not overflow when converted to a UTC bound)."""
    d = _date(value)
    if d is None:
        return None
    return min(max(d, FILTER_DATE_MIN), date.today() + timedelta(days=366))


def _decimal(value: Any) -> Decimal | None:
    raw = str(value or "").strip().replace(",", "")
    if not raw or len(raw) > 20:
        return None
    try:
        d = Decimal(raw)
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def _enum(enum_cls: Any, value: Any) -> Any:
    try:
        return enum_cls(str(value)) if value else None
    except ValueError:
        return None


def _qs(params: dict[str, Any], **override: Any) -> str:
    merged = {**params, **override}
    clean = {k: v for k, v in merged.items() if v not in (None, "", False)}
    return ("?" + urlencode(clean)) if clean else ""


def _tz(user: Any) -> str:
    return safe_zone(getattr(user, "timezone", None)).key


def _local_midnight_utc(d: date, tz: str) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=safe_zone(tz)).astimezone(UTC)


def _cents(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


async def _owner_gyms(db: DB, user: Any) -> list[Gym]:
    return list((await db.execute(select(Gym).where(Gym.owner_id == user.id).order_by(Gym.name, Gym.id))).scalars().all())


def _pick_gym(gyms: list[Gym], raw: Any) -> Gym | None:
    """Gym filter value -> owned gym; a foreign or unknown id is a 404 (no probing)."""
    if raw in (None, "", "all"):
        return None
    gid = _int(raw)
    gym = next((g for g in gyms if g.id == gid), None)
    if gym is None:
        raise NotFound("Gym not found.")
    return gym


def compact(value: Decimal | float | int, currency: str) -> str:
    """Axis label: 0 / 50K / 1.5L / 2Cr (INR) or 0 / 50K / 1.2M."""
    v = float(value)
    if v == 0:
        return "0"
    units = ((1e7, "Cr"), (1e5, "L"), (1e3, "K")) if currency.upper() == "INR" else ((1e9, "B"), (1e6, "M"), (1e3, "K"))
    for size, unit in units:
        if abs(v) >= size:
            return f"{v / size:.1f}".rstrip("0").rstrip(".") + unit
    return f"{v:.0f}"


def _nice_max(value: float) -> float:
    if value <= 0:
        return 4.0
    exp = math.floor(math.log10(value / 4))
    for m in (1, 2, 2.5, 5, 10):
        step = m * 10**exp
        if step * 4 >= value:
            return step * 4
    return 10 ** (exp + 1) * 4


def bar_chart(months: list[dict[str, Any]], currency: str) -> dict[str, Any]:
    """Geometry for the server-rendered SVG (viewBox 1200 x 400, two series per month, 4 gridline intervals)."""
    vb_w, vb_h = 1200, 400
    peak = max([float(m["income"]) for m in months] + [float(m["outgoing"]) for m in months] + [0.0])
    nice = _nice_max(peak)
    band = vb_w / len(months)
    bar_w = min(26.0, band * 0.28)
    gap = 4.0

    def column(x: float, value: float) -> str | None:
        if value <= 0:
            return None
        h = max(3.0, vb_h * value / nice)
        y = vb_h - h
        r = min(6.0, h / 2, bar_w / 2)
        return (f"M{x:.1f},{vb_h} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} H{x + bar_w - r:.1f} "
                f"Q{x + bar_w:.1f},{y:.1f} {x + bar_w:.1f},{y + r:.1f} V{vb_h} Z")

    bars = []
    for i, m in enumerate(months):
        cx = band * i + band / 2
        bars.append({
            **m,
            "band_x": f"{band * i:.1f}", "band_w": f"{band:.1f}",
            "in_path": column(cx - gap / 2 - bar_w, float(m["income"])),
            "out_path": column(cx + gap / 2, float(m["outgoing"])),
        })
    ticks = [{"label": compact(nice * k / 4, currency), "y": f"{vb_h - vb_h * k / 4:.1f}"} for k in range(5)]
    return {"w": vb_w, "h": vb_h, "bars": bars, "ticks": ticks, "currency": currency, "empty": peak <= 0}


def _add_month(d: date) -> date:
    return (d.replace(day=28) + timedelta(days=4)).replace(day=1)


# ============================================================================================ ledger: newest first
def _gym_rows(gym_ids: list[int]) -> Any:
    """The gyms whose ledgers are read (one row each, with the name the CSV prints)."""
    return select(Gym.id.label("gid"), Gym.name.label("gym_name")).where(Gym.id.in_(gym_ids or [-1])).subquery("g")


def _newest_per_gym(gyms: Any, clauses: list[Any], *columns: Any, limit: int) -> Any:
    """``LATERAL``: for each gym of ``gyms``, its newest ``limit`` transactions matching ``clauses`` - one range scan of
    ix_transactions_gym_booked per gym that stops after ``limit`` rows. ``gym_id IN (a, b)`` could not use that order:
    PostgreSQL would read and sort the whole of both ledgers to find the newest few. The caller merges the gyms with
    ``ORDER BY booked DESC, id DESC LIMIT``. ``columns``: what to select (default: the Transaction entity)."""
    return (
        select(*(columns or (Transaction,)))
        .where(Transaction.gym_id == gyms.c.gid, *clauses)
        .order_by(BOOKED_AT.desc(), Transaction.id.desc())
        .limit(max(1, limit))
        .lateral("t")
    )


def _newest_transactions(gym_ids: list[int], clauses: list[Any], *, limit: int, offset: int = 0) -> Select:
    """Transactions (with their gym) of ``gym_ids`` matching ``clauses``, newest first: ``limit`` rows after ``offset``
    - each gym contributes at most ``offset + limit`` rows (``_newest_per_gym``)."""
    gyms = _gym_rows(gym_ids)
    inner = _newest_per_gym(gyms, clauses, limit=offset + limit)
    tx = aliased(Transaction, inner)
    return (
        select(tx).select_from(gyms).join(inner, true())
        .options(joinedload(tx.gym))
        .order_by(func.coalesce(tx.completed_at, tx.created_at).desc(), tx.id.desc())
        .limit(limit).offset(offset)
    )


# ============================================================================================ /owner/finance
@router.get("/owner/finance")
async def finance_overview(request: Request, db: DB, user: OwnerUser):
    gyms = await _owner_gyms(db, user)
    gym = _pick_gym(gyms, request.query_params.get("gym"))
    gym_ids = [gym.id] if gym else [g.id for g in gyms]
    tz = _tz(user)
    today = local_today(tz)
    first_month = today.replace(day=1)
    for _ in range(11):
        first_month = (first_month - timedelta(days=1)).replace(day=1)
    since = _local_midnight_utc(first_month, tz)

    month_rows: list[Any] = []
    pending_rows: list[Any] = []
    pending_items: list[SalaryStatement] = []
    recent: list[Transaction] = []
    if gym_ids:
        bucket = func.date_trunc("month", func.timezone(tz, BOOKED_AT))
        month_rows = list((
            await db.execute(
                select(bucket.label("m"), Transaction.direction, Transaction.currency, func.sum(Transaction.amount))
                .where(Transaction.gym_id.in_(gym_ids), Transaction.status == TxStatus.SUCCESS, BOOKED_AT >= since)
                .group_by(bucket, Transaction.direction, Transaction.currency)
            )
        ).all())
        pending_rows = list((
            await db.execute(
                select(SalaryStatement.currency, func.count(SalaryStatement.id), func.sum(SalaryStatement.net_pay),
                       func.count(SalaryStatement.id).filter(and_(SalaryStatement.due_date < today,
                                                                  SalaryStatement.status.in_(pr.PAYABLE_STATUSES))))
                .where(SalaryStatement.gym_id.in_(gym_ids), SalaryStatement.status.in_(pr.UNPAID_STATUSES))
                .group_by(SalaryStatement.currency)
            )
        ).all())
        pending_items = list((
            await db.execute(
                select(SalaryStatement)
                .options(joinedload(SalaryStatement.employee))
                .where(SalaryStatement.gym_id.in_(gym_ids), SalaryStatement.status.in_(pr.UNPAID_STATUSES))
                .order_by(SalaryStatement.due_date, SalaryStatement.id)
                .limit(PENDING_STATEMENT_LIMIT)
            )
        ).scalars().all())
        recent = list((await db.execute(_newest_transactions(gym_ids, [], limit=RECENT_TX_LIMIT))).scalars().all())

    # ---- currencies & the 12-month series (amounts are never mixed across currencies)
    volume: dict[str, Decimal] = {}
    for _m, _d, cur, amt in month_rows:
        volume[cur] = volume.get(cur, Decimal(0)) + (amt or 0)
    currencies = sorted(volume, key=lambda c: (-volume[c], c))
    fallback_cur = gym.currency if gym else (gyms[0].currency if gyms else "INR")
    req_cur = (request.query_params.get("currency") or "").upper()
    currency = req_cur if req_cur in currencies else (currencies[0] if currencies else fallback_cur)

    keys = []
    d = first_month
    for _ in range(12):
        keys.append((d.year, d.month))
        d = _add_month(d)
    series = {k: {"income": Decimal(0), "outgoing": Decimal(0)} for k in keys}
    for m, direction, cur, amt in month_rows:
        if cur != currency or m is None or (m.year, m.month) not in series:
            continue
        series[(m.year, m.month)]["income" if direction == TxDirection.INCOMING else "outgoing"] += amt or 0
    months = []
    for y, mo in keys:
        inc, out = series[(y, mo)]["income"], series[(y, mo)]["outgoing"]
        first = date(y, mo, 1)
        months.append({"key": f"{y}-{mo:02d}", "short": f"{first:%b}", "label": f"{first:%B %Y}", "income": inc,
                       "outgoing": out, "net": inc - out, "income_fmt": money(inc, currency),
                       "outgoing_fmt": money(out, currency), "net_fmt": money(inc - out, currency)})
    cur_m, prev_m = months[-1], months[-2]

    def delta(now_v: Decimal, prev_v: Decimal) -> str | None:
        if prev_v == 0:
            return None
        pct = (now_v - prev_v) / abs(prev_v) * 100
        return f"{'+' if pct >= 0 else ''}{pct:.0f}% vs {prev_m['short']}"

    pending = [{"currency": c, "count": n, "amount": money(a or 0, c), "overdue": o} for c, n, a, o in pending_rows]
    return render(request, "owner/finance/index.html", {
        "gyms": gyms, "gym": gym, "currency": currency, "currencies": currencies,
        "chart": bar_chart(months, currency), "months": months,
        "kpi": {
            "income": money(cur_m["income"], currency), "outgoing": money(cur_m["outgoing"], currency),
            "net": money(cur_m["net"], currency), "net_negative": cur_m["net"] < 0,
            "income_delta": delta(cur_m["income"], prev_m["income"]),
            "outgoing_delta": delta(cur_m["outgoing"], prev_m["outgoing"]),
            "month": cur_m["label"],
        },
        "pending": pending, "pending_total": sum(p["count"] for p in pending),
        "overdue_total": sum(p["overdue"] for p in pending), "pending_items": pending_items, "recent": recent,
        "today": today, "section": "overview", "qs": _qs({"gym": gym.id if gym else None}),
    })


# ============================================================================================ transactions
@dataclass(slots=True)
class TxFilters:
    """The transactions page / CSV filters: the gyms read + every other where-clause (period included)."""

    gym_ids: list[int]
    clauses: list[Any] = field(default_factory=list)
    values: dict[str, Any] = field(default_factory=dict)  # normalised, for links (None = not set / the default)
    errors: dict[str, str] = field(default_factory=dict)
    period: str = DEFAULT_PERIOD  # a PERIODS key, or "custom" (from / to dates win over the period)

    @property
    def where(self) -> list[Any]:
        """Plain WHERE of the whole selection: the gym scope + the filters."""
        return [Transaction.gym_id.in_(self.gym_ids or [-1]), *self.clauses]

    @property
    def period_label(self) -> str:
        if self.period == "custom":
            start, end = self.values.get("from"), self.values.get("to")
            if start and end:
                return f"{date.fromisoformat(start):%d %b %Y} - {date.fromisoformat(end):%d %b %Y}"
            if start:
                return f"Since {date.fromisoformat(start):%d %b %Y}"
            return f"Until {date.fromisoformat(end):%d %b %Y}" if end else "All time"
        return PERIODS[self.period][0]


def _tx_filters(params: Any, gyms: list[Gym], tz: str) -> TxFilters:
    """Filters + normalised values + filter errors (shared by the list and the CSV export).

    The period: ``?from=`` / ``?to=`` dates when given (``custom``), else ``?period=`` (PERIODS), else the last 12
    months - the page never aggregates an owner's whole ledger unless asked to, and then within TOTALS_ROW_CAP."""
    errors: dict[str, str] = {}
    gym = _pick_gym(gyms, params.get("gym"))
    gym_ids = [gym.id] if gym else [g.id for g in gyms]
    clauses: list[Any] = []
    direction = _enum(TxDirection, params.get("direction"))
    category = _enum(TxCategory, params.get("category"))
    status = _enum(TxStatus, params.get("status"))
    if direction:
        clauses.append(Transaction.direction == direction)
    if category:
        clauses.append(Transaction.category == category)
    if status:
        clauses.append(Transaction.status == status)
    date_from, date_to = _filter_date(params.get("from")), _filter_date(params.get("to"))
    if params.get("from") and date_from is None:
        errors["from"] = "Use a valid start date."
    if params.get("to") and date_to is None:
        errors["to"] = "Use a valid end date."
    if date_from and date_to and date_to < date_from:
        errors["to"] = "The end date is before the start date."
        date_to = None
    custom = bool(str(params.get("from") or "").strip() or str(params.get("to") or "").strip())
    period = "custom" if custom else str(params.get("period") or "")
    if period not in PERIODS and period != "custom":
        period = DEFAULT_PERIOD
    if date_from:
        clauses.append(BOOKED_AT >= _local_midnight_utc(date_from, tz))
    if date_to:
        clauses.append(BOOKED_AT < _local_midnight_utc(date_to + timedelta(days=1), tz))
    days = PERIODS[period][1] if period in PERIODS else None
    if days:
        clauses.append(BOOKED_AT >= _local_midnight_utc(local_today(tz) - timedelta(days=days - 1), tz))
    q = re.sub(r"\s+", " ", str(params.get("q") or "")).strip()[:MAX_SEARCH]
    if q:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        clauses.append(or_(Transaction.description.ilike(like, escape="\\"),
                           Transaction.party_label.ilike(like, escape="\\"),
                           Transaction.provider_reference.ilike(like, escape="\\"),
                           Transaction.provider_order_id.ilike(like, escape="\\")))
    values = {"gym": gym.id if gym else None, "direction": direction.value if direction else None,
              "category": category.value if category else None, "status": status.value if status else None,
              "from": date_from.isoformat() if date_from else None, "to": date_to.isoformat() if date_to else None,
              "q": q or None, "period": period if period in PERIODS and period != DEFAULT_PERIOD else None}
    return TxFilters(gym_ids, clauses, values, errors, period)


async def _tx_totals(db: DB, f: TxFilters) -> tuple[list[Any], int, bool]:
    """Count + completed sums by (direction, currency) of the selection, over at most TOTALS_ROW_CAP + 1 rows: an index
    scan that stops there however long the ledger is. Returns (rows, total, capped) - ``capped``: more than
    TOTALS_ROW_CAP match, ``total`` is TOTALS_ROW_CAP and the sums cover an arbitrary part (the page hides them)."""
    picked = (select(Transaction.direction, Transaction.currency, Transaction.amount, Transaction.status)
              .where(*f.where).limit(TOTALS_ROW_CAP + 1).subquery("picked"))
    rows = (await db.execute(
        select(picked.c.direction, picked.c.currency, func.count(),
               func.coalesce(func.sum(picked.c.amount).filter(picked.c.status == TxStatus.SUCCESS), 0))
        .group_by(picked.c.direction, picked.c.currency)
    )).all()
    total = sum(int(r[2]) for r in rows)
    if total > TOTALS_ROW_CAP:
        return rows, TOTALS_ROW_CAP, True
    return rows, total, False


@router.get("/owner/finance/transactions")
async def transactions(request: Request, db: DB, user: OwnerUser):
    gyms = await _owner_gyms(db, user)
    tz = _tz(user)
    f = _tx_filters(request.query_params, gyms, tz)
    values = f.values
    page = max(1, min(_int(request.query_params.get("page")) or 1, MAX_TX_PAGE))
    totals_rows, total, capped = await _tx_totals(db, f)
    pages = max(1, math.ceil(total / PAGE_SIZE))
    page = min(page, pages)
    offset = (page - 1) * PAGE_SIZE
    rows = list((await db.execute(_newest_transactions(f.gym_ids, f.clauses, limit=PAGE_SIZE, offset=offset)))
                .scalars().all())
    summary: list[dict[str, Any]] = []
    if not capped:  # sums of an arbitrary part of the matches would be wrong money: shown only when exact
        sums: dict[str, dict[str, Decimal]] = {}
        for direction, cur, _n, amount in totals_rows:
            key = "in" if direction == TxDirection.INCOMING else "out"
            sums.setdefault(cur, {"in": Decimal(0), "out": Decimal(0)})[key] += amount
        summary = [{"currency": c, "in": money(v["in"], c), "out": money(v["out"], c), "net": money(v["in"] - v["out"], c),
                    "negative": v["in"] < v["out"]} for c, v in sorted(sums.items())]
    base = {k: v for k, v in values.items() if k not in ("from", "to", "period")}
    period_links = [{"key": key, "label": label, "active": f.period == key,
                     "qs": _qs(base, period=None if key == DEFAULT_PERIOD else key)} for key, (label, _d) in PERIODS.items()]
    return render(request, "owner/finance/transactions.html", {
        "gyms": gyms, "rows": rows, "total": total, "total_capped": capped, "page": page, "pages": pages,
        "filters": values, "errors": f.errors,
        "summary": summary, "qs": _qs(values), "prev_qs": _qs(values, page=page - 1) if page > 1 else None,
        "next_qs": _qs(values, page=page + 1) if page < pages else None, "csv_qs": _qs(values),
        "period": f.period, "period_label": f.period_label, "period_links": period_links,
        "default_period": f.period == DEFAULT_PERIOD, "totals_cap": TOTALS_ROW_CAP,
        "directions": [(d.value, d.value.capitalize()) for d in TxDirection],
        "categories": [(c.value, c.value.replace("_", " ").capitalize()) for c in TxCategory],
        "statuses": [(s.value, s.value.replace("_", " ").capitalize()) for s in TxStatus],
        "section": "transactions", "first_row": offset + 1 if rows else 0,
        "last_row": offset + len(rows),
    })


CSV_COLUMNS = ["id", "booked_at", "gym", "direction", "category", "status", "amount", "currency", "description",
               "party", "provider", "reference", "proof_verified", "salary_statement_id"]


def csv_safe(value: Any) -> str:
    """Neutralise spreadsheet formulas: cells starting with = + - @ (or tab / CR) get a leading single quote."""
    if value is None:
        return ""
    s = str(value)
    if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + s
    return s


def _csv_stmt(f: TxFilters, *, size: int, cursor: tuple[datetime, int] | None = None) -> Select:
    """One export batch: the next ``size`` rows after ``cursor``, newest first - ORDER BY (booked_at, id) DESC is the
    keyset. Per gym (``_newest_per_gym``) the cursor is an index condition of ix_transactions_gym_booked, so every
    batch is a short range scan wherever it starts, never a re-read of the rows already exported."""
    clauses = list(f.clauses)
    if cursor is not None:
        clauses.append(tuple_(BOOKED_AT, Transaction.id) < tuple_(
            literal(cursor[0], type_=Transaction.created_at.type), literal(cursor[1], type_=Transaction.id.type)))
    gyms = _gym_rows(f.gym_ids)
    t = _newest_per_gym(gyms, clauses, Transaction.id, BOOKED_AT.label("booked_at"), Transaction.direction,
                        Transaction.category, Transaction.status, Transaction.amount, Transaction.currency,
                        Transaction.description, Transaction.party_label, Transaction.provider,
                        Transaction.provider_reference, Transaction.proof_verified, Transaction.salary_statement_id,
                        limit=size)
    return (
        select(t.c.id, t.c.booked_at, gyms.c.gym_name, t.c.direction, t.c.category, t.c.status, t.c.amount,
               t.c.currency, t.c.description, t.c.party_label, t.c.provider, t.c.provider_reference, t.c.proof_verified,
               t.c.salary_statement_id)
        .select_from(gyms).join(t, true())
        .order_by(t.c.booked_at.desc(), t.c.id.desc())
        .limit(size)
    )


async def _csv_batch(stmt: Select) -> list[Any]:
    """One batch in its own short transaction: a pooled connection is held only while the batch query runs."""
    async with _export_slots, get_sessionmaker()() as session:
        await long_statement(session, EXPORT_STATEMENT_MS)  # a big ledger may need longer than a page query
        return list((await session.execute(stmt)).all())


async def _csv_rows(f: TxFilters, tz: str, *, batch: int | None = None,
                    max_rows: int | None = None) -> AsyncIterator[str]:
    """The CSV, streamed in keyset batches of ``batch`` rows (``WHERE (booked_at, id) < last row ... LIMIT``), at
    most ``max_rows`` rows. Memory and connection time are bounded per batch whatever the ledger size; rows that
    change while the file downloads may appear in a later batch or not at all (no single snapshot is held)."""
    batch = max(1, batch or EXPORT_BATCH)
    max_rows = max(0, EXPORT_MAX_ROWS if max_rows is None else max_rows)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_COLUMNS)
    yield "﻿" + buf.getvalue()
    zone = safe_zone(tz)
    sent = 0
    cursor: tuple[datetime, int] | None = None
    while sent < max_rows:
        size = min(batch, max_rows - sent)
        rows = await _csv_batch(_csv_stmt(f, size=size, cursor=cursor))
        if not rows:
            break
        buf.seek(0)
        buf.truncate(0)
        for r in rows:
            booked = r.booked_at.astimezone(zone).strftime("%Y-%m-%d %H:%M") if r.booked_at else ""
            writer.writerow([csv_safe(v) for v in (
                r.id, booked, r.gym_name, r.direction.value, r.category.value, r.status.value, f"{r.amount:.2f}",
                r.currency, r.description, r.party_label, r.provider, r.provider_reference,
                "" if r.proof_verified is None else ("yes" if r.proof_verified else "no"), r.salary_statement_id,
            )])
        yield buf.getvalue()
        sent += len(rows)
        if len(rows) < size:
            break
        cursor = (rows[-1].booked_at, rows[-1].id)


@router.get("/owner/finance/transactions.csv")
async def transactions_csv(request: Request, db: DB, user: OwnerUser):
    gyms = await _owner_gyms(db, user)
    tz = _tz(user)
    f = _tx_filters(request.query_params, gyms, tz)
    rows = int(await db.scalar(  # capped COUNT: reads at most EXPORT_MAX_ROWS + 1 rows
        select(func.count()).select_from(select(Transaction.id).where(*f.where).limit(EXPORT_MAX_ROWS + 1).subquery())
    ) or 0)
    if rows > EXPORT_MAX_ROWS:
        return redirect("/owner/finance/transactions" + _qs(f.values),
                        f"That export has more than {EXPORT_MAX_ROWS:,} transactions - choose a shorter date range "
                        "(or more filters) and export again.", "warning")
    await db.close()  # the stream uses its own short sessions: no pooled connection is held while it downloads
    filename = f"ironpulse-transactions-{local_today(tz):%Y%m%d}.csv"
    return StreamingResponse(_csv_rows(f, tz), media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store",
    })


# ============================================================================================ payroll list
def _month_value(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


async def _payroll_context(request: Request, db: DB, user: Any, *, gen_errors: dict[str, str] | None = None,
                           gen_values: dict[str, Any] | None = None) -> dict[str, Any]:
    gyms = await _owner_gyms(db, user)
    params = request.query_params
    gym = _pick_gym(gyms, params.get("gym"))
    gym_ids = [gym.id] if gym else [g.id for g in gyms]
    status_raw = str(params.get("status") or "")
    status_filter: tuple[StatementStatus, ...] | None = None
    if status_raw == "unpaid":
        status_filter = pr.UNPAID_STATUSES
    elif status_raw == "overdue":
        status_filter = pr.PAYABLE_STATUSES
    elif _enum(StatementStatus, status_raw):
        status_filter = (StatementStatus(status_raw),)
    else:
        status_raw = ""
    employee_id = _int(params.get("employee")) if params.get("employee") else None
    tz = _tz(user)
    today = local_today(tz)

    staff_cols = (Employee.id, Employee.full_name, Employee.status, Gym.name)
    staff_scope = (Gym.owner_id == user.id, Employee.deleted_at.is_(None))
    employees = list((  # the filter's options: current staff first, then former staff, at most EMPLOYEE_OPTIONS_LIMIT
        await db.execute(
            select(*staff_cols).join(Gym, Gym.id == Employee.gym_id).where(*staff_scope)
            .order_by(case((Employee.status == EmploymentStatus.TERMINATED, 1), else_=0), Employee.full_name, Employee.id)
            .limit(EMPLOYEE_OPTIONS_LIMIT)
        )
    ).all())
    employee = None
    if params.get("employee"):  # validated with one scoped lookup (it may lie beyond the options' cap)
        employee = (await db.execute(
            select(*staff_cols).join(Gym, Gym.id == Employee.gym_id).where(*staff_scope, Employee.id == employee_id)
        )).first() if employee_id is not None and 0 < employee_id < 2**63 else None
        if employee is None:
            raise NotFound("Staff member not found.")
        if all(e.id != employee.id for e in employees):
            employees.append(employee)

    clauses: list[Any] = [SalaryStatement.gym_id.in_(gym_ids or [-1])]
    if status_filter:
        clauses.append(SalaryStatement.status.in_(status_filter))
    if status_raw == "overdue":
        clauses.append(SalaryStatement.due_date < today)
    if employee is not None:
        clauses.append(SalaryStatement.employee_id == employee.id)

    kpi_rows = (
        await db.execute(
            select(SalaryStatement.status, SalaryStatement.currency, func.count(SalaryStatement.id),
                   func.sum(SalaryStatement.net_pay),
                   func.count(SalaryStatement.id).filter(SalaryStatement.due_date < today))
            .where(SalaryStatement.gym_id.in_(gym_ids or [-1]))
            .group_by(SalaryStatement.status, SalaryStatement.currency)
        )
    ).all()
    total = await db.scalar(select(func.count(SalaryStatement.id)).where(*clauses)) or 0
    pages = max(1, math.ceil(total / STATEMENT_PAGE_SIZE))
    page = min(max(1, _int(params.get("page")) or 1), pages)
    unpaid = SalaryStatement.status.in_(pr.UNPAID_STATUSES)
    statements = list((
        await db.execute(
            select(SalaryStatement)
            .options(joinedload(SalaryStatement.employee))
            .where(*clauses)
            .order_by(case((unpaid, 0), else_=1), case((unpaid, SalaryStatement.due_date)),
                      SalaryStatement.period_end.desc(), SalaryStatement.id.desc())
            .limit(STATEMENT_PAGE_SIZE)
            .offset((page - 1) * STATEMENT_PAGE_SIZE)
        )
    ).scalars().all())

    def bucket(statuses: tuple[StatementStatus, ...]) -> dict[str, Any]:
        count = 0
        amounts: dict[str, Decimal] = {}
        for st, cur, n, amt, _overdue in kpi_rows:
            if st in statuses:
                count += n
                amounts[cur] = amounts.get(cur, Decimal(0)) + (amt or 0)
        label = " + ".join(money(v, c) for c, v in sorted(amounts.items()) if v) or None
        return {"count": count, "amount": label}

    values = {"gym": gym.id if gym else None, "status": status_raw or None, "employee": employee.id if employee else None}
    default_month = _month_value(today.replace(day=1) - timedelta(days=1))
    run = await pr.generation_in_flight(db, user.id)  # "run payroll" for all gyms, still in the background
    generation = None
    if run is not None:
        payload = run.payload or {}
        gym_count = int(payload.get("gyms") or 0)
        done = len(payload.get("done") or [])
        try:
            run_label = pr.GenerationResult(int(payload["year"]), int(payload["month"])).label
        except (KeyError, TypeError, ValueError):
            run_label = "the month"
        generation = {"id": run.id, "status": run.status.value, "label": run_label, "gyms": gym_count, "done": done,
                      "progress": round(run.progress or 0), "url": f"/owner/payroll/runs/{run.id}"}
    return {
        "gyms": gyms, "gym": gym, "gym_names": {g.id: g.name for g in gyms}, "employees": employees,
        "employee": employee, "statements": statements, "total": total, "page": page, "pages": pages,
        "filters": values, "generation": generation,
        "prev_qs": _qs(values, page=page - 1) if page > 1 else None,
        "next_qs": _qs(values, page=page + 1) if page < pages else None,
        "kpi": {
            "due": bucket((StatementStatus.DUE,)),
            "awaiting": bucket((StatementStatus.AWAITING_PROOF,)),
            "review": bucket((StatementStatus.PROOF_REVIEW,)),
            "overdue": {"count": sum(o for s, _c, _n, _a, o in kpi_rows if s in pr.PAYABLE_STATUSES)},
            "paid": bucket((StatementStatus.PAID,)),
        },
        "today": today, "section": "payroll",
        "gen_errors": gen_errors or {},
        "gen_values": gen_values or {"month": default_month, "gym": gym.id if gym else ""},
        "max_month": _month_value(today),
        "statuses": [("unpaid", "All unpaid"), ("overdue", "Overdue"), ("due", "Due"), ("awaiting_proof", "Awaiting proof"),
                     ("proof_review", "Verifying proof"), ("paid", "Paid")],
    }


@router.get("/owner/payroll")
async def payroll_list(request: Request, db: DB, user: OwnerUser):
    return render(request, "owner/payroll/index.html", await _payroll_context(request, db, user))


@router.post("/owner/payroll/generate")
async def payroll_generate(request: Request, db: DB, user: OwnerUser):
    form = await request.form()
    month_raw = str(form.get("month") or "").strip()
    gym_raw = str(form.get("gym") or "").strip()
    gyms = await _owner_gyms(db, user)
    errors: dict[str, str] = {}
    today = local_today(_tz(user))
    year = month = 0
    m = re.fullmatch(r"(\d{4})-(\d{2})", month_raw)
    if not m or not 1 <= int(m.group(2)) <= 12:
        errors["month"] = "Choose a month (YYYY-MM)."
    else:
        year, month = int(m.group(1)), int(m.group(2))
        if (year, month) > (today.year, today.month):
            errors["month"] = "You can't run payroll for a future month."
        elif (today.year - year) * 12 + today.month - month > 36:
            errors["month"] = "Payroll can be generated for the last 36 months only."
    selected = _pick_gym(gyms, gym_raw)  # foreign id -> 404
    targets = [selected] if selected else gyms
    if not targets:
        errors["gym"] = "Add a gym first."
    if errors:
        ctx = await _payroll_context(request, db, user, gen_errors=errors, gen_values={"month": month_raw, "gym": gym_raw})
        return render(request, "owner/payroll/index.html", ctx, status_code=400)
    label = pr.GenerationResult(year, month).label
    if selected is None and len(gyms) > 1:
        # every gym: a background job - one short transaction per gym, progress over task.update, a notification at
        # the end - instead of a request that grows with the number of gyms and their staff
        job, queued = await pr.queue_owner_generation(db, user, year, month, gym_count=len(gyms))
        if queued:
            await audit(db, user, "payroll.generate_queued", "user", user.id, request=request,
                        detail={"month": month_raw, "gyms": len(gyms), "job_id": job.id if job is not None else None})
        await db.commit()
        if not queued:
            return redirect("/owner/payroll", f"Payroll for {label} is already being generated - statements appear "
                                              "here as each gym finishes.", "info")
        return redirect("/owner/payroll", f"Payroll is being generated for {len(gyms)} gyms ({label}). Statements "
                                          "appear here as each gym finishes - we'll notify you when it's done.", "info")
    g = targets[0]  # one gym: generated right here (its INSERTs and id lists are chunked whatever its size)
    gym_id, gym_name = g.id, g.name
    try:
        async with db.begin_nested():
            res = await pr.generate_statements(db, g, year, month, queue_explanations=False)
            explained = res.created_ids[:pr.EXPLAIN_PER_RUN]  # AI explanations: the rest are explained on demand
            await pr.queue_explanations_for(db, explained)
            await audit(db, user, "payroll.generate", "gym", gym_id, gym_id=gym_id,
                        detail={"month": month_raw, "created": len(res.created_ids), "skipped": len(res.skipped)},
                        request=request)
        await db.commit()
    except DBAPIError:  # the SAVEPOINT was rolled back and the session stays usable
        log.exception("payroll generation failed for gym %s (%s)", gym_id, month_raw)
        return redirect("/owner/payroll", f"Nothing was generated for {label}. Payroll could not be generated for "
                                          f"{gym_name} - please try again.", "error")
    created, skipped = len(res.created_ids), len(res.skipped)
    note = ""
    if created > len(explained):
        note = (f" AI explanations were queued for the first {len(explained)}; open any other statement to ask for "
                "one." if explained else " Open a statement to ask the AI to explain it.")
    if created:
        msg = f"{created} salary statement{'s' if created != 1 else ''} created for {label}."
        if skipped:
            msg += f" {skipped} staff member{'s were' if skipped != 1 else ' was'} already covered or not employed that month."
        return redirect("/owner/payroll?status=unpaid", msg + note, "success")
    return redirect("/owner/payroll", f"Nothing new for {label}: every active staff member is already covered.", "info")


@router.get("/owner/payroll/runs/{job_id:int}")
async def payroll_run_status(request: Request, job_id: IdParam, db: DB, user: OwnerUser):
    """JSON progress of one of the owner's own "run payroll" jobs (the payroll page polls it when the socket is quiet)."""
    job = await db.scalar(select(Job).where(Job.id == job_id, Job.user_id == user.id, Job.kind == pr.GENERATE_JOB))
    if job is None:
        raise NotFound("Payroll run not found.")
    payload = job.payload or {}
    done, gyms = len(payload.get("done") or []), int(payload.get("gyms") or 0)
    return JSONResponse({"id": job.id, "status": job.status.value, "progress": round(job.progress or 0),
                         "message": f"{done} of {gyms} gyms done" if gyms else None},
                        headers={"Cache-Control": "no-store"})


# ============================================================================================ statement detail
async def _load_statement(db: DB, user: Any, statement_id: int, *, lock: bool = False) -> tuple[SalaryStatement, Gym]:
    stmt = (
        select(SalaryStatement, Gym)
        .join(Gym, Gym.id == SalaryStatement.gym_id)
        .options(joinedload(SalaryStatement.employee))
        .where(SalaryStatement.id == statement_id, Gym.owner_id == user.id)
    )
    if lock:
        stmt = stmt.with_for_update(of=SalaryStatement)
    row = (await db.execute(stmt)).unique().first()
    if row is None:
        raise NotFound("Salary statement not found.")
    return row[0], row[1]


async def _explaining(db: DB, statement_id: int) -> bool:
    return bool(await db.scalar(
        select(func.count(Job.id)).where(Job.dedupe_key == f"payroll_explain:{statement_id}",
                                         Job.status.in_((JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.PAUSED)))))


async def _detail_context(request: Request, db: DB, user: Any, st: SalaryStatement, gym: Gym, *,
                          errors: dict[str, str] | None = None, form: dict[str, Any] | None = None) -> dict[str, Any]:
    employee = st.employee
    txs = list((
        await db.execute(
            select(Transaction).where(Transaction.salary_statement_id == st.id)
            .order_by(Transaction.created_at, Transaction.id)
        )
    ).scalars().all())
    payment = next((t for t in txs if t.id == st.transaction_id), None)
    explanation, provider = pr.explanation_for(st)
    mode, mode_reason = pr.payout_mode(st, employee)
    today = local_today(_tz(user))
    rules = st.rules or {}
    form = form or {}
    default_ref = (payment.provider_reference if payment and payment.provider != "manual" else "") or ""
    return {
        "st": st, "gym": gym, "employee": employee, "txs": txs, "payment": payment, "rules": rules,
        "explanation": explanation, "explanation_provider": provider, "explaining": await _explaining(db, st.id),
        "payout": pr.payout_instructions(employee, gym, st), "pay_mode": mode, "pay_mode_reason": mode_reason,
        "overdue": st.status in pr.PAYABLE_STATUSES and st.due_date < today,
        "check": (payment.proof_check if payment else None) or {},
        "can_confirm": bool(st.status == StatementStatus.AWAITING_PROOF and payment and payment.proof_key
                            and payment.proof_verified is False),
        "can_upload": st.status in pr.PAYABLE_STATUSES,
        "can_adjust": st.status == StatementStatus.DUE and st.transaction_id is None,
        "errors": errors or {},
        "form": {
            "reference": form.get("reference", default_ref),
            "amount": form.get("amount", f"{st.net_pay:.2f}"),
            "paid_on": form.get("paid_on", today.isoformat()),
            "justification": form.get("justification", ""),
            "unpaid_leave_days": form.get("unpaid_leave_days", st.unpaid_leave_days),
        },
        "today": today, "section": "payroll", "ai_feature": True,
        "period": pr.period_label(st.period_start, st.period_end),
        "deductions_total": sum((Decimal(v["amount"]) for v in (st.employee_deductions or {}).values()), Decimal(0)),
        "employer_total": sum((Decimal(v["amount"]) for v in (st.employer_contributions or {}).values()), Decimal(0)),
        "settlement": rules.get("settlement"), "test_mode": mode == "test",
        "rules_name": rules.get("country_name") or rules_for(st.country_code).name,
    }


@router.get("/owner/payroll/{statement_id:int}")
async def payroll_detail(request: Request, statement_id: IdParam, db: DB, user: OwnerUser):
    st, gym = await _load_statement(db, user, statement_id)
    return render(request, "owner/payroll/detail.html", await _detail_context(request, db, user, st, gym))


async def _detail_error(request: Request, db: DB, user: Any, statement_id: int, errors: dict[str, str],
                        form: dict[str, Any] | None = None):
    st, gym = await _load_statement(db, user, statement_id)
    ctx = await _detail_context(request, db, user, st, gym, errors=errors, form=form)
    return render(request, "owner/payroll/detail.html", ctx, status_code=400)


@router.post("/owner/payroll/{statement_id:int}/pay")
async def payroll_pay(request: Request, statement_id: IdParam, db: DB, user: OwnerUser):
    st, gym = await _load_statement(db, user, statement_id, lock=True)
    try:
        tx = await pr.initiate_payout(db, st, st.employee, gym, user, request=request)
    except pr.PayrollError as exc:
        await db.commit()  # keeps the audit trail / failed payout attempt
        return await _detail_error(request, db, user, statement_id, {"pay": str(exc)})
    await db.commit()
    amount = money(tx.amount, tx.currency)
    if tx.provider == "test":
        msg = (f"TEST MODE: payout of {amount} simulated (reference {tx.provider_reference}) - no money moved. "
               "Upload a proof to complete the ledger.")
    else:
        msg = f"Payout of {amount} sent through Cashfree. Upload the transfer confirmation to complete the ledger."
    return redirect(f"/owner/payroll/{st.id}#proof", msg, "success")


@router.post("/owner/payroll/{statement_id:int}/proof")
async def payroll_proof(request: Request, statement_id: IdParam, db: DB, user: OwnerUser):
    st, _gym = await _load_statement(db, user, statement_id, lock=True)
    form = await request.form()
    values = {k: str(form.get(k) or "").strip() for k in ("reference", "amount", "paid_on")}
    errors: dict[str, str] = {}
    if st.status not in pr.PAYABLE_STATUSES:
        errors["proof"] = ("This proof is already being verified." if st.status == StatementStatus.PROOF_REVIEW
                           else "This statement no longer accepts a proof.")
        return await _detail_error(request, db, user, statement_id, errors, values)
    reference = re.sub(r"\s+", " ", values["reference"])
    if not pr.REFERENCE_RE.fullmatch(reference):
        errors["reference"] = "Enter the bank reference / UTR (6-40 letters or digits)."
    amount = _decimal(values["amount"])
    if amount is None or amount <= 0:
        errors["amount"] = "Enter the amount you paid."
    elif abs(amount - st.net_pay) > max(Decimal("0.01"), st.net_pay * pr.AMOUNT_TOLERANCE):
        errors["amount"] = (f"The amount must match the net pay {money(st.net_pay, st.currency)} (within 1%) - "
                            "partial payments are not supported.")
    paid_on = _date(values["paid_on"]) if values["paid_on"] else None
    today = local_today(_tz(user))
    if values["paid_on"] and paid_on is None:
        errors["paid_on"] = "Use a valid date."
    elif paid_on and paid_on > today:
        errors["paid_on"] = "The payment date can't be in the future."
    elif paid_on and paid_on < st.period_start - timedelta(days=31):
        errors["paid_on"] = "The payment date is too long before this salary period."
    upload = form.get("proof")
    if upload is None or isinstance(upload, str) or not getattr(upload, "filename", ""):
        errors["proof"] = "Attach the proof of transaction (image or PDF)."
    if errors:
        return await _detail_error(request, db, user, statement_id, errors, values)
    try:
        await pr.record_proof(db, st, st.employee, user, upload=upload, reference=reference,
                              amount_paid=_cents(amount), paid_on=paid_on, request=request)
    except storage.UploadError as exc:
        return await _detail_error(request, db, user, statement_id, {"proof": str(exc)}, values)
    except pr.PayrollError as exc:
        return await _detail_error(request, db, user, statement_id, {exc.field or "proof": str(exc)}, values)
    await db.commit()
    return redirect(f"/owner/payroll/{st.id}", "Proof uploaded - our OCR engines are checking the amount, reference and "
                                               "date. This page updates when it's done.", "success")


@router.post("/owner/payroll/{statement_id:int}/confirm")
async def payroll_confirm(request: Request, statement_id: IdParam, db: DB, user: OwnerUser):
    st, _gym = await _load_statement(db, user, statement_id, lock=True)
    form = await request.form()
    justification = re.sub(r"\s+", " ", str(form.get("justification") or "")).strip()
    if len(justification) > 500:
        return await _detail_error(request, db, user, statement_id,
                                   {"justification": "Keep the justification under 500 characters."},
                                   {"justification": justification[:500]})
    try:
        await pr.confirm_despite_mismatch(db, st, st.employee, user, justification, request=request)
    except pr.PayrollError as exc:
        return await _detail_error(request, db, user, statement_id, {exc.field or "confirm": str(exc)},
                                   {"justification": justification})
    await db.commit()
    return redirect(f"/owner/payroll/{st.id}", "Payment confirmed manually. The ledger marks the proof as unverified and "
                                               "your justification is in the audit log.", "warning")


@router.post("/owner/payroll/{statement_id:int}/adjust")
async def payroll_adjust(request: Request, statement_id: IdParam, db: DB, user: OwnerUser):
    st, gym = await _load_statement(db, user, statement_id, lock=True)
    form = await request.form()
    raw = str(form.get("unpaid_leave_days") or "").strip()
    days = _int(raw)
    if days is None or days < 0 or days > 3660:
        return await _detail_error(request, db, user, statement_id,
                                   {"unpaid_leave_days": "Enter a whole number of days (0 or more)."},
                                   {"unpaid_leave_days": raw})
    try:
        await pr.adjust_unpaid_leave(db, st, st.employee, user, days, gym_country=gym.country_code, request=request)
    except pr.PayrollError as exc:
        return await _detail_error(request, db, user, statement_id, {exc.field or "unpaid_leave_days": str(exc)},
                                   {"unpaid_leave_days": raw})
    await db.commit()
    return redirect(f"/owner/payroll/{st.id}", f"Recalculated with {days} unpaid leave day{'s' if days != 1 else ''}: "
                                               f"net pay is now {money(st.net_pay, st.currency)}.", "success")


@router.post("/owner/payroll/{statement_id:int}/explain")
async def payroll_explain(request: Request, statement_id: IdParam, db: DB, user: OwnerUser):
    st, _gym = await _load_statement(db, user, statement_id)
    from app.ai.client import ai

    if not ai.status()["available"]:
        return redirect(f"/owner/payroll/{st.id}", "AI is unavailable right now - the standard explanation is shown.", "warning")
    st.ai_explanation = None
    st.ai_provider = None
    await enqueue(db, "payroll_explain", {"statement_id": st.id}, dedupe_key=f"payroll_explain:{st.id}", max_attempts=2,
                  user_id=user.id)
    await db.commit()
    return redirect(f"/owner/payroll/{st.id}#explanation", "Asking the AI to explain this statement - it appears in a few "
                                                           "seconds.", "info")


@router.get("/owner/payroll/{statement_id:int}/proof")
async def payroll_proof_file(request: Request, statement_id: IdParam, db: DB, user: OwnerUser):
    st, _gym = await _load_statement(db, user, statement_id)
    tx_id = _int(request.query_params.get("tx"))
    stmt = select(Transaction).where(Transaction.salary_statement_id == st.id, Transaction.proof_key.is_not(None))
    stmt = stmt.where(Transaction.id == tx_id) if tx_id else stmt.order_by(Transaction.id.desc())
    tx = (await db.execute(stmt.limit(1))).scalar_one_or_none()
    if tx is None or not tx.proof_key:
        raise NotFound("No proof uploaded.")
    try:
        data = await storage.load_private(tx.proof_key)
    except Exception:
        raise NotFound("The proof file is no longer available.") from None
    is_pdf = data.startswith(b"%PDF-")
    return Response(data, media_type="application/pdf" if is_pdf else "image/jpeg", headers={
        "Content-Disposition": f'inline; filename="proof-{st.id}.{"pdf" if is_pdf else "jpg"}"',
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
    })


@router.get("/owner/payroll/{statement_id:int}/test-receipt.png")
async def payroll_test_receipt(request: Request, statement_id: IdParam, db: DB, user: OwnerUser):
    """TEST MODE: watermarked receipt for a simulated payout (to try the proof upload + OCR verification)."""
    import asyncio

    from app.config import get_settings

    st, gym = await _load_statement(db, user, statement_id)
    payment = await db.get(Transaction, st.transaction_id) if st.transaction_id else None
    if payment is None or payment.provider != "test" or not get_settings().payments_test_mode:
        raise NotFound("Simulated receipts exist only for TEST MODE payouts.")
    png = await asyncio.to_thread(
        pr.render_test_receipt, amount=st.net_pay, currency=st.currency, reference=payment.provider_reference or "",
        paid_on=local_today(_tz(user)), beneficiary=st.employee.display_name, payer=gym.name, statement_id=st.id,
    )
    return Response(png, media_type="image/png", headers={
        "Content-Disposition": f'attachment; filename="test-receipt-{st.id}.png"', "Cache-Control": "no-store",
    })


@router.get("/owner/payroll/{statement_id:int}/status")
async def payroll_status(request: Request, statement_id: IdParam, db: DB, user: OwnerUser):
    st, _gym = await _load_statement(db, user, statement_id)
    return JSONResponse({"id": st.id, "status": st.status.value, "label": st.status.value.replace("_", " ").capitalize(),
                         "explained": bool(st.ai_explanation), "explaining": await _explaining(db, st.id)},
                        headers={"Cache-Control": "no-store"})


# ============================================================================================ settlement / termination
async def _settlement_context(request: Request, db: DB, user: Any, employee: Employee, *,
                              values: dict[str, Any] | None = None, errors: dict[str, str] | None = None,
                              blocked: list[str] | None = None) -> dict[str, Any]:
    today = local_today(_tz(user))
    gym = employee.gym
    dues = await pr.outstanding_dues(db, employee, as_of=today)
    final = await pr.final_settlement_of(db, employee.id)
    blockers = await pr.termination_blockers(db, employee)
    params = values if values is not None else dict(request.query_params)
    v = {
        "termination_date": str(params.get("termination_date") or today.isoformat())[:10],
        "reason": str(params.get("reason") or "resignation")[:40],
        "unpaid_leave_days": str(params.get("unpaid_leave_days") or "0")[:6],
        "note": str(params.get("note") or "")[:200],
    }
    errors = dict(errors or {})
    preview = None
    can_settle = employee.status in (EmploymentStatus.ACTIVE, EmploymentStatus.SUSPENDED) and final is None
    if can_settle:
        days = _int(v["unpaid_leave_days"])
        term = _date(v["termination_date"])
        if days is None or days < 0:
            errors.setdefault("unpaid_leave_days", "Enter a whole number of days (0 or more).")
        elif term is None:
            errors.setdefault("termination_date", "Choose the last working day.")
        else:
            try:
                preview = await pr.settlement_preview(db, employee, termination_date=term, reason=v["reason"],
                                                      unpaid_leave_days=days, gym_country=gym.country_code, today=today)
            except pr.PayrollError as exc:
                errors.setdefault(exc.field or "termination_date", str(exc))
    rules = rules_for(employee.country_code)
    return {
        "employee": employee, "gym": gym, "dues": dues, "dues_totals": dues.totals(), "final": final,
        "blockers": blockers, "blocked": blocked or [], "values": v, "errors": errors, "preview": preview,
        "preview_expl": pr.deterministic_explanation(preview) if preview else None, "can_settle": can_settle,
        "reasons": list(TERMINATION_REASONS.items()), "today": today, "section": "payroll",
        "max_termination": (today + timedelta(days=pr.MAX_FUTURE_TERMINATION_DAYS)).isoformat(),
        "never_started": employee.status in (EmploymentStatus.INVITED, EmploymentStatus.PENDING_VERIFICATION),
        "rules_name": rules.name if rules.code != "DEFAULT" else employee.country_code,
    }


@router.get("/owner/staff/{employee_id:int}/settlement")
async def settlement_page(request: Request, employee_id: IdParam, db: DB, user: OwnerUser):
    employee = await get_owned_employee(db, employee_id, user, joinedload(Employee.gym))
    return render(request, "owner/payroll/settlement.html", await _settlement_context(request, db, user, employee))


@router.post("/owner/staff/{employee_id:int}/settlement")
async def settlement_create(request: Request, employee_id: IdParam, db: DB, user: OwnerUser):
    employee = await get_owned_employee(db, employee_id, user, joinedload(Employee.gym))
    form = await request.form()
    values = {k: str(form.get(k) or "").strip() for k in ("termination_date", "reason", "unpaid_leave_days", "note")}
    errors: dict[str, str] = {}
    term = _date(values["termination_date"])
    if term is None:
        errors["termination_date"] = "Choose the last working day."
    days = _int(values["unpaid_leave_days"] or "0")
    if days is None or days < 0:
        errors["unpaid_leave_days"] = "Enter a whole number of days (0 or more)."
    if values["reason"] not in TERMINATION_REASONS:
        errors["reason"] = "Choose a reason for leaving."
    if len(values["note"]) > 200:
        errors["note"] = "Keep the note under 200 characters."
    if not errors:
        try:
            st = await pr.create_final_settlement(db, employee, term, values["reason"], note=values["note"],
                                                  unpaid_leave_days=days, actor=user, gym=employee.gym,
                                                  request=request, today=local_today(_tz(user)))
        except pr.PayrollError as exc:
            errors[exc.field or "form"] = str(exc)
        else:
            await db.commit()
            if st.status == StatementStatus.PAID:
                return redirect(f"/owner/staff/{employee.id}/settlement",
                                "Final settlement recorded - nothing is owed, so you can now end the employment.", "success")
            return redirect(f"/owner/payroll/{st.id}",
                            f"Final settlement created: pay {money(st.net_pay, st.currency)} to {employee.display_name} "
                            "and upload the proof. Termination unlocks once it's paid.", "success")
    ctx = await _settlement_context(request, db, user, employee, values=values, errors=errors)
    return render(request, "owner/payroll/settlement.html", ctx, status_code=400)


@router.post("/owner/staff/{employee_id:int}/terminate")
async def terminate(request: Request, employee_id: IdParam, db: DB, user: OwnerUser):
    """The old reason-less "Terminate" button. An employment now always ends through the trust-and-safety flow
    (/owner/staff/{id}/remove): a reason from the fixed list is recorded (violations bar re-hiring, severe ones ban),
    unused invite links and the members' trainer assignment are cleared, and a BENIGN reason still waits until payroll is
    settled. Nothing ends here - this only sends the owner to that page."""
    employee = await get_owned_employee(db, employee_id, user)
    if employee.status == EmploymentStatus.TERMINATED:
        return redirect(f"/owner/staff/{employee.id}/settlement", f"{employee.display_name} was already terminated.", "info")
    return redirect(f"/owner/staff/{employee.id}/remove",
                    "Choose why the employment ends - the reason is kept on the staff record.", "info")
