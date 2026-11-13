"""Test data factories (async, explicit commits). Import: ``from tests.factories import *``."""

from __future__ import annotations

import itertools
from datetime import date, time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import normalize_phone, utcnow
from app.models import (
    Branch,
    BranchHours,
    BranchPhoto,
    CustomerProfile,
    Employee,
    Gym,
    HealthCondition,
    Plan,
    Subscription,
    TrainerShift,
    User,
)
from app.models.enums import (
    ConditionKind,
    EmploymentStatus,
    EmployeeRole,
    IdStatus,
    PlanMode,
    PurchaseChannel,
    Role,
    ShiftMode,
    SubscriptionStatus,
    VerificationStatus,
    WorkMode,
)

_seq = itertools.count(1)
DEFAULT_PASSWORD = "Str0ng!Passw0rd"

# Bengaluru landmarks (km apart): Indiranagar <-> Whitefield ~ 12 km, Indiranagar <-> Mysuru ~ 128 km
INDIRANAGAR = (12.9716, 77.6412)
WHITEFIELD = (12.9698, 77.7500)
KORAMANGALA = (12.9352, 77.6245)
MYSURU = (12.2958, 76.6394)


async def make_user(
    db: AsyncSession,
    *,
    role: Role = Role.CUSTOMER,
    email: str | None = None,
    full_name: str | None = None,
    password: str | None = DEFAULT_PASSWORD,
    id_status: IdStatus = IdStatus.VERIFIED,
    date_of_birth: date | None = date(1995, 5, 17),
    phone: str | None = None,
    with_profile: bool | None = None,
    photo_key: str | None = None,
    **kwargs: Any,
) -> User:
    from app.security.passwords import hash_password

    n = next(_seq)
    user = User(
        email=(email or f"user{n}@example.com").lower(),
        full_name=full_name or f"Test User {n}",
        role=role,
        password_hash=await hash_password(password) if password else None,
        id_status=id_status,
        date_of_birth=date_of_birth,
        phone=phone,
        phone_normalized=normalize_phone(phone, "IN") if phone else None,
        email_verified_at=utcnow(),
        photo_key=photo_key if photo_key is not None else (f"public/avatars/{n:032x}.jpg" if role in (Role.OWNER, Role.STAFF) else None),
        timezone=kwargs.pop("timezone", "Asia/Kolkata"),
        **kwargs,
    )
    db.add(user)
    await db.flush()
    if with_profile or (with_profile is None and role == Role.CUSTOMER):
        db.add(CustomerProfile(user_id=user.id, height_cm=175.0, weight_kg=72.0))
    await db.commit()
    return user


async def make_owner(db: AsyncSession, **kwargs: Any) -> User:
    return await make_user(db, role=Role.OWNER, **kwargs)


async def make_customer(db: AsyncSession, *, conditions: list[str] | None = None, **kwargs: Any) -> User:
    user = await make_user(db, role=Role.CUSTOMER, **kwargs)
    for name in conditions or []:
        from app.services.conditions import normalize_condition

        db.add(HealthCondition(customer_id=user.id, kind=ConditionKind.CONDITION, name=name, tag=normalize_condition(name)))
    await db.commit()
    return user


async def make_gym(db: AsyncSession, owner: User, *, name: str | None = None, online: bool = True,
                   offline: bool = True, currency: str = "INR", country: str = "IN") -> Gym:
    n = next(_seq)
    gym = Gym(owner_id=owner.id, name=name or f"Pulse Gym {n}", slug=f"pulse-gym-{n}", country_code=country,
              currency=currency, online_enabled=online, offline_enabled=offline, description="Test gym")
    db.add(gym)
    await db.commit()
    return gym


async def make_branch(db: AsyncSession, gym: Gym, *, name: str | None = None, coords: tuple[float, float] = INDIRANAGAR,
                      city: str = "Bengaluru", verified: bool = True, photos: int = 3, hours: tuple[str, str] = ("06:00", "20:00"),
                      is_open: bool = True) -> Branch:
    n = next(_seq)
    b = Branch(gym_id=gym.id, name=name or f"Branch {n}", address_line1=f"{n} 100 Feet Road", city=city,
               country_code=gym.country_code, latitude=coords[0], longitude=coords[1], timezone="Asia/Kolkata",
               verification_status=VerificationStatus.VERIFIED if verified else VerificationStatus.DRAFT,
               verified_at=utcnow() if verified else None, is_open=is_open)
    db.add(b)
    await db.flush()
    oh, om = map(int, hours[0].split(":"))
    ch, cm = map(int, hours[1].split(":"))
    for wd in range(7):
        db.add(BranchHours(branch_id=b.id, weekday=wd, opens_at=time(oh, om), closes_at=time(ch, cm)))
    for i in range(photos):
        db.add(BranchPhoto(branch_id=b.id, image_key=f"public/branch/{next(_seq):032x}.jpg", sort_order=i))
    await db.commit()
    return b


