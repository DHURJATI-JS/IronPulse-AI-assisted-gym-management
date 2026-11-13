"""Plans, purchase flows and subscription lifecycle (WP-D).

* ``seed_default_plans``      - the 12 standard plans of a gym (idempotent)
* ``gym_availability``        - which plans can be sold right now (online open, published branches, trainers)
* ``start_subscription``      - customer purchase (30 km rule, price copied server-side, pending reuse)
* desk flow                   - ``presence_check`` + ``issue_desk_otp`` (code goes ONLY to the gym owner) +
                                ``verify_desk_otp`` (constant-time, attempts, lockout -> FRAUD event)
* payments                    - ``start_payment`` (Cashfree or TEST MODE), ``settle_order`` (idempotent,
                                amount-mismatch protection), ``activate_subscription`` (+ trainer assignment)
* ``register_cash_subscription`` - owner registers a cash membership at the desk
* ``cancel_subscription``, daily expiry + 3-day reminders (periodic job)

Every price is copied from the plan on the server; client-sent prices are ignored. Plan price changes never
touch existing subscriptions (each subscription stores its own price/currency).
"""

from __future__ import annotations

import logging
import math
import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from functools import partial
from typing import Any

from sqlalchemy import and_, exists, func, inspect, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.ai import guard as ai_guard
from app.config import get_settings
from app.core.geo import haversine_km, valid_coords
from app.core.utils import local_today, money, normalize_phone, numeric_code, to_decimal, utcnow
from app.models import (
    Branch,
    CustomerProfile,
    Employee,
    Gym,
    Notification,
    OtpChallenge,
    Plan,
    SecurityEvent,
    Subscription,
    TrainerClient,
    TrainerShift,
    Transaction,
    User,
)
from app.models.enums import (
    EmploymentStatus,
    NotificationCategory,
    PlanMode,
    PurchaseChannel,
    Role,
    Severity,
    ShiftMode,
    SubscriptionStatus,
    ThreatCategory,
    TxCategory,
    TxDirection,
    TxStatus,
    WorkMode,
)
from app.db import session_scope
from app.security.crypto import equals, hmac_hex
from app.services import gym_limits
from app.services.batching import Chunks
from app.services.jobs import periodic
from app.services.notify import Note, notify, notify_bulk
from app.services.realtime import call_after_commit
from app.services.visibility import OfflineEligibility, offline_eligibility, published_branch_clauses

log = logging.getLogger("ironpulse.checkout")

MAX_PLAN_PRICE = Decimal("10000000")
OTP_CODES_PER_HOUR = 3
PENDING_STATUSES = (SubscriptionStatus.PENDING_OTP, SubscriptionStatus.PENDING_PAYMENT)
OPEN_TX_STATUSES = (TxStatus.CREATED, TxStatus.PENDING)
REMINDER_TITLE = "Your membership ends in 3 days"

# ------------------------------------------------------------------ the 12 standard plans
PLAN_CATALOG: tuple[tuple[str, PlanMode, bool, bool, str, str], ...] = (
    ("offline", PlanMode.OFFLINE, False, False, "Gym floor",
     "Full access to the gym floor at your branch during opening hours, plus the complete pre-recorded video library."),
    ("offline_trainer", PlanMode.OFFLINE, True, False, "Gym floor + Trainer",
     "Gym-floor access with a personal trainer who plans and supervises your sessions at the branch. Video library included."),
    ("offline_ai", PlanMode.OFFLINE, False, True, "Gym floor + AI coach",
     "Gym-floor access plus an AI coach that adapts every workout to your goal and health conditions. Video library included."),
    ("offline_trainer_ai", PlanMode.OFFLINE, True, True, "Gym floor + Trainer + AI coach",
     "Everything for in-person training: gym access, a personal trainer and the AI coach working together. Video library included."),
    ("online", PlanMode.ONLINE, False, False, "Online",
     "Train from anywhere with the full pre-recorded video library and live chat with the gym team."),
    ("online_trainer", PlanMode.ONLINE, True, False, "Online + Trainer",
     "Remote coaching from a personal trainer over live chat and scheduled online sessions, plus the video library."),
    ("online_ai", PlanMode.ONLINE, False, True, "Online + AI coach",
     "Home workouts planned by the AI coach around your goal, equipment and health conditions, plus the video library."),
    ("online_trainer_ai", PlanMode.ONLINE, True, True, "Online + Trainer + AI coach",
     "The complete remote package: a personal trainer, the AI coach and the full video library."),
    ("combo", PlanMode.COMBO, False, False, "Combo",
     "The best of both: gym-floor access at your branch and online training from anywhere, with the video library."),
    ("combo_trainer", PlanMode.COMBO, True, False, "Combo + Trainer",
     "Gym and online access with a personal trainer for in-person and remote sessions. Video library included."),
    ("combo_ai", PlanMode.COMBO, False, True, "Combo + AI coach",
     "Gym and online access with the AI coach planning every session, wherever you train. Video library included."),
    ("combo_trainer_ai", PlanMode.COMBO, True, True, "Combo + Trainer + AI coach",
     "Our most complete membership: gym and online access, a personal trainer and the AI coach. Video library included."),
)
PLAN_CODES: tuple[str, ...] = tuple(row[0] for row in PLAN_CATALOG)

# Monthly price ranges per currency (cheapest = online, dearest = combo + trainer + AI).
PRICE_RANGES: dict[str, tuple[int, int]] = {
    "INR": (799, 3999), "USD": (15, 79), "GBP": (12, 65), "AED": (59, 299), "EUR": (14, 70),
    "AUD": (25, 119), "CAD": (20, 105), "SGD": (20, 105), "NZD": (25, 129), "CHF": (15, 79),
    "SAR": (59, 299), "QAR": (55, 289), "KWD": (5, 24), "OMR": (6, 30), "BHD": (6, 30),
    "JPY": (1999, 10999), "CNY": (99, 499), "KRW": (19900, 99000), "MYR": (69, 349),
    "PHP": (799, 3999), "THB": (499, 2599), "IDR": (199000, 999000), "VND": (349000, 1799000),
    "PKR": (2499, 11999), "LKR": (2999, 14999), "BDT": (1499, 7999), "NPR": (1299, 6499),
    "ZAR": (249, 1399), "KES": (1999, 9999), "NGN": (9999, 49999), "EGP": (699, 3499),
    "BRL": (79, 399), "MXN": (299, 1499), "TRY": (499, 2499), "PLN": (59, 299),
    "SEK": (169, 849), "NOK": (169, 849), "DKK": (109, 549),
}
_MODE_WEIGHT = {PlanMode.ONLINE: 0.0, PlanMode.OFFLINE: 0.3, PlanMode.COMBO: 0.5}

MODE_LABELS = {PlanMode.OFFLINE: "In the gym", PlanMode.ONLINE: "Online", PlanMode.COMBO: "Gym + online"}


def _nice_price(value: float, lo: int, hi: int) -> Decimal:
    """Round to a 'retail' price (…99 / …9) inside [lo, hi]."""
    if hi < 40:
        nice = round(value)
    elif hi < 200:
        nice = int(round(value / 5.0)) * 5 - 1
    elif hi < 2000:
        nice = int(round(value / 10.0)) * 10 - 1
    elif hi < 20000:
        nice = int(round(value / 100.0)) * 100 - 1
    elif hi < 200000:
        nice = int(round(value / 1000.0)) * 1000 - 1
    else:
        nice = int(round(value / 10000.0)) * 10000 - 1000
    return Decimal(min(hi, max(lo, nice))).quantize(Decimal("0.01"))


def default_price(code: str, currency: str, rng: random.Random | None = None) -> Decimal:
    row = next(r for r in PLAN_CATALOG if r[0] == code)
    _, mode, trainer, ai_, _, _ = row
    lo, hi = PRICE_RANGES.get(currency.upper(), PRICE_RANGES["USD"])
    weight = _MODE_WEIGHT[mode] + (0.35 if trainer else 0.0) + (0.15 if ai_ else 0.0)
    if weight <= 0:
        return Decimal(lo).quantize(Decimal("0.01"))
    if weight >= 1:
        return Decimal(hi).quantize(Decimal("0.01"))
    jitter = (rng or random).uniform(-0.035, 0.035) * (hi - lo)
    return _nice_price(lo + weight * (hi - lo) + jitter, lo, hi)


