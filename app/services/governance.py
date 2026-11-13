"""Platform governance - the super-admin's decisions about owners, staff and gyms, each with failsafes for money.

Every action runs as: IMPACT PREVIEW (a dry run: nothing changes) -> a typed confirmation (the account's e-mail / the
gym's name) + a reason from a fixed list + a required note -> execute. Executions are idempotent (state re-read under
row / person locks: a double submit, a second tab or another admin finds the work done), double-submit protected (the
form's one-time submit id, app/core/submit_guard.py), rate-limited per admin (GOVERNANCE_ACTIONS_PER_HOUR), audited
(``governance.*`` rows carry the preview the admin confirmed; gym-scoped rows show the platform, never the admin, to the
gym's owner) and notify everyone affected.

Actions
* BAN AN OWNER (``ban_owner``): a platform ban (``trust_safety.platform_ban``: sign-in refused, sessions revoked,
  identity registered; an IP ban of recent addresses only for a SEVERE reason, with trust & safety's exclusions); every
  gym of the owner is SUSPENDED at once (``Gym.is_active`` false: hidden from public pages and search, no new memberships,
  shop orders or job enquiries; unpaid checkouts cancelled). Members keep access until their paid period ends OR get
  pro-rata refunds (the admin chooses); staff are told; earned pay is protected (statements keep being generated for a
  suspended gym - app/services/payroll.py ``payroll_gym_clause`` - and the gym's settlement desk pays them).
* BAN AN EMPLOYEE / TRAINER (``ban_employee``): a platform ban; for every current employment a FINAL SETTLEMENT
  (``payroll.create_final_settlement``) first, so earned pay is never lost, then the employment ends with the reason
  (``trust_safety.end_employment(platform=True)``: shifts cancelled, invite links invalidated); their member clients are
  reassigned to a trainer the admin picks per gym, or told and unlinked; gym owners are told.
* DELETE A GYM, two steps: ``schedule_gym_deletion`` suspends it at once and starts a cooling-off period
  (GYM_DELETE_COOLING_OFF_DAYS) during which ``restore_gym`` cancels it; ``finalize_gym_deletion`` is BLOCKED until every
  money obligation is settled (``deletion_blockers``): each employee's final settlement created AND paid (payout sent,
  or a payment proof recorded) - or the pay explicitly waived with a written note (``waive_employee_pay``); members'
  active memberships refunded pro rata (``queue_member_refunds`` -> the existing refund path: the payment gateway's
  refund API, TEST MODE simulated, cash recorded by hand) or honoured until they end; open shop orders cancelled and
  refunded (``queue_order_settlement``). Then the gym is ARCHIVED - soft: every row stays. Financial records
  (transactions, salary statements, invoices, refunds) are never deleted.
* LIFT / RESTORE: ``lift_ban`` (with a note; an owner's gyms suspended by the ban open again), ``restore_gym``.

Failsafes: the preview shows counts and money (members, refunds due, staff, earned pay outstanding, pending payouts,
open orders); money moves only on an explicit confirmation (a typed one for bans and deletions, a checkbox naming the
amount on the settlement desk; with Cashfree payouts off a settlement is "to pay" and settled by recording the proof);
a super-admin can never ban themselves or another super-admin (so never the last one); destructive actions are rate-
limited; every step is audited; nothing financial is ever deleted; bans update the blocked-identity registry
(``trust_safety.register_identity``).
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import Date, and_, case, exists, func, literal, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, joinedload

from app.config import get_settings
from app.core.utils import money, utcnow
from app.db import session_scope
from app.models import (
    AccountBan,
    AuthToken,
    Employee,
    GovernanceWaiver,
    Gym,
    JobPosting,
    OtpChallenge,
    Plan,
    SalaryStatement,
    ShopOrder,
    Subscription,
    TrainerClient,
    TrainerShift,
    Transaction,
    User,
)
from app.models.enums import (
    EmploymentStatus,
    NotificationCategory,
    Role,
    Severity,
    StatementStatus,
    SubscriptionStatus,
    TokenPurpose,
    TxCategory,
    TxDirection,
    TxStatus,
)
from app.models.shop import OrderStatus
from app.services import payroll as pr
from app.services import trust_safety as TS
from app.services.audit import audit
from app.services.jobs import enqueue, job_handler
from app.services.notify import Note, notify, notify_bulk

log = logging.getLogger("ironpulse.governance")

# ====================================================================== limits (approved load-safety caps)
NOTE_MIN, NOTE_MAX = 10, 500  # the reason's note of a ban / a deletion
LIFT_NOTE_MIN = TS.LIFT_NOTE_MIN  # lifting a ban, restoring a gym
WAIVE_NOTE_MIN = 15  # waiving an employee's pay must say why, specifically
EMPLOYMENTS_MAX = 50  # current employments one employee ban ends
OWNER_GYMS_MAX = 100  # gyms one owner ban suspends (an owner has a handful)
PREVIEW_STAFF_MAX = 2000  # employment records priced per preview (accrued pay)
DESK_STAFF_MAX = 300  # staff rows of the settlement desk
DESK_LIST_MAX = 100  # manual refunds / open orders listed on the desk
SETTLEMENTS_PER_CLICK = 50  # final settlements one click creates (click again for more)
JOB_BATCH = 100  # refunds / order settlements per job run (each its own transaction), then the job re-queues itself
JOB_BUDGET_S = 240.0
NOTIFY_MAX = 20_000  # members told about one action (notify_bulk inserts them 500 per statement)
REPLACEMENT_TRAINERS_MAX = 200  # trainers offered for client reassignment (all gyms of one employee ban)

CURRENT = (EmploymentStatus.INVITED, EmploymentStatus.PENDING_VERIFICATION, EmploymentStatus.ACTIVE,
           EmploymentStatus.SUSPENDED)
WORKING = (EmploymentStatus.ACTIVE, EmploymentStatus.SUSPENDED)
PENDING_PURCHASES = (SubscriptionStatus.PENDING_OTP, SubscriptionStatus.PENDING_PAYMENT)
OPEN_ORDERS = (OrderStatus.PENDING_PAYMENT, OrderStatus.PAID, OrderStatus.PROCESSING, OrderStatus.READY_FOR_PICKUP,
               OrderStatus.SHIPPED, OrderStatus.REFUND_REQUESTED)
PAID_OPEN_ORDERS = (OrderStatus.PAID, OrderStatus.PROCESSING, OrderStatus.READY_FOR_PICKUP, OrderStatus.SHIPPED,
                    OrderStatus.REFUND_REQUESTED)
ONLINE_PROVIDERS = ("cashfree", "test")  # payments the gateway (or TEST MODE) can refund
UNSETTLED_STATEMENTS = (StatementStatus.DRAFT, StatementStatus.DUE)
CENT = Decimal("0.01")
REFUND_JOB = "governance_refunds"
ORDERS_JOB = "governance_orders"
PLATFORM_BY = "platform"  # ``detail.by`` of gym-scoped audit rows: owners see "IronPulse platform", never the admin


class GovernanceError(ValueError):
    """A refused governance action; the message is safe to show to the super-admin."""


# ====================================================================== reasons
@dataclass(frozen=True, slots=True)
class Reason:
    code: str
    label: str
    severe: bool = False


def _reasons(*items: Reason) -> dict[str, Reason]:
    return {r.code: r for r in items}


OWNER_BAN_REASONS = _reasons(
    Reason("payment_fraud", "Payment fraud - taking members' money without the service", True),
    Reason("member_safety", "Endangering members' safety", True),
    Reason("harassment", "Harassment, threats or violence", True),
    Reason("illegal_activity", "Illegal activity", True),
    Reason("fake_documents", "Forged registration or identity documents", True),
    Reason("unpaid_staff", "Not paying staff"),
    Reason("repeated_complaints", "Repeated serious complaints"),
    Reason("terms_violation", "Breach of the IronPulse terms"),
    Reason("other", "Other (explained in the note)"),
)
# an employee ban ends their employments with one of trust & safety's own reasons (the record keeps it)
EMPLOYEE_BAN_REASONS = {code: Reason(code, r.label, r.category == TS.SEVERE)
                        for code, r in TS.REMOVAL_REASONS.items() if r.category in (TS.VIOLATION, TS.SEVERE)}
GYM_DELETE_REASONS = _reasons(
    Reason("owner_request", "The owner asked for the gym to be removed"),
    Reason("owner_banned", "The owner's account is banned"),
    Reason("closed", "The gym has closed down"),
    Reason("fraud", "Fraud or a fake listing", True),
    Reason("safety", "Unsafe premises or practices", True),
    Reason("duplicate", "Duplicate listing"),
    Reason("legal", "A legal or regulatory order"),
    Reason("other", "Other (explained in the note)"),
)
MEMBER_POLICIES = {
    "keep": "Members keep access until their paid period ends",
    "refund": "Refund the unused part of every membership, pro rata",
}
SETTLEMENT_REASON = "redundancy"  # payroll TERMINATION_REASONS: the gym closes - the position is eliminated


def clean_note(value: Any) -> str:
    return TS.clean_note(value)


def typed_matches(typed: Any, expected: str | None) -> bool:
    """The typed confirmation: the gym's name / the account's e-mail, whitespace and letter case aside."""
    a = " ".join(str(typed or "").split()).casefold()
    b = " ".join(str(expected or "").split()).casefold()
    return bool(a) and a == b


def validate(reasons: dict[str, Reason], reason: str, note: str, typed: str, expected: str | None, *,
             what: str) -> tuple[Reason | None, dict[str, str]]:
    """Field errors of a governance form (a reason from the list, a note of NOTE_MIN..NOTE_MAX characters, the typed
    confirmation) - empty = valid."""
    errors: dict[str, str] = {}
    r = reasons.get(reason)
    if r is None:
        errors["reason"] = "Choose a reason from the list."
    if not NOTE_MIN <= len(note) <= NOTE_MAX:
        errors["note"] = (f"Write what happened in {NOTE_MIN}-{NOTE_MAX} characters - it is kept in the audit log and "
                          "shown in the notices.")
    if not typed_matches(typed, expected):
        errors["confirm"] = f"Type {what} exactly as shown to confirm."
    return r, errors


# ====================================================================== money helpers
def q(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def add_money(target: dict[str, Decimal], currency: str | None, amount: Any) -> None:
    cur = (currency or "INR").upper()
    target[cur] = q(target.get(cur, Decimal("0")) + q(amount))


def merge_money(*parts: dict[str, Decimal]) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for part in parts:
        for cur, amount in part.items():
            add_money(out, cur, amount)
    return out


def money_text(totals: dict[str, Decimal] | None) -> str:
    """"₹12,500.00" / "₹1,000.00 + AED 50.00" (INR first) / "₹0.00"."""
    items = sorted(((c, a) for c, a in (totals or {}).items() if a), key=lambda ca: (ca[0] != "INR", ca[0]))
    return " + ".join(money(a, c) for c, a in items) if items else money(0, "INR")


def pro_rata(price: Any, starts_on: date | None, ends_on: date | None, as_of: date) -> Decimal:
    """The unused part of a paid period [starts_on, ends_on] as of ``as_of`` (that day counts as used): the whole price
    before the start, nothing after the end. The same arithmetic as ``prorata_sql`` (the preview's total)."""
    if starts_on is None or ends_on is None:
        return Decimal("0.00")
    if starts_on > as_of:
        return q(price)
    total = max((ends_on - starts_on).days + 1, 1)
    remaining = max((ends_on - as_of).days, 0)
    return q(Decimal(str(price)) * remaining / total)


def prorata_sql(as_of: date) -> Any:
    starts, ends, price = Subscription.starts_on, Subscription.ends_on, Subscription.price
    day = literal(as_of, Date)
    total = func.greatest(ends - starts + 1, 1)
    remaining = func.greatest(ends - day, 0)
    return case(
        (or_(starts.is_(None), ends.is_(None)), literal(Decimal("0"))),
        (starts > day, price),
        else_=func.round(price * remaining / total, 2),
    )


def _active_membership(as_of: date) -> Any:
    return and_(Subscription.status == SubscriptionStatus.ACTIVE,
                or_(Subscription.ends_on.is_(None), Subscription.ends_on >= as_of))


def _paid_online() -> Any:
    return exists().where(Transaction.subscription_id == Subscription.id, Transaction.direction == TxDirection.INCOMING,
                          Transaction.status == TxStatus.SUCCESS, Transaction.provider.in_(ONLINE_PROVIDERS))


# ====================================================================== gym state
def gym_state(gym: Gym) -> str:
    if gym.archived_at is not None:
        return "archived"
    if gym.deletion_requested_at is not None:
        return "deletion_scheduled"
    if not gym.is_active:
        return "suspended"
    return "active"


STATE_LABELS = {"active": "Active", "suspended": "Suspended", "deletion_scheduled": "Deletion scheduled",
                "archived": "Deleted (archived)"}
STATE_TONES = {"active": "green", "suspended": "amber", "deletion_scheduled": "red", "archived": "zinc"}


def cooling_off_over(gym: Gym, now: datetime | None = None) -> bool:
    return gym.delete_after is not None and gym.delete_after <= (now or utcnow())


# ====================================================================== impact previews
@dataclass(slots=True)
class GymImpact:
    """What an action on a gym (or a sum over gyms) touches - counts and money per currency."""

    gym: Gym | None = None
    members: int = 0  # distinct members with an active membership
    memberships: int = 0
    refunds_due: dict[str, Decimal] = field(default_factory=dict)  # pro rata, as of the preview's day
    online_refunds: int = 0  # memberships paid online (gateway / TEST MODE): refunded through the gateway
    manual_refunds: int = 0  # paid in cash at the desk: refunded by hand, recorded with a reference
    last_end: date | None = None  # the last active membership ends on this day ("honour until")
    pending_purchases: int = 0  # unpaid checkouts (cancelled by a suspension - no money moved)
    staff: int = 0  # current employment records (invited ... suspended)
    staff_working: int = 0  # active / suspended: earning pay
    clients: int = 0  # active trainer-client links
    unpaid_statements: int = 0
    unpaid_pay: dict[str, Decimal] = field(default_factory=dict)  # on statements not yet paid
    accrued_pay: dict[str, Decimal] = field(default_factory=dict)  # earned, not yet on a statement
    pending_payouts: int = 0  # statements whose payment is under way (payout sent / proof in review)
    pending_payout_pay: dict[str, Decimal] = field(default_factory=dict)
    open_orders: int = 0
    unpaid_orders: int = 0
    order_refunds: dict[str, Decimal] = field(default_factory=dict)  # paid, not delivered / refund asked: owed back
    open_jobs: int = 0
    staff_truncated: bool = False

    @property
    def earned_pay(self) -> dict[str, Decimal]:
        """Earned pay outstanding: unpaid statements + pay accrued since the last statement."""
        return merge_money(self.unpaid_pay, self.accrued_pay)

    def absorb(self, other: GymImpact) -> None:
        for name in ("members", "memberships", "online_refunds", "manual_refunds", "pending_purchases", "staff",
                     "staff_working", "clients", "unpaid_statements", "pending_payouts", "open_orders", "unpaid_orders",
                     "open_jobs"):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        for name in ("refunds_due", "unpaid_pay", "accrued_pay", "pending_payout_pay", "order_refunds"):
            setattr(self, name, merge_money(getattr(self, name), getattr(other, name)))
        if other.last_end is not None and (self.last_end is None or other.last_end > self.last_end):
            self.last_end = other.last_end
        self.staff_truncated = self.staff_truncated or other.staff_truncated

    def snapshot(self) -> dict[str, Any]:
        """The figures the admin confirmed (the audit row keeps them)."""
        return {
            "members": self.members, "memberships": self.memberships, "refunds_due": money_text(self.refunds_due),
            "online_refunds": self.online_refunds, "manual_refunds": self.manual_refunds,
            "last_end": self.last_end.isoformat() if self.last_end else None, "pending_purchases": self.pending_purchases,
            "staff": self.staff, "staff_working": self.staff_working, "clients": self.clients,
            "unpaid_statements": self.unpaid_statements, "earned_pay": money_text(self.earned_pay),
            "pending_payouts": self.pending_payouts, "open_orders": self.open_orders,
            "order_refunds": money_text(self.order_refunds),
        }


async def gyms_impact(db: AsyncSession, gyms: Sequence[Gym], *, as_of: date | None = None) -> dict[int, GymImpact]:
    """Per gym: members and refunds due, staff and earned pay, payouts under way, open orders - a constant number of
    set-based queries however many members, staff and orders the gyms have."""
    as_of = as_of or date.today()
    out = {g.id: GymImpact(gym=g) for g in gyms}
    ids = list(out)
    if not ids:
        return out
    active = _active_membership(as_of)
    pending = Subscription.status.in_(PENDING_PURCHASES)
    rows = (await db.execute(
        select(Subscription.gym_id, Subscription.currency,
               func.count().filter(active).label("memberships"),
               func.count(func.distinct(Subscription.customer_id)).filter(active).label("members"),
               func.coalesce(func.sum(prorata_sql(as_of)).filter(active), 0).label("refunds"),
               func.max(Subscription.ends_on).filter(active).label("last_end"),
               func.count().filter(and_(active, _paid_online())).label("online"),
               func.count().filter(pending).label("pending"))
        .where(Subscription.gym_id.in_(ids), or_(active, pending))
        .group_by(Subscription.gym_id, Subscription.currency)
    )).all()
    for r in rows:
        imp = out[r.gym_id]
        imp.memberships += int(r.memberships)
        imp.members += int(r.members)
        imp.online_refunds += int(r.online)
        imp.manual_refunds += int(r.memberships) - int(r.online)
        imp.pending_purchases += int(r.pending)
        if r.refunds:
            add_money(imp.refunds_due, r.currency, r.refunds)
        if r.last_end is not None and (imp.last_end is None or r.last_end > imp.last_end):
            imp.last_end = r.last_end
    emps = list((await db.execute(
        select(Employee).where(Employee.gym_id.in_(ids), Employee.deleted_at.is_(None), Employee.status.in_(CURRENT))
        .order_by(Employee.id).limit(PREVIEW_STAFF_MAX + 1)
    )).scalars().all())
    if len(emps) > PREVIEW_STAFF_MAX:
        emps = emps[:PREVIEW_STAFF_MAX]
        for imp in out.values():
            imp.staff_truncated = True
    for e in emps:
        imp = out[e.gym_id]
        imp.staff += 1
        imp.staff_working += e.status in WORKING
    accrued = await pr.accrued_previews(db, [e for e in emps if e.status in WORKING], as_of=as_of,
                                        gym_countries={g.id: g.country_code for g in gyms})
    by_id = {e.id: e for e in emps}
    for emp_id, acc in accrued.items():
        add_money(out[by_id[emp_id].gym_id].accrued_pay, acc.currency, acc.net)
    for r in (await db.execute(
        select(SalaryStatement.gym_id, SalaryStatement.currency, SalaryStatement.status, func.count().label("n"),
               func.coalesce(func.sum(SalaryStatement.net_pay), 0).label("total"))
        .where(SalaryStatement.gym_id.in_(ids), SalaryStatement.status.in_(pr.UNPAID_STATUSES))
        .group_by(SalaryStatement.gym_id, SalaryStatement.currency, SalaryStatement.status)
    )).all():
        imp = out[r.gym_id]
        imp.unpaid_statements += int(r.n)
        add_money(imp.unpaid_pay, r.currency, r.total)
        if r.status in (StatementStatus.AWAITING_PROOF, StatementStatus.PROOF_REVIEW):
            imp.pending_payouts += int(r.n)
            add_money(imp.pending_payout_pay, r.currency, r.total)
    paid_open = ShopOrder.status.in_(PAID_OPEN_ORDERS)
    for r in (await db.execute(
        select(ShopOrder.gym_id, ShopOrder.currency, func.count().label("n"),
               func.count().filter(ShopOrder.status == OrderStatus.PENDING_PAYMENT).label("unpaid"),
               func.coalesce(func.sum(ShopOrder.total - ShopOrder.refunded_total).filter(paid_open), 0).label("owed"))
        .where(ShopOrder.gym_id.in_(ids), ShopOrder.status.in_(OPEN_ORDERS))
        .group_by(ShopOrder.gym_id, ShopOrder.currency)
    )).all():
        imp = out[r.gym_id]
        imp.open_orders += int(r.n)
        imp.unpaid_orders += int(r.unpaid)
        if r.owed:
            add_money(imp.order_refunds, r.currency, r.owed)
    for gym_id, n in (await db.execute(
        select(Employee.gym_id, func.count(TrainerClient.id)).join(Employee, Employee.id == TrainerClient.employee_id)
        .where(Employee.gym_id.in_(ids), TrainerClient.active.is_(True)).group_by(Employee.gym_id)
    )).all():
        out[gym_id].clients = int(n)
    for gym_id, n in (await db.execute(
        select(JobPosting.gym_id, func.count()).where(JobPosting.gym_id.in_(ids), JobPosting.is_open.is_(True))
        .group_by(JobPosting.gym_id)
    )).all():
        out[gym_id].open_jobs = int(n)
    return out


def total_impact(impacts: Iterable[GymImpact]) -> GymImpact:
    total = GymImpact()
    for imp in impacts:
        total.absorb(imp)
    return total


async def owner_gyms(db: AsyncSession, owner_id: int, *, include_archived: bool = False) -> list[Gym]:
    stmt = select(Gym).where(Gym.owner_id == owner_id)
    if not include_archived:
        stmt = stmt.where(Gym.archived_at.is_(None))
    return list((await db.execute(stmt.order_by(Gym.name, Gym.id).limit(OWNER_GYMS_MAX))).scalars().all())


@dataclass(slots=True)
class EmploymentImpact:
    employee: Employee
    unpaid_statements: int = 0
    unpaid_pay: dict[str, Decimal] = field(default_factory=dict)
    accrued: Any = None  # payroll.AccruedPreview | None
    has_settlement: bool = False
    clients: int = 0
    shifts: int = 0
    trainers: list[tuple[int, str]] = field(default_factory=list)  # (employee id, name) of replacement trainers

    @property
    def earned_pay(self) -> dict[str, Decimal]:
        out = dict(self.unpaid_pay)
        if self.accrued is not None:
            out = merge_money(out, {self.accrued.currency: self.accrued.net})
        return out

    @property
    def gets_settlement(self) -> bool:
        """A final settlement is created for this employment (it was earning pay and has none yet)."""
        return self.employee.status in WORKING and not self.has_settlement


async def employee_impact(db: AsyncSession, user: User, *, as_of: date | None = None) -> list[EmploymentImpact]:
    """The current employments an employee ban ends, with their earned pay, clients, shifts and the trainers their
    clients may be reassigned to - a constant number of queries."""
    as_of = as_of or date.today()
    emps = list((await db.execute(
        select(Employee).options(joinedload(Employee.gym))
        .where(Employee.user_id == user.id, Employee.deleted_at.is_(None), Employee.status.in_(CURRENT))
        .order_by(Employee.id).limit(EMPLOYMENTS_MAX)
    )).scalars().all())
    if not emps:
        return []
    out = {e.id: EmploymentImpact(e) for e in emps}
    ids = list(out)
    for emp_id, currency, n, total in (await db.execute(
        select(SalaryStatement.employee_id, SalaryStatement.currency, func.count(),
               func.coalesce(func.sum(SalaryStatement.net_pay), 0))
        .where(SalaryStatement.employee_id.in_(ids), SalaryStatement.status.in_(pr.UNPAID_STATUSES))
        .group_by(SalaryStatement.employee_id, SalaryStatement.currency)
    )).all():
        out[emp_id].unpaid_statements += int(n)
        add_money(out[emp_id].unpaid_pay, currency, total)
    accrued = await pr.accrued_previews(db, [e for e in emps if e.status in WORKING], as_of=as_of,
                                        gym_countries={e.gym_id: e.gym.country_code for e in emps})
    for emp_id, acc in accrued.items():
        out[emp_id].accrued = acc
    for emp_id in (await db.execute(
        select(SalaryStatement.employee_id).where(SalaryStatement.employee_id.in_(ids),
                                                  SalaryStatement.is_final_settlement.is_(True),
                                                  SalaryStatement.status != StatementStatus.VOID).distinct()
    )).scalars().all():
        out[emp_id].has_settlement = True
    for emp_id, n in (await db.execute(
        select(TrainerClient.employee_id, func.count()).where(TrainerClient.employee_id.in_(ids),
                                                             TrainerClient.active.is_(True))
        .group_by(TrainerClient.employee_id)
    )).all():
        out[emp_id].clients = int(n)
    for emp_id, n in (await db.execute(
        select(TrainerShift.employee_id, func.count()).where(TrainerShift.employee_id.in_(ids))
        .group_by(TrainerShift.employee_id)
    )).all():
        out[emp_id].shifts = int(n)
    gym_ids = sorted({e.gym_id for e in emps})
    trainers: dict[int, list[tuple[int, str]]] = {}
    for emp_id, gym_id, name in (await db.execute(
        select(Employee.id, Employee.gym_id, Employee.full_name).where(
            Employee.gym_id.in_(gym_ids), Employee.status == EmploymentStatus.ACTIVE, Employee.is_trainer.is_(True),
            Employee.deleted_at.is_(None), Employee.id.not_in(ids),
            or_(Employee.user_id.is_(None), Employee.user_id != user.id))
        .order_by(Employee.gym_id, Employee.full_name, Employee.id).limit(REPLACEMENT_TRAINERS_MAX)
    )).all():
        trainers.setdefault(gym_id, []).append((emp_id, name))
    for imp in out.values():
        imp.trainers = trainers.get(imp.employee.gym_id, [])
    return list(out.values())


# ====================================================================== guards
async def ban_refusal(db: AsyncSession, admin: User, target: User) -> str | None:
    """Why ``target`` can't be banned here - None when it can. A super-admin never bans themselves or another
    super-admin (so never the last one); deleted accounts are gone already."""
    if target.id == admin.id:
        return "You can't ban your own account."
    if target.role == Role.SUPERADMIN:
        admins = int(await db.scalar(select(func.count()).select_from(
            select(User.id).where(User.role == Role.SUPERADMIN, User.deleted_at.is_(None), User.is_active.is_(True))
            .limit(2).subquery())) or 0)
        if admins <= 1:
            return "This is the last platform admin account - it can never be banned."
        return "Platform admin accounts can't be banned here."
    if target.deleted_at is not None:
        return "This account is deleted."
    if target.role not in (Role.OWNER, Role.STAFF):
        return "Governance bans apply to gym owners and staff accounts."
    return None


async def rate_refusal(admin: User) -> str | None:
    """GOVERNANCE_ACTIONS_PER_HOUR destructive actions per super-admin (shared by every worker through Redis)."""
    from app.security.ratelimit import limiter

    limit = int(get_settings().governance_actions_per_hour)
    if await limiter.note(f"governance:{admin.id}", "governance", window=3600.0) > limit:
        return (f"You ran {limit} governance actions within the last hour - the limit for destructive actions. Wait a "
                "little and try again.")
    return None


# ====================================================================== shared steps
async def _cancel_pending_purchases(db: AsyncSession, gym_ids: list[int]) -> tuple[int, int]:
    """No new memberships or orders: unpaid checkouts of these gyms end (nothing was paid, nothing to refund)."""
    if not gym_ids:
        return 0, 0
    now = utcnow()
    subs = list((await db.execute(
        update(Subscription).where(Subscription.gym_id.in_(gym_ids), Subscription.status.in_(PENDING_PURCHASES))
        .values(status=SubscriptionStatus.CANCELLED, cancelled_at=now).returning(Subscription.id)
        .execution_options(synchronize_session=False)
    )).scalars().all())
    if subs:
        await db.execute(update(Transaction).where(Transaction.subscription_id.in_(subs),
                                                   Transaction.status.in_((TxStatus.CREATED, TxStatus.PENDING)))
                         .values(status=TxStatus.CANCELLED).execution_options(synchronize_session=False))
        await db.execute(update(OtpChallenge).where(OtpChallenge.subscription_id.in_(subs),
                                                    OtpChallenge.consumed_at.is_(None))
                         .values(expires_at=now).execution_options(synchronize_session=False))
    orders = (await db.execute(
        update(ShopOrder).where(ShopOrder.gym_id.in_(gym_ids), ShopOrder.status == OrderStatus.PENDING_PAYMENT)
        .values(status=OrderStatus.CANCELLED, cancelled_at=now, stock_held_until=None)
        .execution_options(synchronize_session=False)
    )).rowcount or 0
    return len(subs), int(orders)


def _suspend(gym: Gym, *, kind: str, reason: str, now: datetime) -> bool:
    """Suspend (hide) ``gym``; False when it already was. A deletion keeps the suspension's original time."""
    was_active = gym.is_active
    gym.is_active = False
    gym.suspended_at = gym.suspended_at or now
    if gym.suspension_kind != "deletion" or kind == "deletion":
        gym.suspension_kind = kind
    gym.suspension_reason = reason[:300]
    return was_active


async def _member_ids(db: AsyncSession, gym_ids: list[int], *, as_of: date) -> list[int]:
    if not gym_ids:
        return []
    return list((await db.execute(
        select(Subscription.customer_id).where(Subscription.gym_id.in_(gym_ids), _active_membership(as_of))
        .distinct().limit(NOTIFY_MAX)
    )).scalars().all())


async def _staff_user_ids(db: AsyncSession, gym_ids: list[int], *, exclude: int | None = None) -> list[int]:
    if not gym_ids:
        return []
    stmt = select(Employee.user_id).where(Employee.gym_id.in_(gym_ids), Employee.deleted_at.is_(None),
                                          Employee.status.in_(CURRENT), Employee.user_id.is_not(None))
    if exclude is not None:
        stmt = stmt.where(Employee.user_id != exclude)
    return list((await db.execute(stmt.distinct().limit(NOTIFY_MAX))).scalars().all())


def _names(gyms: Sequence[Gym], limit: int = 3) -> str:
    names = [g.name for g in gyms[:limit]]
    extra = len(gyms) - len(names)
    return ", ".join(names) + (f" and {extra} more" if extra > 0 else "")


async def _audit_gyms(db: AsyncSession, admin: User, action: str, gyms: Sequence[Gym], detail: dict[str, Any],
                      request: Any) -> None:
    """One audit row per gym (ONE batched INSERT at the next flush), attributed to the platform in the gym's own log
    (``PLATFORM_BY``)."""
    from app.models import AuditLog
    from app.security.net import client_ip

    ip = client_ip(request) if request is not None else None
    db.add_all([AuditLog(actor_id=admin.id, gym_id=g.id, action=action[:80], entity_type="gym", entity_id=g.id,
                         detail={**detail, "by": PLATFORM_BY, "gym": g.name}, ip=ip) for g in gyms])


# ====================================================================== ban an owner
@dataclass(slots=True)
class OwnerBanOutcome:
    banned_now: bool
    gyms_suspended: int = 0
    refunds_queued: int = 0
    impact: GymImpact | None = None


async def ban_owner(db: AsyncSession, admin: User, owner: User, *, reason: Reason, note: str, member_policy: str,
                    request: Any = None) -> OwnerBanOutcome:
    """Ban a gym owner platform-wide and suspend every gym of theirs (see the module docstring). The caller validated
    the form and checked ``ban_refusal`` / ``rate_refusal``; it commits."""
    if member_policy not in MEMBER_POLICIES:
        raise GovernanceError("Choose what happens to the members' memberships.")
    await TS.lock_person(db, TS.Person(owner))
    current = (await db.execute(select(User.role, User.deleted_at).where(User.id == owner.id).with_for_update())).first()
    if current is None or current.deleted_at is not None or current.role != Role.OWNER:
        raise GovernanceError("This account is not a gym owner's any more.")
    if await TS.active_ban(db, owner.id) is not None:
        return OwnerBanOutcome(banned_now=False)
    now = utcnow()
    today = date.today()
    gyms = await owner_gyms(db, owner.id)
    gym_ids = [g.id for g in gyms]
    if gym_ids:  # the gym rows, locked in id order (the lock order of every gym-wide governance step)
        await db.execute(select(Gym.id).where(Gym.id.in_(gym_ids)).order_by(Gym.id).with_for_update())
    impacts = await gyms_impact(db, gyms, as_of=today)
    total = total_impact(impacts.values())
    summary = f"{reason.label}: {note}"
    ban, created = await TS.platform_ban(db, owner, reason=summary, severe=reason.severe, actor=admin, request=request)
    members = await _member_ids(db, gym_ids, as_of=today)
    staff = await _staff_user_ids(db, gym_ids, exclude=owner.id)
    suspended = sum(_suspend(g, kind="owner_ban", reason=f"Owner banned: {reason.label}", now=now) for g in gyms)
    cancelled_subs, cancelled_orders = await _cancel_pending_purchases(db, gym_ids)
    queued = 0
    if member_policy == "refund":
        for g in gyms:  # bounded: OWNER_GYMS_MAX - one deduplicated job per gym
            if impacts[g.id].memberships:
                await queue_member_refunds(db, admin, g, as_of=today, request=request, audit_it=False)
                queued += impacts[g.id].memberships
    detail = {"reason": reason.code, "label": reason.label, "note": note, "severe": reason.severe,
              "member_policy": member_policy, "ban_id": ban.id, "gyms": [g.id for g in gyms],
              "cancelled_checkouts": cancelled_subs, "cancelled_orders": cancelled_orders, "impact": total.snapshot()}
    await audit(db, admin, "governance.owner_ban", "user", owner.id, request=request, detail=detail)
    await _audit_gyms(db, admin, "governance.gym_suspended", gyms, {"reason": "owner_ban", "label": reason.label}, request)
    notes: list[Note] = []
    names = _names(gyms)
    if gyms:
        for uid in staff:
            notes.append(Note(uid, "Your gym has been suspended",
                              f"{names} {'is' if len(gyms) == 1 else 'are'} suspended by the IronPulse platform. Your "
                              "employment record and the pay you have earned are kept and will be settled - you will "
                              "be told when it is paid. You can't work shifts there while the gym is suspended.",
                              link="/staff", category=NotificationCategory.STAFF, severity=Severity.WARNING))
        for uid in members:
            if member_policy == "refund":
                body = (f"{names} {'has' if len(gyms) == 1 else 'have'} been suspended by the IronPulse platform. The "
                        "unused part of your membership is being refunded to your original payment method (a cash "
                        "payment is refunded by hand - the platform team will contact you).")
            else:
                body = (f"{names} {'has' if len(gyms) == 1 else 'have'} been suspended by the IronPulse platform. Your "
                        "membership stays valid until the end of the period you paid for; the gym can't sell new "
                        "memberships or shop orders meanwhile.")
            notes.append(Note(uid, "Your gym has been suspended", body, link="/me/subscriptions",
                              category=NotificationCategory.PAYMENT, severity=Severity.WARNING))
    if notes:
        await db.flush()
        await notify_bulk(db, notes)
    await TS.notify_superadmins(db, f"Gym owner banned: {owner.full_name}",
                                f"{admin.full_name} banned owner account #{owner.id} ({reason.label}); "
                                f"{len(gyms)} gym{'s' if len(gyms) != 1 else ''} suspended. Settle staff pay and "
                                "members on each gym's governance page.",
                                link=f"/superadmin/governance/users/{owner.id}", severity=Severity.WARNING)
    from app.core.nav import refresh_nav_state

    await refresh_nav_state(db)  # their job postings left the jobs board
    return OwnerBanOutcome(banned_now=created, gyms_suspended=suspended, refunds_queued=queued, impact=total)


# ====================================================================== ban an employee
@dataclass(slots=True)
class EmployeeBanOutcome:
    banned_now: bool
    ended: int = 0
    settlements: list[int] = field(default_factory=list)  # final settlement statement ids
    settlement_errors: list[str] = field(default_factory=list)
    clients_reassigned: int = 0
    clients_unlinked: int = 0


async def _reassign_clients(db: AsyncSession, emp: Employee, new_trainer_id: int) -> tuple[int, list[int]]:
    """Move ``emp``'s active client links (and the memberships' trainer) to the trainer the admin picked - a link the
    new trainer already has for that membership is closed instead. Returns (moved, the members told)."""
    links = list((await db.execute(
        select(TrainerClient).where(TrainerClient.employee_id == emp.id, TrainerClient.active.is_(True))
        .with_for_update()
    )).scalars().all())
    if not links:
        return 0, []
    taken = {(c, s) for c, s in (await db.execute(
        select(TrainerClient.customer_id, TrainerClient.subscription_id).where(TrainerClient.employee_id == new_trainer_id)
    )).all()}
    moved: list[int] = []
    for link in links:  # rows already loaded: attribute changes only, written by the next flush
        if (link.customer_id, link.subscription_id) in taken:
            link.active = False
            continue
        link.employee_id = new_trainer_id
        moved.append(link.customer_id)
    await db.execute(update(Subscription).where(Subscription.trainer_id == emp.id, Subscription.id.in_(
        [lk.subscription_id for lk in links if lk.subscription_id and lk.customer_id in moved] or [-1]))
        .values(trainer_id=new_trainer_id).execution_options(synchronize_session=False))
    await db.flush()
    return len(moved), sorted(set(moved))


async def ban_employee(db: AsyncSession, admin: User, person: User, *, reason: Reason, note: str,
                       reassign: dict[int, int] | None = None, request: Any = None) -> EmployeeBanOutcome:
    """Ban an employee / trainer platform-wide: final settlements first, then every current employment ends with the
    reason (see the module docstring). ``reassign``: employment id -> the trainer (employee id, same gym) their clients
    move to. The caller validated the form and checked ``ban_refusal`` / ``rate_refusal``; it commits."""
    tsp = TS.Person(person, {h for h in (TS.email_hash(person.email),) if h})
    await TS.lock_person(db, tsp)
    current = (await db.execute(select(User.role, User.deleted_at).where(User.id == person.id).with_for_update())).first()
    if current is None or current.deleted_at is not None or current.role != Role.STAFF:
        raise GovernanceError("This account is not a staff account any more.")
    if await TS.active_ban(db, person.id) is not None:
        return EmployeeBanOutcome(banned_now=False)
    today = date.today()
    emps = list((await db.execute(
        select(Employee).options(joinedload(Employee.gym))
        .where(Employee.user_id == person.id, Employee.deleted_at.is_(None), Employee.status.in_(CURRENT))
        .order_by(Employee.id).limit(EMPLOYMENTS_MAX).with_for_update(of=Employee)
    )).unique().scalars().all())
    ids = {e.id for e in emps}
    with_settlement = set((await db.execute(
        select(SalaryStatement.employee_id).where(SalaryStatement.employee_id.in_(ids or {-1}),
                                                  SalaryStatement.is_final_settlement.is_(True),
                                                  SalaryStatement.status != StatementStatus.VOID).distinct()
    )).scalars().all())
    valid_trainers: dict[int, tuple[int, str]] = {}  # trainer employee id -> (gym id, name)
    wanted = {t for t in (reassign or {}).values() if t}
    if wanted:
        valid_trainers = {tid: (gid, name) for tid, gid, name in (await db.execute(
            select(Employee.id, Employee.gym_id, Employee.full_name).where(
                Employee.id.in_(wanted), Employee.status == EmploymentStatus.ACTIVE, Employee.is_trainer.is_(True),
                Employee.deleted_at.is_(None), Employee.id.not_in(ids or {-1}),
                or_(Employee.user_id.is_(None), Employee.user_id != person.id))
        )).all()}
    summary = f"{reason.label}: {note}"
    ban, created = await TS.platform_ban(db, person, reason=summary, severe=reason.severe, actor=admin,
                                         request=request, suspend_employments=False)
    out = EmployeeBanOutcome(banned_now=created)
    owner_notes: list[Note] = []
    client_notes: list[Note] = []
    for emp in emps:  # bounded: EMPLOYMENTS_MAX (a person rarely has more than a few)
        gym = emp.gym
        settlement = None
        if emp.status in WORKING and emp.id not in with_settlement:
            try:
                async with db.begin_nested():
                    settlement = await pr.create_final_settlement(
                        db, emp, today, "misconduct" if reason.severe else "dismissal",
                        note=f"Platform ban by the IronPulse team: {reason.label}"[:200], actor=admin, gym=gym,
                        request=request, today=today)
                    settlement.rules = {**(settlement.rules or {}), "governance": {"kind": "employee_ban",
                                                                                 "ban_id": ban.id}}
                out.settlements.append(settlement.id)
            except pr.PayrollError as exc:  # earned pay stays on its statements; the desk shows it
                out.settlement_errors.append(f"{gym.name}: {exc}")
        target = (reassign or {}).get(emp.id)
        if target and valid_trainers.get(target, (None, ""))[0] == emp.gym_id:
            moved, customers = await _reassign_clients(db, emp, target)
            out.clients_reassigned += moved
            new_name = valid_trainers[target][1]
            client_notes += [Note(cid, "Your trainer has changed",
                                  f"{emp.full_name} no longer trains at {gym.name}. Your new trainer is {new_name} - "
                                  "your plan and history are unchanged.", link="/chat",
                                  category=NotificationCategory.STAFF) for cid in customers]
        outcome = await TS.end_employment(db, emp, reason=reason.code, note=note, actor=admin, gym=gym,
                                          person=tsp, request=request, termination_date=today, notify_person=False,
                                          platform=True)
        out.clients_unlinked += outcome.clients_ended
        out.ended += 1
        pay = ""
        if settlement is not None:
            pay = (f" Their final settlement of {money(settlement.net_pay, settlement.currency)} is under Payroll - "
                   "pay it there so their earned pay is settled.")
        owner_notes.append(Note(gym.owner_id, f"{emp.full_name}'s employment was ended by the platform",
                                f"The IronPulse platform team banned {emp.full_name}'s account, so their employment at "
                                f"{gym.name} has ended and their shifts are cancelled.{pay}",
                                link=f"/owner/staff/{emp.id}", category=NotificationCategory.STAFF,
                                severity=Severity.WARNING))
    if owner_notes or client_notes:
        await db.flush()
        await notify_bulk(db, owner_notes + client_notes)
    await audit(db, admin, "governance.employee_ban", "user", person.id, request=request, detail={
        "reason": reason.code, "label": reason.label, "note": note, "severe": reason.severe, "ban_id": ban.id,
        "employments": sorted(ids), "settlements": out.settlements, "settlement_errors": out.settlement_errors,
        "clients_reassigned": out.clients_reassigned, "clients_unlinked": out.clients_unlinked})
    await TS.notify_superadmins(db, f"Employee banned: {person.full_name}",
                                f"{admin.full_name} banned staff account #{person.id} ({reason.label}); {out.ended} "
                                f"employment{'s' if out.ended != 1 else ''} ended, {len(out.settlements)} final "
                                "settlement(s) created.", link=f"/superadmin/governance/users/{person.id}",
                                severity=Severity.WARNING)
    return out


# ====================================================================== lift a ban
async def lift_ban(db: AsyncSession, admin: User, user: User, *, note: str, request: Any = None) -> tuple[bool, int]:
    """Lift the account's active ban (trust & safety's lift: sign-in allowed, its IP bans lifted, the person told) and,
    for an owner, open again every gym the ban suspended (not one scheduled for deletion). Returns (lifted, gyms
    reopened); (False, 0) when nothing was banned (idempotent). The caller commits."""
    await TS.lock_person(db, TS.Person(user))
    ban = await db.scalar(select(AccountBan).where(AccountBan.user_id == user.id, AccountBan.lifted_at.is_(None))
                          .order_by(AccountBan.id.desc()).limit(1).with_for_update())
    if ban is None:
        return False, 0
    lifted_ips = await TS.lift_account_ban(db, ban, actor=admin, note=note, request=request)
    reopened = await restore_owner_gyms(db, admin, user.id, request=request) if user.role == Role.OWNER else 0
    await audit(db, admin, "governance.ban_lifted", "user", user.id, request=request, detail={
        "ban_id": ban.id, "note": note, "ip_bans_lifted": lifted_ips, "gyms_reopened": reopened})
    return True, reopened


async def restore_owner_gyms(db: AsyncSession, admin: User, owner_id: int, *, request: Any = None) -> int:
    """Open again the owner's gyms an owner ban suspended - never one scheduled for deletion or archived. Staff and
    members are told. Returns how many. The caller commits."""
    gyms = list((await db.execute(
        select(Gym).where(Gym.owner_id == owner_id, Gym.suspension_kind == "owner_ban", Gym.archived_at.is_(None),
                          Gym.deletion_requested_at.is_(None))
        .order_by(Gym.id).limit(OWNER_GYMS_MAX).with_for_update()
    )).scalars().all())
    if not gyms:
        return 0
    for g in gyms:
        g.is_active, g.suspended_at, g.suspension_kind, g.suspension_reason = True, None, None, None
    ids = [g.id for g in gyms]
    staff = await _staff_user_ids(db, ids, exclude=owner_id)
    members = await _member_ids(db, ids, as_of=date.today())
    names = _names(gyms)
    notes = [Note(uid, "Your gym is open again", f"{names} {'is' if len(gyms) == 1 else 'are'} no longer suspended by "
                  "the IronPulse platform.", link="/staff", category=NotificationCategory.STAFF,
                  severity=Severity.SUCCESS) for uid in staff]
    notes += [Note(uid, "Your gym is open again", f"{names} {'is' if len(gyms) == 1 else 'are'} no longer suspended by "
                   "the IronPulse platform.", link="/me/subscriptions", category=NotificationCategory.PAYMENT,
                   severity=Severity.SUCCESS) for uid in members]
    if notes:
        await db.flush()
        await notify_bulk(db, notes)
    await _audit_gyms(db, admin, "governance.gym_reopened", gyms, {"reason": "ban_lifted"}, request)
    from app.core.nav import refresh_nav_state

    await refresh_nav_state(db)
    return len(gyms)


# ====================================================================== delete a gym: step 1 (schedule)
async def lock_gym(db: AsyncSession, gym_id: int) -> Gym | None:
    return await db.scalar(select(Gym).where(Gym.id == gym_id).with_for_update().execution_options(populate_existing=True))


async def schedule_gym_deletion(db: AsyncSession, admin: User, gym: Gym, *, reason: Reason, note: str,
                                request: Any = None) -> bool:
    """Suspend ``gym`` at once and start the cooling-off period (GYM_DELETE_COOLING_OFF_DAYS). False when it was
    scheduled (or archived) already - idempotent. The caller locked the gym (``lock_gym``) and commits."""
    if gym.archived_at is not None or gym.deletion_requested_at is not None:
        return False
    now = utcnow()
    today = date.today()
    impact = (await gyms_impact(db, [gym], as_of=today))[gym.id]
    days = int(get_settings().gym_delete_cooling_off_days)
    _suspend(gym, kind="deletion", reason=f"Deletion scheduled: {reason.label}", now=now)
    gym.deletion_requested_at = now
    gym.delete_after = now + timedelta(days=days)
    cancelled_subs, cancelled_orders = await _cancel_pending_purchases(db, [gym.id])
    members = await _member_ids(db, [gym.id], as_of=today)
    staff = await _staff_user_ids(db, [gym.id], exclude=gym.owner_id)
    when = f"{gym.delete_after:%d %b %Y}"
    notes = [Note(gym.owner_id, f"{gym.name} is scheduled for deletion",
                  f"The IronPulse platform suspended {gym.name} and scheduled it for deletion ({reason.label}). It is "
                  f"hidden from members and the public now; it can be restored until {when}. Staff pay and members' "
                  "memberships are settled before the gym is deleted. Financial records are kept.",
                  link="/owner", category=NotificationCategory.SYSTEM, severity=Severity.DANGER)]
    notes += [Note(uid, f"{gym.name} is closing", f"{gym.name} is suspended and scheduled to close on IronPulse. Your "
                   "employment record and the pay you have earned are kept: a final settlement is paid before the gym "
                   "is deleted.", link="/staff", category=NotificationCategory.STAFF, severity=Severity.WARNING)
              for uid in staff]
    notes += [Note(uid, f"{gym.name} is closing", f"{gym.name} is suspended and scheduled to close on IronPulse. Your "
                   "membership stays valid for now; before the gym is deleted the unused part is refunded to your "
                   "original payment method, or you keep access until it ends.", link="/me/subscriptions",
                   category=NotificationCategory.PAYMENT, severity=Severity.WARNING) for uid in members]
    await db.flush()
    await notify_bulk(db, notes)
    detail = {"reason": reason.code, "label": reason.label, "note": note, "delete_after": gym.delete_after.isoformat(),
              "cooling_off_days": days, "cancelled_checkouts": cancelled_subs, "cancelled_orders": cancelled_orders,
              "impact": impact.snapshot(), "by": PLATFORM_BY}
    await audit(db, admin, "governance.gym_delete_scheduled", "gym", gym.id, gym_id=gym.id, request=request,
                detail=detail)
    await TS.notify_superadmins(db, f"Gym scheduled for deletion: {gym.name}",
                                f"{admin.full_name} scheduled {gym.name} (#{gym.id}) for deletion ({reason.label}). "
                                f"Cooling-off until {when}.", link=f"/superadmin/governance/gyms/{gym.id}",
                                severity=Severity.WARNING)
    from app.core.nav import refresh_nav_state

    await refresh_nav_state(db)
    return True


# ====================================================================== restore (cancel the deletion)
async def restore_gym(db: AsyncSession, admin: User, gym: Gym, *, note: str, request: Any = None) -> str | None:
    """Cancel a scheduled deletion: the gym opens again - or stays suspended while its owner is banned. Unpaid final
    settlements the deletion created are voided (a paid one ended that employment: kept). Returns the new state, or
    None when no deletion was scheduled (idempotent). The caller locked the gym and commits."""
    if gym.archived_at is not None or gym.deletion_requested_at is None:
        return None
    owner_banned = await TS.active_ban(db, gym.owner_id) is not None
    gym.deletion_requested_at = None
    gym.delete_after = None
    if owner_banned:
        gym.suspension_kind, gym.suspension_reason = "owner_ban", "Owner banned"
        state = "suspended"
    else:
        gym.is_active, gym.suspended_at, gym.suspension_kind, gym.suspension_reason = True, None, None, None
        state = "active"
    voided = list((await db.execute(
        update(SalaryStatement).where(
            SalaryStatement.gym_id == gym.id, SalaryStatement.is_final_settlement.is_(True),
            SalaryStatement.status == StatementStatus.DUE, SalaryStatement.transaction_id.is_(None),
            SalaryStatement.rules["governance"]["kind"].astext == "gym_delete")
        .values(status=StatementStatus.VOID).returning(SalaryStatement.id).execution_options(synchronize_session=False)
    )).scalars().all())
    members = await _member_ids(db, [gym.id], as_of=date.today())
    staff = await _staff_user_ids(db, [gym.id], exclude=gym.owner_id)
    text_ = (f"{gym.name} is no longer scheduled for deletion" + (" and is open again." if state == "active" else
                                                                  " (it stays suspended for now)."))
    notes = [Note(gym.owner_id, f"{gym.name} was restored", text_, link="/owner", category=NotificationCategory.SYSTEM,
                  severity=Severity.SUCCESS)]
    notes += [Note(uid, f"{gym.name} was restored", text_, link="/staff", category=NotificationCategory.STAFF,
                   severity=Severity.SUCCESS) for uid in staff]
    notes += [Note(uid, f"{gym.name} was restored", text_, link="/me/subscriptions",
                   category=NotificationCategory.PAYMENT, severity=Severity.SUCCESS) for uid in members]
    await db.flush()
    await notify_bulk(db, notes)
    await audit(db, admin, "governance.gym_restored", "gym", gym.id, gym_id=gym.id, request=request, detail={
        "note": note, "state": state, "voided_settlements": voided, "by": PLATFORM_BY})
    from app.core.nav import refresh_nav_state

    await refresh_nav_state(db)
    return state


# ====================================================================== the settlement desk
def statement_settled(st: SalaryStatement, tx: Transaction | None) -> bool:
    """Paid for the final deletion's purpose: PAID; a proof in review; or awaiting proof after a payout the gateway
    accepted (or with a proof recorded)."""
    if st.status in (StatementStatus.PAID, StatementStatus.VOID, StatementStatus.PROOF_REVIEW):
        return True
    if st.status == StatementStatus.AWAITING_PROOF and tx is not None:
        return bool(tx.proof_key) or tx.provider in ONLINE_PROVIDERS
    return False


def _settled_tx_clause() -> Any:
    tx = aliased(Transaction)
    return exists().where(tx.id == SalaryStatement.transaction_id,
                          or_(tx.proof_key.is_not(None), tx.provider.in_(ONLINE_PROVIDERS)))


def _unsettled_clause() -> Any:
    return or_(SalaryStatement.status.in_(UNSETTLED_STATEMENTS),
               and_(SalaryStatement.status == StatementStatus.AWAITING_PROOF, ~_settled_tx_clause()))


@dataclass(slots=True)
class StaffRow:
    employee: Employee
    statements: list[SalaryStatement] = field(default_factory=list)  # unpaid ones + the final settlement
    transactions: dict[int, Transaction] = field(default_factory=dict)
    accrued: Any = None
    waiver: GovernanceWaiver | None = None

    @property
    def settlement(self) -> SalaryStatement | None:
        return next((s for s in self.statements if s.is_final_settlement and s.status != StatementStatus.VOID), None)

    @property
    def open_statements(self) -> list[SalaryStatement]:
        return [s for s in self.statements if not statement_settled(s, self.transactions.get(s.transaction_id or 0))]

    @property
    def needs_settlement(self) -> bool:
        return self.employee.deleted_at is None and self.employee.status in WORKING and self.settlement is None

    @property
    def cleared(self) -> bool:
        return self.waiver is not None or (not self.open_statements and not self.needs_settlement)

    @property
    def owed(self) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for s in self.open_statements:
            add_money(out, s.currency, s.net_pay)
        if self.accrued is not None and self.settlement is None:
            add_money(out, self.accrued.currency, self.accrued.net)
        return out

    def payout_mode(self, st: SalaryStatement) -> tuple[str | None, str]:
        return pr.payout_mode(st, self.employee)


@dataclass(slots=True)
class Desk:
    gym: Gym
    staff: list[StaffRow] = field(default_factory=list)
    staff_more: bool = False
    manual_refunds: list[tuple[Subscription, str, Decimal]] = field(default_factory=list)  # (sub, member, amount)
    orders: list[ShopOrder] = field(default_factory=list)
    refund_job: Any = None  # the queued / running refund job, if any
    orders_job: Any = None


async def desk(db: AsyncSession, gym: Gym, *, as_of: date | None = None) -> Desk:
    """The settlement desk of a suspended gym: every employee with pay to settle (current staff, and anyone with an
    unpaid statement), members paid in cash (refunded by hand), open shop orders - a constant number of queries."""
    from app.models import Job
    from app.models.enums import JobStatus

    as_of = as_of or date.today()
    out = Desk(gym)
    has_unpaid = exists().where(SalaryStatement.employee_id == Employee.id,
                                SalaryStatement.status.in_(pr.UNPAID_STATUSES))
    emps = list((await db.execute(
        select(Employee).where(Employee.gym_id == gym.id,
                               or_(and_(Employee.deleted_at.is_(None), Employee.status.in_(WORKING)), has_unpaid))
        .order_by(Employee.full_name, Employee.id).limit(DESK_STAFF_MAX + 1)
    )).scalars().all())
    out.staff_more = len(emps) > DESK_STAFF_MAX
    emps = emps[:DESK_STAFF_MAX]
    rows = {e.id: StaffRow(e) for e in emps}
    ids = list(rows)
    if ids:
        sts = list((await db.execute(
            select(SalaryStatement).where(
                SalaryStatement.employee_id.in_(ids), SalaryStatement.status != StatementStatus.VOID,
                or_(SalaryStatement.status.in_(pr.UNPAID_STATUSES), SalaryStatement.is_final_settlement.is_(True)))
            .order_by(SalaryStatement.period_start, SalaryStatement.id)
        )).scalars().all())
        tx_ids = {s.transaction_id for s in sts if s.transaction_id}
        txs = {t.id: t for t in (await db.execute(select(Transaction).where(Transaction.id.in_(tx_ids)))).scalars()} \
            if tx_ids else {}
        for s in sts:
            row = rows[s.employee_id]
            row.statements.append(s)
            if s.transaction_id in txs:
                row.transactions[s.transaction_id] = txs[s.transaction_id]
        accrued = await pr.accrued_previews(db, [e for e in emps if e.status in WORKING and e.deleted_at is None],
                                            as_of=as_of, gym_countries={gym.id: gym.country_code})
        for emp_id, acc in accrued.items():
            rows[emp_id].accrued = acc
        for w in (await db.execute(select(GovernanceWaiver).where(GovernanceWaiver.employee_id.in_(ids)))).scalars():
            rows[w.employee_id].waiver = w
    out.staff = list(rows.values())
    cash = list((await db.execute(
        select(Subscription, User.full_name).join(User, User.id == Subscription.customer_id)
        .where(Subscription.gym_id == gym.id, _active_membership(as_of), ~_paid_online())
        .order_by(Subscription.ends_on, Subscription.id).limit(DESK_LIST_MAX)
    )).all())
    out.manual_refunds = [(s, name, pro_rata(s.price, s.starts_on, s.ends_on, as_of)) for s, name in cash]
    out.orders = list((await db.execute(
        select(ShopOrder).where(ShopOrder.gym_id == gym.id, ShopOrder.status.in_(OPEN_ORDERS))
        .order_by(ShopOrder.id).limit(DESK_LIST_MAX)
    )).scalars().all())
    jobs = {j.kind: j for j in (await db.execute(
        select(Job).where(Job.dedupe_key.in_([f"{REFUND_JOB}:{gym.id}", f"{ORDERS_JOB}:{gym.id}"]),
                          Job.status.in_((JobStatus.QUEUED, JobStatus.RUNNING)))
    )).scalars()}
    out.refund_job, out.orders_job = jobs.get(REFUND_JOB), jobs.get(ORDERS_JOB)
    return out


@dataclass(slots=True)
class Blockers:
    cooling_off_until: datetime | None = None
    staff_unpaid: int = 0  # employees with an unsettled statement (and no waiver)
    staff_unsettled: int = 0  # employees still working without a final settlement (and no waiver)
    memberships: int = 0  # active memberships (refund them, or wait until they end)
    last_end: date | None = None
    open_orders: int = 0

    @property
    def items(self) -> list[str]:
        out: list[str] = []
        if self.cooling_off_until is not None:
            out.append(f"The cooling-off period runs until {self.cooling_off_until:%d %b %Y, %H:%M} UTC - the gym can "
                       "still be restored until then.")
        if self.staff_unsettled:
            out.append(f"{self.staff_unsettled} employee{'s' if self.staff_unsettled != 1 else ''} without a final "
                       "settlement - create the final settlements (or waive the pay with a note).")
        if self.staff_unpaid:
            out.append(f"{self.staff_unpaid} employee{'s' if self.staff_unpaid != 1 else ''} with unpaid salary - pay "
                       "through Cashfree, record the payment proof, or waive it with a note.")
        if self.memberships:
            end = f" (the last one ends on {self.last_end:%d %b %Y})" if self.last_end else ""
            out.append(f"{self.memberships} active membership{'s' if self.memberships != 1 else ''} - refund them pro "
                       f"rata, or honour them until they end{end}.")
        if self.open_orders:
            out.append(f"{self.open_orders} open shop order{'s' if self.open_orders != 1 else ''} - cancel and refund "
                       "them.")
        return out

    @property
    def clear(self) -> bool:
        return not self.items


async def deletion_blockers(db: AsyncSession, gym: Gym, *, now: datetime | None = None) -> Blockers:
    """Why the final deletion can't run yet - computed live from the ledger, statements, memberships and orders (never
    from a cached list): four bounded COUNT queries."""
    now = now or utcnow()
    today = date.today()
    b = Blockers()
    if gym.delete_after is None or gym.delete_after > now:
        b.cooling_off_until = gym.delete_after or now
    waived = exists().where(GovernanceWaiver.employee_id == SalaryStatement.employee_id)
    b.staff_unpaid = int(await db.scalar(
        select(func.count(func.distinct(SalaryStatement.employee_id)))
        .where(SalaryStatement.gym_id == gym.id, _unsettled_clause(), ~waived)) or 0)
    has_fs = exists().where(SalaryStatement.employee_id == Employee.id, SalaryStatement.is_final_settlement.is_(True),
                            SalaryStatement.status != StatementStatus.VOID)
    emp_waived = exists().where(GovernanceWaiver.employee_id == Employee.id)
    b.staff_unsettled = int(await db.scalar(
        select(func.count(Employee.id)).where(Employee.gym_id == gym.id, Employee.deleted_at.is_(None),
                                              Employee.status.in_(WORKING), ~has_fs, ~emp_waived)) or 0)
    row = (await db.execute(
        select(func.count(Subscription.id), func.max(Subscription.ends_on))
        .where(Subscription.gym_id == gym.id, _active_membership(today))
    )).one()
    b.memberships, b.last_end = int(row[0] or 0), row[1]
    b.open_orders = int(await db.scalar(
        select(func.count(ShopOrder.id)).where(ShopOrder.gym_id == gym.id, ShopOrder.status.in_(OPEN_ORDERS))) or 0)
    return b


# ---------------------------------------------------------------------- final settlements
async def create_settlements(db: AsyncSession, admin: User, gym: Gym, *, last_day: date,
                             request: Any = None) -> tuple[list[int], list[str]]:
    """Create the final settlement of every employee still working at a gym scheduled for deletion (at most
    SETTLEMENTS_PER_CLICK per call) - nothing is paid here. Returns (statement ids, problems). The caller locked the gym
    and commits."""
    if gym.deletion_requested_at is None or gym.archived_at is not None:
        raise GovernanceError("Final settlements are created for a gym scheduled for deletion.")
    has_fs = exists().where(SalaryStatement.employee_id == Employee.id, SalaryStatement.is_final_settlement.is_(True),
                            SalaryStatement.status != StatementStatus.VOID)
    emps = list((await db.execute(
        select(Employee).where(Employee.gym_id == gym.id, Employee.deleted_at.is_(None), Employee.status.in_(WORKING),
                               ~has_fs, ~exists().where(GovernanceWaiver.employee_id == Employee.id))
        .order_by(Employee.id).limit(SETTLEMENTS_PER_CLICK).with_for_update()
    )).scalars().all())
    created: list[int] = []
    problems: list[str] = []
    for emp in emps:  # bounded: SETTLEMENTS_PER_CLICK; each settlement is priced for its own person
        try:
            async with db.begin_nested():
                st = await pr.create_final_settlement(db, emp, last_day, SETTLEMENT_REASON,
                                                      note=f"Gym closed by the IronPulse platform ({gym.name})"[:200],
                                                      actor=admin, gym=gym, request=request)
                st.rules = {**(st.rules or {}), "governance": {"kind": "gym_delete", "gym_id": gym.id}}
            created.append(st.id)
        except pr.PayrollError as exc:
            problems.append(f"{emp.full_name}: {exc}")
    if created:
        notes = [Note(e.user_id, f"Your final settlement at {gym.name}",
                      f"{gym.name} is closing on IronPulse. Your final settlement (salary up to {last_day:%d %b %Y} and "
                      "any end-of-service benefit) has been prepared - you are told as soon as it is paid.",
                      link="/staff", category=NotificationCategory.PAYMENT, severity=Severity.INFO)
                 for e in emps if e.user_id]
        if notes:
            await db.flush()
            await notify_bulk(db, notes)
        await audit(db, admin, "governance.settlements_created", "gym", gym.id, gym_id=gym.id, request=request,
                    detail={"statements": created, "last_day": last_day.isoformat(), "problems": problems[:20],
                            "by": PLATFORM_BY})
    return created, problems


async def load_statement(db: AsyncSession, statement_id: int) -> tuple[SalaryStatement, Employee, Gym] | None:
    row = (await db.execute(
        select(SalaryStatement, Employee, Gym).join(Employee, Employee.id == SalaryStatement.employee_id)
        .join(Gym, Gym.id == SalaryStatement.gym_id).where(SalaryStatement.id == statement_id)
        .with_for_update(of=SalaryStatement)
    )).first()
    return (row[0], row[1], row[2]) if row is not None else None


async def pay_statement(db: AsyncSession, admin: User, st: SalaryStatement, emp: Employee, gym: Gym, *,
                        request: Any = None) -> Transaction:
    """Pay a due statement of a gym under governance through Cashfree Payouts (or the TEST MODE simulation) - money
    moves. Raises payroll.PayrollError when the payout isn't possible (then: pay by bank transfer and record the
    proof). The caller commits."""
    if gym.is_active or gym.archived_at is not None:
        raise GovernanceError("Payouts from here are for suspended gyms - the owner pays an active gym's staff.")
    tx = await pr.initiate_payout(db, st, emp, gym, admin, request=request)
    await audit(db, admin, "governance.payout", "salary_statement", st.id, gym_id=gym.id, request=request, detail={
        "amount": str(st.net_pay), "currency": st.currency, "transaction_id": tx.id, "by": PLATFORM_BY})
    return tx


async def record_statement_proof(db: AsyncSession, admin: User, st: SalaryStatement, emp: Employee, gym: Gym, *,
                                 upload: Any, reference: str, amount_paid: Decimal, paid_on: date | None,
                                 request: Any = None) -> Transaction:
    """Record the proof of a bank transfer made outside IronPulse (payroll's proof path: stored encrypted, OCR-checked
    in the background). The caller commits."""
    if gym.is_active or gym.archived_at is not None:
        raise GovernanceError("Payment proofs from here are for suspended gyms.")
    tx = await pr.record_proof(db, st, emp, admin, upload=upload, reference=reference, amount_paid=amount_paid,
                               paid_on=paid_on, request=request)
    await audit(db, admin, "governance.payment_proof", "salary_statement", st.id, gym_id=gym.id, request=request,
                detail={"amount_paid": str(amount_paid), "reference": reference, "transaction_id": tx.id,
                        "by": PLATFORM_BY})
    return tx


async def waive_employee_pay(db: AsyncSession, admin: User, emp: Employee, gym: Gym, *, note: str,
                             request: Any = None) -> GovernanceWaiver | None:
    """Waive an employee's outstanding pay at a gym scheduled for deletion, with a written note (paid outside
    IronPulse, or forgone): an immutable record of the amount, the employment ends, the person is told. None when a
    waiver exists already (idempotent). The caller locked the employee row and commits."""
    if gym.deletion_requested_at is None or gym.archived_at is not None:
        raise GovernanceError("Pay is waived only for a gym scheduled for deletion.")
    if len(note) < WAIVE_NOTE_MIN or len(note) > NOTE_MAX:
        raise GovernanceError(f"Explain the waiver in {WAIVE_NOTE_MIN}-{NOTE_MAX} characters (for example: paid by "
                              "bank transfer on 3 Nov, reference ...).")
    if await db.scalar(select(GovernanceWaiver.id).where(GovernanceWaiver.employee_id == emp.id)) is not None:
        return None
    owed: dict[str, Decimal] = {}
    for currency, total in (await db.execute(
        select(SalaryStatement.currency, func.coalesce(func.sum(SalaryStatement.net_pay), 0))
        .where(SalaryStatement.employee_id == emp.id, _unsettled_clause()).group_by(SalaryStatement.currency)
    )).all():
        add_money(owed, currency, total)
    if emp.status in WORKING and emp.deleted_at is None and await pr.final_settlement_of(db, emp.id) is None:
        acc = (await pr.accrued_previews(db, [emp], gym_countries={gym.id: gym.country_code})).get(emp.id)
        if acc is not None:
            add_money(owed, acc.currency, acc.net)
    amount = owed.get((emp.currency or "INR").upper(), Decimal("0.00"))
    waiver = GovernanceWaiver(gym_id=gym.id, employee_id=emp.id, employee_name=(emp.full_name or "")[:120],
                              actor_id=admin.id, amount=amount, currency=(emp.currency or "INR").upper(),
                              note=note[:NOTE_MAX])
    db.add(waiver)
    if emp.deleted_at is None and emp.status in CURRENT:
        await _end_for_closure(db, [emp], gym, reason="Gym closed by the IronPulse platform (pay waived: " +
                               note[:120] + ")")
    if emp.user_id:
        await notify(db, emp.user_id, f"Your pay at {gym.name} was recorded as settled",
                     f"The IronPulse platform recorded your outstanding pay at {gym.name} ({money_text(owed)}) as "
                     f"settled outside IronPulse: {note}. If that is not right, reply to this e-mail.",
                     category=NotificationCategory.PAYMENT, severity=Severity.WARNING, link="/staff")
    await db.flush()
    await audit(db, admin, "governance.pay_waived", "employee", emp.id, gym_id=gym.id, request=request, detail={
        "waiver_id": waiver.id, "amount": str(amount), "currency": waiver.currency, "owed": money_text(owed),
        "note": note, "by": PLATFORM_BY})
    return waiver


async def _end_for_closure(db: AsyncSession, emps: Sequence[Employee], gym: Gym, *, reason: str) -> None:
    """End employments because the gym closes (set-based): TERMINATED today, shifts cancelled, client links closed,
    unused invitation links invalidated. Earned pay stays on its statements."""
    if not emps:
        return
    ids = [e.id for e in emps]
    today = date.today()
    await db.execute(update(Employee).where(Employee.id.in_(ids), Employee.status.in_(CURRENT))
                     .values(status=EmploymentStatus.TERMINATED, termination_reason=reason[:255],
                             termination_date=func.coalesce(Employee.termination_date, today))
                     .execution_options(synchronize_session=False))
    await db.execute(TrainerShift.__table__.delete().where(TrainerShift.employee_id.in_(ids)))
    await db.execute(update(TrainerClient).where(TrainerClient.employee_id.in_(ids), TrainerClient.active.is_(True))
                     .values(active=False).execution_options(synchronize_session=False))
    await db.execute(update(Subscription).where(Subscription.trainer_id.in_(ids)).values(trainer_id=None)
                     .execution_options(synchronize_session=False))
    await db.execute(AuthToken.__table__.delete().where(AuthToken.employee_id.in_(ids),
                                                        AuthToken.purpose == TokenPurpose.STAFF_INVITE,
                                                        AuthToken.used_at.is_(None)))
    for e in emps:  # the loaded objects follow the database (attributes only, no SQL)
        if e.status in CURRENT:
            e.status = EmploymentStatus.TERMINATED


# ---------------------------------------------------------------------- members: pro-rata refunds
@dataclass(slots=True)
class RefundResult:
    state: str  # refunded | manual | nothing | skipped | failed
    amount: Decimal = Decimal("0.00")
    message: str = ""
    currency: str = "INR"


async def _payment_of(db: AsyncSession, sub: Subscription) -> Transaction | None:
    return await db.scalar(
        select(Transaction).where(Transaction.subscription_id == sub.id, Transaction.direction == TxDirection.INCOMING,
                                  Transaction.category == TxCategory.SUBSCRIPTION, Transaction.status == TxStatus.SUCCESS)
        .order_by(Transaction.id.desc()).limit(1))


async def _close_membership(db: AsyncSession, sub: Subscription, now: datetime) -> None:
    from functools import partial

    from app.ai.guard import guard
    from app.services.realtime import call_after_commit

    sub.status = SubscriptionStatus.CANCELLED
    sub.cancelled_at = now
    await db.execute(update(TrainerClient).where(TrainerClient.subscription_id == sub.id).values(active=False)
                     .execution_options(synchronize_session=False))
    call_after_commit(db, partial(guard.forget_entitlement, sub.customer_id))  # the AI tier ends with it


async def refund_membership(db: AsyncSession, sub_id: int, *, as_of: date, actor_id: int | None,
                            manual_reference: str | None = None, note: str = "") -> RefundResult:
    """Refund the unused part of one ACTIVE membership pro rata through the existing refund path and end it:
    paid online -> the payment gateway's refund API (``shop_payments.cashfree_refund``; TEST MODE simulated); paid in
    cash -> only with ``manual_reference`` (the admin refunded it by hand). A REFUND ledger row records it. The
    membership row is locked; the caller commits (each refund in its own transaction - money moved at the gateway is
    never rolled back with someone else's failure)."""
    from app.services.shop_payments import cashfree_refund

    sub = await db.scalar(select(Subscription).options(joinedload(Subscription.gym), joinedload(Subscription.plan),
                                                       joinedload(Subscription.customer))
                          .where(Subscription.id == sub_id).with_for_update(of=Subscription)
                          .execution_options(populate_existing=True))
    if sub is None or sub.status != SubscriptionStatus.ACTIVE:
        return RefundResult("skipped", message="The membership is not active any more.")
    now = utcnow()
    paid = await _payment_of(db, sub)
    amount = pro_rata(sub.price, sub.starts_on, sub.ends_on, as_of)
    if paid is not None:
        amount = min(amount, q(paid.amount))
    if amount <= 0:
        await _close_membership(db, sub, now)
        await notify(db, sub.customer_id, f"Your membership at {sub.gym.name} has ended",
                     f"{sub.gym.name} is closing on IronPulse. Your {sub.plan.name} membership had no unused paid days "
                     "left, so nothing is refunded.", category=NotificationCategory.PAYMENT, link="/me/subscriptions")
        return RefundResult("nothing")
    online = paid is not None and paid.provider in ONLINE_PROVIDERS
    if not online and not manual_reference:
        return RefundResult("manual", amount, "Paid in cash - refund it by hand and record the reference.",
                            sub.currency)
    reference, simulated, gateway_status = manual_reference, False, None
    if online and paid is not None:
        if paid.provider == "cashfree":
            result = await cashfree_refund(paid.provider_order_id or "", f"RS-{sub.id}-{paid.id}", amount,
                                           f"Pro-rata refund: {sub.gym.name} closed")
            if not result.ok:
                return RefundResult("failed", amount, result.error or "The payment provider refused the refund.",
                                    sub.currency)
            reference, gateway_status = result.refund_id, result.status
        else:
            reference, simulated, gateway_status = f"TEST-RS-{secrets.token_hex(5).upper()}", True, "SUCCESS"
    tx = Transaction(gym_id=sub.gym_id, direction=TxDirection.OUTGOING, category=TxCategory.REFUND,
                     status=TxStatus.SUCCESS, amount=amount, currency=sub.currency,
                     description=f"Pro-rata refund: {sub.plan.name} membership - {sub.gym.name}"[:255],
                     party_label=((paid.party_label if paid is not None else None) or sub.customer.full_name)[:160],
                     provider=(paid.provider if online and paid is not None else "manual"),
                     provider_reference=(reference or "")[:80] or None, customer_id=sub.customer_id,
                     subscription_id=sub.id, created_by_id=actor_id, completed_at=now,
                     meta={"kind": "subscription_refund", "governance": True, "as_of": as_of.isoformat(),
                           "simulated": simulated, "gateway_status": gateway_status, "note": note[:300] or None,
                           "payment_transaction_id": paid.id if paid is not None else None})
    db.add(tx)
    await _close_membership(db, sub, now)
    await db.flush()
    how = ("is on its way back to your original payment method (banks usually take 5-7 working days)" if online
           else "was refunded to you by the IronPulse team")
    await notify(db, sub.customer_id, "Membership refunded",
                 f"{sub.gym.name} is closing on IronPulse, so your {sub.plan.name} membership has ended. "
                 f"{money(amount, sub.currency)} for the unused days {how}.",
                 category=NotificationCategory.PAYMENT, severity=Severity.SUCCESS, link="/me/subscriptions")
    return RefundResult("refunded", amount, currency=sub.currency)


async def queue_member_refunds(db: AsyncSession, admin: User, gym: Gym, *, as_of: date | None = None,
                               request: Any = None, audit_it: bool = True) -> Any:
    """Explicitly confirmed by the admin: refund every active membership of a suspended gym pro rata, in the
    background (``governance_refunds`` job: one refund per transaction, JOB_BATCH per run). Memberships paid in cash
    are left for the desk's manual refund. The caller commits."""
    if gym.is_active or gym.archived_at is not None:
        raise GovernanceError("Members are refunded from here only while the gym is suspended.")
    as_of = as_of or date.today()
    job = await enqueue(db, REFUND_JOB, {"gym_id": gym.id, "actor_id": admin.id, "as_of": as_of.isoformat()},
                        dedupe_key=f"{REFUND_JOB}:{gym.id}", max_attempts=3, user_id=None)
    if audit_it:
        await audit(db, admin, "governance.refunds_queued", "gym", gym.id, gym_id=gym.id, request=request,
                    detail={"as_of": as_of.isoformat(), "by": PLATFORM_BY})
    return job


async def queue_order_settlement(db: AsyncSession, admin: User, gym: Gym, *, request: Any = None) -> Any:
    """Explicitly confirmed by the admin: cancel and refund every open shop order of a suspended gym, in the background
    (``governance_orders`` job). The caller commits."""
    if gym.is_active or gym.archived_at is not None:
        raise GovernanceError("Orders are settled from here only while the gym is suspended.")
    job = await enqueue(db, ORDERS_JOB, {"gym_id": gym.id, "actor_id": admin.id}, dedupe_key=f"{ORDERS_JOB}:{gym.id}",
                        max_attempts=3, user_id=None)
    await audit(db, admin, "governance.orders_queued", "gym", gym.id, gym_id=gym.id, request=request,
                detail={"by": PLATFORM_BY})
    return job


@job_handler(REFUND_JOB, on_exit="keep")
async def _refunds_job(db: AsyncSession, payload: dict[str, Any]) -> None:
    """Refund the gym's active memberships paid online, one per transaction (JOB_BATCH per run, JOB_BUDGET_S), then
    re-queue itself while any remain. Cash-paid ones stay for the desk."""
    import time

    gym_id = int(payload["gym_id"])
    actor_id = payload.get("actor_id")
    as_of = date.fromisoformat(str(payload.get("as_of") or date.today().isoformat()))
    started = time.monotonic()
    done = failed = 0
    last_id = 0
    while done + failed < JOB_BATCH and time.monotonic() - started < JOB_BUDGET_S:
        sub_id = await db.scalar(
            select(Subscription.id).where(Subscription.gym_id == gym_id, Subscription.id > last_id,
                                          _active_membership(as_of), _paid_online())
            .order_by(Subscription.id).limit(1))
        await db.commit()
        if sub_id is None:
            break
        last_id = sub_id
        result = await refund_membership(db, sub_id, as_of=as_of, actor_id=actor_id)
        await db.commit()  # each refund on its own: money moved at the gateway is never rolled back with another
        if result.state == "failed":
            failed += 1
            log.warning("governance refund of membership %s failed: %s", sub_id, result.message)
        else:
            done += 1
    if done + failed >= JOB_BATCH:  # more to do: the next run continues
        await enqueue(db, REFUND_JOB, payload, dedupe_key=f"{REFUND_JOB}:{gym_id}:next", max_attempts=3)
        await db.commit()


@job_handler(ORDERS_JOB, on_exit="keep")
async def _orders_job(db: AsyncSession, payload: dict[str, Any]) -> None:
    """Cancel and refund the gym's open shop orders, one per transaction: cancellable ones through the shop's own
    cancellation (refund + restock); shipped ones and ones with a refund request get the rest of their total back."""
    import time

    from app.services import shop_payments as sp

    gym_id = int(payload["gym_id"])
    actor = await db.get(User, int(payload["actor_id"])) if payload.get("actor_id") else None
    if actor is None:
        return
    started = time.monotonic()
    handled = 0
    last_id = 0
    while handled < JOB_BATCH and time.monotonic() - started < JOB_BUDGET_S:
        order_id = await db.scalar(select(ShopOrder.id).where(ShopOrder.gym_id == gym_id, ShopOrder.id > last_id,
                                                              ShopOrder.status.in_(OPEN_ORDERS))
                                   .order_by(ShopOrder.id).limit(1))
        await db.commit()
        if order_id is None:
            break
        last_id = order_id
        try:
            outcome = await sp.platform_close_order(db, order_id, actor,
                                                    reason="The store closed on IronPulse - full refund")
            await db.commit()
            log.info("governance: order %s of gym %s -> %s", order_id, gym_id, outcome)
        except Exception:  # noqa: BLE001 - one order's failure never stops the others (it stays open: a blocker)
            await db.rollback()
            log.exception("governance: closing order %s failed", order_id)
        handled += 1
    if handled >= JOB_BATCH:
        await enqueue(db, ORDERS_JOB, payload, dedupe_key=f"{ORDERS_JOB}:{gym_id}:next", max_attempts=3)
        await db.commit()


async def manual_membership_refund(db: AsyncSession, admin: User, sub_id: int, *, reference: str, note: str,
                                   request: Any = None) -> RefundResult:
    """Record a refund the admin made by hand (a cash membership): the REFUND ledger row with the reference, the
    membership ends, the member is told. The caller commits."""
    reference = " ".join((reference or "").split())[:80]
    if len(reference) < 4:
        raise GovernanceError("Enter the refund's reference (bank / UPI reference or receipt number).")
    if not WAIVE_NOTE_MIN <= len(note) <= NOTE_MAX:
        raise GovernanceError(f"Describe how it was refunded in {WAIVE_NOTE_MIN}-{NOTE_MAX} characters.")
    gym_id = await db.scalar(select(Subscription.gym_id).where(Subscription.id == sub_id))
    gym = await db.get(Gym, gym_id) if gym_id else None
    if gym is None or gym.is_active or gym.archived_at is not None:
        raise GovernanceError("Memberships are refunded from here only while the gym is suspended.")
    result = await refund_membership(db, sub_id, as_of=date.today(), actor_id=admin.id, manual_reference=reference,
                                     note=note)
    if result.state == "refunded":
        await audit(db, admin, "governance.manual_refund", "subscription", sub_id, gym_id=gym.id, request=request,
                    detail={"amount": str(result.amount), "reference": reference, "note": note, "by": PLATFORM_BY})
    return result


# ====================================================================== delete a gym: step 2 (final)
async def finalize_gym_deletion(db: AsyncSession, admin: User, gym: Gym, *, note: str, request: Any = None) -> bool:
    """The final deletion - only after the cooling-off and with every money obligation settled (``deletion_blockers``
    re-read under the gym's lock). The gym is ARCHIVED: remaining employments end, job postings close, unpaid
    checkouts end; no row is deleted - transactions, salary statements, invoices and refunds stay, read-only. False when
    it was archived already (idempotent); GovernanceError while anything blocks it. The caller locked the gym and
    commits."""
    if gym.archived_at is not None:
        return False
    if gym.deletion_requested_at is None:
        raise GovernanceError("Schedule the deletion first - the gym is suspended and can still be restored during the "
                              "cooling-off period.")
    blockers = await deletion_blockers(db, gym)
    if not blockers.clear:
        raise GovernanceError("The gym can't be deleted yet: " + " ".join(blockers.items))
    now = utcnow()
    leftover = list((await db.execute(
        select(Employee).where(Employee.gym_id == gym.id, Employee.deleted_at.is_(None), Employee.status.in_(CURRENT))
        .order_by(Employee.id).with_for_update()
    )).scalars().all())
    since = (gym.deletion_requested_at or now).date()
    # everyone who still worked there when the deletion was scheduled (a waiver may have ended a job since)
    staff = list((await db.execute(
        select(Employee.user_id).where(Employee.gym_id == gym.id, Employee.user_id.is_not(None),
                                       or_(Employee.status.in_(CURRENT), Employee.termination_date >= since))
        .distinct().limit(NOTIFY_MAX)
    )).scalars().all())
    await _end_for_closure(db, leftover, gym, reason="Gym closed by the IronPulse platform")
    jobs_closed = (await db.execute(update(JobPosting).where(JobPosting.gym_id == gym.id, JobPosting.is_open.is_(True))
                                    .values(is_open=False).execution_options(synchronize_session=False))).rowcount or 0
    cancelled_subs, cancelled_orders = await _cancel_pending_purchases(db, [gym.id])
    members = list((await db.execute(
        select(Subscription.customer_id).where(Subscription.gym_id == gym.id, Subscription.ends_on >= since)
        .distinct().limit(NOTIFY_MAX)
    )).scalars().all())
    gym.is_active = False
    gym.archived_at = now
    notes = [Note(gym.owner_id, f"{gym.name} was deleted",
                  f"{gym.name} was deleted from IronPulse by the platform team. Its financial records (transactions, "
                  "salary statements, invoices and refunds) are kept, read-only, for legal and tax purposes.",
                  link="/owner", category=NotificationCategory.SYSTEM, severity=Severity.DANGER)]
    notes += [Note(uid, f"{gym.name} has closed", f"{gym.name} has closed on IronPulse and your staff access there has "
                   "ended. Your pay records are kept.", link="/staff", category=NotificationCategory.STAFF,
                   severity=Severity.WARNING) for uid in staff]
    notes += [Note(uid, f"{gym.name} has closed", f"{gym.name} has closed on IronPulse. Your payment and membership "
                   "records are kept.", link="/me/subscriptions", category=NotificationCategory.PAYMENT,
                   severity=Severity.INFO) for uid in members]
    await db.flush()
    await notify_bulk(db, notes)
    await audit(db, admin, "governance.gym_archived", "gym", gym.id, gym_id=gym.id, request=request, detail={
        "note": note, "employments_ended": len(leftover), "job_postings_closed": int(jobs_closed),
        "cancelled_checkouts": cancelled_subs, "cancelled_orders": cancelled_orders, "by": PLATFORM_BY})
    await TS.notify_superadmins(db, f"Gym deleted: {gym.name}",
                                f"{admin.full_name} completed the deletion of {gym.name} (#{gym.id}). Every money "
                                "obligation was settled first; its financial records are kept.",
                                link=f"/superadmin/governance/gyms/{gym.id}", severity=Severity.INFO)
    from app.core.nav import refresh_nav_state

    await refresh_nav_state(db)
    return True


__all__ = [
    "EMPLOYEE_BAN_REASONS", "GYM_DELETE_REASONS", "MEMBER_POLICIES", "OWNER_BAN_REASONS", "Blockers", "Desk",
    "GovernanceError", "GymImpact", "Reason", "ban_employee", "ban_owner", "ban_refusal", "create_settlements",
    "deletion_blockers", "desk", "employee_impact", "finalize_gym_deletion", "gym_state", "gyms_impact", "lift_ban",
    "lock_gym", "manual_membership_refund", "money_text", "owner_gyms", "pay_statement", "pro_rata",
    "queue_member_refunds", "queue_order_settlement", "rate_refusal", "record_statement_proof", "refund_membership",
    "restore_gym", "restore_owner_gyms", "schedule_gym_deletion", "statement_settled", "total_impact", "typed_matches",
    "validate", "waive_employee_pay",
]
