"""The seeded world of the N+1 checks (tests/test_no_n_plus_one.py).

``build`` makes a SMALL world: one gym brand of the owner with a published branch, a trainer (staff account), a member
(customer) of that gym, the super-admin and one other gym - with ONE OR TWO rows of every collection one of those viewers
can see (memberships, plans, products, orders and their items, reviews, wish list, bag, notifications, conversations
and their messages, tasks, health metrics / conditions / alerts, the fitness profile, staff, customers, payroll,
transactions, job postings and enquiries, audit and security events, bans, mail, jobs, trust and safety rows ...).

``grow`` then adds ``n`` more rows to EVERY one of those collections - the SAME viewers' data, nested collections
included: items of the member's order, messages of their conversation, branches of the gym, clients of the trainer,
reviews and photos of the product, enquiries of the job posting, events of the workout session, runs and documents of
the branch / employee (the ones in the super-admin's review too), bans, accounts and events of the reviewed IP address,
trainers on shift at the branch, the trainer's due statements (settlement) ... Conversations whose messages grow are
UNREAD again, so the first view's mark-as-read work is measured on the large world as well. Nested sections a detail
page shows are non-empty in the small world too (no "empty section" step). Per-viewer caps are respected (an owner runs
at most 20 gyms, a branch has at most 12 photos and 20 documents, a product 8 photos ...), so a page never sees more
than the product allows.

Every table is written with ONE multi-row INSERT per size (no factories in loops). A column a later change makes
NOT NULL without a default gets a neutral value (``fill``), so the world keeps seeding while the app evolves."""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import date, time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import ARRAY, JSON, Boolean, Date, DateTime, Float, Integer, Numeric, String, Text, Time, func, insert, select, update
from sqlalchemy import Enum as SAEnum

from app.core.utils import utcnow
from app.models import (
    AccountBan,
    AICallLog,
    AssistantMessage,
    AuditLog,
    Branch,
    BranchDocument,
    BranchFeature,
    BranchHours,
    BranchPhoto,
    CartItem,
    ChatMessage,
    ChatThread,
    Coupon,
    CustomerProfile,
    EmailOutbox,
    Employee,
    EmployeeDocument,
    Exercise,
    FitnessProfile,
    Gym,
    HealthAlert,
    HealthCondition,
    IdentityDocument,
    InventoryMovement,
    IpBan,
    Job,
    JobEnquiry,
    JobPosting,
    MetricEntry,
    MisconductReport,
    Notification,
    Plan,
    Product,
    ProductImage,
    ProductReview,
    Refund,
    SalaryStatement,
    SecurityEvent,
    ShopOrder,
    ShopOrderItem,
    StaffViolation,
    Subscription,
    SuspensionEvent,
    Task,
    TrainerClient,
    TrainerShift,
    Transaction,
    TrustIpAction,
    BlockedIdentity,
    User,
    VerificationRun,
    Video,
    WellnessCheck,
    WishlistItem,
    WorkoutPlan,
    WorkoutSession,
    WorkoutSessionEvent,
)
from app.models.enums import (
    AnomalyLevel,
    BranchDocType,
    ConditionKind,
    EmailStatus,
    EmployeeRole,
    EmploymentStatus,
    EmploymentType,
    EnquiryStatus,
    Goal,
    IdDocType,
    IdStatus,
    JobStatus,
    NotificationCategory,
    PhotoKind,
    PlanMode,
    PurchaseChannel,
    ReportStatus,
    Role,
    Severity,
    ShiftMode,
    StatementStatus,
    StressLevel,
    SubscriptionStatus,
    TaskKind,
    ThreadStatus,
    ThreatCategory,
    TxCategory,
    TxDirection,
    TxStatus,
    VerificationStatus,
    WorkMode,
)
from app.models.shop import OrderStatus, ProductCategory, RefundStatus

_seq = itertools.count(1)

PLAN_MATRIX = [  # (code, mode, trainer, ai, price) - the 12 plan codes of a gym
    ("combo_trainer_ai", PlanMode.COMBO, True, True, "3999"), ("online", PlanMode.ONLINE, False, False, "799"),
    ("offline", PlanMode.OFFLINE, False, False, "1499"), ("offline_trainer", PlanMode.OFFLINE, True, False, "2999"),
    ("offline_ai", PlanMode.OFFLINE, False, True, "1999"), ("offline_trainer_ai", PlanMode.OFFLINE, True, True, "3499"),
    ("online_trainer", PlanMode.ONLINE, True, False, "1999"), ("online_ai", PlanMode.ONLINE, False, True, "1199"),
    ("online_trainer_ai", PlanMode.ONLINE, True, True, "2499"), ("combo", PlanMode.COMBO, False, False, "1999"),
    ("combo_trainer", PlanMode.COMBO, True, False, "3499"), ("combo_ai", PlanMode.COMBO, False, True, "2499"),
]
SMALL_PLANS = 2  # the first two codes above; ``grow`` adds the other ten
CONDITION_NAMES = ["Asthma", "Hypertension", "Knee injury", "Lower back pain", "Diabetes", "Shoulder injury",
                   "Peanut allergy", "Metformin", "Migraine", "Ankle sprain"]
LIKES = ["strength", "calisthenics", "walking", "hiking", "cycling", "swimming", "yoga", "pilates", "hiit", "crossfit",
         "dance", "martial_arts", "rowing", "stretching", "cardio_machines", "team_sports", "climbing", "running",
         "jumping"]
GOALS = ["lose_weight", "build_muscle", "get_stronger", "endurance", "flexibility", "general_fitness", "tone_up",
         "sport", "rehab", "stress"]
EQUIPMENT = ["none", "full_gym", "dumbbells", "barbell", "kettlebells", "resistance_bands", "pull_up_bar", "bench",
             "mat", "jump_rope", "treadmill", "exercise_bike", "rower", "elliptical", "cable_machine", "machines",
             "suspension", "medicine_ball", "foam_roller", "step_box"]
CITIES = [("Bengaluru", 12.9716, 77.6412), ("Bengaluru", 12.9352, 77.6245), ("Mysuru", 12.2958, 76.6394),
          ("Chennai", 13.0827, 80.2707), ("Hyderabad", 17.3850, 78.4867)]


# ==================================================================================================== bulk inserts
def _neutral(col: Any) -> Any:
    """A harmless value for a NOT NULL column the world does not know about (added by a later change)."""
    t = col.type
    if isinstance(t, SAEnum):
        return next(iter(t.enum_class)) if t.enum_class is not None else t.enums[0]
    if isinstance(t, ARRAY):
        return []
    if isinstance(t, JSON):
        return {}
    if isinstance(t, Boolean):
        return False
    if isinstance(t, DateTime):
        return utcnow()
    if isinstance(t, Date):
        return date.today()
    if isinstance(t, Time):
        return time(6, 0)
    if isinstance(t, Float):
        return 1.0
    if isinstance(t, Numeric):
        return Decimal("1")
    if isinstance(t, Integer):
        return 1
    if isinstance(t, (String, Text)):
        return f"np{next(_seq)}"[: (getattr(t, "length", None) or 40)]
    raise TypeError(f"{col.table.name}.{col.name}: no neutral value for {t!r} - set it in tests/nplus_world.py")


def fill(model: Any, row: dict[str, Any]) -> dict[str, Any]:
    """``row`` plus a neutral value for every NOT NULL column without a default that it leaves out."""
    for col in model.__table__.columns:
        if col.key in row or col.nullable or col.default is not None or col.server_default is not None:
            continue
        if col.computed is not None or (col.primary_key and col.autoincrement in (True, "auto")
                                        and isinstance(col.type, Integer)):
            continue
        if col.foreign_keys:
            raise ValueError(f"{model.__tablename__}.{col.key}: a required foreign key tests/nplus_world.py does not set")
        row[col.key] = _neutral(col)
    return row


async def put(db: Any, model: Any, rows: list[dict[str, Any]], *, ids: bool = False) -> list[int]:
    """One bulk INSERT of ``rows`` (the ids in order when ``ids``)."""
    if not rows:
        return []
    keys = set(rows[0])
    if any(set(r) != keys for r in rows):  # one parameter-set shape = one statement (and no NULL for a default)
        raise ValueError(f"{model.__tablename__}: rows of one put() must name the same columns")
    rows = [fill(model, dict(r)) for r in rows]
    if ids:
        res = await db.execute(insert(model).returning(model.id, sort_by_parameter_order=True), rows)
        return list(res.scalars())
    await db.execute(insert(model), rows)
    return []