async def seed_default_plans(db: AsyncSession, gym: Gym) -> list[Plan]:
    """Create the 12 standard plans for ``gym`` (30 days each). Existing plans are never modified, so calling
    it again only adds missing codes. The caller commits. Returns all plans of the gym in display order."""
    rng = random.Random(f"ironpulse-plans:{gym.id}:{gym.currency}")
    rows = []
    for i, (code, mode, trainer, ai_, name, description) in enumerate(PLAN_CATALOG):
        rows.append({
            "gym_id": gym.id, "code": code, "name": name, "mode": mode, "includes_trainer": trainer,
            "includes_ai": ai_, "price": default_price(code, gym.currency, rng), "currency": gym.currency.upper(),
            "duration_days": 30, "is_enabled": True, "description": description, "sort_order": i,
        })
    stmt = pg_insert(Plan).values(rows).on_conflict_do_nothing(index_elements=["gym_id", "code"])
    await db.execute(stmt)
    return list((await db.execute(select(Plan).where(Plan.gym_id == gym.id).order_by(Plan.sort_order, Plan.id)))
                .scalars().all())


# ------------------------------------------------------------------ availability
TRAINER_MODES: dict[PlanMode, tuple[WorkMode, ...]] = {
    PlanMode.ONLINE: (WorkMode.ONLINE, WorkMode.BOTH),
    PlanMode.OFFLINE: (WorkMode.OFFLINE, WorkMode.BOTH),
    PlanMode.COMBO: (WorkMode.ONLINE, WorkMode.OFFLINE, WorkMode.BOTH),
}


@dataclass(slots=True)
class GymAvailability:
    gym: Gym
    branches: list[Branch] = field(default_factory=list)  # published branches
    trainers: dict[WorkMode, int] = field(default_factory=dict)  # ACTIVE trainers per work mode

    @property
    def online_open(self) -> bool:
        return bool(self.gym.is_active and self.gym.online_enabled and self.gym.online_open)

    @property
    def has_branch(self) -> bool:
        return bool(self.branches)

    def trainer_count(self, mode: PlanMode) -> int:
        return sum(self.trainers.get(m, 0) for m in TRAINER_MODES[mode])

    def unavailable_reason(self, plan: Plan) -> str | None:
        """None when the plan can be sold now, else a short owner-facing reason."""
        if not plan.is_enabled:
            return "Disabled"
        if not self.gym.is_active:
            return "Gym is inactive"
        if plan.mode in (PlanMode.ONLINE, PlanMode.COMBO) and not self.online_open:
            return "Online training is switched off" if not self.gym.online_enabled else "Online training is closed"
        if plan.mode in (PlanMode.OFFLINE, PlanMode.COMBO) and not self.branches:
            return "No published branch yet"
        if plan.includes_trainer and self.trainer_count(plan.mode) == 0:
            return "No active trainer for this mode"
        return None

    def is_available(self, plan: Plan) -> bool:
        return self.unavailable_reason(plan) is None


async def gym_availability(db: AsyncSession, gym: Gym, *, with_hours: bool = False) -> GymAvailability:
    """Two queries: published branches + ACTIVE trainer counts per work mode."""
    stmt = (
        select(Branch)
        .join(Gym, Gym.id == Branch.gym_id)
        .where(Branch.gym_id == gym.id, *published_branch_clauses())
        .order_by(Branch.name, Branch.id)
    )
    if with_hours:
        from sqlalchemy.orm import selectinload

        stmt = stmt.options(selectinload(Branch.hours))
    branches = list((await db.execute(stmt)).scalars().all())
    rows = await db.execute(
        select(Employee.work_mode, func.count(Employee.id))
        .where(
            Employee.gym_id == gym.id,
            Employee.is_trainer.is_(True),
            Employee.status == EmploymentStatus.ACTIVE,
            Employee.deleted_at.is_(None),
        )
        .group_by(Employee.work_mode)
    )
    trainers = {WorkMode(m): int(n) for m, n in rows.all()}
    return GymAvailability(gym=gym, branches=branches, trainers=trainers)


# ------------------------------------------------------------------ customer location & gating
@dataclass(slots=True)
class CustomerLocation:
    lat: float | None
    lon: float | None
    city: str | None
    source: str  # "gps" (shared now) | "saved" (account coordinates) | "city" | "none"

    @property
    def has_coords(self) -> bool:
        return valid_coords(self.lat, self.lon)


def parse_coord(value: Any, lo: float, hi: float) -> float | None:
    if value is None or value == "":
        return None
    try:
        v = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v) or not lo <= v <= hi:
        return None
    return round(v, 6)


def resolve_location(user: User, lat: Any = None, lon: Any = None) -> CustomerLocation:
    plat, plon = parse_coord(lat, -90, 90), parse_coord(lon, -180, 180)
    if valid_coords(plat, plon):
        return CustomerLocation(plat, plon, user.city, "gps")
    if valid_coords(user.latitude, user.longitude):
        return CustomerLocation(float(user.latitude), float(user.longitude), user.city, "saved")  # type: ignore[arg-type]
    if user.city:
        return CustomerLocation(None, None, user.city, "city")
    return CustomerLocation(None, None, None, "none")


def eligibility_for(branches: Sequence[Branch], loc: CustomerLocation) -> OfflineEligibility:
    return offline_eligibility(branches, lat=loc.lat, lon=loc.lon, city=loc.city)


def branch_distances(branches: Sequence[Branch], loc: CustomerLocation) -> list[tuple[Branch, float | None]]:
    """Branches sorted by distance from the customer (unknown distances last, then by name)."""
    out: list[tuple[Branch, float | None]] = []
    for b in branches:
        d = None
        if loc.has_coords and valid_coords(b.latitude, b.longitude):
            d = round(haversine_km(float(loc.lat), float(loc.lon), float(b.latitude), float(b.longitude)), 1)  # type: ignore[arg-type]
        out.append((b, d))
    out.sort(key=lambda bd: (bd[1] is None, bd[1] if bd[1] is not None else 0.0, bd[0].name.lower()))
    return out


async def _is_suspended(db: AsyncSession, user: User) -> bool:
    state = inspect(user)
    if "customer_profile" not in state.unloaded:
        prof = user.customer_profile
        return bool(prof is not None and prof.activity_suspended)
    return bool(await db.scalar(select(CustomerProfile.activity_suspended).where(CustomerProfile.user_id == user.id)))


@dataclass(slots=True)
class Block:
    code: str  # "id" | "suspended" | "inactive"
    title: str
    message: str
    action_label: str | None = None
    action_href: str | None = None


async def customer_block(db: AsyncSession, user: User) -> Block | None:
    """Why this customer may not buy a membership right now (None = allowed)."""
    from app.services.id_verification import id_requirement_met

    if user.deleted_at is not None or not user.is_active:
        return Block("inactive", "Account unavailable", "This account can't buy memberships.")
    if await _is_suspended(db, user):
        return Block(
            "suspended", "Gym activities are paused",
            "Your gym activities are paused for your safety, so new memberships can't be bought right now. "
            "A gym admin must approve reactivation first.", "See details", "/me/health",
        )
    if not id_requirement_met(user.id_status):
        pending = user.id_status.value in ("pending", "processing", "needs_review")
        return Block(
            "id", "Verify your ID card first",
            ("Your ID card is still being verified. Memberships unlock as soon as it's approved."
             if pending else
             "Every member needs a verified government ID card before buying a membership. It only takes a minute."),
            "View ID status" if pending else "Upload ID card", "/account/identity",
        )
    return None


# ------------------------------------------------------------------ fraud evidence
FRAUD_POINTS = {"no_gps": 10, "presence_far": 40, "otp_lockout": 60, "amount_mismatch": 80,
                "duplicate_payment": 0, "paid_after_close": 20, "gym_limit": 0}


def add_fraud_flag(sub: Subscription, flag: str) -> None:
    flags = list(sub.fraud_flags or [])
    if flag not in flags:
        flags.append(flag)
        sub.fraud_flags = flags  # reassign: JSONB lists are not mutation-tracked
        sub.fraud_score = min(100, int(sub.fraud_score or 0) + FRAUD_POINTS.get(flag, 10))


def record_fraud_event(db: AsyncSession, *, detail: str, user_id: int | None, request: Any = None,
                       severity: int = 2, ip: str | None = None) -> SecurityEvent:
    """Persist a FRAUD security event in the caller's transaction (shown in the security centre)."""
    from app.security.net import client_ip

    ev = SecurityEvent(
        ip=ip or (client_ip(request) if request is not None else "0.0.0.0"),
        user_id=user_id,
        method=(request.method if request is not None else "SYSTEM")[:10],
        path=(request.url.path if request is not None else "/payments")[:512],
        user_agent=((request.headers.get("user-agent") or "")[:300] or None) if request is not None else None,
        category=ThreatCategory.FRAUD,
        severity=severity,
        detail=detail[:500],
        action="logged",
    )
    db.add(ev)
    return ev


