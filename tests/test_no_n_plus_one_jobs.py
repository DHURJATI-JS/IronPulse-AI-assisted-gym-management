"""No N+1 in background work: every ``@periodic`` task, job handler and batching helper that walks many rows runs the
same number of SQL statements for a batch of 2 rows and for a batch of 20 (``app.db.QueryCounter``).

Each case seeds SMALL rows, runs ONE batch of the work (batch sizes are larger than 20, so both runs are a single chunk)
and records its statements; then seeds LARGE more rows - the first run already handled the first ones, so the second
run handles exactly the new ones - and records again. The test checks that the run really processed the rows (no
vacuous pass), then fails when the statements grow - in total, or any one normalised statement running at least twice
more often (a per-row repeat hidden by a statement that went away) - with the normalised statements that repeat.
Cases cover the code PATHS of a task, not only its name: each branch that walks rows (ban lifts, extensions, permanent
bans, owners and employee records of the purge ...) has its own case.

Growth that is per row BY DESIGN (each unit of work in its own short transaction, e.g. an order verified with the
payment gateway before it is settled, or one gym's payroll per transaction) is allowed only by a ``job_growth`` entry of
tests/n_plus_one_allowlist.json: ``per_row`` statements at most, ONLY of the statement ``shapes`` listed there (prefixes
of the normalised SQL), with the reason - any other statement that repeats per row still fails.

Not measured here (each handles ONE entity, not a batch of rows): verify_identity, verify_branch, verify_payment_proof,
staff_activation_check, payroll_explain, coach_plan, health_ai_note, health_ac_note - and the job runner itself (one
claim per batch, then each job is its own unit of work with its own fenced status write)."""

from __future__ import annotations

import functools
import itertools
import json
import os
import re
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event, func, select, update

from app import db as dbmod
from app.core.utils import utcnow
from app.db import QueryCounter
from app.models import (
    AICallLog,
    AuditLog,
    CustomerProfile,
    EmailOutbox,
    Employee,
    IdentityDocument,
    IpBan,
    Job,
    JobPosting,
    Notification,
    Product,
    ProductReview,
    SalaryStatement,
    SecurityEvent,
    ShopOrder,
    ShopOrderItem,
    Subscription,
    Task,
    TrainerClient,
    Transaction,
    User,
)
from app.models.enums import (
    EmailStatus,
    EmployeeRole,
    EmploymentStatus,
    EmploymentType,
    IdDocType,
    IdStatus,
    JobStatus,
    PurchaseChannel,
    Role,
    Severity,
    ShiftMode,
    StatementStatus,
    SubscriptionStatus,
    TaskKind,
    ThreatCategory,
    TxCategory,
    TxDirection,
    TxStatus,
    VerificationStatus,
    WorkMode,
)
from app.models.shop import OrderStatus, ProductCategory
from tests.factories import make_employee, make_gym, make_owner, make_plans
from tests.nplus_world import put

ALLOWLIST_PATH = Path(__file__).with_name("n_plus_one_allowlist.json")
SMALL, LARGE = 2, 20
SHAPE_REPEAT = 2  # one normalised statement running this many more times for the larger batch = a per-row repeat
T0 = datetime(2026, 11, 10, 16, 35, tzinfo=UTC)  # 22:05 IST: inside the 22:00-22:30 bedtime window of a 06:30 wake-up
_seq = itertools.count(1)


def job_allowance(name: str) -> tuple[int, str | None, list[str]]:
    """(statements allowed on top for the larger batch, the reason, the statement shapes that may repeat per row)."""
    entry = json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8")).get("job_growth", {}).get(name)
    if not entry:
        return 0, None, []
    return int(entry["per_row"]) * (LARGE - SMALL), entry["reason"], list(entry.get("shapes") or [])