# ==================================================================================================== the world
@dataclass
class World:
    """Ids of the rows the crawler's path parameters point at (the same rows in the small and the large world)."""

    password_hash: str
    admin: int = 0
    owner: int = 0
    other_owner: int = 0
    member: int = 0
    member2: int = 0  # a second member of the gym (a closed conversation with the trainer)
    trainer: int = 0  # the trainer's user account
    gym: int = 0
    gym_slug: str = ""
    gym2: int = 0  # the owner's second gym (pages with a gym chooser: /owner/staff/new, /owner/jobs/new)
    other_gym: int = 0
    other_gym_slug: str = ""
    branch: int = 0  # the gym's published branch
    branch2: int = 0  # ... and a second one (a branch page lists the other branches)
    review_branch: int = 0  # a branch of the gym waiting for the super-admin's review
    employee: int = 0  # the trainer's (active) employment at the gym
    review_employee: int = 0  # an employment waiting for the super-admin's review
    invited_employee: int = 0  # an invitation nobody accepted yet (/auth/staff-invite)
    plans: dict[str, int] = field(default_factory=dict)
    subscription: int = 0  # the member's active membership (with the trainer)
    pending_subscription: int = 0  # ... and one waiting for payment (/checkout/{sid})
    product: int = 0
    product_slug: str = ""
    products: list[int] = field(default_factory=list)  # every product of the gym (the product first)
    order: int = 0  # the member's completed order (its items grow)
    pending_order: int = 0  # ... and one waiting for payment
    thread: int = 0  # the member <-> the gym's admin team (its messages grow)
    staff_thread: int = 0  # the member <-> the trainer
    report: int = 0  # the member's misconduct report about the trainer
    statement: int = 0  # the trainer's salary statement
    workout_session: int = 0  # the member's live workout (its events grow)
    workout_plan: int = 0
    wellness_check: int = 0
    metric_entry: int = 0
    job_posting: int = 0  # the gym's open job posting (its enquiries grow)
    enquiry: int = 0
    identity_doc: int = 0  # the member's ID card, registered by the owner, waiting for a review
    employee_doc: int = 0
    branch_doc: int = 0
    review_branch_doc: int = 0
    review_employee_doc: int = 0
    email: int = 0
    payroll_job: int = 0  # the owner's "run payroll" job
    ip: str = "203.0.113.77"  # the address of the security events
    coach_job: int = 0  # the member's AI plan request (/me/coach/status?job=)
    tokens: dict[str, str] = field(default_factory=dict)  # raw one-time tokens by purpose (/auth/reset?token= ...)
    checkout_orders: dict[str, str] = field(default_factory=dict)  # gateway order id of the pending membership
    video_slug: str = ""
    members: list[int] = field(default_factory=list)  # other members of the gym (grow)
    sizes: list[int] = field(default_factory=list)  # n of every grow() so far

    @property
    def users(self) -> dict[str, int]:
        return {"superadmin": self.admin, "owner": self.owner, "staff": self.trainer, "member": self.member}


def _user(w: World, role: Role, name: str, *, k: int | None = None, **extra: Any) -> dict[str, Any]:
    k = next(_seq) if k is None else k
    city, lat, lon = CITIES[k % 2]
    row = {"email": f"np{k}-{role.value}@example.com", "full_name": name, "role": role,
           "password_hash": w.password_hash, "id_status": IdStatus.VERIFIED, "email_verified_at": utcnow(),
           "date_of_birth": date(1992, 3, 1) + timedelta(days=k % 900), "timezone": "Asia/Kolkata",
           "country_code": "IN", "city": city, "latitude": lat, "longitude": lon,
           "photo_key": f"public/avatars/{k:032x}.jpg" if role in (Role.OWNER, Role.STAFF) else None}
    row.update(extra)
    return row


def _gym(owner: int, name: str) -> dict[str, Any]:
    k = next(_seq)
    return {"owner_id": owner, "name": name, "slug": f"np-gym-{k}", "tagline": "Strength for everyone",
            "description": "A seeded gym.", "country_code": "IN", "currency": "INR", "online_enabled": True,
            "offline_enabled": True, "online_open": True, "is_active": True, "contact_email": f"gym{k}@example.com"}


def _branch(gym: int, name: str, *, status: VerificationStatus = VerificationStatus.VERIFIED,
            city: int = 0) -> dict[str, Any]:
    k = next(_seq)
    city, lat, lon = CITIES[city % len(CITIES)]
    return {"gym_id": gym, "name": name, "address_line1": f"{k} 100 Feet Road", "city": city, "country_code": "IN",
            "timezone": "Asia/Kolkata", "latitude": lat + (k % 7) * 0.001, "longitude": lon,
            "verification_status": status, "registration_number": f"REG{k:06d}",
            "verified_at": utcnow() if status == VerificationStatus.VERIFIED else None, "is_open": True}


async def _branches(db: Any, rows: list[dict[str, Any]], *, photos: int = 3) -> list[int]:
    """Branches with their opening hours and ``photos`` photos (published: verified, open, >= 3 photos)."""
    ids = await put(db, Branch, rows, ids=True)
    await put(db, BranchHours, [{"branch_id": b, "weekday": wd, "opens_at": time(6), "closes_at": time(20)}
                                for b in ids for wd in range(7)])
    await put(db, BranchPhoto, [{"branch_id": b, "image_key": f"public/branch/{next(_seq):032x}.jpg", "sort_order": i,
                                 "kind": PhotoKind.MACHINES if i == 0 else PhotoKind.OVERALL, "size_bytes": 1000}
                                for b in ids for i in range(photos)])
    return ids


def _plans(gym: int, codes: list[tuple[str, PlanMode, bool, bool, str]], start: int = 0) -> list[dict[str, Any]]:
    return [{"gym_id": gym, "code": code, "name": code.replace("_", " + ").title(), "mode": mode,
             "includes_trainer": trainer, "includes_ai": ai_, "price": Decimal(price), "currency": "INR",
             "duration_days": 30, "sort_order": start + i, "description": "Seeded plan."}
            for i, (code, mode, trainer, ai_, price) in enumerate(codes)]


def _employee(gym: int, user: int | None, name: str, *, role: EmployeeRole = EmployeeRole.TRAINER,
              status: EmploymentStatus = EmploymentStatus.ACTIVE,
              verification: VerificationStatus = VerificationStatus.VERIFIED, k: int | None = None) -> dict[str, Any]:
    k = next(_seq) if k is None else k
    return {"gym_id": gym, "user_id": user, "invite_email": f"np{k}-emp@example.com", "full_name": name, "role": role,
            "is_trainer": role == EmployeeRole.TRAINER, "work_mode": WorkMode.BOTH,
            "employment_type": EmploymentType.FULL_TIME, "country_code": "IN", "currency": "INR",
            "monthly_salary": Decimal(30000 + 100 * (k % 50)), "hire_date": date.today() - timedelta(days=400 + k % 30),
            "status": status, "photo_key": f"public/staff/{k:032x}.jpg", "is_public": True, "bio": "Seeded coach.",
            "years_experience": 3 + k % 10, "verification_status": verification,
            "verified_at": utcnow() if verification == VerificationStatus.VERIFIED else None,
            "id_status": IdStatus.VERIFIED, "specialties": ["strength"]}


def _subscription(customer: int, gym: int, plan: int, *, branch: int | None = None, trainer: int | None = None,
                  status: SubscriptionStatus = SubscriptionStatus.ACTIVE, days_ago: int = 10,
                  price: str = "3999") -> dict[str, Any]:
    today = date.today()
    starts = today - timedelta(days=days_ago)
    return {"customer_id": customer, "gym_id": gym, "plan_id": plan, "branch_id": branch, "trainer_id": trainer,
            "status": status, "channel": PurchaseChannel.ONLINE_CHECKOUT, "price": Decimal(price), "currency": "INR",
            "starts_on": starts, "ends_on": starts + timedelta(days=30),
            "activated_at": utcnow() - timedelta(days=days_ago) if status != SubscriptionStatus.PENDING_PAYMENT else None}


