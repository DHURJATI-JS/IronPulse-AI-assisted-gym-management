"""WP-D customer purchase flow: subscribe (30 km rule), desk presence + OTP, payment (Cashfree or TEST MODE),
return + webhook verification, the test gateway, and "My memberships"."""

from __future__ import annotations

import json
import logging
import re
import secrets
from datetime import date
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.config import get_settings
from app.core.deps import DB, CustomerUser, NotFound
from app.core.templating import redirect, render
from app.core.utils import local_now, local_today, utcnow
from app.models import BranchHours, Gym, Plan, Subscription, Transaction, User
from app.models.enums import PlanMode, PurchaseChannel, SubscriptionStatus, ThreatCategory, TxStatus
from app.security.net import client_ip
from app.services import cashfree, gym_limits
from app.services import checkout as svc

log = logging.getLogger("ironpulse.checkout")
router = APIRouter()

ORDER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,45}$")
SUBSCRIBE_GROUPS = (
    (PlanMode.OFFLINE, "In the gym", "Full access to the gym floor at one branch, confirmed in person at its desk."),
    (PlanMode.ONLINE, "Online", "Train from anywhere with videos, live chat and optional coaching."),
    (PlanMode.COMBO, "Gym + online", "Train at your branch and from anywhere - the best of both."),
)


def _int(value: Any) -> int | None:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return n if 0 < n < 2**62 else None


async def _gym_by_slug(db: Any, slug: str) -> Gym:
    gym = await db.scalar(select(Gym).where(Gym.slug == slug[:140], Gym.is_active.is_(True)))
    if gym is None:
        raise NotFound("Gym not found.")
    return gym


# ------------------------------------------------------------------ subscribe
async def _render_subscribe(request: Request, db: Any, user: User, gym: Gym, *, plan_code: str | None, lat: Any,
                            lon: Any, branch_id: int | None, errors: dict | None = None, status_code: int = 200):
    block = await svc.customer_block(db, user)
    plans = list((await db.execute(
        select(Plan).where(Plan.gym_id == gym.id, Plan.is_enabled.is_(True)).order_by(Plan.sort_order, Plan.id)
    )).scalars().all())
    availability = await svc.gym_availability(db, gym)
    loc = svc.resolve_location(user, lat, lon)
    sellable = [p for p in plans if availability.is_available(p)]
    has_branch_plans = any(p.needs_branch for p in sellable)
    eligibility = svc.eligibility_for(availability.branches, loc) if has_branch_plans else None
    offline_ok = bool(eligibility and eligibility.eligible)
    if eligibility is not None and eligibility.method == "unknown":
        lock_reason = "Share your location to unlock"
    else:
        lock_reason = "Not near your area"
    slots = await gym_limits.gym_slots(db, user.id)  # 3 online / 1 in-person gyms per person: the card says why

    def reason(p: Plan) -> str | None:
        return (lock_reason if p.needs_branch and not offline_ok else None) or slots.short_reason(gym.id, p.mode)

    groups = []
    for mode, label, blurb in SUBSCRIBE_GROUPS:
        items = [{"plan": p, "disabled": reason(p) is not None, "reason": reason(p)}
                 for p in sellable if p.mode == mode]
        if items:
            groups.append({"mode": mode.value, "label": label, "blurb": blurb, "items": items,
                           "locked": all(i["disabled"] for i in items)})
    groups.sort(key=lambda g: g["locked"])  # what the customer can buy comes first
    selectable = {i["plan"].code: i["plan"] for g in groups for i in g["items"] if not i["disabled"]}
    selected = selectable.get(plan_code or "")
    subs = (await db.execute(
        select(Subscription).options(joinedload(Subscription.plan))
        .where(Subscription.customer_id == user.id, Subscription.gym_id == gym.id,
               Subscription.status.in_([SubscriptionStatus.ACTIVE, *svc.PENDING_STATUSES]))
        .order_by(Subscription.id.desc())
    )).unique().scalars().all()
    today = local_today(user.timezone)
    return render(request, "checkout/subscribe.html", {
        "gym": gym,
        "block": block,
        "groups": groups,
        "selected_code": selected.code if selected else None,
        "selected_plan": selected,
        "branches": svc.branch_distances(availability.branches, loc),
        "selected_branch_id": branch_id,
        "loc": loc,
        "eligibility": eligibility,
        "has_branch_plans": has_branch_plans,
        "errors": errors or {},
        "active_subs": [s for s in subs if s.status == SubscriptionStatus.ACTIVE and (s.ends_on is None or s.ends_on >= today)],
        "pending_subs": [s for s in subs if s.status in svc.PENDING_STATUSES],
        "slots": slots,
        # some plans are locked by the gyms-per-person limit: say why once, naming the gyms that hold the slots
        "limit_notice": next((slots.refusal(gym.id, p.mode) for p in sellable if slots.short_reason(gym.id, p.mode)),
                             None),
    }, status_code=status_code)