async def make_employee(db: AsyncSession, gym: Gym, *, user: User | None = None, role: EmployeeRole = EmployeeRole.TRAINER,
                        status: EmploymentStatus = EmploymentStatus.ACTIVE, salary: str = "30000",
                        work_mode: WorkMode = WorkMode.BOTH, hire_date: date | None = None, country: str = "IN",
                        currency: str = "INR") -> Employee:
    if user is None and status == EmploymentStatus.ACTIVE:
        user = await make_user(db, role=Role.STAFF)
    n = next(_seq)
    emp = Employee(gym_id=gym.id, user_id=user.id if user else None, invite_email=(user.email if user else f"staff{n}@example.com"),
                   full_name=user.full_name if user else f"Staff {n}", role=role, is_trainer=role == EmployeeRole.TRAINER,
                   work_mode=work_mode, country_code=country, currency=currency, monthly_salary=Decimal(salary),
                   hire_date=hire_date or date.today() - timedelta(days=400), status=status,
                   photo_key=f"public/staff/{n:032x}.jpg", verification_status=VerificationStatus.VERIFIED,
                   id_status=IdStatus.VERIFIED)
    db.add(emp)
    await db.commit()
    return emp


async def make_shift(db: AsyncSession, employee: Employee, *, branch: Branch | None, weekday: int = 0,
                     start: str = "06:00", end: str = "10:00") -> TrainerShift:
    sh, sm = map(int, start.split(":"))
    eh, em = map(int, end.split(":"))
    s = TrainerShift(employee_id=employee.id, branch_id=branch.id if branch else None,
                     mode=ShiftMode.OFFLINE if branch else ShiftMode.ONLINE, weekday=weekday,
                     start_time=time(sh, sm), end_time=time(eh, em))
    db.add(s)
    await db.commit()
    return s


PLAN_MATRIX = [
    ("offline", PlanMode.OFFLINE, False, False, "1499"), ("offline_trainer", PlanMode.OFFLINE, True, False, "2999"),
    ("offline_ai", PlanMode.OFFLINE, False, True, "1999"), ("offline_trainer_ai", PlanMode.OFFLINE, True, True, "3499"),
    ("online", PlanMode.ONLINE, False, False, "799"), ("online_trainer", PlanMode.ONLINE, True, False, "1999"),
    ("online_ai", PlanMode.ONLINE, False, True, "1199"), ("online_trainer_ai", PlanMode.ONLINE, True, True, "2499"),
    ("combo", PlanMode.COMBO, False, False, "1999"), ("combo_trainer", PlanMode.COMBO, True, False, "3499"),
    ("combo_ai", PlanMode.COMBO, False, True, "2499"), ("combo_trainer_ai", PlanMode.COMBO, True, True, "3999"),
]


async def make_plans(db: AsyncSession, gym: Gym) -> list[Plan]:
    plans = []
    for i, (code, mode, trainer, ai_, price) in enumerate(PLAN_MATRIX):
        p = Plan(gym_id=gym.id, code=code, name=code.replace("_", " + ").title(), mode=mode, includes_trainer=trainer,
                 includes_ai=ai_, price=Decimal(price), currency=gym.currency, duration_days=30, sort_order=i)
        db.add(p)
        plans.append(p)
    await db.commit()
    return plans


async def make_subscription(db: AsyncSession, customer: User, plan: Plan, *, branch: Branch | None = None,
                            status: SubscriptionStatus = SubscriptionStatus.ACTIVE) -> Subscription:
    today = date.today()
    sub = Subscription(customer_id=customer.id, gym_id=plan.gym_id, plan_id=plan.id, branch_id=branch.id if branch else None,
                       status=status, channel=PurchaseChannel.ONLINE_CHECKOUT, price=plan.price, currency=plan.currency,
                       starts_on=today, ends_on=today + timedelta(days=plan.duration_days),
                       activated_at=utcnow() if status == SubscriptionStatus.ACTIVE else None)
    db.add(sub)
    await db.commit()
    return sub


def png_bytes(size: tuple[int, int] = (64, 64), color: str = "white", text: str | None = None) -> bytes:
    import io

    from PIL import Image, ImageDraw

    im = Image.new("RGB", size, color)
    if text:
        ImageDraw.Draw(im).text((10, 10), text, fill="black")
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()