def _note(user: int, i: int) -> dict[str, Any]:
    cats = list(NotificationCategory)
    return {"user_id": user, "title": f"Notice {i}", "body": "Your plan renews soon.", "category": cats[i % len(cats)],
            "severity": Severity.INFO, "link": "/account" if i % 2 else None, "read_at": utcnow() if i % 3 == 0 else None,
            "emailed": True, "created_at": utcnow() - timedelta(minutes=5 + i)}


def _task(user: int, i: int) -> dict[str, Any]:
    # the small world's two tasks are an hour before and after now (one is always in today's / this month's calendar,
    # whatever the time of day); the others spread over -4 .. +5 days
    due = utcnow() + (timedelta(hours=(-1, 1)[i]) if i < 2 else timedelta(hours=(i % 40) * 6 - 100))
    return {"user_id": user, "title": f"Task {i}", "notes": "Seeded task.", "kind": TaskKind.REMINDER if i % 2 else TaskKind.TASK,
            "due_at": due, "remind_at": due - timedelta(minutes=30) if i % 2 else None, "created_by": "user",
            "created_at": utcnow() - timedelta(days=2, minutes=i)}


def _product(gym: int, i: int, branch: int | None) -> dict[str, Any]:
    k = next(_seq)
    return {"gym_id": gym, "name": f"Whey Protein {k}", "slug": f"whey-protein-{k}",
            "category": ProductCategory.SUPPLEMENTS if i % 2 == 0 else ProductCategory.ACCESSORIES, "brand": "PulseLabs",
            "description": "Seeded product for daily training.", "price": Decimal(499 + 10 * (i % 40)), "currency": "INR",
            "tax_rate": Decimal("18"), "stock": 25, "low_stock_threshold": 5, "sku": f"SKU{k}", "is_active": True,
            "is_supplement": i % 2 == 0, "fulfillment": "both", "pickup_branch_id": branch,
            "created_at": utcnow() - timedelta(hours=i)}


def _order(w: World, customer: int, i: int, status: OrderStatus, *, gym: int | None = None,
           branch: int | None = None) -> dict[str, Any]:
    k = next(_seq)
    created = utcnow() - timedelta(days=1 + i % 20, minutes=k % 60)
    paid = status not in (OrderStatus.PENDING_PAYMENT, OrderStatus.CANCELLED, OrderStatus.PAYMENT_FAILED)
    return {"order_number": f"NP{k:08d}", "gym_id": gym or w.gym, "customer_id": customer,
            "customer_label": f"Member {customer}", "status": status, "fulfillment": "pickup",
            "pickup_branch_id": branch or w.branch, "subtotal": Decimal("999"), "total": Decimal("999"), "currency": "INR",
            "payment_provider": "test", "created_at": created, "paid_at": created if paid else None,
            "completed_at": created if status == OrderStatus.COMPLETED else None}


def _item(order: int, product: int, i: int) -> dict[str, Any]:
    return {"order_id": order, "product_id": product, "product_name": f"Seeded product {i}",
            "unit_price": Decimal("999"), "tax_rate": Decimal("18"), "quantity": 1, "line_total": Decimal("999")}


def _thread(gym: int, customer: int, employee: int | None, i: int) -> dict[str, Any]:
    return {"gym_id": gym, "customer_id": customer, "employee_id": employee, "subject": f"Question {i}",
            "status": ThreadStatus.CLOSED if i % 4 == 3 else ThreadStatus.OPEN,
            "last_message_at": utcnow() - timedelta(minutes=10 + i),
            "last_message_preview": "See you tomorrow", "customer_unread": i % 2, "staff_unread": (i + 1) % 2}


def _message(thread: int, sender: int, role: str, i: int) -> dict[str, Any]:
    return {"thread_id": thread, "sender_id": sender, "sender_role": role, "body": f"Hello {i}!",
            "created_at": utcnow() - timedelta(minutes=500 - i)}


def _metric(customer: int, days_ago: int) -> dict[str, Any]:
    h, wt = 172.0, 70.0 + (days_ago % 5) * 0.4
    return {"customer_id": customer, "recorded_on": date.today() - timedelta(days=days_ago), "height_cm": h,
            "weight_kg": wt, "bmi": round(wt / (h / 100) ** 2, 1), "source": "manual", "confirmed": True,
            "anomaly_level": AnomalyLevel.NONE}


def _alert(customer: int, i: int) -> dict[str, Any]:
    return {"customer_id": customer, "level": AnomalyLevel.WARNING if i % 2 else AnomalyLevel.INFO,
            "title": f"Weight changed {i}", "message": "Your weight changed quickly.", "recommendation": "Talk to a doctor.",
            "source": "rule", "created_at": utcnow() - timedelta(hours=i)}


def _condition(customer: int, i: int) -> dict[str, Any]:
    kinds = list(ConditionKind)
    base = CONDITION_NAMES[i % len(CONDITION_NAMES)]
    return {"customer_id": customer, "kind": kinds[i % len(kinds)], "name": f"{base} {i}" if i >= len(CONDITION_NAMES) else base,
            "tag": ["asthma", "hypertension", "knee_injury", "lower_back", None][i % 5], "notes": "Seeded.",
            "source": "self"}


def _wellness(customer: int, i: int) -> dict[str, Any]:
    return {"customer_id": customer, "pss_answers": [1, 2, 1, 2], "pss_score": 6, "symptoms": ["tired"],
            "stress_level": StressLevel.MODERATE, "recommendations": "Sleep 8 hours.", "ai_provider": "simulated",
            "created_at": utcnow() - timedelta(days=i)}


WORKOUT = {"days": [{"name": "Day 1", "items": [{"exercise": "Push-up", "sets": 3, "reps": 10, "rest_s": 60}]}]}
SESSION_ITEMS = [{"exercise": "Push-up", "slug": "push-up", "category": "calisthenics", "sets": 3, "reps": 10,
                  "duration_s": None, "rest_s": 60, "load": None, "notes": None, "cues": ["Brace"], "video_slug": None,
                  "muscles": ["chest"], "equipment": "none", "description": "A push-up.", "load_cap": False,
                  "swapped_from": None, "swap_reason": None}]


def _workout_plan(customer: int, gym: int, i: int, *, active: bool) -> dict[str, Any]:
    return {"customer_id": customer, "gym_id": gym, "goal": Goal.GENERAL_FITNESS, "source": "template",
            "title": f"Plan {i}", "plan": WORKOUT, "is_active": active}


def _session(customer: int, plan: int | None, i: int) -> dict[str, Any]:
    return {"customer_id": customer, "plan_id": plan, "title": f"Workout {i}", "status": "completed", "items": SESSION_ITEMS,
            "started_at": utcnow() - timedelta(days=i, hours=1), "ended_at": utcnow() - timedelta(days=i)}


def _event(session: int, i: int) -> dict[str, Any]:
    return {"session_id": session, "kind": "set_done" if i % 2 else "user_said", "message": f"Event {i}",
            "detail": {"i": i}}