@router.get("/subscribe/{gym_slug}")
async def subscribe_page(request: Request, gym_slug: str, db: DB, user: CustomerUser):
    gym = await _gym_by_slug(db, gym_slug)
    q = request.query_params
    return await _render_subscribe(request, db, user, gym, plan_code=q.get("plan"), lat=q.get("lat"),
                                   lon=q.get("lon"), branch_id=_int(q.get("branch")))


@router.post("/subscribe/{gym_slug}")
async def subscribe_submit(request: Request, gym_slug: str, db: DB, user: CustomerUser):
    gym = await _gym_by_slug(db, gym_slug)
    form = await request.form()
    plan_code = str(form.get("plan") or "").strip()[:40]
    branch_id = _int(form.get("branch_id"))
    lat, lon = form.get("lat"), form.get("lon")
    ctx = dict(plan_code=plan_code, lat=lat, lon=lon, branch_id=branch_id)
    if await svc.customer_block(db, user) is not None:
        return await _render_subscribe(request, db, user, gym, **ctx, status_code=403)
    errors: dict[str, str] = {}
    plan = await db.scalar(select(Plan).where(Plan.gym_id == gym.id, Plan.code == plan_code)) if plan_code else None
    if plan is None or not plan.is_enabled:
        errors["plan"] = "Choose one of the plans below."
    else:
        availability = await svc.gym_availability(db, gym)
        loc = svc.resolve_location(user, lat, lon)
        try:
            sub, created = await svc.start_subscription(
                db, customer=user, gym=gym, plan=plan, availability=availability, branch_id=branch_id,
                location=loc, ip=client_ip(request),
            )
        except svc.PurchaseError as exc:
            if exc.field == "location":
                errors["plan"] = "Gym and combo plans need you within the distance limit of a branch - see the note above."
            else:
                errors[exc.field] = str(exc)
    if errors:
        return await _render_subscribe(request, db, user, gym, **ctx, errors=errors,
                                       status_code=409 if "limit" in errors else 400)
    await db.commit()
    if sub.status == SubscriptionStatus.PENDING_OTP:
        msg = "Saved. Gym memberships are confirmed at the branch desk - follow the steps below."
    else:
        msg = "Almost there - review your order and pay to activate your membership."
    return redirect(f"/checkout/{sub.id}", msg if created else "Picking up where you left off.", "success" if created else "info")


# ------------------------------------------------------------------ checkout page
async def _load_sub(db: Any, sid: int, user: User, *, lock: bool = False) -> Subscription:
    sub = await svc.load_customer_subscription(db, sid, user.id, lock=lock)
    if sub is None:
        raise NotFound("Purchase not found.")
    return sub


