"""WP-D owner pages: plan management (prices, descriptions, enable/disable), the front desk with pending
desk OTPs, and cash registrations at the desk."""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from fastapi import APIRouter, Path, Request
from sqlalchemy import func, select
from sqlalchemy.orm import joinedload

from app.config import get_settings
from app.core.deps import DB, NotFound, OwnerUser, get_owned_gym
from app.core.templating import redirect, render
from app.core.utils import local_today, money, safe_zone, utcnow
from app.models import Branch, Gym, Notification, OtpChallenge, Plan, Subscription, Transaction, User
from app.models.enums import PlanMode, PurchaseChannel, SubscriptionStatus, TxCategory, TxDirection, TxStatus
from app.services import checkout as svc
from app.services.audit import audit

router = APIRouter()
MAX_ID = 2**63 - 1  # BIGINT ids: a larger path id is a 422 page, never a driver error (a 500)
IdParam = Annotated[int, Path(ge=1, le=MAX_ID)]

CODE_RE = re.compile(r"Code:\s*(\d{6})")
PLAN_GROUPS = (
    (PlanMode.OFFLINE, "In the gym",
     "Gym-floor access at one branch. Sold within 30 km of a published branch and confirmed at the desk."),
    (PlanMode.ONLINE, "Online", "Remote training from anywhere. Needs online training switched on for this gym."),
    (PlanMode.COMBO, "Gym + online", "Both together. Needs online training and at least one published branch."),
)


# ------------------------------------------------------------------ plans
def parse_price(raw: str) -> tuple[Decimal | None, str | None]:
    text = (raw or "").strip().replace(",", "").replace(" ", "")
    if not text:
        return None, "Enter a price."
    if not re.fullmatch(r"\d{1,12}(\.\d+)?", text):
        return None, "Enter a price like 1499 or 1499.50 (numbers only)."
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None, "Enter a valid price."
    if value.as_tuple().exponent < -2:  # type: ignore[operator]
        return None, "Use at most 2 decimal places."
    if value <= 0:
        return None, "The price must be more than zero."
    if value > svc.MAX_PLAN_PRICE:
        return None, "The price can't be more than 10,000,000."
    return value.quantize(Decimal("0.01")), None


async def _render_plans(request: Request, db: Any, owner: User, gym: Gym, *, errors: dict | None = None,
                        values: dict | None = None, status_code: int = 200):
    plans = list((await db.execute(select(Plan).where(Plan.gym_id == gym.id).order_by(Plan.sort_order, Plan.id)))
                 .scalars().all())
    availability = await svc.gym_availability(db, gym)
    counts = dict((await db.execute(
        select(Subscription.plan_id, func.count(Subscription.id))
        .where(Subscription.gym_id == gym.id, Subscription.status == SubscriptionStatus.ACTIVE)
        .group_by(Subscription.plan_id)
    )).all())
    now = utcnow()
    month_start = datetime.combine(local_today(owner.timezone).replace(day=1), time.min, tzinfo=safe_zone(owner.timezone))
    revenue = await db.scalar(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.gym_id == gym.id, Transaction.category == TxCategory.SUBSCRIPTION,
            Transaction.direction == TxDirection.INCOMING, Transaction.status == TxStatus.SUCCESS,
            Transaction.currency == gym.currency, Transaction.completed_at >= month_start,
        )
    )
    waiting = await db.scalar(
        select(func.count(OtpChallenge.id)).join(Subscription, Subscription.id == OtpChallenge.subscription_id)
        .where(Subscription.gym_id == gym.id, Subscription.status == SubscriptionStatus.PENDING_OTP,
               OtpChallenge.consumed_at.is_(None), OtpChallenge.expires_at > now,
               OtpChallenge.attempts < OtpChallenge.max_attempts)
    )
    reasons = {p.id: availability.unavailable_reason(p) for p in plans}
    groups = [
        {"mode": mode.value, "label": label, "blurb": blurb, "plans": [p for p in plans if p.mode == mode]}
        for mode, label, blurb in PLAN_GROUPS
    ]
    existing = {p.code for p in plans}
    return render(request, "owner/plans/index.html", {
        "gym": gym,
        "plans": plans,
        "groups": [g for g in groups if g["plans"]],
        "availability": availability,
        "reasons": reasons,
        "counts": counts,
        "errors": errors or {},
        "values": values or {},
        "missing": [c for c in svc.PLAN_CODES if c not in existing],
        "min_photos": get_settings().min_branch_photos,
        "trainer_total": sum(availability.trainers.values()),
        "trainers_online": availability.trainer_count(PlanMode.ONLINE),
        "trainers_offline": availability.trainer_count(PlanMode.OFFLINE),
        "stats": {
            "members": sum(counts.values()),
            "on_sale": sum(1 for r in reasons.values() if r is None),
            "revenue": revenue or Decimal("0"),
            "waiting": int(waiting or 0),
        },
    }, status_code=status_code)