# ------------------------------------------------------------------ loading helpers
def subscription_options() -> list[Any]:
    # the customer is often the signed-in user (same identity): keep its customer_profile loaded, because
    # populate_existing (used under row locks) would otherwise reset it and base.html reads it.
    return [
        joinedload(Subscription.plan),
        joinedload(Subscription.gym),
        joinedload(Subscription.branch),
        joinedload(Subscription.trainer),
        joinedload(Subscription.customer).joinedload(User.customer_profile),
    ]


async def load_customer_subscription(db: AsyncSession, sid: int, customer_id: int, *, lock: bool = False) -> Subscription | None:
    stmt = select(Subscription).options(*subscription_options()).where(
        Subscription.id == sid, Subscription.customer_id == customer_id
    )
    if lock:
        stmt = stmt.with_for_update(of=Subscription).execution_options(populate_existing=True)
    return (await db.execute(stmt)).unique().scalar_one_or_none()


# ------------------------------------------------------------------ purchase
class PurchaseError(ValueError):
    def __init__(self, message: str, field: str = "plan") -> None:
        super().__init__(message)
        self.field = field


async def start_subscription(
    db: AsyncSession,
    *,
    customer: User,
    gym: Gym,
    plan: Plan,
    availability: GymAvailability,
    branch_id: int | None,
    location: CustomerLocation,
    ip: str | None,
) -> tuple[Subscription, bool]:
    """Create (or reuse) a pending subscription. Returns (subscription, created).

    Raises PurchaseError (message safe to show) for every rule violation. The caller commits."""
    if plan.gym_id != gym.id:
        raise PurchaseError("Choose one of this gym's plans.")
    reason = availability.unavailable_reason(plan)
    if reason is not None:
        raise PurchaseError("This plan isn't available right now. Please choose another one.")

    branch: Branch | None = None
    distance: float | None = None
    if plan.needs_branch:
        branch = next((b for b in availability.branches if b.id == branch_id), None)
        if branch is None:
            raise PurchaseError("Choose the branch where you'll train.", "branch_id")
        overall = eligibility_for(availability.branches, location)
        if not overall.eligible:
            raise PurchaseError(overall.message, "location")
        chosen = eligibility_for([branch], location)
        if not chosen.eligible:
            limit = get_settings().offline_max_distance_km
            where = f" about {chosen.distance_km:.0f} km away" if chosen.distance_km is not None else " not in your city"
            raise PurchaseError(
                f"{branch.name} is{where}. Offline memberships are only sold within {limit:.0f} km - "
                "please choose a branch closer to you.", "branch_id",
            )
        distance = chosen.distance_km

    status = SubscriptionStatus.PENDING_OTP if plan.needs_branch else SubscriptionStatus.PENDING_PAYMENT
    channel = PurchaseChannel.BRANCH_DESK_OTP if plan.needs_branch else PurchaseChannel.ONLINE_CHECKOUT

    # gyms per person (3 online / 1 in person, app/services/gym_limits.py): under the person's lock, taken before the
    # pending row lock below, so two checkouts of one person at the same moment are counted one after the other
    over = await gym_limits.join_refusal(db, customer.id, gym.id, plan.mode)
    if over is not None:
        raise PurchaseError(over, "limit")

    existing = await db.scalar(
        select(Subscription)
        .where(Subscription.customer_id == customer.id, Subscription.plan_id == plan.id,
               Subscription.status.in_(PENDING_STATUSES))
        .order_by(Subscription.id.desc())
        .limit(1)
        .with_for_update()
    )
    if existing is not None:
        open_tx = await db.scalar(
            select(func.count(Transaction.id)).where(Transaction.subscription_id == existing.id,
                                                     Transaction.status.in_(OPEN_TX_STATUSES))
        )
        if branch is not None and existing.branch_id != branch.id:
            # a different branch means a different desk: restart the desk check there
            existing.branch_id = branch.id
            existing.status = SubscriptionStatus.PENDING_OTP
            existing.otp_verified_at = None
            existing.physically_present = None
            await db.execute(update(OtpChallenge).where(OtpChallenge.subscription_id == existing.id,
                                                       OtpChallenge.consumed_at.is_(None))
                             .values(expires_at=utcnow()))
        if not open_tx and (existing.price != plan.price or existing.currency != plan.currency):
            existing.price, existing.currency = plan.price, plan.currency  # nothing paid yet: current price
        if location.has_coords:
            existing.customer_latitude, existing.customer_longitude = location.lat, location.lon
            existing.distance_km = distance
        existing.created_ip = ip or existing.created_ip
        return existing, False

    sub = Subscription(
        customer_id=customer.id,
        gym_id=gym.id,
        plan_id=plan.id,
        branch_id=branch.id if branch else None,
        status=status,
        channel=channel,
        price=plan.price,  # always the server-side plan price
        currency=plan.currency,
        created_ip=ip,
        customer_latitude=location.lat if location.has_coords else None,
        customer_longitude=location.lon if location.has_coords else None,
        distance_km=distance,
        fraud_flags=[],
    )
    db.add(sub)
    await db.flush()
    return sub, True


# ------------------------------------------------------------------ desk presence + OTP
@dataclass(slots=True)
class PresenceResult:
    allowed: bool
    gps: bool
    distance_km: float | None
    message: str = ""


def presence_check(branch: Branch, lat: Any, lon: Any) -> PresenceResult:
    radius = get_settings().presence_radius_km
    plat, plon = parse_coord(lat, -90, 90), parse_coord(lon, -180, 180)
    if valid_coords(plat, plon) and valid_coords(branch.latitude, branch.longitude):
        d = haversine_km(float(plat), float(plon), float(branch.latitude), float(branch.longitude))  # type: ignore[arg-type]
        if d > radius:
            return PresenceResult(
                False, True, round(d, 2),
                f"Your location is about {d:.1f} km from {branch.name}. Desk purchases need you inside the branch "
                f"(within {radius:g} km). Head to the desk and try again.",
            )
        return PresenceResult(True, True, round(d, 2))
    return PresenceResult(True, False, None)


class OtpRateLimited(Exception):
    pass


async def issue_desk_otp(db: AsyncSession, sub: Subscription, *, ip: str | None) -> OtpChallenge:
    """Generate a 6-digit desk code, store only its HMAC and send it ONLY to the gym owner.

    ``sub`` must have plan, gym, branch and customer loaded. Raises OtpRateLimited after 3 codes per hour."""
    s = get_settings()
    now = utcnow()
    since = now - timedelta(hours=1)
    recent = await db.execute(
        select(func.count(OtpChallenge.id), func.min(OtpChallenge.created_at))
        .where(OtpChallenge.subscription_id == sub.id, OtpChallenge.created_at > since)
    )
    count, oldest = recent.one()
    if int(count or 0) >= OTP_CODES_PER_HOUR:
        wait = 60
        if oldest is not None:
            wait = max(1, math.ceil(((oldest + timedelta(hours=1)) - now).total_seconds() / 60))
        raise OtpRateLimited(
            f"You've asked for {OTP_CODES_PER_HOUR} desk codes in the last hour. Please try again in {wait} minute"
            f"{'s' if wait != 1 else ''}, or ask the gym owner to register you at the desk."
        )
    # only the newest code works
    await db.execute(update(OtpChallenge).where(OtpChallenge.subscription_id == sub.id,
                                                OtpChallenge.consumed_at.is_(None),
                                                OtpChallenge.expires_at > now).values(expires_at=now))
    code = numeric_code(6)
    branch = sub.branch
    assert branch is not None
    challenge = OtpChallenge(
        purpose="offline_purchase",
        subscription_id=sub.id,
        branch_id=branch.id,
        customer_id=sub.customer_id,
        recipient_user_id=sub.gym.owner_id,
        code_hash=hmac_hex(code, "otp"),
        expires_at=now + timedelta(minutes=s.otp_ttl_minutes),
        max_attempts=s.otp_max_attempts,
        created_ip=ip,
    )
    db.add(challenge)
    await db.flush()
    customer = sub.customer
    await notify(
        db, sub.gym.owner_id,
        f"Desk OTP for {customer.full_name}",
        (f"Code: {code}\n"
         f"Customer: {customer.full_name}\n"
         f"Plan: {sub.plan.name}\n"
         f"Amount: {money(sub.price, sub.currency)}\n"
         f"Branch: {branch.name}\n"
         f"Valid for {s.otp_ttl_minutes} minutes. Tell this code to the customer in person at the desk only - "
         "never over the phone or chat."),
        category=NotificationCategory.PAYMENT, severity=Severity.INFO, link=f"/owner/desk#otp-{challenge.id}",
    )
    return challenge