def _statement(w: World, i: int, *, employee: int | None = None) -> dict[str, Any]:
    start = date(2024, 1, 1) + timedelta(days=31 * i)
    start = start.replace(day=1)
    end = (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    return {"employee_id": employee or w.employee, "gym_id": w.gym, "period_start": start, "period_end": end,
            "days_in_period": (end - start).days + 1, "days_worked": (end - start).days + 1,
            "monthly_salary": Decimal("30000"), "gross_pay": Decimal("30000"), "net_pay": Decimal("28000"),
            "currency": "INR", "country_code": "IN", "status": StatementStatus.DUE if i == 0 else StatementStatus.PAID,
            "due_date": end + timedelta(days=7), "paid_at": None if i == 0 else utcnow()}


def _due_statement(w: World, k: int) -> dict[str, Any]:
    """A DUE statement of the trainer for an old month (k-th month from 2015) - the settlement page lists the dues."""
    start = (date(2015, 1, 1) + timedelta(days=31 * k)).replace(day=1)
    end = (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    return {**_statement(w, 0), "period_start": start, "period_end": end, "days_in_period": (end - start).days + 1,
            "days_worked": (end - start).days + 1, "due_date": end + timedelta(days=7)}


def _tx(w: World, i: int) -> dict[str, Any]:
    incoming = i % 2 == 0
    return {"gym_id": w.gym, "direction": TxDirection.INCOMING if incoming else TxDirection.OUTGOING,
            "category": TxCategory.SUBSCRIPTION if incoming else TxCategory.EXPENSE, "status": TxStatus.SUCCESS,
            "amount": Decimal(500 + i), "currency": "INR", "description": f"Seeded transaction {i}",
            "party_label": "Member", "provider": "test", "customer_id": w.member if incoming else None,
            "completed_at": utcnow() - timedelta(days=i % 25, hours=1)}


def _audit(w: World, i: int, *, gym: int | None) -> dict[str, Any]:
    return {"actor_id": w.owner, "gym_id": gym, "action": ["gym.update", "staff.update", "plan.update"][i % 3],
            "entity_type": "gym", "entity_id": w.gym, "detail": {"n": i}, "ip": "10.0.0.1",
            "created_at": utcnow() - timedelta(hours=i)}


def _event_row(ip: str, i: int, user: int | None = None) -> dict[str, Any]:
    cats = [c for c in ThreatCategory if c != ThreatCategory.OTHER]
    return {"ip": ip, "user_id": user, "method": "GET", "path": f"/wp-login.php?x={i}", "user_agent": "curl/8",
            "category": cats[i % len(cats)], "severity": 1 + i % 5, "detail": f"probe {i}",
            "action": "blocked" if i % 2 else "logged", "ai_reviewed": i % 3 == 0,
            "created_at": utcnow() - timedelta(minutes=30 + i)}


def _ban(i: int, *, ip: str | None = None, user: int | None = None, lifted: bool = False) -> dict[str, Any]:
    return {"ip": ip or f"198.18.{i // 250}.{1 + i % 250}", "reason": "Automated probe", "severity": 3,
            "banned_until": utcnow() + timedelta(days=7), "created_by": "auto", "offense_count": 1,
            "created_at": utcnow() - timedelta(hours=i), "user_id": user, "lifted_at": utcnow() if lifted else None}


def _mail(user: int | None, i: int) -> dict[str, Any]:
    return {"user_id": user, "to_email": f"np-mail-{next(_seq)}@example.com", "subject": f"Your receipt {i}",
            "text_body": "Thanks!", "status": [EmailStatus.SENT, EmailStatus.LOGGED, EmailStatus.FAILED][i % 3],
            "created_at": utcnow() - timedelta(minutes=i)}


def _job(i: int, *, user: int | None = None, kind: str = "np_noop") -> dict[str, Any]:
    return {"kind": kind, "payload": {"i": i}, "status": [JobStatus.DONE, JobStatus.FAILED][i % 2], "attempts": 1,
            "finished_at": utcnow(), "user_id": user, "created_at": utcnow() - timedelta(minutes=i)}


def _ai_log(user: int | None, i: int) -> dict[str, Any]:
    return {"feature": ["assistant", "coach", "triage"][i % 3], "provider": "simulated", "model": "sim-1", "ok": i % 4 != 0,
            "latency_ms": 100 + i, "user_id": user, "owner_kind": "user" if user else "system", "queue_ms": 5,
            "created_at": utcnow() - timedelta(minutes=i)}


def _identity(user: int | None, *, uploaded_by: int | None, employee: int | None = None,
              status: IdStatus = IdStatus.NEEDS_REVIEW) -> dict[str, Any]:
    k = next(_seq)
    return {"user_id": user, "employee_id": employee, "doc_type": IdDocType.AADHAAR, "country_code": "IN",
            "front_key": f"private/id/{k:032x}.bin", "size_bytes": 1000, "status": status,
            "id_number_masked": "XXXX XXXX 1234", "id_number_hash": f"{k:064x}", "name_on_id": "Seeded Person",
            "consensus_score": 0.5, "ai_summary": "Seeded for review.", "uploaded_by_id": uploaded_by,
            "engines": ["simulated"], "engine_results": [], "checks": [{"check": "name", "passed": True, "detail": "ok"}]}


def _run(*, branch: int | None = None, employee: int | None = None, i: int = 0) -> dict[str, Any]:
    return {"branch_id": branch, "employee_id": employee, "outcome": VerificationStatus.NEEDS_REVIEW,
            "score": 0.5, "checks": [{"check": "photos", "passed": True, "detail": "ok"}], "summary": f"Run {i}",
            "ai_provider": "simulated", "manual": False, "created_at": utcnow() - timedelta(hours=i)}


def _doc(branch: int, i: int) -> dict[str, Any]:
    k = next(_seq)
    return {"branch_id": branch, "doc_type": list(BranchDocType)[i % len(BranchDocType)],
            "file_key": f"private/branch/{k:032x}.bin", "original_filename": f"doc{i}.pdf",
            "content_type": "application/pdf", "sha256": f"{k:064x}", "size_bytes": 1000, "accepted": True}


def _emp_doc(employee: int, i: int) -> dict[str, Any]:
    k = next(_seq)
    return {"employee_id": employee, "doc_type": ["certification", "resume", "contract", "other"][i % 4],
            "file_key": f"private/staff/{k:032x}.bin", "original_filename": f"cert{i}.pdf",
            "content_type": "application/pdf", "sha256": f"{k:064x}", "size_bytes": 1000}


def _trust(w: World, start: int, n: int) -> tuple[list[dict[str, Any]], ...]:
    violations = [{"employee_id": w.employee, "gym_id": w.gym, "owner_id": w.owner, "actor_id": w.owner,
                   "subject_user_id": 900_000 + start + i, "subject_hashes": [f"{start + i:064x}"], "gym_name": "Iron Temple",
                   "person_name": f"Removed {start + i}", "reason": "harassment", "category": "violation",
                   "note": "Seeded violation."} for i in range(n)]
    bans = [{"user_id": 900_000 + start + i, "person_name": f"Banned {start + i}", "source": "violations",
             "reason": "Threshold of violations reached.", "created_by_id": w.owner} for i in range(n)]
    identities = [{"kind": ["email", "phone", "id_number"][i % 3], "value_hash": f"{start + i:064x}",
                   "reason": "banned", "user_id": 900_000 + start + i, "actor_id": w.admin} for i in range(n)]
    return violations, bans, identities


def _video(i: int) -> dict[str, Any]:
    k = next(_seq)
    return {"slug": f"np-video-{k}", "title": f"Seeded session {k}",
            "goal": ["calisthenics", "bulking", "weight_loss", "general_fitness", "recovery"][i % 5], "level": "beginner",
            "description": "A seeded video.", "duration_seconds": 300, "file_name": f"np-video-{k}.mp4",
            "exercise_slugs": [], "sort_order": i, "is_active": True}


def _exercise(i: int) -> dict[str, Any]:
    k = next(_seq)
    return {"slug": f"np-exercise-{k}", "name": f"Seeded exercise {k}", "category": "strength", "goals": ["bulking"],
            "muscles": ["chest"], "equipment": "none", "intensity": "moderate", "impact": "low",
            "description": "A seeded exercise.", "cues": ["Brace"], "contraindications": []}


# ==================================================================================================== small
async def build(db: Any) -> World:
    """The SMALL world: one or two rows of every collection (see the module docstring). Commits."""
    from app.security.passwords import hash_password

    w = World(password_hash=await hash_password("Str0ng!Passw0rd"))
    now = utcnow()
    w.admin, w.owner, w.other_owner, w.member, w.trainer, w.member2 = await put(db, User, [
        _user(w, Role.SUPERADMIN, "Platform Admin"), _user(w, Role.OWNER, "Gym Owner"),
        _user(w, Role.OWNER, "Other Owner"), _user(w, Role.CUSTOMER, "Member One"),
        _user(w, Role.STAFF, "Trainer One"), _user(w, Role.CUSTOMER, "Member Two"),
    ], ids=True)
    await put(db, CustomerProfile, [{"user_id": u, "height_cm": 172.0, "weight_kg": 70.0,
                                     "health_notes": "Mild asthma.", "emergency_contact_name": "Parent",
                                     "emergency_contact_phone": "+919800000000"} for u in (w.member, w.member2)])
    await put(db, FitnessProfile, [{"user_id": w.member, "likes": LIKES[:1], "avoids": ["running"], "goals": GOALS[:1],
                                    "equipment": EQUIPMENT[:1], "sessions_per_week": 3, "session_minutes": 45,
                                    "preferred_time": "morning", "notes": "Knees first.", "forgotten": []}])

    # ---------------------------------------------------------------- gyms, branches, plans
    w.gym, w.other_gym, w.gym2 = await put(db, Gym, [_gym(w.owner, "Iron Temple"), _gym(w.other_owner, "Elsewhere Fitness"),
                                                     _gym(w.owner, "Iron Temple Annex")], ids=True)
    w.gym_slug, w.other_gym_slug = (await db.execute(select(Gym.slug).where(Gym.id.in_([w.gym, w.other_gym]))
                                                     .order_by(Gym.id))).scalars().all()
    w.branch, w.branch2, _ = await _branches(db, [_branch(w.gym, "Indiranagar"), _branch(w.gym, "Koramangala", city=1),
                                                  _branch(w.other_gym, "Elsewhere Central")])
    (w.review_branch,) = await _branches(db, [_branch(w.gym, "Whitefield (in review)",
                                                       status=VerificationStatus.NEEDS_REVIEW)])
    await _branches(db, [_branch(w.gym2, "Annex Central")])
    await put(db, BranchFeature, [{"branch_id": w.branch, "name": "Sauna"}])
    w.branch_doc, w.review_branch_doc = await put(db, BranchDocument, [_doc(w.branch, 0), _doc(w.review_branch, 1)],
                                                  ids=True)
    await put(db, VerificationRun, [_run(branch=w.branch), _run(branch=w.review_branch)])
    plan_ids = await put(db, Plan, _plans(w.gym, PLAN_MATRIX[:SMALL_PLANS]) + _plans(w.gym2, PLAN_MATRIX[1:2])
                         + _plans(w.other_gym, PLAN_MATRIX[1:2]), ids=True)
    w.plans = {code: pid for (code, *_), pid in zip(PLAN_MATRIX[:SMALL_PLANS], plan_ids, strict=False)}
    other_online_plan = plan_ids[-1]

    # ---------------------------------------------------------------- staff
    w.employee, w.review_employee, w.invited_employee = await put(db, Employee, [
        _employee(w.gym, w.trainer, "Trainer One"),
        _employee(w.gym, None, "Applicant In Review", role=EmployeeRole.RECEPTIONIST,
                  status=EmploymentStatus.PENDING_VERIFICATION, verification=VerificationStatus.NEEDS_REVIEW),
        _employee(w.gym, None, "Invited Coach", status=EmploymentStatus.INVITED, verification=VerificationStatus.DRAFT),
    ], ids=True)
    await put(db, TrainerShift, [{"employee_id": w.employee, "branch_id": w.branch, "mode": ShiftMode.OFFLINE, "weekday": 0,
                                  "start_time": time(6), "end_time": time(10)},
                                 {"employee_id": w.employee, "branch_id": None, "mode": ShiftMode.ONLINE, "weekday": 2,
                                  "start_time": time(17), "end_time": time(19)}])
    w.employee_doc, w.review_employee_doc = await put(db, EmployeeDocument, [_emp_doc(w.employee, 0),
                                                                            _emp_doc(w.review_employee, 1)], ids=True)
    await put(db, VerificationRun, [_run(employee=w.employee), _run(employee=w.review_employee)])

    # ---------------------------------------------------------------- memberships
    w.subscription, w.pending_subscription, _, _ = await put(db, Subscription, [
        _subscription(w.member, w.gym, w.plans["combo_trainer_ai"], branch=w.branch, trainer=w.employee),
        _subscription(w.member, w.gym, w.plans["online"], status=SubscriptionStatus.PENDING_PAYMENT, days_ago=0,
                      price="799"),
        _subscription(w.member, w.other_gym, other_online_plan, status=SubscriptionStatus.EXPIRED, days_ago=60,
                      price="799"),
        # a former member of the gym (/owner/customers?status=expired lists someone on the small world too)
        _subscription(w.member2, w.gym, w.plans["online"], status=SubscriptionStatus.EXPIRED, days_ago=90, price="799"),
    ], ids=True)
    await put(db, TrainerClient, [{"employee_id": w.employee, "customer_id": w.member, "subscription_id": w.subscription,
                                   "mode": ShiftMode.OFFLINE, "active": True}])

    # ---------------------------------------------------------------- shop
    w.products = await put(db, Product, [_product(w.gym, 0, w.branch), _product(w.gym, 1, w.branch)], ids=True)
    w.product = w.products[0]
    w.product_slug = await db.scalar(select(Product.slug).where(Product.id == w.product))
    await put(db, ProductImage, [{"product_id": p, "image_key": f"public/products/{next(_seq):032x}.jpg", "sort_order": 0,
                                  "size_bytes": 1000} for p in w.products])
    await put(db, InventoryMovement, [{"product_id": w.product, "change": 25, "reason": "restock", "actor_id": w.owner}])
    await put(db, Coupon, [{"gym_id": w.gym, "code": "WELCOME10", "percent_off": 10, "is_active": True}])
    w.order, w.pending_order = await put(db, ShopOrder, [_order(w, w.member, 0, OrderStatus.COMPLETED),
                                                         _order(w, w.member, 1, OrderStatus.PENDING_PAYMENT)], ids=True)
    await put(db, ShopOrderItem, [_item(w.order, w.product, 0), _item(w.pending_order, w.products[1], 1)])
    await put(db, Refund, [{"order_id": w.order, "amount": Decimal("10"), "reason": "Damaged lid",
                            "status": RefundStatus.REQUESTED, "requested_by_id": w.member}])
    await put(db, ProductReview, [{"product_id": w.product, "user_id": w.member, "rating": 5, "title": "Great",
                                   "body": "Mixes well.", "is_hidden": False},
                                  {"product_id": w.products[1], "user_id": w.member, "rating": 1, "title": "Spam",
                                   "body": "Hidden by the owner.", "is_hidden": True}])
    await put(db, WishlistItem, [{"user_id": w.member, "product_id": w.product}])
    await put(db, CartItem, [{"user_id": w.member, "product_id": w.products[1], "quantity": 1}])

    # ---------------------------------------------------------------- notifications, tasks, conversations, reports
    await put(db, Notification, [_note(u, i) for u in w.users.values() for i in range(2)])
    await put(db, Task, [_task(u, i) for u in w.users.values() for i in range(2)])
    # i % 4 == 3: closed conversations (/chat?status=closed) - one of the member's, one the owner and trainer see
    w.thread, w.staff_thread, closed, closed2 = await put(db, ChatThread, [
        _thread(w.gym, w.member, None, 0), _thread(w.gym, w.member, w.employee, 1),
        _thread(w.other_gym, w.member, None, 3), _thread(w.gym, w.member2, w.employee, 7)], ids=True)
    await put(db, ChatMessage, [_message(w.thread, w.member, "customer", 0), _message(w.thread, w.owner, "owner", 1),
                                _message(w.staff_thread, w.member, "customer", 2), _message(closed, w.member, "customer", 3),
                                _message(closed2, w.member2, "customer", 4)])
    (w.report,) = await put(db, MisconductReport, [{"gym_id": w.gym, "employee_id": w.employee, "reporter_id": w.member,
                                                    "category": "rudeness", "description": "Was rude at the desk.",
                                                    "incident_date": date.today() - timedelta(days=3),
                                                    "status": ReportStatus.OPEN}], ids=True)

    # ---------------------------------------------------------------- health, coaching
    (w.metric_entry, _) = await put(db, MetricEntry, [_metric(w.member, 1), _metric(w.member, 2)], ids=True)
    await put(db, HealthCondition, [_condition(w.member, 0), _condition(w.member2, 1)])
    await put(db, HealthAlert, [_alert(w.member, 0)])
    (w.wellness_check,) = await put(db, WellnessCheck, [_wellness(w.member, 0)], ids=True)
    await put(db, SuspensionEvent, [{"customer_id": w.member, "action": "reactivate", "reason": "Cleared by the gym.",
                                     "actor": "owner", "actor_user_id": w.owner}])
    (w.workout_plan,) = await put(db, WorkoutPlan, [_workout_plan(w.member, w.gym, 0, active=True)], ids=True)
    (w.workout_session,) = await put(db, WorkoutSession, [_session(w.member, w.workout_plan, 0)], ids=True)
    await put(db, WorkoutSessionEvent, [_event(w.workout_session, i) for i in range(2)])
    await put(db, AssistantMessage, [{"user_id": w.member, "role": role, "content": f"{role} says hi"}
                                     for role in ("user", "assistant")])
    videos = [_video(i) for i in range(2)]
    await put(db, Video, videos)
    w.video_slug = videos[0]["slug"]
    await put(db, Exercise, [_exercise(i) for i in range(2)])

    # ---------------------------------------------------------------- identity, jobs board
    (w.identity_doc,) = await put(db, IdentityDocument, [_identity(w.member, uploaded_by=w.owner)], ids=True)
    (w.job_posting,) = await put(db, JobPosting, [{
        "gym_id": w.gym, "branch_id": w.branch, "title": "Strength coach", "role": EmployeeRole.TRAINER,
        "employment_type": EmploymentType.FULL_TIME, "location": "Bengaluru", "salary_min": Decimal(20000),
        "salary_max": Decimal(40000), "currency": "INR", "requirements": "Certified, friendly.", "is_open": True}],
        ids=True)
    (w.enquiry,) = await put(db, JobEnquiry, [{"job_id": w.job_posting, "name": "Applicant", "email": "applicant@example.com",
                                               "message": "I would love to coach here.", "status": EnquiryStatus.NEW}],
                             ids=True)

    # ---------------------------------------------------------------- money, platform
    await put(db, Transaction, [_tx(w, i) for i in range(2)])
    (w.statement,) = await put(db, SalaryStatement, [_statement(w, 0)], ids=True)
    await put(db, AuditLog, [_audit(w, i, gym=w.gym) for i in range(2)])
    # the reviewed address (/superadmin/security/ip/{ip}): an event of a known account, a ban naming it, a sign-in
    await put(db, SecurityEvent, [_event_row(w.ip, 0, user=w.member), _event_row(w.ip, 1, user=w.member)])
    await put(db, IpBan, [_ban(0, user=w.member2), _ban(1, ip=w.ip, user=w.member, lifted=True)])  # one names an account
    await db.execute(update(User).where(User.id == w.member).values(last_login_ip=w.ip))
    (w.email, _) = await put(db, EmailOutbox, [_mail(w.member, i) for i in range(2)], ids=True)
    from app.services import payroll as pr

    (w.payroll_job,) = await put(db, Job, [{**_job(0, user=w.owner, kind=pr.GENERATE_JOB),
                                            "payload": {"gyms": 1, "done": [w.gym]}}], ids=True)
    from app.services import coach as coach_service

    (w.coach_job,) = await put(db, Job, [{**_job(0, user=w.member, kind=coach_service.JOB_KIND),
                                          "payload": {"goal": "general_fitness"}}], ids=True)
    await _tokens_and_payments(db, w)
    await put(db, Job, [_job(1)])
    await put(db, AICallLog, [_ai_log(w.member, i) for i in range(2)])
    violations, account_bans, identities = _trust(w, 0, 1)
    (violation,) = await put(db, StaffViolation, violations, ids=True)
    ban_ids = await put(db, AccountBan, [{**b, "violation_id": violation} for b in account_bans], ids=True)
    await put(db, TrustIpAction, [{"account_ban_id": ban_ids[0], "user_id": 900_000, "ip": "198.51.100.9",
                                   "action": "banned"}])
    await put(db, BlockedIdentity, identities)
    await db.commit()
    w.sizes.append(0)
    return w


async def _tokens_and_payments(db: Any, w: World) -> None:
    """One-time links (/auth/reset, /auth/invite, /auth/staff-invite) and the gateway orders of the pending membership
    (/checkout/{sid}/gateway and /test-gateway)."""
    from app.models import AuthToken
    from app.models.enums import TokenPurpose
    from app.security.crypto import token_hash

    emails = {uid: email for uid, email in (await db.execute(select(User.id, User.email).where(User.id == w.member))).all()}
    invite_email = await db.scalar(select(Employee.invite_email).where(Employee.id == w.invited_employee))
    specs = [("reset", TokenPurpose.PASSWORD_RESET, w.member, emails[w.member], None),
             ("invite", TokenPurpose.INVITE, w.member, emails[w.member], None),
             ("staff-invite", TokenPurpose.STAFF_INVITE, None, invite_email, w.invited_employee)]
    rows = []
    for name, purpose, user, email, employee in specs:
        raw = f"np-{name}-{next(_seq)}-{'x' * 32}"
        w.tokens[name] = raw
        rows.append({"purpose": purpose, "token_hash": token_hash(raw), "user_id": user, "email": email,
                     "employee_id": employee, "expires_at": utcnow() + timedelta(days=2)})
    await put(db, AuthToken, rows)
    for provider in ("test", "cashfree"):
        w.checkout_orders[provider] = f"NPSUB{next(_seq):08d}"
    await put(db, Transaction, [{
        "gym_id": w.gym, "direction": TxDirection.INCOMING, "category": TxCategory.SUBSCRIPTION, "status": TxStatus.PENDING,
        "amount": Decimal("799"), "currency": "INR", "description": "Online membership", "party_label": "Member One",
        "provider": provider, "provider_order_id": order, "payment_session_id": f"session_{order}",
        "customer_id": w.member, "subscription_id": w.pending_subscription, "completed_at": None}
        for provider, order in w.checkout_orders.items()])


# ==================================================================================================== grow
async def grow(db: Any, w: World, n: int = 25) -> None:  # noqa: PLR0915 - one linear seed script
    """Add ``n`` rows (or up to the product's cap) to every collection of the world. Commits."""
    start = 1000 * (len(w.sizes) + 1)  # numbering of this growth (distinct dates, names, periods ...)
    now = utcnow()

    # ---------------------------------------------------------------- people
    members = await put(db, User, [_user(w, Role.CUSTOMER, f"Member {start + i}") for i in range(n)], ids=True)
    w.members += members
    await put(db, CustomerProfile, [{"user_id": m, "height_cm": 170.0, "weight_kg": 68.0,
                                     "share_health_with_trainers": True} for m in members])
    staff_users = await put(db, User, [_user(w, Role.STAFF, f"Staff {start + i}") for i in range(2 * n)], ids=True)
    other_owners = await put(db, User, [_user(w, Role.OWNER, f"Directory Owner {start + i}") for i in range(5)], ids=True)

    # ---------------------------------------------------------------- the public directory: n more gyms
    dir_gyms = await put(db, Gym, [_gym(other_owners[i % 5], f"Directory Gym {start + i}") for i in range(n)], ids=True)
    dir_branches = await _branches(db, [_branch(g, f"Directory Branch {start + i}", city=i) for i, g in enumerate(dir_gyms)])
    dir_plans = await put(db, Plan, [r for g in dir_gyms for r in _plans(g, PLAN_MATRIX[1:3])], ids=True)
    online_plan_of = dict(zip(dir_gyms, dir_plans[0::2], strict=True))
    await put(db, Employee, [_employee(g, staff_users[n + i], f"Coach {start + i}") for i, g in enumerate(dir_gyms)])
    dir_products = await put(db, Product, [_product(g, i, b) for i, (g, b) in enumerate(zip(dir_gyms, dir_branches,
                                                                                            strict=True))], ids=True)
    # the trainer also works at other gyms (several employments, a shift each: /staff, /staff/schedule)
    other_jobs = await put(db, Employee, [_employee(g, w.trainer, "Trainer One") for g in dir_gyms[:5]], ids=True)
    await put(db, TrainerShift, [{"employee_id": e, "branch_id": None, "mode": ShiftMode.ONLINE, "weekday": i % 7,
                                  "start_time": time(20), "end_time": time(21)} for i, e in enumerate(other_jobs)])

    # ---------------------------------------------------------------- the owner's other gyms (up to 20 per owner)
    owned = int(await db.scalar(select(func.count(Gym.id)).where(Gym.owner_id == w.owner)) or 0)
    extra_gyms = await put(db, Gym, [_gym(w.owner, f"Iron Temple {start + i}") for i in range(max(0, 20 - owned))], ids=True)
    await _branches(db, [_branch(g, f"Temple Branch {start + i}") for i, g in enumerate(extra_gyms)])
    await put(db, Plan, [r for g in extra_gyms for r in _plans(g, PLAN_MATRIX[1:2])])

    # ---------------------------------------------------------------- the gym: branches, plans, staff
    new_branches = await _branches(db, [_branch(w.gym, f"Temple {start + i}") for i in range(n)])
    have = set((await db.execute(select(Plan.code).where(Plan.gym_id == w.gym))).scalars())
    rest = [p for p in PLAN_MATRIX if p[0] not in have]
    for (code, *_), pid in zip(rest, await put(db, Plan, _plans(w.gym, rest, start=len(have)), ids=True), strict=True):
        w.plans[code] = pid
    roles = [EmployeeRole.TRAINER, EmployeeRole.RECEPTIONIST, EmployeeRole.MANAGER, EmployeeRole.NUTRITIONIST]
    statuses = [EmploymentStatus.ACTIVE] * 3 + [EmploymentStatus.PENDING_VERIFICATION, EmploymentStatus.SUSPENDED]
    employees = await put(db, Employee, [
        _employee(w.gym, staff_users[i], f"Staff {start + i}", role=roles[i % len(roles)],
                  status=statuses[i % len(statuses)],
                  verification=VerificationStatus.VERIFIED if i % 5 else VerificationStatus.NEEDS_REVIEW)
        for i in range(n)], ids=True)
    review_employees = await put(db, Employee, [
        _employee(w.gym, None, f"Applicant {start + i}", role=EmployeeRole.RECEPTIONIST,
                  status=EmploymentStatus.PENDING_VERIFICATION, verification=VerificationStatus.NEEDS_REVIEW)
        for i in range(n)], ids=True)
    review_branches = await _branches(db, [_branch(w.gym, f"In review {start + i}", status=VerificationStatus.NEEDS_REVIEW)
                                           for i in range(n)])
    await put(db, VerificationRun, [_run(branch=b, i=i) for i, b in enumerate(review_branches)]
              + [_run(employee=e, i=i) for i, e in enumerate(review_employees)])

    # nested rows of the branch and the trainer
    photos = int(await db.scalar(select(func.count(BranchPhoto.id))
                                 .where(BranchPhoto.branch_id == w.branch)) or 0)
    await put(db, BranchPhoto, [{"branch_id": w.branch, "image_key": f"public/branch/{next(_seq):032x}.jpg",
                                 "sort_order": photos + i, "kind": PhotoKind.OTHER, "size_bytes": 1000}
                                for i in range(max(0, 12 - photos))])
    await put(db, BranchFeature, [{"branch_id": w.branch, "name": f"Feature {start + i}"} for i in range(n)])
    docs = int(await db.scalar(select(func.count(BranchDocument.id))
                               .where(BranchDocument.branch_id == w.branch)) or 0)
    await put(db, BranchDocument, [_doc(w.branch, i) for i in range(max(0, 20 - docs))])
    await put(db, VerificationRun, [_run(branch=w.branch, i=i) for i in range(n)]
              + [_run(branch=w.review_branch, i=i) for i in range(n)]
              + [_run(employee=w.employee, i=i) for i in range(n)]
              + [_run(employee=w.review_employee, i=i) for i in range(n)])
    await put(db, TrainerShift, [{"employee_id": w.employee, "branch_id": None, "mode": ShiftMode.ONLINE,
                                  "weekday": i % 7, "start_time": time(11 + i % 6), "end_time": time(12 + i % 6)}
                                 for i in range(n)])
    await put(db, EmployeeDocument, [_emp_doc(w.employee, i) for i in range(n)])
    await put(db, SalaryStatement, [_statement(w, sum(w.sizes) + i) for i in range(1, n + 1)]
              + [_due_statement(w, sum(w.sizes) + i) for i in range(n)])  # the settlement lists every due month
    # the super-admin's review subjects: documents (and photos) of the branch and the employment under review
    rb_docs = int(await db.scalar(select(func.count(BranchDocument.id))
                                  .where(BranchDocument.branch_id == w.review_branch)) or 0)
    await put(db, BranchDocument, [_doc(w.review_branch, i) for i in range(max(0, 20 - rb_docs))])
    rb_photos = int(await db.scalar(select(func.count(BranchPhoto.id))
                                    .where(BranchPhoto.branch_id == w.review_branch)) or 0)
    await put(db, BranchPhoto, [{"branch_id": w.review_branch, "image_key": f"public/branch/{next(_seq):032x}.jpg",
                                 "sort_order": rb_photos + i, "kind": PhotoKind.OTHER, "size_bytes": 1000}
                                for i in range(max(0, 12 - rb_photos))])
    re_docs = int(await db.scalar(select(func.count(EmployeeDocument.id))
                                  .where(EmployeeDocument.employee_id == w.review_employee)) or 0)
    await put(db, EmployeeDocument, [_emp_doc(w.review_employee, i) for i in range(min(n, max(0, 40 - re_docs)))])

    # ---------------------------------------------------------------- the gym's members (the trainer's clients)
    # former members of the gym (an expired membership only: /owner/customers?status=expired)
    former = await put(db, User, [_user(w, Role.CUSTOMER, f"Former {start + i}") for i in range(n)], ids=True)
    await put(db, CustomerProfile, [{"user_id": m, "height_cm": 170.0, "weight_kg": 68.0} for m in former])
    await put(db, Subscription, [_subscription(m, w.gym, w.plans["online"], status=SubscriptionStatus.EXPIRED,
                                               days_ago=90 + i, price="799") for i, m in enumerate(former)])
    # trainers of the gym on shift at the branch (the public branch page lists them)
    await put(db, TrainerShift, [{"employee_id": e, "branch_id": w.branch, "mode": ShiftMode.OFFLINE, "weekday": i % 7,
                                  "start_time": time(7), "end_time": time(9)}
                                 for i, e in enumerate(employees) if roles[i % len(roles)] == EmployeeRole.TRAINER])
    subs = await put(db, Subscription, [_subscription(m, w.gym, w.plans["combo_trainer_ai"], branch=w.branch,
                                                      trainer=w.employee) for m in members], ids=True)
    await put(db, TrainerClient, [{"employee_id": w.employee, "customer_id": m, "subscription_id": s,
                                   "mode": ShiftMode.OFFLINE, "active": True} for m, s in zip(members, subs, strict=True)])
    await put(db, HealthCondition, [_condition(m, i) for i, m in enumerate(members)])
    await put(db, MetricEntry, [_metric(m, 1) for m in members])
    await put(db, IdentityDocument, [_identity(m, uploaded_by=w.owner) for m in members])
    gym_threads = await put(db, ChatThread, [_thread(w.gym, m, None, i) for i, m in enumerate(members)]
                            + [_thread(w.gym, m, w.employee, i) for i, m in enumerate(members)], ids=True)
    await put(db, ChatMessage, [_message(t, members[i % n], "customer", i) for i, t in enumerate(gym_threads)])
    await put(db, MisconductReport, [{"gym_id": w.gym, "employee_id": w.employee, "reporter_id": m, "category": "rudeness",
                                      "description": "Was rude at the desk.", "status": ReportStatus.OPEN}
                                     for m in members]
              + [{"gym_id": w.gym, "employee_id": e, "reporter_id": w.member, "category": "rudeness",
                  "description": "Was rude at the desk.", "status": ReportStatus.OPEN} for e in employees])

    # ---------------------------------------------------------------- the shop
    new_products = await put(db, Product, [_product(w.gym, i, w.branch) for i in range(n)], ids=True)
    w.products += new_products
    await put(db, ProductImage, [{"product_id": p, "image_key": f"public/products/{next(_seq):032x}.jpg", "sort_order": 0,
                                  "size_bytes": 1000} for p in new_products])
    images = int(await db.scalar(select(func.count(ProductImage.id))
                                 .where(ProductImage.product_id == w.product)) or 0)
    await put(db, ProductImage, [{"product_id": w.product, "image_key": f"public/products/{next(_seq):032x}.jpg",
                                  "sort_order": images + i, "size_bytes": 1000} for i in range(max(0, 8 - images))])
    await put(db, InventoryMovement, [{"product_id": w.product, "change": -1, "reason": "sale", "actor_id": w.owner}
                                      for _ in range(n)])
    await put(db, Coupon, [{"gym_id": w.gym, "code": f"NP{start + i}", "percent_off": 5 + i % 20, "is_active": True}
                           for i in range(n)])
    await put(db, ProductReview, [{"product_id": w.product, "user_id": m, "rating": 1 + i % 5, "title": f"Review {i}",
                                   "body": "Seeded review.", "is_hidden": i % 5 == 4} for i, m in enumerate(members)]
              + [{"product_id": p, "user_id": w.member, "rating": 4, "title": "Good", "body": "Seeded review.",
                  "is_hidden": False} for p in new_products])
    statuses_o = [OrderStatus.COMPLETED, OrderStatus.PAID, OrderStatus.SHIPPED, OrderStatus.PROCESSING,
                  OrderStatus.CANCELLED]
    orders = await put(db, ShopOrder, [_order(w, w.member, i, statuses_o[i % len(statuses_o)]) for i in range(n)]
                       + [_order(w, m, i, statuses_o[i % len(statuses_o)]) for i, m in enumerate(members)], ids=True)
    await put(db, ShopOrderItem, [_item(o, new_products[i % n], i) for i, o in enumerate(orders)]
              + [_item(w.order, p, i) for i, p in enumerate(new_products)])
    await put(db, Refund, [{"order_id": w.order, "amount": Decimal("1"), "reason": f"Partial refund {i}",
                            "status": RefundStatus.REJECTED, "requested_by_id": w.member} for i in range(n)])
    await put(db, WishlistItem, [{"user_id": w.member, "product_id": p} for p in new_products])
    await put(db, CartItem, [{"user_id": w.member, "product_id": p, "quantity": 1} for p in new_products])

    # ---------------------------------------------------------------- the member's own collections
    await put(db, Notification, [_note(u, start + i) for u in w.users.values() for i in range(n)])
    await put(db, Task, [_task(u, 2 + i) for u in w.users.values() for i in range(n)])
    member_threads = await put(db, ChatThread, [_thread(w.gym, w.member, e, i) for i, e in enumerate(employees)], ids=True)
    await put(db, ChatMessage, [_message(t, w.member, "customer", i) for i, t in enumerate(member_threads)]
              + [_message(w.thread, w.member if i % 2 else w.owner, "customer" if i % 2 else "owner", start + i)
                 for i in range(n)]
              + [_message(w.staff_thread, w.member if i % 2 else w.trainer, "customer" if i % 2 else "staff", start + i)
                 for i in range(n)])
    # ... and those conversations are unread again: the first view's mark-as-read runs on the large world too
    await db.execute(update(ChatThread).where(ChatThread.id.in_([w.thread, w.staff_thread, *member_threads]))
                     .values(customer_unread=ChatThread.customer_unread + n, staff_unread=ChatThread.staff_unread + n))
    await put(db, MetricEntry, [_metric(w.member, start + i) for i in range(n)])
    await put(db, HealthCondition, [_condition(w.member, start + i) for i in range(min(n, 45))])
    await put(db, HealthAlert, [_alert(w.member, start + i) for i in range(n)])
    await put(db, WellnessCheck, [_wellness(w.member, i + 1) for i in range(n)])
    await put(db, SuspensionEvent, [{"customer_id": w.member, "action": ["suspend", "reactivate"][i % 2],
                                     "reason": "Seeded.", "actor": "rule"} for i in range(n)])
    await put(db, WorkoutPlan, [_workout_plan(w.member, w.gym, start + i, active=False) for i in range(n)])
    await put(db, WorkoutSession, [_session(w.member, w.workout_plan, i + 1) for i in range(n)])
    await put(db, WorkoutSessionEvent, [_event(w.workout_session, start + i) for i in range(n)])
    await put(db, AssistantMessage, [{"user_id": w.member, "role": ("user", "assistant")[i % 2],
                                      "content": f"Message {start + i}"} for i in range(n)])
    await put(db, Subscription, [_subscription(w.member, g, online_plan_of[g], status=SubscriptionStatus.EXPIRED,
                                               days_ago=60 + i, price="799") for i, g in enumerate(dir_gyms[2:])])
    # ... and active memberships up to the caps: 3 online gyms (the combo at the gym counts) + 1 in-person
    await put(db, Subscription, [_subscription(w.member, g, online_plan_of[g], days_ago=5, price="799")
                                 for g in dir_gyms[:2]])
    # the member also shops at other gyms: orders, bag and wish list across several stores
    other_orders = await put(db, ShopOrder, [_order(w, w.member, i, OrderStatus.COMPLETED, gym=g, branch=b)
                                             for i, (g, b) in enumerate(zip(dir_gyms[:5], dir_branches[:5], strict=True))],
                             ids=True)
    await put(db, ShopOrderItem, [_item(o, p, i) for i, (o, p) in enumerate(zip(other_orders, dir_products, strict=False))])
    await put(db, CartItem, [{"user_id": w.member, "product_id": p, "quantity": 1} for p in dir_products[5:10]])
    await put(db, WishlistItem, [{"user_id": w.member, "product_id": p} for p in dir_products[10:15]])
    await db.execute(FitnessProfile.__table__.update().where(FitnessProfile.user_id == w.member).values(
        likes=LIKES[:18], avoids=["running", "jumping"], goals=GOALS[:10], equipment=EQUIPMENT[:20]))

    # ---------------------------------------------------------------- jobs board
    await put(db, JobPosting, [{"gym_id": w.gym, "branch_id": new_branches[i], "title": f"Coach {start + i}",
                                "role": roles[i % len(roles)], "employment_type": EmploymentType.PART_TIME,
                                "location": "Bengaluru", "currency": "INR", "requirements": "Friendly.", "is_open": True,
                                "created_at": now - timedelta(hours=i)} for i in range(n)])
    await put(db, JobEnquiry, [{"job_id": w.job_posting, "name": f"Applicant {start + i}",
                                "email": f"np-applicant-{start + i}@example.com", "message": "Hire me, please.",
                                "status": EnquiryStatus.NEW, "created_at": now - timedelta(minutes=i)} for i in range(n)])

    # ---------------------------------------------------------------- money, platform, trust and safety
    await put(db, Transaction, [_tx(w, start + i) for i in range(n)])
    await put(db, AuditLog, [_audit(w, start + i, gym=w.gym) for i in range(n)] + [_audit(w, i, gym=None) for i in range(n)])
    await put(db, SecurityEvent, [_event_row(w.ip, start + i, user=members[i]) for i in range(n)]
              + [_event_row(f"203.0.113.{100 + i}", start + i) for i in range(n)])
    await put(db, IpBan, [_ban(start + i) for i in range(n)]
              + [_ban(start + n + i, ip=w.ip, user=members[i], lifted=i % 2 == 0) for i in range(n)])
    await db.execute(update(User).where(User.id.in_(members[:5])).values(last_login_ip=w.ip))  # accounts seen on it
    await put(db, EmailOutbox, [_mail(w.member, start + i) for i in range(n)])
    await put(db, Job, [_job(start + i) for i in range(n)])
    await put(db, AICallLog, [_ai_log(members[i % n], start + i) for i in range(n)])
    violations, account_bans, identities = _trust(w, start, n)
    violation_ids = await put(db, StaffViolation, violations, ids=True)
    ban_ids = await put(db, AccountBan, [{**b, "violation_id": v} for b, v in zip(account_bans, violation_ids, strict=True)],
                        ids=True)
    await put(db, TrustIpAction, [{"account_ban_id": b, "user_id": 900_000 + start + i, "ip": f"198.51.100.{10 + i}",
                                   "action": "banned"} for i, b in enumerate(ban_ids)])
    await put(db, BlockedIdentity, identities)
    await put(db, Video, [_video(i) for i in range(n)])
    await put(db, Exercise, [_exercise(i) for i in range(n)])
    await db.commit()
    w.sizes.append(n)