@router.get("/owner/gyms/{gym_id}/plans")
async def plans_page(request: Request, gym_id: IdParam, db: DB, user: OwnerUser):
    gym = await get_owned_gym(db, gym_id, user)
    return await _render_plans(request, db, user, gym)


@router.post("/owner/gyms/{gym_id}/plans/seed")
async def seed_plans(request: Request, gym_id: IdParam, db: DB, user: OwnerUser):
    gym = await get_owned_gym(db, gym_id, user)
    before = int(await db.scalar(select(func.count(Plan.id)).where(Plan.gym_id == gym.id)) or 0)
    plans = await svc.seed_default_plans(db, gym)
    added = len(plans) - before
    if added:
        await audit(db, user, "plans.seed", "gym", gym.id, gym_id=gym.id,
                    detail={"added": added, "currency": gym.currency}, request=request)
    await db.commit()
    if added:
        return redirect(f"/owner/gyms/{gym.id}/plans",
                        f"Added {added} plan{'s' if added != 1 else ''} with suggested {gym.currency} prices. Review them below.")
    return redirect(f"/owner/gyms/{gym.id}/plans", "All 12 standard plans already exist.", "info")


@router.post("/owner/plans/{plan_id}")
async def update_plan(request: Request, plan_id: IdParam, db: DB, user: OwnerUser):
    plan = (await db.execute(
        select(Plan).options(joinedload(Plan.gym)).join(Gym, Gym.id == Plan.gym_id)
        .where(Plan.id == plan_id, Gym.owner_id == user.id)
    )).unique().scalar_one_or_none()
    if plan is None:
        raise NotFound("Plan not found.")
    form = await request.form()
    raw_price = str(form.get("price") or "")[:40]
    description = str(form.get("description") or "").strip()
    enabled = str(form.get("is_enabled") or "").lower() in ("1", "on", "true", "yes")
    errors: dict[str, str] = {}
    price, price_error = parse_price(raw_price)
    if price_error:
        errors["price"] = price_error
    if len(description) > 600:
        errors["description"] = "Keep the description under 600 characters."
    if errors:
        return await _render_plans(
            request, db, user, plan.gym, errors={plan.id: errors},
            values={plan.id: {"price": raw_price, "description": description[:2000], "is_enabled": enabled}},
            status_code=400,
        )
    assert price is not None
    changes: dict[str, Any] = {}
    if plan.price != price:
        changes["price"] = [str(plan.price), str(price)]
    if (plan.description or "") != description:
        changes["description"] = True
    if plan.is_enabled != enabled:
        changes["is_enabled"] = [plan.is_enabled, enabled]
    if not changes:
        return redirect(f"/owner/gyms/{plan.gym_id}/plans#plan-{plan.id}", "No changes to save.", "info")
    plan.price = price  # applies to new purchases only - subscriptions keep their own price
    plan.description = description or None
    plan.is_enabled = enabled
    await audit(db, user, "plans.update", "plan", plan.id, gym_id=plan.gym_id,
                detail={"code": plan.code, **changes}, request=request)
    await db.commit()
    state = "" if "is_enabled" not in changes else (" and is on sale again" if enabled else " and is no longer sold")
    return redirect(f"/owner/gyms/{plan.gym_id}/plans#plan-{plan.id}", f"{plan.name} saved{state}.")