async def active_challenge(db: AsyncSession, sub_id: int) -> OtpChallenge | None:
    now = utcnow()
    return await db.scalar(
        select(OtpChallenge)
        .where(OtpChallenge.subscription_id == sub_id, OtpChallenge.consumed_at.is_(None),
               OtpChallenge.expires_at > now, OtpChallenge.attempts < OtpChallenge.max_attempts)
        .order_by(OtpChallenge.id.desc())
        .limit(1)
    )


@dataclass(slots=True)
class OtpOutcome:
    result: str  # "ok" | "wrong" | "locked" | "expired"
    attempts_left: int = 0


async def verify_desk_otp(db: AsyncSession, sub: Subscription, code: str, *, request: Any = None) -> OtpOutcome:
    """Constant-time check of the newest open challenge. Too many wrong codes lock the purchase."""
    now = utcnow()
    challenge = await db.scalar(
        select(OtpChallenge)
        .where(OtpChallenge.subscription_id == sub.id, OtpChallenge.consumed_at.is_(None))
        .order_by(OtpChallenge.id.desc())
        .limit(1)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if challenge is None or challenge.expires_at <= now or challenge.attempts >= challenge.max_attempts:
        return OtpOutcome("expired")
    challenge.attempts += 1
    if equals(hmac_hex(code, "otp"), challenge.code_hash):
        challenge.consumed_at = now
        sub.otp_verified_at = now
        sub.physically_present = True
        sub.status = SubscriptionStatus.PENDING_PAYMENT
        return OtpOutcome("ok")
    left = challenge.max_attempts - challenge.attempts
    if left > 0:
        return OtpOutcome("wrong", left)
    # lockout
    challenge.expires_at = now
    sub.status = SubscriptionStatus.FAILED
    add_fraud_flag(sub, "otp_lockout")
    customer = sub.customer
    record_fraud_event(
        db, request=request, user_id=sub.customer_id, severity=2,
        detail=f"Desk OTP locked after {challenge.attempts} wrong codes (subscription {sub.id}, gym {sub.gym_id})",
    )
    await notify(
        db, sub.gym.owner_id, "Desk purchase locked",
        f"{customer.full_name} entered {challenge.attempts} wrong desk codes for {sub.plan.name}. The purchase was "
        "locked for security. If this was a genuine customer, register them at the desk yourself.",
        category=NotificationCategory.SECURITY, severity=Severity.WARNING, link="/owner/desk",
    )
    await notify(
        db, customer, "Purchase locked for security",
        f"Too many incorrect desk codes were entered for {sub.plan.name} at {sub.gym.name}. Please speak to the gym "
        "owner at the desk to complete your membership.",
        category=NotificationCategory.SECURITY, severity=Severity.WARNING, link=f"/checkout/{sub.id}",
    )
    return OtpOutcome("locked")


# ------------------------------------------------------------------ payments
@dataclass(slots=True)
class PaymentStart:
    kind: str  # "cashfree" | "test"
    transaction: Transaction
    redirect_url: str


async def start_payment(db: AsyncSession, sub: Subscription) -> PaymentStart:
    """Create a gateway order (Cashfree) or a TEST MODE order. ``sub`` needs plan, gym and customer loaded.

    Raises cashfree.CashfreeError when the gateway fails. The caller commits."""
    from app.services import cashfree

    s = get_settings()
    customer, plan, gym = sub.customer, sub.plan, sub.gym
    order_id = cashfree.new_order_id(sub.id)
    common: dict[str, Any] = dict(
        gym_id=sub.gym_id, direction=TxDirection.INCOMING, category=TxCategory.SUBSCRIPTION, status=TxStatus.PENDING,
        amount=sub.price, currency=sub.currency, description=f"{plan.name} membership - {gym.name}"[:255],
        party_label=customer.full_name[:160], customer_id=customer.id, subscription_id=sub.id,
        provider_order_id=order_id,
    )
    if s.cashfree_enabled:
        order = await cashfree.create_order(
            order_id=order_id, amount=sub.price, currency=sub.currency, customer_id=f"ipc{customer.id}",
            customer_email=customer.email, customer_phone=customer.phone, customer_name=customer.full_name,
            return_url=f"{s.public_base_url}/checkout/return?order_id={order_id}",
            notify_url=f"{s.public_base_url}/payments/cashfree/webhook",
            note=f"{gym.name} - {plan.name}",
        )
        tx = Transaction(provider="cashfree", payment_session_id=order.payment_session_id,
                         meta={"cf_order_id": order.cf_order_id, "sdk_mode": cashfree.sdk_mode()}, **common)
        redirect_url = f"/checkout/{sub.id}/gateway?order_id={order_id}"
    else:
        tx = Transaction(provider="test", meta={"test_mode": True}, **common)
        redirect_url = f"/checkout/{sub.id}/test-gateway?order_id={order_id}"
    # older unpaid orders of this subscription are superseded (a late success is still honoured)
    await db.execute(update(Transaction).where(Transaction.subscription_id == sub.id,
                                               Transaction.status.in_(OPEN_TX_STATUSES))
                     .values(status=TxStatus.CANCELLED))
    db.add(tx)
    await db.flush()
    return PaymentStart("cashfree" if s.cashfree_enabled else "test", tx, redirect_url)


async def lock_transaction(db: AsyncSession, order_id: str, *, customer_id: int | None = None) -> Transaction | None:
    """The order's transaction, row-locked - after the member's gym-limit lock (lock order: person -> transaction ->
    subscription, app/services/gym_limits.py), which ``settle_order`` needs before it may activate a membership."""
    stmt = select(Transaction).where(Transaction.provider_order_id == order_id,
                                     Transaction.category == TxCategory.SUBSCRIPTION)
    if customer_id is not None:
        stmt = stmt.where(Transaction.customer_id == customer_id)
    person = customer_id if customer_id is not None else await db.scalar(
        select(Transaction.customer_id).where(Transaction.provider_order_id == order_id,
                                              Transaction.category == TxCategory.SUBSCRIPTION).limit(1))
    if person is not None:
        await gym_limits.lock(db, person)
    return await db.scalar(stmt.with_for_update().execution_options(populate_existing=True))


async def settle_order(
    db: AsyncSession,
    tx: Transaction,
    *,
    paid: bool,
    amount: Any = None,
    currency: str | None = None,
    payment_id: str | None = None,
    source: str = "return",
    request: Any = None,
) -> str:
    """Apply a VERIFIED gateway outcome to a locked transaction. Idempotent.

    Returns "success" | "failed" | "mismatch" | "review". The caller commits."""
    if tx.subscription_id is None:
        return "review"
    sub = (await db.execute(
        select(Subscription).options(*subscription_options()).where(Subscription.id == tx.subscription_id)
        .with_for_update(of=Subscription).execution_options(populate_existing=True)
    )).unique().scalar_one()
    if tx.status == TxStatus.SUCCESS:
        return "success" if sub.status == SubscriptionStatus.ACTIVE else "review"

    if not paid:
        if tx.status == TxStatus.FAILED:
            return "failed"
        tx.status = TxStatus.FAILED
        tx.meta = {**(tx.meta or {}), "failed_via": source}
        if sub.status == SubscriptionStatus.PENDING_PAYMENT:
            sub.status = SubscriptionStatus.FAILED
            await notify(
                db, sub.customer, "Payment failed",
                f"Your payment of {money(tx.amount, tx.currency)} for {sub.plan.name} at {sub.gym.name} did not go "
                "through. No money was taken - you can try again any time.",
                category=NotificationCategory.PAYMENT, severity=Severity.WARNING, link=f"/checkout/{sub.id}",
            )
        return "failed"

    try:
        paid_amount = to_decimal(amount) if amount is not None else None
    except ValueError:
        paid_amount = None
    expected = to_decimal(tx.amount)
    if (paid_amount != expected or (currency or "").upper() != tx.currency.upper()
            or to_decimal(sub.price) != expected or sub.currency.upper() != tx.currency.upper()):
        tx.status = TxStatus.FAILED
        tx.meta = {**(tx.meta or {}), "amount_mismatch": {
            "expected": str(expected), "expected_currency": tx.currency, "paid": str(amount),
            "paid_currency": currency, "source": source}}
        add_fraud_flag(sub, "amount_mismatch")
        record_fraud_event(
            db, request=request, user_id=sub.customer_id, severity=3,
            detail=(f"Payment amount mismatch on order {tx.provider_order_id}: expected {expected} {tx.currency}, "
                    f"got {amount} {currency} (subscription {sub.id})"),
        )
        await notify(
            db, sub.gym.owner_id, "Payment needs review",
            f"A payment for {sub.customer.full_name}'s {sub.plan.name} membership did not match the expected amount "
            f"({money(expected, tx.currency)}). The membership was NOT activated.",
            category=NotificationCategory.SECURITY, severity=Severity.DANGER, link="/owner/finance",
        )
        await notify(
            db, sub.customer, "We couldn't confirm your payment",
            f"Your payment for {sub.plan.name} at {sub.gym.name} could not be matched to your order, so the membership "
            "is not active yet. The gym has been notified and will contact you.",
            category=NotificationCategory.PAYMENT, severity=Severity.WARNING, link="/me/subscriptions",
        )
        return "mismatch"

    if payment_id:
        tx.provider_payment_id = str(payment_id)[:64]
    if sub.status == SubscriptionStatus.ACTIVE:
        tx.status, tx.completed_at = TxStatus.SUCCESS, utcnow()
        add_fraud_flag(sub, "duplicate_payment")
        await notify(
            db, sub.gym.owner_id, "Duplicate payment received",
            f"{sub.customer.full_name} paid twice for {sub.plan.name} ({money(tx.amount, tx.currency)}, order "
            f"{tx.provider_order_id}). Please refund the duplicate.",
            category=NotificationCategory.PAYMENT, severity=Severity.WARNING, link="/owner/finance",
        )
        return "success"
    if sub.status not in (SubscriptionStatus.PENDING_PAYMENT, SubscriptionStatus.FAILED) or "otp_lockout" in (sub.fraud_flags or []):
        tx.status, tx.completed_at = TxStatus.SUCCESS, utcnow()
        add_fraud_flag(sub, "paid_after_close")
        await notify(
            db, sub.gym.owner_id, "Payment received for a closed purchase",
            f"{sub.customer.full_name} paid {money(tx.amount, tx.currency)} for {sub.plan.name}, but the purchase was "
            f"{sub.status.value.replace('_', ' ')}. Please review and activate or refund it.",
            category=NotificationCategory.PAYMENT, severity=Severity.WARNING, link="/owner/finance",
        )
        await notify(
            db, sub.customer, "Payment received - under review",
            f"We received {money(tx.amount, tx.currency)} for {sub.plan.name}. The gym will review it shortly.",
            category=NotificationCategory.PAYMENT, severity=Severity.INFO, link="/me/subscriptions",
        )
        return "review"
    if await hold_over_limit(db, sub, tx):
        return "review"
    await activate_subscription(db, sub, tx=tx)
    return "success"


async def hold_over_limit(db: AsyncSession, sub: Subscription, tx: Transaction) -> bool:
    """The activation re-check of the gyms-per-person limit (app/services/gym_limits.py). True = ``sub`` was NOT
    activated: a payment arrived for a join that slipped past the limit (an unfinished purchase from before the limit,
    a late payment of a failed one). The money is booked (the ledger shows it), the purchase is closed and flagged for
    review, and the owner (refund it) and the member (why, and that the gym will refund) are told - never a silent
    charge. ``sub`` needs plan, gym and customer loaded; the caller holds the transaction row lock."""
    over = await gym_limits.join_refusal(db, sub.customer_id, sub.gym_id, sub.plan.mode, exclude_sub_id=sub.id)
    if over is None:
        return False
    tx.status, tx.completed_at = TxStatus.SUCCESS, utcnow()
    tx.meta = {**(tx.meta or {}), "gym_limit": True}
    add_fraud_flag(sub, "gym_limit")
    sub.status = SubscriptionStatus.FAILED
    await notify(
        db, sub.gym.owner_id, "Payment to refund: membership over the gym limit",
        f"{sub.customer.full_name} paid {money(tx.amount, tx.currency)} (order {tx.provider_order_id}) for "
        f"{sub.plan.name}, but already holds the most gym memberships one person may have at once, so it was NOT "
        "activated. Please refund the payment, or register them again once they have left another gym.",
        category=NotificationCategory.PAYMENT, severity=Severity.WARNING, link="/owner/finance",
    )
    await notify(
        db, sub.customer, "Payment received - membership not activated",
        f"We received {money(tx.amount, tx.currency)} for {sub.plan.name} at {sub.gym.name}, but it was not activated: "
        f"{over} {sub.gym.name} has been asked to refund your payment.",
        category=NotificationCategory.PAYMENT, severity=Severity.WARNING, link=gym_limits.MY_MEMBERSHIPS,
    )
    log.warning("membership %s over the gym limit at payment: held for review (order %s)", sub.id, tx.provider_order_id)
    return True


# ------------------------------------------------------------------ activation + trainer
# Active clients one trainer takes before automatic assignment passes them over (approved load limit): the
# TRAINER_MAX_CLIENTS setting (app/config.py ``trainer_max_clients``, default 150). A member who joins while every
# matching trainer is full stays without a trainer and the owner is told - at most once per gym per
# TRAINER_NOTICE_HOURS, however many members join meanwhile.
TRAINER_MAX_CLIENTS = 150  # used only if the setting is missing
TRAINER_NOTICE_HOURS = 24


def trainer_client_cap() -> int:
    """Active clients per trainer for automatic assignment (setting ``trainer_max_clients``, at least 1)."""
    value = getattr(get_settings(), "trainer_max_clients", None)
    try:
        return max(1, int(value)) if value is not None else TRAINER_MAX_CLIENTS
    except (TypeError, ValueError):
        return TRAINER_MAX_CLIENTS


def _trainer_lock(gym_id: int) -> int:
    """Key of the transaction-scoped advisory lock that serialises trainer assignment within one gym."""
    import hashlib

    digest = hashlib.blake2b(f"trainer-assign:{int(gym_id)}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


async def _tell_owner_no_trainer(db: AsyncSession, sub: Subscription, *, full: bool, cap: int) -> None:
    """The owner learns that a member on a trainer plan joined without a trainer - at most once per gym per
    TRAINER_NOTICE_HOURS (one notification and e-mail, never one per member). Runs under ``assign_trainer``'s gym
    lock, so two activations at the same moment cannot both find no recent notice and both send one."""
    gym = (await db.execute(select(Gym.owner_id, Gym.name).where(Gym.id == sub.gym_id))).first()
    if gym is None:
        return
    link = f"/owner/staff?gym={sub.gym_id}&role=trainer"
    since = utcnow() - timedelta(hours=TRAINER_NOTICE_HOURS)
    if await db.scalar(select(exists().where(Notification.user_id == gym.owner_id, Notification.link == link,
                                             Notification.created_at >= since))):
        return
    if full:
        title = f"Trainers at {gym.name} are fully booked"
        body = (f"Every active trainer at {gym.name} already has {cap} active clients - the most one trainer takes - "
                "so new members on trainer plans are joining without a trainer. Add a trainer or move some clients, "
                "then assign them from Staff. We remind you at most once a day.")
    else:
        title = f"No trainer available at {gym.name}"
        body = (f"New members on trainer plans at {gym.name} are joining without a trainer: no active trainer with a "
                "matching work mode and a linked account is available. Assign one from Staff. We remind you at most "
                "once a day.")
    await notify(db, gym.owner_id, title, body, category=NotificationCategory.STAFF, severity=Severity.WARNING,
                 link=link)


async def assign_trainer(db: AsyncSession, sub: Subscription, plan: Plan) -> Employee | None:
    """Least-loaded ACTIVE trainer with a compatible work mode and fewer than ``trainer_client_cap()`` active clients
    (prefers the customer's previous trainer, then trainers with shifts at the subscription's branch). Creates the
    TrainerClient row. When no trainer qualifies - none matches, or every match is full - the member stays unassigned
    and the owner is told once (``_tell_owner_no_trainer``).

    The load is a correlated count per candidate that stops at the cap (this gym's trainers only, served by the
    trainer_clients.employee_id index) - never an aggregate over every trainer-client link on the platform, nor a
    count of a busy trainer's whole client list. A transaction-scoped advisory lock per gym (held until the caller
    commits) makes simultaneous activations count each other's new clients: the cap holds under concurrency."""
    cap = trainer_client_cap()
    await db.execute(select(func.pg_advisory_xact_lock(_trainer_lock(sub.gym_id))))
    load = (
        select(func.count())
        .select_from(select(TrainerClient.id)
                     .where(TrainerClient.employee_id == Employee.id, TrainerClient.active.is_(True))
                     .correlate(Employee).limit(cap).subquery("clients"))
        .scalar_subquery()
    )
    previous = exists(select(TrainerClient.id).where(TrainerClient.employee_id == Employee.id,
                                                     TrainerClient.customer_id == sub.customer_id))
    order: list[Any] = [previous.desc()]
    if sub.branch_id is not None and plan.mode != PlanMode.ONLINE:
        at_branch = exists(select(TrainerShift.id).where(TrainerShift.employee_id == Employee.id,
                                                         TrainerShift.branch_id == sub.branch_id))
        order.append(at_branch.desc())
    order += [load.asc(), Employee.id.asc()]
    eligible = (
        Employee.gym_id == sub.gym_id,
        Employee.is_trainer.is_(True),
        Employee.status == EmploymentStatus.ACTIVE,
        Employee.deleted_at.is_(None),
        Employee.user_id.is_not(None),
        Employee.work_mode.in_(TRAINER_MODES[plan.mode]),
    )
    trainer = await db.scalar(select(Employee).where(*eligible, load < cap).order_by(*order).limit(1))
    if trainer is None:
        full = bool(await db.scalar(select(exists().where(*eligible))))  # trainers match, but all are at the cap
        await _tell_owner_no_trainer(db, sub, full=full, cap=cap)
        return None
    if plan.mode == PlanMode.ONLINE:
        mode = ShiftMode.ONLINE
    elif plan.mode == PlanMode.OFFLINE:
        mode = ShiftMode.OFFLINE
    else:
        mode = ShiftMode.OFFLINE if trainer.work_mode in (WorkMode.OFFLINE, WorkMode.BOTH) else ShiftMode.ONLINE
    db.add(TrainerClient(employee_id=trainer.id, customer_id=sub.customer_id, subscription_id=sub.id, mode=mode))
    sub.trainer_id = trainer.id
    return trainer


async def activate_subscription(
    db: AsyncSession,
    sub: Subscription,
    *,
    tx: Transaction | None = None,
    actor: User | None = None,
) -> bool:
    """Activate ``sub`` (idempotent). Marks ``tx`` SUCCESS, assigns a trainer when the plan includes one and
    notifies the customer (guardian CC automatic), the owner and the trainer. Returns False if already active.
    The caller commits."""
    now = utcnow()
    if tx is not None and tx.status != TxStatus.SUCCESS:
        tx.status, tx.completed_at = TxStatus.SUCCESS, now
    if sub.status == SubscriptionStatus.ACTIVE:
        return False
    plan = await db.get(Plan, sub.plan_id)
    gym = await db.get(Gym, sub.gym_id)
    customer = await db.get(User, sub.customer_id)
    assert plan is not None and gym is not None and customer is not None
    today = local_today(customer.timezone)
    start = today
    # an early renewal of the same plan starts the day after the current membership ends
    current_end = await db.scalar(
        select(func.max(Subscription.ends_on)).where(
            Subscription.customer_id == sub.customer_id, Subscription.gym_id == sub.gym_id,
            Subscription.plan_id == sub.plan_id, Subscription.status == SubscriptionStatus.ACTIVE,
            Subscription.id != sub.id, Subscription.ends_on >= today,
        )
    )
    if current_end is not None:
        start = current_end + timedelta(days=1)
    sub.status = SubscriptionStatus.ACTIVE
    sub.starts_on = start
    sub.ends_on = start + timedelta(days=plan.duration_days)
    sub.activated_at = now
    sub.cancelled_at = None
    call_after_commit(db, partial(ai_guard.guard.forget_entitlement, sub.customer_id))  # an AI plan counts at once

    trainer: Employee | None = None
    if plan.includes_trainer and sub.trainer_id is None:
        trainer = await assign_trainer(db, sub, plan)

    period = f"{sub.starts_on:%d %b %Y} - {sub.ends_on:%d %b %Y}"
    extra = f" Your trainer is {trainer.full_name}." if trainer else ""
    await notify(
        db, customer, "Your membership is active",
        f"{plan.name} at {gym.name} is active ({period}). Pre-recorded videos are included.{extra}",
        category=NotificationCategory.PAYMENT, severity=Severity.SUCCESS, link="/me/subscriptions",
    )
    how = {PurchaseChannel.OWNER_MANUAL: "registered at the desk (cash)",
           PurchaseChannel.BRANCH_DESK_OTP: "bought at the branch desk",
           PurchaseChannel.ONLINE_CHECKOUT: "bought online"}.get(sub.channel, "bought")
    if actor is None or actor.id != gym.owner_id:
        await notify(
            db, gym.owner_id, f"New member: {customer.full_name}",
            f"{customer.full_name} {how}: {plan.name} for {money(sub.price, sub.currency)} ({period}).",
            category=NotificationCategory.PAYMENT, severity=Severity.SUCCESS, link=f"/owner/customers/{customer.id}",
        )
    if trainer is not None and trainer.user_id is not None:
        await notify(
            db, trainer.user_id, "New client assigned",
            f"{customer.full_name} joined {gym.name} on {plan.name} and has been assigned to you.",
            category=NotificationCategory.STAFF, severity=Severity.INFO, link="/staff/clients",
        )
    elif plan.includes_trainer and sub.trainer_id is None:
        await notify(
            db, gym.owner_id, "No trainer available",
            f"{customer.full_name}'s {plan.name} membership includes a trainer, but no active trainer with a matching "
            "work mode is free. Please assign one.",
            category=NotificationCategory.STAFF, severity=Severity.WARNING, link="/owner/staff",
        )
    return True


# ------------------------------------------------------------------ owner: cash registration at the desk
# The desk form takes any e-mail or phone number, and a desk registration makes the gym - its owner and, through the
# trainer it assigns, its trainers - a manager of that member's private record (health conditions, medications,
# guardian, emergency contact). So the desk only completes what the member agreed to themselves (``desk_consent``). For
# anyone else - and for an e-mail or phone with no account at all - it gives the ONE answer below: no name, no other
# gym, no ID or safety status, no sign of whether an account exists, and nothing is written or sent.
DESK_NEEDS_MEMBER = (
    "This person can't be registered from the desk yet. Ask them to open {gym} in their IronPulse app and choose this "
    "plan themselves - that's how they confirm it's really them - then record their cash payment here. "
    "New to IronPulse? Register them under Members first."
)


class ManualError(ValueError):
    def __init__(self, errors: dict[str, str]) -> None:
        super().__init__("; ".join(errors.values()))
        self.errors = errors


async def desk_consent(db: AsyncSession, *, owner: User, customer: User, gym: Gym, plan: Plan) -> bool:
    """Has ``customer`` already agreed to be a member of ``gym``? Only then may its owner register a cash membership for
    them at the desk. Agreed means one of:

    * a current (active or suspended) membership at this gym - a renewal or a plan change at the desk;
    * a purchase of THIS plan at this gym that the member started in their own app (waiting for the desk code or the
      payment) - the desk records the cash for it;
    * an account this owner registered under Members (``CustomerProfile.registered_by_id``), where the owner confirmed
      the member's consent.

    A purchase the desk itself created (``owner_manual``) never counts, nor does anything that ended (expired,
    cancelled, failed): a member who left or declined is a stranger to the desk again."""
    profile = customer.customer_profile if "customer_profile" not in inspect(customer).unloaded else None
    if profile is None:
        profile = await db.scalar(select(CustomerProfile).where(CustomerProfile.user_id == customer.id))
    if profile is not None and profile.registered_by_id == owner.id:
        return True
    return bool(await db.scalar(select(exists().where(
        Subscription.customer_id == customer.id,
        Subscription.gym_id == gym.id,
        or_(
            Subscription.status.in_((SubscriptionStatus.ACTIVE, SubscriptionStatus.SUSPENDED)),
            and_(Subscription.plan_id == plan.id, Subscription.status.in_(PENDING_STATUSES),
                 Subscription.channel != PurchaseChannel.OWNER_MANUAL),
        ),
    ))))


def _desk_limit_refusal(name: str) -> str:
    """The gyms-per-person refusal at the desk: the rule, never the other gyms (another gym's members are private)."""
    online, in_person = gym_limits.max_online(), gym_limits.max_in_person()
    return (f"{name} already holds as many memberships as one person can at a time (in person: at most {in_person} "
            f"gym{'s' if in_person != 1 else ''}; online: at most {online} gym{'s' if online != 1 else ''}), so this "
            "plan can't be added. They can see which membership to end under My memberships in their app - or choose "
            "a plan here that fits.")


async def find_customer(db: AsyncSession, ref: str, country_code: str | None = None) -> User | None:
    ref = (ref or "").strip()
    if not ref:
        return None
    stmt = select(User).options(joinedload(User.customer_profile)).where(
        User.role == Role.CUSTOMER, User.deleted_at.is_(None))
    if "@" in ref:
        stmt = stmt.where(User.email == ref.lower())
    else:
        try:
            phone = normalize_phone(ref, country_code)
        except ValueError:
            return None
        if not phone:
            return None
        stmt = stmt.where(User.phone_normalized == phone)
    return (await db.execute(stmt)).unique().scalar_one_or_none()


async def register_cash_subscription(
    db: AsyncSession,
    *,
    owner: User,
    customer_ref: str,
    plan_id: Any,
    branch_id: Any,
    note: str | None,
) -> Subscription:
    """Owner sells a membership for cash at the desk: validates, records a ``cash`` Transaction (SUCCESS) and
    activates. Raises ManualError(field errors). The caller audits and commits.

    Only for a member who agreed to this gym (``desk_consent``): anyone else - or an unknown e-mail / phone - gets the
    single neutral DESK_NEEDS_MEMBER answer, and nothing is written, so the desk can neither enrol another gym's member
    (and with it read and edit their health record) nor be used to look people up."""
    from app.services.id_verification import id_requirement_met

    errors: dict[str, str] = {}
    note = (note or "").strip()
    if len(note) > 300:
        errors["note"] = "Keep the note under 300 characters."
    try:
        pid = int(str(plan_id))
    except (TypeError, ValueError):
        pid = 0
    plan = await db.scalar(
        select(Plan).options(joinedload(Plan.gym)).join(Gym, Gym.id == Plan.gym_id)
        .where(Plan.id == pid, Gym.owner_id == owner.id)
    )
    if plan is None:
        errors["plan_id"] = "Choose one of your plans."
    elif not plan.is_enabled:
        errors["plan_id"] = "This plan is disabled. Enable it on the plans page first."
    elif not plan.gym.is_active:  # suspended by the platform (app/services/governance.py): nothing new is sold
        errors["plan_id"] = "This gym is suspended by the IronPulse platform, so it can't sell memberships."
    gym = plan.gym if plan is not None else None

    customer = await find_customer(db, customer_ref, gym.country_code if gym else None) if gym is not None else None
    consented = customer is not None and plan is not None and gym is not None and await desk_consent(
        db, owner=owner, customer=customer, gym=gym, plan=plan)
    if not (customer_ref or "").strip():
        errors["customer"] = "Enter the customer's e-mail address or phone number."
    elif gym is None:
        pass  # not one of this owner's plans: that is the error to fix first - nothing is said about the person
    elif not consented:
        # one answer for an unknown e-mail / phone AND for anyone who hasn't chosen this gym: the desk is no lookup
        errors["customer"] = DESK_NEEDS_MEMBER.format(gym=gym.name)
    elif not id_requirement_met(customer.id_status):
        errors["customer"] = f"{customer.full_name}'s ID card isn't verified yet ({customer.id_status.value.replace('_', ' ')})."
    elif customer.customer_profile is not None and customer.customer_profile.activity_suspended:
        errors["customer"] = f"{customer.full_name}'s gym activities are paused for safety. Reactivate them first."

    branch: Branch | None = None
    if plan is not None and plan.needs_branch:
        try:
            bid = int(str(branch_id))
        except (TypeError, ValueError):
            bid = 0
        branch = await db.scalar(select(Branch).where(Branch.id == bid, Branch.gym_id == plan.gym_id))
        if branch is None:
            errors["branch_id"] = "Choose the branch where the customer will train."
        elif not branch.is_open:
            errors["branch_id"] = "This branch is closed."
    if errors:
        raise ManualError(errors)
    assert plan is not None and gym is not None and customer is not None
    # gyms per person (app/services/gym_limits.py), under the person's lock like every other join - worded without the
    # other gyms' names (gym_limits' own refusal names them: right for the member, a leak to another gym's owner)
    if await gym_limits.join_refusal(db, customer.id, gym.id, plan.mode) is not None:
        raise ManualError({"customer": _desk_limit_refusal(customer.full_name)})

    sub = await db.scalar(
        select(Subscription).where(Subscription.customer_id == customer.id, Subscription.plan_id == plan.id,
                                   Subscription.status.in_(PENDING_STATUSES)).order_by(Subscription.id.desc()).limit(1)
    )
    if sub is None:
        sub = Subscription(customer_id=customer.id, gym_id=gym.id, plan_id=plan.id, fraud_flags=[],
                           status=SubscriptionStatus.PENDING_PAYMENT, channel=PurchaseChannel.OWNER_MANUAL,
                           price=plan.price, currency=plan.currency)
        db.add(sub)
    sub.channel = PurchaseChannel.OWNER_MANUAL
    sub.branch_id = branch.id if branch else None
    sub.price, sub.currency = plan.price, plan.currency
    sub.physically_present = True
    sub.status = SubscriptionStatus.PENDING_PAYMENT
    await db.flush()
    await db.execute(update(Transaction).where(Transaction.subscription_id == sub.id,
                                               Transaction.status.in_(OPEN_TX_STATUSES))
                     .values(status=TxStatus.CANCELLED))
    tx = Transaction(
        gym_id=gym.id, direction=TxDirection.INCOMING, category=TxCategory.SUBSCRIPTION, status=TxStatus.PENDING,
        amount=plan.price, currency=plan.currency, description=f"{plan.name} membership - {gym.name} (cash at desk)"[:255],
        party_label=customer.full_name[:160], provider="cash", customer_id=customer.id, subscription_id=sub.id,
        created_by_id=owner.id, meta={"note": note} if note else {},
    )
    db.add(tx)
    await db.flush()
    await activate_subscription(db, sub, tx=tx, actor=owner)
    return sub


# ------------------------------------------------------------------ cancellation
async def cancel_subscription(db: AsyncSession, sub: Subscription, *, actor: User) -> bool:
    """Cancel an active or pending subscription (no refunds). ``sub`` needs plan, gym and customer loaded."""
    if sub.status not in (SubscriptionStatus.ACTIVE, *PENDING_STATUSES):
        return False
    was_active = sub.status == SubscriptionStatus.ACTIVE
    now = utcnow()
    sub.status = SubscriptionStatus.CANCELLED
    sub.cancelled_at = now
    await db.execute(update(TrainerClient).where(TrainerClient.subscription_id == sub.id).values(active=False))
    await db.execute(update(OtpChallenge).where(OtpChallenge.subscription_id == sub.id,
                                                OtpChallenge.consumed_at.is_(None)).values(expires_at=now))
    await db.execute(update(Transaction).where(Transaction.subscription_id == sub.id,
                                               Transaction.status.in_(OPEN_TX_STATUSES))
                     .values(status=TxStatus.CANCELLED))
    if was_active:
        call_after_commit(db, partial(ai_guard.guard.forget_entitlement, sub.customer_id))  # AI tier ends with it
        await notify(
            db, sub.customer, "Membership cancelled",
            f"Your {sub.plan.name} membership at {sub.gym.name} has been cancelled. As stated at purchase, "
            "cancellations are not refunded.",
            category=NotificationCategory.PAYMENT, severity=Severity.INFO, link="/me/subscriptions",
        )
        if actor.id != sub.gym.owner_id:
            await notify(
                db, sub.gym.owner_id, f"Membership cancelled: {sub.customer.full_name}",
                f"{sub.customer.full_name} cancelled {sub.plan.name}.",
                category=NotificationCategory.PAYMENT, severity=Severity.INFO, link=f"/owner/customers/{sub.customer_id}",
            )
    return True


# ------------------------------------------------------------------ daily lifecycle (periodic)
# Each step walks its rows in chunks of SUBS_BATCH - one short transaction per chunk (app/services/batching.py) - so a
# day's expiry cohort, or a backlog after downtime, is never one huge transaction, never an unbounded id list (asyncpg
# refuses more than 32,767 parameters) and never one notification flush per member. Every chunk records its work with
# its notifications, so a crash or a spent SUBS_BUDGET_S leaves the rest to the next hourly run and nothing repeats.
SUBS_BATCH = 500
SUBS_BUDGET_S = 300.0  # per step and run
REMINDER_LOOKBACK_DAYS = 7  # "already reminded" probe: an index range of the member's recent notifications


async def expire_due_subscriptions(db: AsyncSession, today: date | None = None) -> int:
    """ACTIVE subscriptions past ``ends_on`` -> EXPIRED (+ trainer links closed, customer notified).

    Chunk = ``UPDATE ... WHERE id IN (SELECT id ... ORDER BY id LIMIT n FOR UPDATE SKIP LOCKED) RETURNING``, that
    chunk's trainer links, one bulk notification insert; ``db`` only holds the periodic's lock."""
    today = today or date.today()
    run = Chunks(holder=db, budget_s=SUBS_BUDGET_S)
    total = last_id = 0
    while await run.next():
        async with session_scope() as s:
            doomed = (
                select(Subscription.id)
                .where(Subscription.status == SubscriptionStatus.ACTIVE, Subscription.ends_on < today,
                       Subscription.id > last_id)
                .order_by(Subscription.id).limit(SUBS_BATCH).with_for_update(skip_locked=True)
            )
            rows = (await s.execute(
                update(Subscription)
                .where(Subscription.id.in_(doomed))
                .values(status=SubscriptionStatus.EXPIRED)
                .returning(Subscription.id, Subscription.customer_id, Subscription.plan_id)
                .execution_options(synchronize_session=False)
            )).all()
            if rows:
                await _close_expired(s, rows)
        if rows:
            last_id = max(r.id for r in rows)
        total += len(rows)
        run.done(len(rows), SUBS_BATCH)
    return total


async def _close_expired(db: AsyncSession, rows: Sequence[Any]) -> None:
    """One expiry chunk (at most SUBS_BATCH rows): trainer links, the members' notices, the AI tier."""
    await db.execute(update(TrainerClient).where(TrainerClient.subscription_id.in_([r.id for r in rows]))
                     .values(active=False).execution_options(synchronize_session=False))
    names = {
        pid: (pname, gname, slug)
        for pid, pname, gname, slug in (await db.execute(
            select(Plan.id, Plan.name, Gym.name, Gym.slug).join(Gym, Gym.id == Plan.gym_id)
            .where(Plan.id.in_({r.plan_id for r in rows}))
        )).all()
    }
    notes = []
    for r in rows:
        pname, gname, gym_slug = names.get(r.plan_id, ("Your plan", "the gym", None))
        notes.append(Note(r.customer_id, "Your membership has expired",
                          f"{pname} at {gname} has ended. Renew any time to keep training - your history is kept.",
                          link=f"/subscribe/{gym_slug}" if gym_slug else "/me/subscriptions",
                          category=NotificationCategory.REMINDER, severity=Severity.INFO))
    await notify_bulk(db, notes)
    for customer_id in sorted({r.customer_id for r in rows}):
        call_after_commit(db, partial(ai_guard.guard.forget_entitlement, customer_id))  # AI tier ends with it


async def send_expiry_reminders(db: AsyncSession, today: date | None = None, days: int = 3) -> int:
    """Remind customers 3 days before their membership ends (once per subscription), in keyset chunks."""
    today = today or date.today()
    target = today + timedelta(days=days)
    since = utcnow() - timedelta(days=REMINDER_LOOKBACK_DAYS)
    link_expr = func.concat("/me/subscriptions#sub-", Subscription.id)
    s2 = Subscription.__table__.alias("s2")
    renewed = exists(select(s2.c.id).where(
        s2.c.customer_id == Subscription.customer_id, s2.c.plan_id == Subscription.plan_id, s2.c.id != Subscription.id,
        s2.c.status == SubscriptionStatus.ACTIVE.value, s2.c.ends_on > target,
    ))
    # a reminder for ends_on == target can only have been sent in the last few days (ix_notifications_user_created)
    already = exists(select(Notification.id).where(
        Notification.user_id == Subscription.customer_id, Notification.created_at >= since,
        Notification.title == REMINDER_TITLE, Notification.link == link_expr,
    ))
    run = Chunks(holder=db, budget_s=SUBS_BUDGET_S)
    total = last_id = 0
    while await run.next():
        async with session_scope() as s:
            subs = (await s.execute(
                select(Subscription).options(joinedload(Subscription.plan), joinedload(Subscription.gym),
                                             joinedload(Subscription.customer))
                .where(Subscription.status == SubscriptionStatus.ACTIVE, Subscription.ends_on == target,
                       Subscription.id > last_id, ~already, ~renewed)
                .order_by(Subscription.id).limit(SUBS_BATCH)
            )).unique().scalars().all()
            if subs:
                total += len(await notify_bulk(s, [
                    Note(sub.customer, REMINDER_TITLE,
                         f"{sub.plan.name} at {sub.gym.name} ends on {sub.ends_on:%d %b %Y}. Renew now to keep your "
                         "trainer, AI coach and video access without a break.",
                         link=f"/me/subscriptions#sub-{sub.id}", category=NotificationCategory.REMINDER,
                         severity=Severity.INFO)
                    for sub in subs
                ]))
        if subs:
            last_id = subs[-1].id
        run.done(len(subs), SUBS_BATCH)
    return total


async def cancel_stale_pending(db: AsyncSession, older_than_days: int = 7) -> int:
    """Quietly cancel purchases that were started but never finished (chunked ``UPDATE ... RETURNING``)."""
    cutoff = utcnow() - timedelta(days=older_than_days)
    run = Chunks(holder=db, budget_s=SUBS_BUDGET_S)
    total = last_id = 0
    while await run.next():
        async with session_scope() as s:
            stale = (
                select(Subscription.id)
                .where(Subscription.status.in_(PENDING_STATUSES), Subscription.created_at < cutoff,
                       Subscription.id > last_id)
                .order_by(Subscription.id).limit(SUBS_BATCH).with_for_update(skip_locked=True)
            )
            ids = list((await s.execute(
                update(Subscription).where(Subscription.id.in_(stale))
                .values(status=SubscriptionStatus.CANCELLED, cancelled_at=utcnow())
                .returning(Subscription.id).execution_options(synchronize_session=False)
            )).scalars().all())
            if ids:
                await s.execute(update(Transaction).where(Transaction.subscription_id.in_(ids),
                                                          Transaction.status.in_(OPEN_TX_STATUSES))
                                .values(status=TxStatus.CANCELLED).execution_options(synchronize_session=False))
        if ids:
            last_id = max(ids)
        total += len(ids)
        run.done(len(ids), SUBS_BATCH)
    return total


@periodic("subscriptions_daily", every_seconds=3600, initial_delay=45)
async def _subscriptions_daily(db: AsyncSession) -> None:
    """Hourly + idempotent, so it effectively runs once a day per subscription. The steps commit separately: one
    failing step is logged and never blocks the others."""
    counts: dict[str, int] = {}
    for step, fn in (("expired", expire_due_subscriptions), ("reminded", send_expiry_reminders),
                     ("stale purchases cancelled", cancel_stale_pending)):
        try:
            counts[step] = await fn(db)
        except Exception:
            log.exception("subscriptions_daily: step %r failed - the other steps still run", step)
            counts[step] = 0
    if any(counts.values()):
        log.info("subscriptions: %s", ", ".join(f"{n} {step}" for step, n in counts.items()))


__all__ = [
    "PLAN_CATALOG", "PLAN_CODES", "MODE_LABELS", "seed_default_plans", "default_price", "gym_availability",
    "GymAvailability", "resolve_location", "eligibility_for", "branch_distances", "customer_block", "Block",
    "start_subscription", "PurchaseError", "presence_check", "issue_desk_otp", "verify_desk_otp", "OtpRateLimited",
    "active_challenge", "start_payment", "settle_order", "lock_transaction", "activate_subscription",
    "assign_trainer", "register_cash_subscription", "ManualError", "desk_consent", "DESK_NEEDS_MEMBER",
    "cancel_subscription", "expire_due_subscriptions",
    "send_expiry_reminders", "add_fraud_flag", "record_fraud_event",
]
