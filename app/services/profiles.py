"""Profile pages for everyone, with privacy levels.

Access levels (highest wins):
  self       - the person themself: everything
  admin      - platform super-admin: everything
  manager    - an owner of a gym the person belongs to (customer with a subscription / registered by them,
               or an employee of their gym): profile + health & safety info + memberships at their gyms
  trainer    - an active trainer assigned to the customer (TrainerClient): profile + health & safety info - unless
               the member switched off sharing health details with trainers (then a one-line notice instead)
  public     - owners and active public staff are public (they appear on gym pages)
  none       - customers are private -> 404 (existence is not revealed)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from app.models import (
    Branch,
    CustomerProfile,
    Employee,
    Gym,
    HealthCondition,
    MetricEntry,
    Subscription,
    TrainerClient,
    TrainerShift,
    User,
    WorkoutSession,
)
from app.models.enums import EmploymentStatus, Role, SubscriptionStatus, VerificationStatus

LEVELS = ("none", "public", "trainer", "manager", "admin", "self")
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def at_least(level: str, minimum: str) -> bool:
    return LEVELS.index(level) >= LEVELS.index(minimum)


async def access_level(db: AsyncSession, viewer: User | None, target: User) -> str:
    if viewer is not None and viewer.id == target.id:
        return "self"
    if viewer is not None and viewer.role == Role.SUPERADMIN:
        return "admin"
    if target.deleted_at is not None:
        return "none"
    level = "none"
    if target.role == Role.OWNER:
        level = "public"
    elif target.role == Role.STAFF:
        public_job = await db.scalar(select(exists().where(
            Employee.user_id == target.id, Employee.status == EmploymentStatus.ACTIVE,
            Employee.is_public.is_(True), Employee.deleted_at.is_(None))))
        level = "public" if public_job else "none"
    if viewer is None:
        return level
    if viewer.role == Role.OWNER:
        from app.services.intake import consented_subscription_clause

        manages = await db.scalar(select(or_(
            # a current or member-started membership (or one that ended lately) - the rule of intake.is_related
            exists().where(Subscription.customer_id == target.id, Subscription.gym_id == Gym.id, Gym.owner_id == viewer.id,
                           consented_subscription_clause()),
            exists().where(CustomerProfile.user_id == target.id, CustomerProfile.registered_by_id == viewer.id),
            exists().where(Employee.user_id == target.id, Employee.gym_id == Gym.id, Gym.owner_id == viewer.id),
        )))
        if manages:
            return "manager"
    if viewer.role == Role.STAFF and target.role == Role.CUSTOMER:
        trains = await db.scalar(select(exists().where(
            TrainerClient.customer_id == target.id, TrainerClient.active.is_(True),
            TrainerClient.employee_id == Employee.id, Employee.user_id == viewer.id,
            Employee.status == EmploymentStatus.ACTIVE)))
        if trains:
            return "trainer"
    return level


@dataclass
class ProfileData:
    user: User
    level: str
    employments: list[Employee] = field(default_factory=list)
    shifts_by_employee: dict[int, list[TrainerShift]] = field(default_factory=dict)
    owned_gyms: list[tuple[Gym, int]] = field(default_factory=list)  # (gym, published-or-verified branch count)
    subscriptions: list[Subscription] = field(default_factory=list)
    conditions: list[HealthCondition] = field(default_factory=list)
    latest_metric: MetricEntry | None = None
    stats: dict[str, int] = field(default_factory=dict)
    # a trainer-level viewer while the member has switched off "Share my health and safety information with my gym's
    # trainers": no health row is read, the page shows ``health_message`` instead (app/services/trainer_safety.py)
    health_withheld: bool = False
    health_message: str | None = None

    @property
    def show_health(self) -> bool:
        return self.user.role == Role.CUSTOMER and at_least(self.level, "trainer") and not self.health_withheld

    @property
    def show_private(self) -> bool:
        return at_least(self.level, "admin")


async def load_profile(db: AsyncSession, viewer: User | None, user_id: int) -> ProfileData | None:
    """All data for /profile/{id} in a constant number of queries. None -> 404."""
    target = (await db.execute(
        select(User).options(joinedload(User.customer_profile)).where(User.id == user_id)
    )).unique().scalar_one_or_none()
    if target is None:
        return None
    level = await access_level(db, viewer, target)
    if level == "none":
        return None
    data = ProfileData(user=target, level=level)

    if target.role in (Role.STAFF, Role.OWNER) or at_least(level, "manager"):
        emp_stmt = (
            select(Employee).options(joinedload(Employee.gym))
            .where(Employee.user_id == target.id, Employee.deleted_at.is_(None))
            .order_by(Employee.hire_date)
        )
        if not at_least(level, "manager"):
            emp_stmt = emp_stmt.where(Employee.status == EmploymentStatus.ACTIVE, Employee.is_public.is_(True))
        elif level == "manager" and viewer is not None:
            emp_stmt = emp_stmt.where(Employee.gym.has(Gym.owner_id == viewer.id))
        data.employments = list((await db.execute(emp_stmt)).unique().scalars().all())
        if data.employments:
            shifts = (await db.execute(
                select(TrainerShift).options(joinedload(TrainerShift.branch))
                .where(TrainerShift.employee_id.in_([e.id for e in data.employments]))
                .order_by(TrainerShift.weekday, TrainerShift.start_time)
            )).unique().scalars().all()
            for s in shifts:
                data.shifts_by_employee.setdefault(s.employee_id, []).append(s)

    if target.role == Role.OWNER:
        branch_count = (
            select(func.count(Branch.id)).where(Branch.gym_id == Gym.id,
                                                 Branch.verification_status == VerificationStatus.VERIFIED)
            .correlate(Gym).scalar_subquery()
        )
        rows = (await db.execute(
            select(Gym, branch_count).where(Gym.owner_id == target.id, Gym.is_active.is_(True)).order_by(Gym.name)
        )).all()
        data.owned_gyms = [(g, int(c or 0)) for g, c in rows]

    if target.role == Role.CUSTOMER and level == "trainer":
        prof = target.customer_profile
        if prof is not None and prof.share_health_with_trainers is False:
            from app.services.trainer_safety import WITHHELD_TEXT

            data.health_withheld, data.health_message = True, WITHHELD_TEXT

    if target.role == Role.CUSTOMER and at_least(level, "trainer"):
        sub_stmt = (
            select(Subscription).options(joinedload(Subscription.plan), joinedload(Subscription.gym),
                                         joinedload(Subscription.branch))
            .where(Subscription.customer_id == target.id,
                   Subscription.status.in_([SubscriptionStatus.ACTIVE, SubscriptionStatus.EXPIRED]))
            .order_by(Subscription.created_at.desc()).limit(10)
        )
        if level == "manager" and viewer is not None and viewer.role == Role.OWNER:
            sub_stmt = sub_stmt.where(Subscription.gym.has(Gym.owner_id == viewer.id))
        data.subscriptions = list((await db.execute(sub_stmt)).unique().scalars().all())

    if target.role == Role.CUSTOMER and data.show_health:
        from app.services.health import CONDITIONS_READ_MAX, safety_first  # WP-E1: the capped, safety-first read

        # at most CONDITIONS_READ_MAX rows, the safety-relevant ones first (a padded list can't hide a heart condition
        # from the trainer), shown in the page's order: by kind, then name
        capped = (await db.execute(
            select(HealthCondition).where(HealthCondition.customer_id == target.id)
            .order_by(*safety_first()).limit(CONDITIONS_READ_MAX)
        )).scalars().all()
        data.conditions = sorted(capped, key=lambda c: (str(getattr(c.kind, "value", c.kind)), c.name))
        data.latest_metric = (await db.execute(
            select(MetricEntry).where(MetricEntry.customer_id == target.id).order_by(MetricEntry.recorded_on.desc()).limit(1)
        )).scalar_one_or_none()

    if target.role == Role.CUSTOMER and at_least(level, "trainer"):
        since = date.today() - timedelta(days=30)
        workouts, logged_days = (await db.execute(
            select(
                select(func.count(WorkoutSession.id)).where(WorkoutSession.customer_id == target.id,
                                                            WorkoutSession.status == "completed").scalar_subquery(),
                select(func.count(MetricEntry.id)).where(MetricEntry.customer_id == target.id,
                                                         MetricEntry.recorded_on >= since).scalar_subquery(),
            )
        )).one()
        data.stats = {"workouts": int(workouts or 0), "logged_days_30": int(logged_days or 0)}
    return data