# ------------------------------------------------------------------ front desk
async def _render_desk(request: Request, db: Any, owner: User, *, form: dict | None = None,
                       manual_errors: dict | None = None, status_code: int = 200):
    now = utcnow()
    challenges = (await db.execute(
        select(OtpChallenge)
        .options(joinedload(OtpChallenge.subscription).options(
            joinedload(Subscription.customer), joinedload(Subscription.plan), joinedload(Subscription.branch)))
        .join(Subscription, Subscription.id == OtpChallenge.subscription_id)
        .join(Gym, Gym.id == Subscription.gym_id)
        .where(Gym.owner_id == owner.id, Subscription.status == SubscriptionStatus.PENDING_OTP,
               OtpChallenge.consumed_at.is_(None), OtpChallenge.expires_at > now,
               OtpChallenge.attempts < OtpChallenge.max_attempts)
        .order_by(OtpChallenge.created_at.desc(), OtpChallenge.id.desc())
    )).unique().scalars().all()
    links = [f"/owner/desk#otp-{c.id}" for c in challenges]
    codes: dict[str, str] = {}
    if links:  # the code lives only in the owner's own notification (the challenge stores an HMAC)
        for link, body in (await db.execute(
            select(Notification.link, Notification.body).where(Notification.user_id == owner.id,
                                                               Notification.link.in_(links))
        )).all():
            m = CODE_RE.search(body or "")
            if m:
                codes[link] = m.group(1)
    rows = []
    for c in challenges:
        sub = c.subscription
        rows.append({
            "challenge": c, "sub": sub, "customer": sub.customer, "plan": sub.plan, "branch": sub.branch,
            "code": codes.get(f"/owner/desk#otp-{c.id}"),
            "attempts_left": max(0, c.max_attempts - c.attempts),
            "minutes_left": max(1, int((c.expires_at - now).total_seconds() // 60)),
            "flags": [f for f in (sub.fraud_flags or []) if f in ("no_gps", "presence_far")],
        })

    desk_channels = [PurchaseChannel.BRANCH_DESK_OTP, PurchaseChannel.OWNER_MANUAL]
    recent = (await db.execute(
        select(Subscription)
        .options(joinedload(Subscription.customer), joinedload(Subscription.plan), joinedload(Subscription.branch))
        .join(Gym, Gym.id == Subscription.gym_id)
        .where(Gym.owner_id == owner.id, Subscription.channel.in_(desk_channels),
               Subscription.activated_at.is_not(None), Subscription.activated_at > now - timedelta(days=7))
        .order_by(Subscription.activated_at.desc())
        .limit(12)
    )).unique().scalars().all()

    day_start = datetime.combine(local_today(owner.timezone), time.min, tzinfo=safe_zone(owner.timezone))
    totals = (await db.execute(
        select(Subscription.currency, func.count(Subscription.id), func.coalesce(func.sum(Subscription.price), 0))
        .join(Gym, Gym.id == Subscription.gym_id)
        .where(Gym.owner_id == owner.id, Subscription.channel.in_(desk_channels),
               Subscription.activated_at >= day_start)
        .group_by(Subscription.currency)
    )).all()
    today_count = sum(int(n) for _, n, _ in totals)
    if not totals:
        today_amount = "—"
    else:
        today_amount = " + ".join(money(total, cur) for cur, _, total in totals)

    plans = (await db.execute(
        select(Plan).join(Gym, Gym.id == Plan.gym_id)
        .where(Gym.owner_id == owner.id, Plan.is_enabled.is_(True))
        .order_by(Plan.gym_id, Plan.sort_order, Plan.id)
    )).scalars().all()
    gyms = (await db.execute(select(Gym).where(Gym.owner_id == owner.id).order_by(Gym.name))).scalars().all()
    branches = (await db.execute(
        select(Branch).join(Gym, Gym.id == Branch.gym_id)
        .where(Gym.owner_id == owner.id, Branch.is_open.is_(True)).order_by(Branch.name)
    )).scalars().all()
    plan_options = [{"gym": g, "plans": [p for p in plans if p.gym_id == g.id]} for g in gyms]
    return render(request, "owner/desk.html", {
        "challenges": rows,
        "recent": recent,
        "stats": {"today_count": today_count, "today_amount": today_amount, "mixed": len(totals) > 1},
        "plan_options": [o for o in plan_options if o["plans"]],
        "branch_options": branches,
        "form": form or {"customer": "", "plan_id": "", "branch_id": "", "note": ""},
        "manual_errors": manual_errors or {},
    }, status_code=status_code)


@router.get("/owner/desk")
async def desk_page(request: Request, db: DB, user: OwnerUser):
    return await _render_desk(request, db, user)


@router.post("/owner/subscriptions/manual")
async def manual_subscription(request: Request, db: DB, user: OwnerUser):
    form = await request.form()
    values = {
        "customer": str(form.get("customer") or "").strip()[:254],
        "plan_id": str(form.get("plan_id") or "").strip()[:20],
        "branch_id": str(form.get("branch_id") or "").strip()[:20],
        "note": str(form.get("note") or "").strip()[:1000],
    }
    try:
        sub = await svc.register_cash_subscription(
            db, owner=user, customer_ref=values["customer"], plan_id=values["plan_id"],
            branch_id=values["branch_id"], note=values["note"],
        )
    except svc.ManualError as exc:
        return await _render_desk(request, db, user, form=values, manual_errors=exc.errors, status_code=400)
    customer = await db.get(User, sub.customer_id)
    plan = await db.get(Plan, sub.plan_id)
    await audit(db, user, "subscriptions.manual", "subscription", sub.id, gym_id=sub.gym_id, request=request,
                detail={"customer_id": sub.customer_id, "plan": plan.code if plan else None, "amount": str(sub.price),
                        "currency": sub.currency, "branch_id": sub.branch_id, "note": values["note"] or None})
    await db.commit()
    name = customer.full_name if customer else "The customer"
    until = f" until {sub.ends_on:%d %b %Y}" if sub.ends_on else ""
    return redirect("/owner/desk", f"{name}'s {plan.name if plan else 'membership'} is active{until}. Cash payment recorded.")