async def _render_checkout(request: Request, db: Any, sub: Subscription, *, step_hint: str | None = None,
                           presence_error: str | None = None, otp_error: str | None = None,
                           otp_notice: str | None = None, pay_error: str | None = None, status_code: int = 200):
    challenge = None
    hours_today = None
    if sub.status == SubscriptionStatus.PENDING_OTP:
        challenge = await svc.active_challenge(db, sub.id)
        step = "otp" if challenge is not None else ("directions" if step_hint == "directions" else "desk")
    elif sub.status == SubscriptionStatus.PENDING_PAYMENT:
        step = "pay"
    elif sub.status == SubscriptionStatus.ACTIVE:
        step = "done"
    else:
        step = "closed"
    if step == "directions" and sub.branch is not None:
        weekday = local_now(sub.branch.timezone).weekday()
        hours_today = await db.scalar(select(BranchHours).where(BranchHours.branch_id == sub.branch.id,
                                                                BranchHours.weekday == weekday))
    now = utcnow()
    return render(request, "checkout/checkout.html", {
        "sub": sub,
        "step": step,
        "challenge": challenge,
        "challenge_minutes": max(1, int((challenge.expires_at - now).total_seconds() // 60)) if challenge else 0,
        "attempts_left": (challenge.max_attempts - challenge.attempts) if challenge else 0,
        "hours_today": hours_today,
        "presence_radius": f"{get_settings().presence_radius_km:g}",
        "presence_error": presence_error,
        "otp_error": otp_error,
        "otp_notice": otp_notice,
        "pay_error": pay_error,
    }, status_code=status_code)


@router.get("/checkout/return")
async def checkout_return(request: Request, db: DB, user: CustomerUser):
    order_id = str(request.query_params.get("order_id") or "")
    if not ORDER_ID_RE.match(order_id):
        raise NotFound("Order not found.")
    tx = await svc.lock_transaction(db, order_id, customer_id=user.id)
    if tx is None or tx.subscription_id is None:
        raise NotFound("Order not found.")
    outcome = "pending"
    if tx.status == TxStatus.SUCCESS:
        outcome = await svc.settle_order(db, tx, paid=True, amount=tx.amount, currency=tx.currency, source="return")
    elif tx.status == TxStatus.FAILED:
        outcome = "mismatch" if "amount_mismatch" in (tx.meta or {}) else "failed"
    elif tx.provider == "test":
        state = (tx.meta or {}).get("test_status")
        if state == "PAID":
            outcome = await svc.settle_order(db, tx, paid=True, amount=tx.amount, currency=tx.currency,
                                             source="return", request=request)
        elif state == "FAILED":
            outcome = await svc.settle_order(db, tx, paid=False, source="return", request=request)
        elif tx.status == TxStatus.CANCELLED:
            outcome = "failed"
    elif tx.provider == "cashfree":
        try:
            order = await cashfree.get_order(order_id)  # never trust the redirect alone
        except (cashfree.CashfreeError, ValueError) as exc:
            log.warning("could not verify order %s: %s", order_id, exc)
            order = None
        if order is not None and order.is_paid:
            outcome = await svc.settle_order(db, tx, paid=True, amount=order.amount, currency=order.currency,
                                             source="return", request=request)
        elif order is not None and order.is_dead:
            outcome = await svc.settle_order(db, tx, paid=False, source="return", request=request)
        elif tx.status == TxStatus.CANCELLED:
            outcome = "failed"
    await db.commit()
    sub = await _load_sub(db, tx.subscription_id, user)
    return render(request, "checkout/result.html", {"outcome": outcome, "sub": sub, "tx": tx})


@router.get("/checkout/{sid}")
async def checkout_page(request: Request, sid: int, db: DB, user: CustomerUser):
    sub = await _load_sub(db, sid, user)
    return await _render_checkout(request, db, sub, step_hint=request.query_params.get("step"))


@router.post("/checkout/{sid}/presence")
async def checkout_presence(request: Request, sid: int, db: DB, user: CustomerUser):
    form = await request.form()
    sub = await _load_sub(db, sid, user, lock=True)
    if sub.status != SubscriptionStatus.PENDING_OTP or sub.branch is None:
        return redirect(f"/checkout/{sid}", "This purchase doesn't need a desk check any more.", "info")
    present = str(form.get("present") or "").strip().lower()
    if present not in ("yes", "no"):
        return await _render_checkout(request, db, sub, presence_error="Tell us whether you're at the branch.",
                                      status_code=400)
    if present == "no":
        return redirect(f"/checkout/{sid}?step=directions")
    block = await svc.customer_block(db, user)
    if block is not None:
        return await _render_checkout(request, db, sub, presence_error=block.message, status_code=403)
    result = svc.presence_check(sub.branch, form.get("lat"), form.get("lon"))
    if result.gps:
        sub.customer_latitude = svc.parse_coord(form.get("lat"), -90, 90)
        sub.customer_longitude = svc.parse_coord(form.get("lon"), -180, 180)
        sub.distance_km = result.distance_km
    if not result.allowed:
        svc.add_fraud_flag(sub, "presence_far")
        await db.commit()
        return await _render_checkout(request, db, sub, presence_error=result.message, status_code=400)
    if not result.gps:
        svc.add_fraud_flag(sub, "no_gps")
    sub.physically_present = True
    try:
        await svc.issue_desk_otp(db, sub, ip=client_ip(request))
    except svc.OtpRateLimited as exc:
        await db.commit()
        return await _render_checkout(request, db, sub, otp_notice=str(exc), status_code=429)
    await db.commit()
    return redirect(f"/checkout/{sid}", "We've sent a 6-digit code to the gym's desk. Ask the staff for it and enter it below.")


@router.post("/checkout/{sid}/otp")
async def checkout_otp(request: Request, sid: int, db: DB, user: CustomerUser):
    form = await request.form()
    code = re.sub(r"\s+", "", str(form.get("code") or ""))[:12]
    sub = await _load_sub(db, sid, user, lock=True)
    if sub.status != SubscriptionStatus.PENDING_OTP:
        return redirect(f"/checkout/{sid}", "The desk check for this purchase is already finished.", "info")
    if not re.fullmatch(r"\d{6}", code):
        return await _render_checkout(request, db, sub, otp_error="Enter the 6-digit code the desk gave you.",
                                      status_code=400)
    outcome = await svc.verify_desk_otp(db, sub, code, request=request)
    await db.commit()
    if outcome.result == "ok":
        return redirect(f"/checkout/{sid}", "Desk code confirmed - you can pay now.")
    if outcome.result == "locked":
        return redirect(f"/checkout/{sid}", "Too many incorrect codes - this purchase was locked for security. "
                                            "Please speak to the gym owner at the desk.", "error")
    if outcome.result == "expired":
        return await _render_checkout(request, db, sub, otp_notice="Your desk code has expired. Tap “Yes, I'm at the "
                                      "desk” to get a new one.", status_code=400)
    left = outcome.attempts_left
    return await _render_checkout(request, db, sub, otp_error=f"That code isn't right. {left} {'try' if left == 1 else 'tries'} left.",
                                  status_code=400)


@router.post("/checkout/{sid}/pay")
async def checkout_pay(request: Request, sid: int, db: DB, user: CustomerUser):
    await gym_limits.lock(db, user.id)  # lock order: person -> subscription -> transaction (app/services/gym_limits.py)
    sub = await _load_sub(db, sid, user, lock=True)
    if sub.status == SubscriptionStatus.ACTIVE:
        return redirect(f"/checkout/{sid}", "This membership is already active.", "info")
    if sub.status != SubscriptionStatus.PENDING_PAYMENT:
        return redirect(f"/checkout/{sid}", "This purchase can't be paid right now.", "warning")
    if sub.channel == PurchaseChannel.BRANCH_DESK_OTP and sub.otp_verified_at is None:
        return redirect(f"/checkout/{sid}", "Confirm the desk code first.", "warning")
    block = await svc.customer_block(db, user)
    if block is not None:
        return await _render_checkout(request, db, sub, pay_error=block.message, status_code=403)
    if not sub.plan.is_enabled:
        return await _render_checkout(request, db, sub, pay_error="This plan is no longer on sale. Please choose another one.",
                                      status_code=400)
    # the gyms-per-person limit again, BEFORE any money moves (an unfinished purchase may predate a newer join)
    over = await gym_limits.join_refusal(db, user.id, sub.gym_id, sub.plan.mode, exclude_sub_id=sub.id)
    if over is not None:
        return await _render_checkout(request, db, sub, pay_error=over, status_code=409)
    try:
        started = await svc.start_payment(db, sub)
    except cashfree.CashfreeError as exc:
        return await _render_checkout(request, db, sub, pay_error=str(exc), status_code=502)
    except ValueError:
        return await _render_checkout(request, db, sub, pay_error="This order can't be paid online. Please contact the gym.",
                                      status_code=400)
    await db.commit()
    return redirect(started.redirect_url)


async def _customer_tx(db: Any, sub: Subscription, order_id: str, provider: str) -> Transaction:
    if not ORDER_ID_RE.match(order_id or ""):
        raise NotFound("Order not found.")
    tx = await db.scalar(select(Transaction).where(
        Transaction.provider_order_id == order_id, Transaction.subscription_id == sub.id,
        Transaction.customer_id == sub.customer_id, Transaction.provider == provider))
    if tx is None:
        raise NotFound("Order not found.")
    return tx


@router.get("/checkout/{sid}/gateway")
async def checkout_gateway(request: Request, sid: int, db: DB, user: CustomerUser):
    sub = await _load_sub(db, sid, user)
    order_id = str(request.query_params.get("order_id") or "")
    tx = await _customer_tx(db, sub, order_id, "cashfree")
    if tx.status != TxStatus.PENDING or not tx.payment_session_id:
        return redirect(f"/checkout/return?order_id={order_id}")
    return render(request, "checkout/cashfree.html", {"sub": sub, "tx": tx, "sdk_mode": cashfree.sdk_mode()})


@router.get("/checkout/{sid}/test-gateway")
async def test_gateway_page(request: Request, sid: int, db: DB, user: CustomerUser):
    if not get_settings().payments_test_mode:
        raise NotFound()
    sub = await _load_sub(db, sid, user)
    order_id = str(request.query_params.get("order_id") or "")
    tx = await _customer_tx(db, sub, order_id, "test")
    if tx.status != TxStatus.PENDING:
        return redirect(f"/checkout/return?order_id={order_id}")
    return render(request, "checkout/test_gateway.html", {"sub": sub, "tx": tx})


@router.post("/checkout/{sid}/test-gateway")
async def test_gateway_submit(request: Request, sid: int, db: DB, user: CustomerUser):
    if not get_settings().payments_test_mode:
        raise NotFound()
    form = await request.form()
    order_id = str(form.get("order_id") or "")[:64]
    decision = str(form.get("decision") or "").lower()
    if not ORDER_ID_RE.match(order_id):
        raise NotFound("Order not found.")
    tx = await svc.lock_transaction(db, order_id, customer_id=user.id)
    if tx is None or tx.subscription_id != sid or tx.provider != "test":
        raise NotFound("Order not found.")
    if decision not in ("approve", "decline"):
        return redirect(f"/checkout/{sid}/test-gateway?order_id={order_id}", "Choose Approve or Decline.", "warning")
    if tx.status == TxStatus.PENDING:
        approve = decision == "approve"
        tx.meta = {**(tx.meta or {}), "test_status": "PAID" if approve else "FAILED"}
        await svc.settle_order(db, tx, paid=approve, amount=tx.amount, currency=tx.currency,
                               payment_id=f"TESTPAY-{secrets.token_hex(5).upper()}" if approve else None,
                               source="test_gateway", request=request)
        await db.commit()
    return redirect(f"/checkout/return?order_id={order_id}")


# ------------------------------------------------------------------ Cashfree webhook (CSRF-exempt)
async def _shop_webhook(db: Any, order_id: str, order: dict, payment: dict, event_type: str) -> JSONResponse:
    """Dispatch a verified Cashfree event for a shop order (IPO-*) to the shop payment service (idempotent)."""
    from app.services.shop_payments import handle_payment_failure, handle_payment_success

    pay_status = str(payment.get("payment_status") or "").upper()
    if event_type == "PAYMENT_SUCCESS_WEBHOOK" or pay_status == "SUCCESS":
        result = await handle_payment_success(
            db, order_id, payment.get("payment_amount", order.get("order_amount")),
            payment.get("payment_currency") or order.get("order_currency"),
            str(payment.get("cf_payment_id") or "") or None, source="webhook",
        )
    elif event_type in ("PAYMENT_FAILED_WEBHOOK", "PAYMENT_USER_DROPPED_WEBHOOK") or pay_status in ("FAILED", "USER_DROPPED"):
        result = await handle_payment_failure(db, order_id, reason=event_type or pay_status, source="webhook")
    else:
        result = "ignored"
    await db.commit()  # the shop handlers never commit themselves
    return JSONResponse({"ok": True, "result": result})


@router.post("/payments/cashfree/webhook", include_in_schema=False)
async def cashfree_webhook(request: Request, db: DB):
    raw = await request.body()
    if not get_settings().cashfree_enabled:
        return JSONResponse({"error": "Online payments are not configured"}, status_code=503)
    if not cashfree.verify_webhook_signature(raw, request.headers.get("x-webhook-timestamp"),
                                             request.headers.get("x-webhook-signature")):
        try:
            from app.security.bans import security_events

            security_events.record_from_request(request, ThreatCategory.FRAUD, 2, "invalid or stale Cashfree webhook signature")
        except Exception:  # pragma: no cover - reporting must never break the response
            pass
        return JSONResponse({"error": "Invalid signature"}, status_code=401)
    try:
        payload = json.loads(raw)
    except ValueError:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    data = payload.get("data") if isinstance(payload, dict) else None
    data = data if isinstance(data, dict) else {}
    order = data.get("order") if isinstance(data.get("order"), dict) else {}
    payment = data.get("payment") if isinstance(data.get("payment"), dict) else {}
    event_type = str(payload.get("type") or "") if isinstance(payload, dict) else ""
    order_id = str(order.get("order_id") or "")
    if order_id.startswith("IPO-"):  # gym-shop orders (WP-L) share this signed webhook
        return await _shop_webhook(db, order_id, order, payment, event_type)
    tx = await svc.lock_transaction(db, order_id) if ORDER_ID_RE.match(order_id) else None
    if tx is None or tx.provider != "cashfree":
        return JSONResponse({"ok": True, "result": "ignored"})
    pay_status = str(payment.get("payment_status") or "").upper()
    if event_type == "PAYMENT_SUCCESS_WEBHOOK" or pay_status == "SUCCESS":
        amount = payment.get("payment_amount", order.get("order_amount"))
        currency = payment.get("payment_currency") or order.get("order_currency")
        result = await svc.settle_order(db, tx, paid=True, amount=amount, currency=currency,
                                        payment_id=str(payment.get("cf_payment_id") or "") or None,
                                        source="webhook", request=request)
    elif event_type in ("PAYMENT_FAILED_WEBHOOK", "PAYMENT_USER_DROPPED_WEBHOOK") or pay_status in ("FAILED", "USER_DROPPED"):
        # the order can still be retried on Cashfree's page, so only record the attempt
        tx.meta = {**(tx.meta or {}), "last_failure": {"type": event_type, "status": pay_status,
                                                       "message": str(payment.get("payment_message") or "")[:200],
                                                       "at": utcnow().isoformat()}}
        result = "noted"
    else:
        result = "ignored"
    await db.commit()
    return JSONResponse({"ok": True, "result": result})


# ------------------------------------------------------------------ my memberships
@router.get("/me/subscriptions")
async def my_subscriptions(request: Request, db: DB, user: CustomerUser):
    subs = (await db.execute(
        select(Subscription)
        .options(joinedload(Subscription.plan), joinedload(Subscription.gym), joinedload(Subscription.branch),
                 joinedload(Subscription.trainer))
        .where(Subscription.customer_id == user.id)
        .order_by(Subscription.created_at.desc(), Subscription.id.desc())
        .limit(200)
    )).unique().scalars().all()
    today = local_today(user.timezone)
    active = sorted((s for s in subs if s.status == SubscriptionStatus.ACTIVE),
                    key=lambda s: (s.ends_on or date.max, s.id))
    pending = [s for s in subs if s.status in svc.PENDING_STATUSES]
    history = [s for s in subs if s.status not in (SubscriptionStatus.ACTIVE, *svc.PENDING_STATUSES)]
    meta: dict[int, dict[str, Any]] = {}
    for s in active:
        start = s.starts_on or today
        end = s.ends_on or today
        total = max(1, (end - start).days)
        meta[s.id] = {
            "total": total,
            "elapsed": min(total, max(0, (today - start).days)),
            "days_left": max(0, (end - today).days),
            "upcoming": start > today,
        }
    return render(request, "customer/subscriptions.html",
                  {"active": active, "pending": pending, "history": history, "meta": meta,
                   "slots": await gym_limits.gym_slots(db, user.id)})


@router.post("/me/subscriptions/{sid}/cancel")
async def cancel_my_subscription(request: Request, sid: int, db: DB, user: CustomerUser):
    sub = await _load_sub(db, sid, user, lock=True)
    was_active = sub.status == SubscriptionStatus.ACTIVE
    if not await svc.cancel_subscription(db, sub, actor=user):
        return redirect("/me/subscriptions", "This membership can't be cancelled.", "warning")
    await db.commit()
    if was_active:
        return redirect("/me/subscriptions", f"{sub.plan.name} at {sub.gym.name} was cancelled.", "info")
    return redirect("/me/subscriptions", "Unfinished purchase discarded.", "info")