# ==================================================================================================== measuring
class Statements:
    """The statements executed inside the block (engine-wide: background work opens its own sessions)."""

    def __init__(self) -> None:
        self.items: list[str] = []
        self._engine = dbmod.get_engine().sync_engine

    def __enter__(self) -> Statements:
        event.listen(self._engine, "before_cursor_execute", self._record)
        return self

    def __exit__(self, *exc: object) -> None:
        event.remove(self._engine, "before_cursor_execute", self._record)

    def _record(self, conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
        self.items.append(statement)


def normalise(sql: str) -> str:
    s = " ".join(sql.split())
    s = re.sub(r"\$\d+(::[A-Za-z_ ]+(\[\])?)?", "?", s)
    s = re.sub(r"'(?:[^']|'')*'", "'S'", s)
    s = re.sub(r"\b\d+(\.\d+)?\b", "N", s)
    s = re.sub(r"\(\?(?:, \?)*\)", "(?...)", s)  # IN (?), IN (?, ?) ...: one shape
    s = re.sub(r"(?:\?, )+\?", "?...", s)
    s = re.sub(r"(\((?:\?\.\.\.|\?)(?:, N)?\))(?:, \((?:\?\.\.\.|\?)(?:, N)?\))+", r"\1, ...", s)  # VALUES (..), (..)
    return s


def repeats(small: list[str], large: list[str], top: int = 6) -> list[str]:
    a, b = Counter(map(normalise, small)), Counter(map(normalise, large))
    grown = sorted(((b[s] - a[s], s) for s in b if b[s] > a[s]), reverse=True)
    return [f"+{d} x {s[:240]}" for d, s in grown[:top]]


def shape_growth(small: list[str], large: list[str], at_least: int = SHAPE_REPEAT) -> dict[str, int]:
    """{normalised statement: extra runs} of the statements the larger batch ran at least ``at_least`` more times."""
    a, b = Counter(map(normalise, small)), Counter(map(normalise, large))
    return {s: b[s] - a[s] for s in b if b[s] - a[s] >= at_least}


# ==================================================================================================== seeding
async def users(db: Any, n: int, *, role: Role = Role.CUSTOMER, profile: bool = True, **extra: Any) -> list[int]:
    k = next(_seq)
    rows = [{"email": f"npj{k}-{i}@example.com", "full_name": f"Person {k}-{i}", "role": role,
             "timezone": "Asia/Kolkata", "date_of_birth": date(1995, 5, 17), "id_status": IdStatus.VERIFIED,
             "email_verified_at": utcnow(), "photo_key": f"public/avatars/{k:016x}{i:016x}.jpg", **extra}
            for i in range(n)]
    ids = await put(db, User, rows, ids=True)
    if profile:
        await put(db, CustomerProfile, [{"user_id": u} for u in ids])
    await db.commit()
    return ids


async def gym_and_plans(db: Any) -> tuple[Any, dict[str, Any]]:
    gym = await make_gym(db, await make_owner(db))
    return gym, {p.code: p for p in await make_plans(db, gym)}


async def subscriptions(db: Any, gym: Any, plan: Any, members: list[int], *, ends_on: date,
                        status: SubscriptionStatus = SubscriptionStatus.ACTIVE, created_at: datetime | None = None) -> list[int]:
    rows = [{"customer_id": m, "gym_id": gym.id, "plan_id": plan.id, "status": status,
             "channel": PurchaseChannel.ONLINE_CHECKOUT, "price": plan.price, "currency": plan.currency,
             "starts_on": ends_on - timedelta(days=30), "ends_on": ends_on, "created_at": created_at or utcnow()}
            for m in members]
    ids = await put(db, Subscription, rows, ids=True)
    await db.commit()
    return ids


async def employees(db: Any, gym: Any, n: int, *, status: EmploymentStatus = EmploymentStatus.ACTIVE,
                    verification: VerificationStatus = VerificationStatus.VERIFIED) -> list[int]:
    staff = await users(db, n, role=Role.STAFF, profile=False)
    k = next(_seq)
    rows = [{"gym_id": gym.id, "user_id": u, "invite_email": f"npj-emp-{k}-{i}@example.com", "full_name": f"Staff {k}-{i}",
             "role": EmployeeRole.TRAINER, "is_trainer": True, "work_mode": WorkMode.BOTH,
             "employment_type": EmploymentType.FULL_TIME, "country_code": gym.country_code, "currency": gym.currency,
             "monthly_salary": Decimal("30000"), "hire_date": date(2020, 1, 15), "status": status,
             "photo_key": f"public/staff/{k:016x}{i:016x}.jpg", "verification_status": verification,
             "id_status": IdStatus.VERIFIED} for i, u in enumerate(staff)]
    ids = await put(db, Employee, rows, ids=True)
    await db.commit()
    return ids


# ==================================================================================================== cases
Seed = Callable[[Any, dict[str, Any], int], Awaitable[None]]
Run = Callable[[Any, dict[str, Any]], Awaitable[int]]


@dataclass
class Case:
    name: str  # the periodic task / job / helper, as the report names it
    task: str  # the registered @periodic / @job_handler name ("" for a worker loop or a helper)
    seed: Seed  # add n rows the next run will handle
    run: Run  # one run (one batch); returns how many rows it handled
    prepare: Callable[[Any, dict[str, Any]], Awaitable[None]] | None = None
    patches: list[tuple[str, str, Any]] = field(default_factory=list)  # (module, attribute, value)
    at_least: bool = False  # the run may also re-read earlier rows (handled >= n)


CASES: list[Case] = []


def case(name: str, task: str = "", **kw: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register ``seed`` (the decorated function) with its ``run`` and options."""

    def deco(seed: Seed) -> Seed:
        CASES.append(Case(name=name, task=task, seed=seed, **kw))
        return seed

    return deco


# ---------------------------------------------------------------------------------------------------- notifications
async def _prep_gym(db: Any, st: dict[str, Any]) -> None:
    st["gym"], st["plans"] = await gym_and_plans(db)
    st["since"] = utcnow() - timedelta(minutes=1)


async def _run_member_notice(db: Any, st: dict[str, Any]) -> int:
    from app.services import member_notices

    gym = st["gym"]
    return await member_notices.send_member_notice(
        gym_id=gym.id, title="Online classes paused", body="Back soon.", link=f"/gyms/{gym.slug}",
        severity=Severity.WARNING, since=st["since"], as_of=date.today())


@case("notify_members (job: gym-wide member notice)", task="notify_members", prepare=_prep_gym, run=_run_member_notice)
async def _seed_member_notice(db: Any, st: dict[str, Any], n: int) -> None:
    await subscriptions(db, st["gym"], st["plans"]["online"], await users(db, n), ends_on=date.today() + timedelta(days=20))


async def _run_notify_many(db: Any, st: dict[str, Any]) -> int:
    from app.services.notify import notify_many

    ids, st["new"] = st["new"], []
    sent = await notify_many(db, ids, "Hello", "Body", link="/me")
    await db.commit()
    return sent


@case("notify_many / notify_bulk (batching helper)", run=_run_notify_many)
async def _seed_notify_many(db: Any, st: dict[str, Any], n: int) -> None:
    st["new"] = await users(db, n)


# ---------------------------------------------------------------------------------------------------- reminders
async def _run_task_reminders(db: Any, st: dict[str, Any]) -> int:
    from app.services import tasks as task_service

    return await task_service.send_task_reminders(db, now=utcnow())


@case("task_reminders (periodic)", task="task_reminders", run=_run_task_reminders)
async def _seed_task_reminders(db: Any, st: dict[str, Any], n: int) -> None:
    ids = await users(db, n)
    await put(db, Task, [{"user_id": u, "title": f"Ping {u}", "kind": TaskKind.REMINDER,
                          "remind_at": utcnow() - timedelta(minutes=2)} for u in ids])
    await db.commit()


async def _run_bedtime(db: Any, st: dict[str, Any]) -> int:
    from app.services import health as hs

    return await hs.bedtime_reminders(db, now=T0)


@case("bedtime_reminders (periodic)", task="bedtime_reminders", run=_run_bedtime)
async def _seed_bedtime(db: Any, st: dict[str, Any], n: int) -> None:
    await users(db, n)


async def _run_expiry_reminders(db: Any, st: dict[str, Any]) -> int:
    from app.services import checkout as svc

    return await svc.send_expiry_reminders(db, date.today())


@case("subscriptions_daily: expiry reminders (periodic)", task="subscriptions_daily", prepare=_prep_gym, run=_run_expiry_reminders)
async def _seed_expiry_reminders(db: Any, st: dict[str, Any], n: int) -> None:
    await subscriptions(db, st["gym"], st["plans"]["online"], await users(db, n), ends_on=date.today() + timedelta(days=3))


async def _run_salary_reminders(db: Any, st: dict[str, Any]) -> int:
    from app.services import payroll as pr

    return await pr.send_salary_reminders(db)


async def _prep_payroll(db: Any, st: dict[str, Any]) -> None:
    st["owner"] = await make_owner(db)
    st["gym"] = await make_gym(db, st["owner"])
    st["periods"] = itertools.count(0)


@case("payroll_salary_reminders (periodic)", task="payroll_salary_reminders", prepare=_prep_payroll, run=_run_salary_reminders)
async def _seed_salary_reminders(db: Any, st: dict[str, Any], n: int) -> None:
    gym = st["gym"]
    emps = await employees(db, gym, n)
    rows = []
    for e in emps:
        start = date(2025, 1, 1) + timedelta(days=31 * next(st["periods"]))
        start = start.replace(day=1)
        end = (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        rows.append({"employee_id": e, "gym_id": gym.id, "period_start": start, "period_end": end,
                     "days_in_period": (end - start).days + 1, "days_worked": (end - start).days + 1,
                     "monthly_salary": Decimal("30000"), "gross_pay": Decimal("30000"), "net_pay": Decimal("28000"),
                     "currency": "INR", "country_code": "IN", "status": StatementStatus.DUE,
                     "due_date": date.today() - timedelta(days=1)})
    await put(db, SalaryStatement, rows)
    await db.commit()


# ---------------------------------------------------------------------------------------------------- expiry / renewal
async def _prep_expiry(db: Any, st: dict[str, Any]) -> None:
    await _prep_gym(db, st)
    st["trainer"] = await make_employee(db, st["gym"])


async def _run_expire(db: Any, st: dict[str, Any]) -> int:
    from app.services import checkout as svc

    return await svc.expire_due_subscriptions(db, date.today())


@case("subscriptions_daily: expire memberships (periodic)", task="subscriptions_daily", prepare=_prep_expiry, run=_run_expire)
async def _seed_expire(db: Any, st: dict[str, Any], n: int) -> None:
    members = await users(db, n)
    subs = await subscriptions(db, st["gym"], st["plans"]["online_trainer"], members,
                               ends_on=date.today() - timedelta(days=1))
    await put(db, TrainerClient, [{"employee_id": st["trainer"].id, "customer_id": c, "subscription_id": s,
                                   "mode": ShiftMode.ONLINE} for c, s in zip(members, subs, strict=True)])
    await db.commit()


async def _run_cancel_stale(db: Any, st: dict[str, Any]) -> int:
    from app.services import checkout as svc

    return await svc.cancel_stale_pending(db)


@case("subscriptions_daily: cancel stale purchases (periodic)", task="subscriptions_daily", prepare=_prep_gym, run=_run_cancel_stale)
async def _seed_cancel_stale(db: Any, st: dict[str, Any], n: int) -> None:
    gym, plan = st["gym"], st["plans"]["online"]
    subs = await subscriptions(db, gym, plan, await users(db, n), ends_on=date.today() + timedelta(days=30),
                               status=SubscriptionStatus.PENDING_PAYMENT, created_at=utcnow() - timedelta(days=8))
    await put(db, Transaction, [{"gym_id": gym.id, "direction": TxDirection.INCOMING, "category": TxCategory.SUBSCRIPTION,
                                 "status": TxStatus.PENDING, "amount": Decimal("799"), "currency": "INR",
                                 "description": "x", "provider": "test", "subscription_id": s} for s in subs])
    await db.commit()


async def _prep_shop(db: Any, st: dict[str, Any]) -> None:
    st["owner"] = await make_owner(db)
    st["gym"] = await make_gym(db, st["owner"])
    (st["customer"],) = await users(db, 1)
    (st["product"],) = await put(db, Product, [{
        "gym_id": st["gym"].id, "name": "Whey", "slug": f"whey-{next(_seq)}", "category": ProductCategory.SUPPLEMENTS,
        "description": "x", "price": Decimal("999"), "currency": "INR", "stock": 50}], ids=True)
    await db.commit()


async def _run_expire_unpaid(db: Any, st: dict[str, Any]) -> int:
    from app.services import shop_payments as pay

    return await pay.expire_unpaid_orders(db)


@case("shop_expire_unpaid (periodic)", task="shop_expire_unpaid", prepare=_prep_shop, run=_run_expire_unpaid)
async def _seed_expire_unpaid(db: Any, st: dict[str, Any], n: int) -> None:
    created = utcnow() - timedelta(days=2)
    orders = await put(db, ShopOrder, [{
        "order_number": f"NPJ{next(_seq):010d}", "gym_id": st["gym"].id, "customer_id": st["customer"],
        "customer_label": "Member", "status": OrderStatus.PENDING_PAYMENT, "fulfillment": "pickup",
        "subtotal": Decimal("999"), "total": Decimal("999"), "currency": "INR", "payment_provider": "test",
        "created_at": created} for _ in range(n)], ids=True)
    await put(db, ShopOrderItem, [{"order_id": o, "product_id": st["product"], "product_name": "Whey",
                                   "unit_price": Decimal("999"), "quantity": 1, "line_total": Decimal("999")}
                                  for o in orders])
    await db.commit()


# ---------------------------------------------------------------------------------------------------- payroll
async def _run_monthly_generation(db: Any, st: dict[str, Any]) -> int:
    from app.services import payroll as pr

    return await pr.run_monthly_generation(db, today=date(2026, 11, 1))


@case("payroll_monthly_generation: staff of one gym (periodic)", task="payroll_monthly_generation", prepare=_prep_payroll, run=_run_monthly_generation)
async def _seed_generation_staff(db: Any, st: dict[str, Any], n: int) -> None:
    await employees(db, st["gym"], n)


@case("payroll_monthly_generation: gyms (periodic)", task="payroll_monthly_generation", run=_run_monthly_generation)
async def _seed_generation_gyms(db: Any, st: dict[str, Any], n: int) -> None:
    for _ in range(n):
        await employees(db, await make_gym(db, await make_owner(db)), 1)


async def _run_owner_generation(db: Any, st: dict[str, Any]) -> int:
    from app.services import payroll as pr

    out = await pr.run_owner_generation(st["owner"].id, 2026, 10, state={"owner_id": st["owner"].id, "year": 2026,
                                                                          "month": 10})
    return int(out["created"])


@case("payroll_generate (job: the owner's 'run payroll')", task="payroll_generate", prepare=_prep_payroll, run=_run_owner_generation)
async def _seed_owner_generation(db: Any, st: dict[str, Any], n: int) -> None:
    await employees(db, st["gym"], n)


# ---------------------------------------------------------------------------------------------------- sweeps
async def _run_activation_sweep(db: Any, st: dict[str, Any]) -> int:
    from app.services import staffing as S

    S._sweep_after_id = 0  # noqa: SLF001 - the rotating cursor starts at the beginning
    queued = await S.staff_activation_sweep(db)
    await db.commit()
    return queued


@case("staff_activation_sweep (periodic)", task="staff_activation_sweep", prepare=_prep_payroll, run=_run_activation_sweep,
      patches=[("app.services.staffing", "_sweep_after_id", 0)])  # restored after the test
async def _seed_activation_sweep(db: Any, st: dict[str, Any], n: int) -> None:
    # the previous run's employments were queued: activate them, so the sweep sees only the new ones
    await db.execute(update(Employee).where(Employee.gym_id == st["gym"].id).values(status=EmploymentStatus.ACTIVE))
    await employees(db, st["gym"], n, status=EmploymentStatus.INVITED, verification=VerificationStatus.DRAFT)


async def _run_identity_sweep(db: Any, st: dict[str, Any]) -> int:
    from app.services import id_verification as idv

    done = await idv.identity_sweep(db)
    await db.commit()
    return done


async def _stuck_cards(db: Any, n: int, *, attempts: int) -> None:
    members = await users(db, n, id_status=IdStatus.PENDING)
    await put(db, IdentityDocument, [{
        "user_id": m, "doc_type": IdDocType.AADHAAR, "country_code": "IN", "front_key": f"private/id/{m:032x}.bin",
        "status": IdStatus.PENDING, "attempts": attempts, "updated_at": utcnow() - timedelta(hours=1)} for m in members])
    await db.commit()


@case("identity_sweep: re-queue stuck cards (periodic)", task="identity_sweep", run=_run_identity_sweep)
async def _seed_identity_sweep(db: Any, st: dict[str, Any], n: int) -> None:
    await _stuck_cards(db, n, attempts=0)


async def _run_identity_sweep_review(db: Any, st: dict[str, Any]) -> int:
    from app.services import id_verification as idv

    await idv.identity_sweep(db)
    await db.commit()
    return int(await db.scalar(select(func.count(IdentityDocument.id))
                               .where(IdentityDocument.status == IdStatus.NEEDS_REVIEW)) or 0) - st.setdefault("seen", 0)


@case("identity_sweep: cards sent to a person (periodic)", task="identity_sweep", run=_run_identity_sweep_review)
async def _seed_identity_review(db: Any, st: dict[str, Any], n: int) -> None:
    st["seen"] = int(await db.scalar(select(func.count(IdentityDocument.id))
                                     .where(IdentityDocument.status == IdStatus.NEEDS_REVIEW)) or 0)
    await _stuck_cards(db, n, attempts=5)


# ---------------------------------------------------------------------------------------------------- security
def _ai_verdicts(verdict: str, action: str) -> Callable[..., str]:
    def handler(**kw: Any) -> str:
        ips = re.findall(r'"ip":"([^"]+)"', kw["prompt"])
        return json.dumps({"ips": [{"ip": ip, "verdict": verdict, "recommended_action": action, "severity": 3,
                                    "rationale": "Seeded verdict."} for ip in ips]})

    return handler


async def _run_triage(db: Any, st: dict[str, Any]) -> int:
    from app.services import security_triage as sectri

    return await sectri._periodic_triage(db)  # noqa: SLF001 - the periodic itself


async def _seed_events(db: Any, st: dict[str, Any], n: int) -> None:
    k = next(_seq)
    await put(db, SecurityEvent, [{"ip": f"198.51.{k % 250}.{i + 1}", "method": "GET", "path": "/wp-login.php",
                                   "category": ThreatCategory.PROBE, "severity": 2, "detail": f"probe {k}-{i}"}
                                  for i in range(n)])
    await db.commit()


# one AI call takes at most MAX_IPS_PER_CALL addresses within PROMPT_BUDGET_CHARS (bounded batches by design): widen
# both so the 20 addresses of the large batch are ONE batch, like the 2 of the small one (the budget through a wrapper,
# never the function's __defaults__: a parameter added later must not take the value)
@dataclass(frozen=True)
class Lazy:
    """A patch value made when the test runs (from the module's attribute of that moment)."""

    factory: Callable[[], Any]


def _wide_prompt_budget() -> Any:
    from app.ai import triage

    return functools.partial(triage.build_prompt, budget=10**6)


ONE_AI_BATCH = [("app.ai.triage", "MAX_IPS_PER_CALL", 50), ("app.ai.triage", "build_prompt", Lazy(_wide_prompt_budget))]


@case("security_triage: AI says no action (periodic)", task="security_triage", run=_run_triage, patches=ONE_AI_BATCH,
      prepare=lambda db, st: st["fake_ai"](handler=_ai_verdicts("benign", "none")))
async def _seed_triage_benign(db: Any, st: dict[str, Any], n: int) -> None:
    await _seed_events(db, st, n)


@case("security_triage: AI extends bans (periodic)", task="security_triage", run=_run_triage, patches=ONE_AI_BATCH,
      prepare=lambda db, st: st["fake_ai"](handler=_ai_verdicts("malicious", "extend_ban")))
async def _seed_triage_ban(db: Any, st: dict[str, Any], n: int) -> None:
    await _seed_events(db, st, n)


async def _triage_counting(db: Any, st: dict[str, Any], where: Any) -> int:
    """Run the periodic triage; returns how many ip_bans rows match ``where`` now that did not before (the rows this
    run lifted / extended / placed)."""
    from app.services import security_triage as sectri

    before = int(await db.scalar(select(func.count(IpBan.id)).where(where)) or 0)
    await sectri._periodic_triage(db)  # noqa: SLF001 - the periodic itself
    db.expire_all()
    return int(await db.scalar(select(func.count(IpBan.id)).where(where)) or 0) - before


async def _banned_ips(db: Any, n: int, *, severity: int, until: Any, lifted: bool = False,
                      event_severity: int = 2) -> list[int]:
    """n addresses, each with an automatic ban naming an account (active unless ``lifted``) and one unreviewed event."""
    k = next(_seq)
    members = await users(db, n)
    ips = [f"198.20.{k % 250}.{i + 1}" for i in range(n)]
    now = utcnow()
    await put(db, IpBan, [{"ip": ip, "user_id": m, "reason": "Automated probe", "severity": severity,
                           "banned_until": until, "created_by": "auto", "offense_count": 1,
                           "created_at": now - timedelta(hours=1), "lifted_at": now - timedelta(minutes=30) if lifted else None}
                          for ip, m in zip(ips, members, strict=True)])
    await put(db, SecurityEvent, [{"ip": ip, "method": "GET", "path": "/wp-login.php", "category": ThreatCategory.PROBE,
                                   "severity": event_severity, "detail": f"probe {k}-{i}"} for i, ip in enumerate(ips)])
    await db.commit()
    return members


async def _run_triage_lifts(db: Any, st: dict[str, Any]) -> int:
    return await _triage_counting(db, st, IpBan.lifted_at.is_not(None))


@case("security_triage: AI lifts automatic bans (periodic)", task="security_triage", run=_run_triage_lifts,
      patches=ONE_AI_BATCH, prepare=lambda db, st: st["fake_ai"](handler=_ai_verdicts("benign", "lift_ban")))
async def _seed_triage_lifts(db: Any, st: dict[str, Any], n: int) -> None:
    await _banned_ips(db, n, severity=2, until=utcnow() + timedelta(hours=2))  # automatic, temporary, severity <= 3


async def _run_triage_extends(db: Any, st: dict[str, Any]) -> int:
    return await _triage_counting(db, st, IpBan.reason.contains("extended"))


@case("security_triage: AI extends active bans (periodic)", task="security_triage", run=_run_triage_extends,
      patches=ONE_AI_BATCH, prepare=lambda db, st: st["fake_ai"](handler=_ai_verdicts("malicious", "extend_ban")))
async def _seed_triage_extends(db: Any, st: dict[str, Any], n: int) -> None:
    await _banned_ips(db, n, severity=2, until=utcnow() + timedelta(minutes=1), event_severity=3)  # about to end


async def _run_triage_permanent(db: Any, st: dict[str, Any]) -> int:
    return await _triage_counting(db, st, IpBan.banned_until.is_(None))


async def _prep_superadmins(db: Any, st: dict[str, Any]) -> None:
    await users(db, 2, role=Role.SUPERADMIN, profile=False)  # they are told about every permanent ban
    st["fake_ai"](handler=_ai_verdicts("malicious", "extend_ban"))


@case("security_triage: repeat offenders banned permanently (periodic)", task="security_triage",
      run=_run_triage_permanent, patches=ONE_AI_BATCH, prepare=_prep_superadmins)
async def _seed_triage_permanent(db: Any, st: dict[str, Any], n: int) -> None:
    # an earlier ban (lifted) within 30 days: the next one of a non-subscriber is permanent - and the admins are told
    await _banned_ips(db, n, severity=3, until=utcnow() + timedelta(hours=1), lifted=True, event_severity=3)


async def _run_security_flush(db: Any, st: dict[str, Any]) -> int:
    from app.security import bans

    done = await bans.flush_security_queue(db)
    await db.commit()
    return done


@case("security flush: queued ban decisions (worker loop)", run=_run_security_flush)
async def _seed_security_flush(db: Any, st: dict[str, Any], n: int) -> None:
    from app.security import bans

    k = next(_seq)
    for i in range(n):
        bans.security_events.request_ban(bans.PendingBan(f"198.18.{k % 250}.{i + 1}", 3, "probe", None))


async def _run_ban_sync(db: Any, st: dict[str, Any]) -> int:
    from app.security import bans

    return await bans.sync_bans_from_db(db)


async def _prep_ban_sync(db: Any, st: dict[str, Any]) -> None:
    from app.security import bans

    await bans.sync_bans_from_db(db)  # the start-up full load: later runs are incremental


@case("ban sync (worker loop)", prepare=_prep_ban_sync, run=_run_ban_sync, at_least=True)
async def _seed_ban_sync(db: Any, st: dict[str, Any], n: int) -> None:
    k = next(_seq)
    await put(db, IpBan, [{"ip": f"198.19.{k % 250}.{i + 1}", "reason": "x", "severity": 3, "created_by": "auto",
                           "banned_until": utcnow() + timedelta(hours=2)} for i in range(n)])
    await db.commit()


async def _run_ai_logs(db: Any, st: dict[str, Any]) -> int:
    from app.ai.client import flush_ai_call_logs

    done = await flush_ai_call_logs(db)
    await db.commit()
    return done


@case("AI call log flush (worker loop)", run=_run_ai_logs)
async def _seed_ai_logs(db: Any, st: dict[str, Any], n: int) -> None:
    from app.ai.client import CallLog, call_log_queue

    for i in range(n):
        call_log_queue.append(CallLog("assistant", "fake", "fake-model", True, 10 + i, None, user_id=None))


async def _run_mail(db: Any, st: dict[str, Any]) -> int:
    from app.services.mailer import send_pending_emails

    done = await send_pending_emails(db, limit=25)
    await db.commit()
    return done


@case("e-mail outbox (worker loop)", run=_run_mail)
async def _seed_mail(db: Any, st: dict[str, Any], n: int) -> None:
    k = next(_seq)
    await put(db, EmailOutbox, [{"to_email": f"npj-mail-{k}-{i}@example.com", "subject": "Hi", "text_body": "Hello",
                                 "status": EmailStatus.QUEUED} for i in range(n)])
    await db.commit()


# ---------------------------------------------------------------------------------------------------- retention, purge
async def _run_retention(db: Any, st: dict[str, Any]) -> int:
    from app.services import retention

    report = await retention.run_retention(now=utcnow())
    return report.total


@case("data_retention: old rows of every rule (periodic)", task="data_retention", run=_run_retention)
async def _seed_retention(db: Any, st: dict[str, Any], n: int) -> None:
    now = utcnow()
    (user,) = await users(db, 1)
    await put(db, SecurityEvent, [{"ip": "198.51.100.1", "method": "GET", "path": "/x", "category": ThreatCategory.PROBE,
                                   "severity": 1, "created_at": now - timedelta(days=100)} for _ in range(n)])
    await put(db, AICallLog, [{"feature": "assistant", "provider": "fake", "model": "m", "ok": True, "latency_ms": 1,
                               "created_at": now - timedelta(days=40)} for _ in range(n)])
    await put(db, EmailOutbox, [{"to_email": "x@example.com", "subject": "s", "text_body": "t", "status": EmailStatus.SENT,
                                 "created_at": now - timedelta(days=40)} for _ in range(n)])
    await put(db, Notification, [{"user_id": user, "title": "t", "body": "b", "read_at": now - timedelta(days=99),
                                  "created_at": now - timedelta(days=100)} for _ in range(n)])
    await put(db, Job, [{"kind": "npj_noop", "status": JobStatus.DONE, "created_at": now - timedelta(days=21),
                         "finished_at": now - timedelta(days=20)} for _ in range(n)])
    await put(db, AuditLog, [{"action": "x.y", "entity_type": "user", "created_at": now - timedelta(days=400)}
                             for _ in range(n)])
    await db.commit()
    st["expect_factor"] = 6  # six retention rules got n rows each


async def _run_trim(db: Any, st: dict[str, Any]) -> int:
    from app.services import retention

    report = retention.RetentionReport()
    await retention.trim_notifications(3, report=report)
    return int(report.deleted.get("notifications_over_cap", 0)) // 2


@case("data_retention: notifications kept per person (periodic)", task="data_retention", run=_run_trim)
async def _seed_trim(db: Any, st: dict[str, Any], n: int) -> None:
    ids = await users(db, n)
    now = utcnow()
    await put(db, Notification, [{"user_id": u, "title": f"t{i}", "body": "b", "created_at": now - timedelta(minutes=i)}
                                 for u in ids for i in range(5)])  # 5 each, 3 kept: 2 per person go
    await db.commit()


async def _run_purge(db: Any, st: dict[str, Any]) -> int:
    from app.services import accounts

    return int((await accounts.purge_due_accounts(db))["users"])


@case("purge_deleted_accounts (periodic)", task="purge_deleted_accounts", run=_run_purge)
async def _seed_purge(db: Any, st: dict[str, Any], n: int) -> None:
    past = utcnow() - timedelta(minutes=1)
    await users(db, n, deleted_at=past, purge_after=past, is_active=False, photo_key=None)


async def _run_purge_employees(db: Any, st: dict[str, Any]) -> int:
    from app.services import accounts

    return int((await accounts.purge_due_accounts(db))["employees"])


@case("purge_deleted_accounts: employee records (periodic)", task="purge_deleted_accounts", prepare=_prep_payroll,
      run=_run_purge_employees)
async def _seed_purge_employees(db: Any, st: dict[str, Any], n: int) -> None:
    gym, past = st["gym"], utcnow() - timedelta(minutes=1)
    emps = await employees(db, gym, n, status=EmploymentStatus.TERMINATED)
    await db.execute(update(Employee).where(Employee.id.in_(emps)).values(deleted_at=past, purge_after=past))
    start = date(2024, 1, 1)
    end = date(2024, 1, 31)
    await put(db, SalaryStatement, [{  # salary settled: nothing owed, the record may go
        "employee_id": e, "gym_id": gym.id, "period_start": start, "period_end": end, "days_in_period": 31,
        "days_worked": 31, "monthly_salary": Decimal("30000"), "gross_pay": Decimal("30000"), "net_pay": Decimal("28000"),
        "currency": "INR", "country_code": "IN", "status": StatementStatus.PAID, "due_date": end,
        "paid_at": utcnow()} for e in emps])
    await put(db, Transaction, [{"gym_id": gym.id, "direction": TxDirection.OUTGOING, "category": TxCategory.SALARY,
                                 "status": TxStatus.SUCCESS, "amount": Decimal("28000"), "currency": "INR",
                                 "description": "Salary", "provider": "test", "employee_id": e} for e in emps])
    await db.commit()


@case("purge_deleted_accounts: gym owners (periodic)", task="purge_deleted_accounts", run=_run_purge)
async def _seed_purge_owners(db: Any, st: dict[str, Any], n: int) -> None:
    past = utcnow() - timedelta(minutes=1)
    for _ in range(n):  # an owner's whole gym cascades with them: each owner is purged in a transaction of its own
        owner = await make_owner(db)
        await make_gym(db, owner)
        await db.execute(update(User).where(User.id == owner.id).values(deleted_at=past, purge_after=past,
                                                                        is_active=False))
    await db.commit()


# ---------------------------------------------------------------------------------------------------- shop
async def _run_low_stock(db: Any, st: dict[str, Any]) -> int:
    from app.services import shop as shop_service

    return await shop_service.run_low_stock_digests(now=utcnow())


async def _low_products(db: Any, gym_id: int, n: int) -> None:
    await put(db, Product, [{"gym_id": gym_id, "name": f"Low {i}", "slug": f"low-{next(_seq)}",
                             "category": ProductCategory.SUPPLEMENTS, "description": "x", "price": Decimal("999"),
                             "currency": "INR", "stock": 1, "low_stock_threshold": 3, "is_active": True}
                            for i in range(n)])
    await db.commit()


@case("shop_low_stock_digest: owners (periodic)", task="shop_low_stock_digest", run=_run_low_stock)
async def _seed_low_stock_owners(db: Any, st: dict[str, Any], n: int) -> None:
    for _ in range(n):
        await _low_products(db, (await make_gym(db, await make_owner(db))).id, 1)


async def _run_low_stock_one(db: Any, st: dict[str, Any]) -> int:
    """The products the ONE digest sent names: its body counts them ("<n> products are running low: ...")."""
    from app.services import shop as shop_service

    sent = await shop_service.run_low_stock_digests(now=utcnow())
    if sent != 1:
        return -sent
    body = await db.scalar(select(Notification.body).where(Notification.user_id == st["owner_id"],
                                                           Notification.title == shop_service.LOW_STOCK_TITLE)
                           .order_by(Notification.id.desc()).limit(1))
    found = re.match(r"(\d+) products? (?:are|is) running low", body or "")
    return int(found.group(1)) if found else -1


@case("shop_low_stock_digest: products of one owner (periodic)", task="shop_low_stock_digest", run=_run_low_stock_one)
async def _seed_low_stock_products(db: Any, st: dict[str, Any], n: int) -> None:
    owner = await make_owner(db)  # a new owner: not throttled yet
    await _low_products(db, (await make_gym(db, owner)).id, n)
    st["owner_id"] = owner.id


async def _run_ratings(db: Any, st: dict[str, Any]) -> int:
    from app.services import shop as shop_service

    return await shop_service.backfill_product_ratings(budget_s=None)


@case("shop product ratings reconcile (periodic)", task="shop_product_ratings", prepare=_prep_shop, run=_run_ratings)
async def _seed_ratings(db: Any, st: dict[str, Any], n: int) -> None:
    products = await put(db, Product, [{"gym_id": st["gym"].id, "name": f"Rated {i}", "slug": f"rated-{next(_seq)}",
                                        "category": ProductCategory.SUPPLEMENTS, "description": "x",
                                        "price": Decimal("999"), "currency": "INR", "stock": 5} for i in range(n)],
                         ids=True)
    await put(db, ProductReview, [{"product_id": p, "user_id": st["customer"], "rating": 4} for p in products])
    await db.commit()  # written past the flush hook (a raw INSERT): the stored summaries are stale


async def _run_nav(db: Any, st: dict[str, Any]) -> int:
    from app.core import nav

    await nav.refresh_nav_state(db)
    return nav.nav_state.open_jobs


@case("refresh_nav_state (periodic)", task="refresh_nav_state", prepare=_prep_payroll, run=_run_nav, at_least=True)
async def _seed_nav(db: Any, st: dict[str, Any], n: int) -> None:
    await put(db, JobPosting, [{"gym_id": st["gym"].id, "title": f"Coach {i}", "role": EmployeeRole.TRAINER,
                                "employment_type": EmploymentType.FULL_TIME, "location": "Bengaluru", "currency": "INR",
                                "requirements": "x", "is_open": True} for i in range(n)])
    await db.commit()


# ---------------------------------------------------------------------------------------------------- governance
async def _prep_governance(db: Any, st: dict[str, Any]) -> None:
    """A gym the platform suspended (its settlement desk works only then) and the super-admin who confirmed it."""
    from app.models import Gym

    st["gym"], st["plans"] = await gym_and_plans(db)
    (st["admin"],) = await users(db, 1, role=Role.SUPERADMIN, profile=False)
    (st["product"],) = await put(db, Product, [{
        "gym_id": st["gym"].id, "name": "Whey", "slug": f"whey-{next(_seq)}", "category": ProductCategory.SUPPLEMENTS,
        "description": "x", "price": Decimal("999"), "currency": "INR", "stock": 50}], ids=True)
    await db.execute(update(Gym).where(Gym.id == st["gym"].id)
                     .values(is_active=False, suspended_at=utcnow(), suspension_kind="deletion"))
    await db.commit()


async def _counted(db: Any, stmt: Any) -> int:
    n = int(await db.scalar(stmt) or 0)
    await db.commit()
    return n


async def _run_governance_refunds(db: Any, st: dict[str, Any]) -> int:
    from app.services import governance as G

    refunds = select(func.count(Transaction.id)).where(Transaction.category == TxCategory.REFUND)
    before = await _counted(db, refunds)
    await G._refunds_job(db, {"gym_id": st["gym"].id, "actor_id": st["admin"], "as_of": date.today().isoformat()})
    await db.commit()
    return await _counted(db, refunds) - before


@case("governance_refunds: memberships of a suspended gym (job)", task="governance_refunds", prepare=_prep_governance,
      run=_run_governance_refunds)
async def _seed_governance_refunds(db: Any, st: dict[str, Any], n: int) -> None:
    members = await users(db, n)
    plan = st["plans"]["online"]
    subs = await subscriptions(db, st["gym"], plan, members, ends_on=date.today() + timedelta(days=20))
    await put(db, Transaction, [{"gym_id": st["gym"].id, "direction": TxDirection.INCOMING,
                                 "category": TxCategory.SUBSCRIPTION, "status": TxStatus.SUCCESS, "amount": plan.price,
                                 "currency": "INR", "description": "x", "provider": "test", "subscription_id": s,
                                 "customer_id": m} for s, m in zip(subs, members, strict=True)])
    await db.commit()


async def _run_governance_orders(db: Any, st: dict[str, Any]) -> int:
    from app.services import governance as G

    closed = select(func.count(ShopOrder.id)).where(ShopOrder.gym_id == st["gym"].id,
                                                   ShopOrder.status == OrderStatus.CANCELLED)
    before = await _counted(db, closed)
    await G._orders_job(db, {"gym_id": st["gym"].id, "actor_id": st["admin"]})
    await db.commit()
    return await _counted(db, closed) - before


@case("governance_orders: open orders of a suspended gym (job)", task="governance_orders", prepare=_prep_governance,
      run=_run_governance_orders)
async def _seed_governance_orders(db: Any, st: dict[str, Any], n: int) -> None:
    (customer,) = await users(db, 1)
    orders = await put(db, ShopOrder, [{
        "order_number": f"NPG{next(_seq):010d}", "gym_id": st["gym"].id, "customer_id": customer,
        "customer_label": "Member", "status": OrderStatus.PAID, "fulfillment": "pickup", "subtotal": Decimal("999"),
        "total": Decimal("999"), "currency": "INR", "payment_provider": "test", "paid_at": utcnow()}
        for _ in range(n)], ids=True)
    await put(db, ShopOrderItem, [{"order_id": o, "product_id": st["product"], "product_name": "Whey",
                                   "unit_price": Decimal("999"), "quantity": 1, "line_total": Decimal("999")}
                                  for o in orders])
    await db.commit()


# ==================================================================================================== the test
def test_every_periodic_task_and_job_handler_is_measured_or_explained():
    """A new ``@periodic`` task or ``@job_handler`` is measured here - or explained in the allowlist's
    ``jobs_not_batched`` (it handles one entity, never a batch of rows) - the day it is registered."""
    import app.main  # noqa: F401 - registers every router's periodic tasks and job handlers
    import app.services.retention  # noqa: F401 - registered by the worker at start-up
    from app.services.jobs import registered_handlers, registered_periodic

    # the app's own work only: other test modules register throw-away handlers (``test_job``) in the same process
    work = {name for name, fn in registered_handlers().items() if getattr(fn, "__module__", "").startswith("app.")}
    work |= {name for name, t in registered_periodic().items() if getattr(t.fn, "__module__", "").startswith("app.")}
    assert len(work) > 15, f"almost no background work registered - is app.main still importing it? {sorted(work)}"
    measured = {c.task for c in CASES if c.task}
    explained = json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8")).get("jobs_not_batched", {})
    assert all(isinstance(r, str) and len(r) >= 10 for r in explained.values()), "every entry needs a reason"
    missing = sorted(work - measured - set(explained))
    assert not missing, (f"background work without a 2-vs-20 measurement in tests/test_no_n_plus_one_jobs.py or a "
                         f"reason in jobs_not_batched: {missing}")
    gone = sorted((measured | set(explained)) - work)
    assert not gone, f"cases / jobs_not_batched entries for background work that is not registered any more: {gone}"


@pytest.mark.parametrize("c", CASES, ids=lambda c: c.name.split(" (")[0])
async def test_statements_per_batch_do_not_grow_with_its_rows(db, c: Case, monkeypatch, fake_ai):
    import importlib

    for module, attr, value in c.patches:
        target = importlib.import_module(module)
        *owners, name = attr.split(".")
        for part in owners:  # "Class.attr": an attribute of an object of the module
            target = getattr(target, part)
        monkeypatch.setattr(target, name, value.factory() if isinstance(value, Lazy) else value)
    st: dict[str, Any] = {"fake_ai": fake_ai}
    if c.prepare is not None:
        out = c.prepare(db, st)
        if hasattr(out, "__await__"):
            await out
        await db.commit()
    counts: dict[int, tuple[int, list[str]]] = {}
    for n in (SMALL, LARGE):
        await c.seed(db, st, n)
        with Statements() as sql, QueryCounter() as qc:
            handled = await c.run(db, st)
        await db.commit()
        expected = n * int(st.get("expect_factor", 1))
        assert (handled >= expected) if c.at_least else (handled == expected), (
            f"{c.name}: the run handled {handled} rows, expected {expected} - the measurement would be vacuous")
        counts[n] = (qc.count, sql.items)
    (small, small_sql), (large, large_sql) = counts[SMALL], counts[LARGE]
    extra, reason, shapes = job_allowance(c.name.split(" (")[0])
    grown = shape_growth(small_sql, large_sql)
    outside = {s: d for s, d in grown.items() if not (extra and any(s.startswith(p) for p in shapes))}
    if os.environ.get("NPLUS_REPORT"):  # every measurement as JSON (key "jobs")
        out = Path(os.environ["NPLUS_REPORT"])
        data = json.loads(out.read_text()) if out.exists() else {}
        data.setdefault("jobs", {})[c.name] = {"task": c.task, "small_rows": SMALL, "large_rows": LARGE, "small": small,
                                               "large": large, "allowed_extra": extra, "allowed_reason": reason,
                                               "allowed_shapes": shapes, "repeats": repeats(small_sql, large_sql)}
        out.write_text(json.dumps(data, indent=2, default=str))
    assert large <= small + extra and not outside, (
        f"N+1 in background work: {c.name} ran {small} statements for a batch of {SMALL} rows and {large} for "
        f"{LARGE} rows{f' (allowed: +{extra} of the shapes {shapes}, {reason})' if reason else ''}. Statements that "
        "repeat (normalised):\n"
        + "\n".join([f"+{d} x {s[:240]}" for s, d in sorted(outside.items(), key=lambda x: -x[1])]
                    or repeats(small_sql, large_sql) or ["(none repeats - new statements appeared)"]))
