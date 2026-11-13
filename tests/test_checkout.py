"""WP-D: subscribe flow (30 km rule, ID/suspension gates, server-side prices), desk presence + OTP,
test gateway payments, trainer auto-assignment, "My memberships", cancellation."""

from __future__ import annotations

import asyncio
import re
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.core.utils import local_today, utcnow
from app.db import QueryCounter
from app.models import (
    CustomerProfile,
    Notification,
    OtpChallenge,
    SecurityEvent,
    Subscription,
    TrainerClient,
    Transaction,
)
from app.models.enums import (
    IdStatus,
    PurchaseChannel,
    ShiftMode,
    SubscriptionStatus,
    ThreatCategory,
    TxStatus,
    WorkMode,
)
from app.security.crypto import hmac_hex
from tests.conftest import login_as, post_form
from tests.factories import (
    INDIRANAGAR,
    KORAMANGALA,
    MYSURU,
    WHITEFIELD,
    make_branch,
    make_customer,
    make_employee,
    make_gym,
    make_owner,
    make_plans,
    make_subscription,
)

NOT_NEAR = "not near your area"
VISIT_OWNER = "visit the gym owner directly for offline registration"


# ------------------------------------------------------------------ helpers (also used by test_plans / test_cashfree)
async def setup_gym(db: Any, *, coords: tuple[float, float] = KORAMANGALA, city: str = "Bengaluru", online: bool = True,
                    offline: bool = True, trainer_modes: tuple[WorkMode, ...] = (WorkMode.BOTH,),
                    currency: str = "INR") -> SimpleNamespace:
    owner = await make_owner(db)
    gym = await make_gym(db, owner, online=online, offline=offline, currency=currency)
    branch = await make_branch(db, gym, coords=coords, city=city, name="Koramangala Club")
    plans = await make_plans(db, gym)
    trainers = [await make_employee(db, gym, work_mode=m) for m in trainer_modes]
    return SimpleNamespace(owner=owner, gym=gym, branch=branch, plans={p.code: p for p in plans}, trainers=trainers)


async def near_customer(db: Any, **kwargs: Any):
    kwargs.setdefault("latitude", INDIRANAGAR[0])
    kwargs.setdefault("longitude", INDIRANAGAR[1])
    kwargs.setdefault("city", "Bengaluru")
    return await make_customer(db, **kwargs)


async def subscribe(client: Any, gym: Any, plan_code: str, **extra: Any):
    data = {"plan": plan_code, **{k: str(v) for k, v in extra.items()}}
    return await post_form(client, f"/subscribe/{gym.slug}", data)


def fresh(stmt: Any) -> Any:
    """Re-read rows the app changed in another session (async sessions can't lazy-refresh expired objects)."""
    return stmt.execution_options(populate_existing=True)


async def latest_sub(db: Any, customer_id: int) -> Subscription | None:
    return await db.scalar(fresh(select(Subscription).where(Subscription.customer_id == customer_id)
                                 .order_by(Subscription.id.desc()).limit(1)))


async def notifications_for(db: Any, user_id: int) -> list[Notification]:
    return list((await db.execute(fresh(select(Notification).where(Notification.user_id == user_id)
                                        .order_by(Notification.id)))).scalars().all())


async def desk_code_for(db: Any, owner_id: int) -> str:
    notes = await notifications_for(db, owner_id)
    for n in reversed(notes):
        m = re.search(r"Code:\s*(\d{6})", n.body)
        if m:
            return m.group(1)
    raise AssertionError("no desk code notification for the owner")


async def start_offline(client: Any, db: Any, env: SimpleNamespace, customer: Any, code: str = "offline") -> Subscription:
    r = await subscribe(client, env.gym, code, branch_id=env.branch.id)
    assert r.status_code == 303, r.text[:500]
    sub = await latest_sub(db, customer.id)
    assert sub is not None and sub.status == SubscriptionStatus.PENDING_OTP
    return sub


async def desk_verified(client: Any, db: Any, env: SimpleNamespace, customer: Any, code: str = "offline") -> Subscription:
    sub = await start_offline(client, db, env, customer, code)
    r = await post_form(client, f"/checkout/{sub.id}/presence", {"present": "yes", "lat": KORAMANGALA[0], "lon": KORAMANGALA[1]})
    assert r.status_code == 303
    otp = await desk_code_for(db, env.owner.id)
    r = await post_form(client, f"/checkout/{sub.id}/otp", {"code": otp})
    assert r.status_code == 303
    return await latest_sub(db, customer.id)


async def pay_in_test_gateway(client: Any, db: Any, sub: Subscription, decision: str = "approve"):
    r = await post_form(client, f"/checkout/{sub.id}/pay", {})
    assert r.status_code == 303, r.text[:300]
    loc = r.headers["location"]
    assert loc.startswith(f"/checkout/{sub.id}/test-gateway?order_id=IPS{sub.id}-")
    order_id = loc.split("order_id=")[1]
    page = await client.get(loc)
    assert page.status_code == 200 and "Test mode" in page.text
    r = await post_form(client, f"/checkout/{sub.id}/test-gateway", {"order_id": order_id, "decision": decision})
    assert r.status_code == 303 and r.headers["location"] == f"/checkout/return?order_id={order_id}"
    return order_id


# ------------------------------------------------------------------ access & gating
async def test_subscribe_requires_customer(client, db):
    env = await setup_gym(db)
    r = await client.get(f"/subscribe/{env.gym.slug}")
    assert r.status_code == 303 and r.headers["location"].startswith("/auth/login")
    await login_as(client, env.owner)
    r = await client.get(f"/subscribe/{env.gym.slug}")
    assert r.status_code == 403


async def test_unknown_or_inactive_gym_404(client, db):
    env = await setup_gym(db)
    cust = await near_customer(db)
    await login_as(client, cust)
    assert (await client.get("/subscribe/no-such-gym")).status_code == 404
    env.gym.is_active = False
    await db.commit()
    assert (await client.get(f"/subscribe/{env.gym.slug}")).status_code == 404


async def test_subscribe_page_renders_plans_and_branch(client, db):
    env = await setup_gym(db)
    cust = await near_customer(db)
    await login_as(client, cust)
    r = await client.get(f"/subscribe/{env.gym.slug}?plan=online_ai")
    assert r.status_code == 200
    html = r.text
    assert "Join " + env.gym.name in html
    assert 'value="online_ai" class="sr-only" checked' in html
    assert "Koramangala Club" in html and "km" in html
    assert "Pre-recorded videos are included in every plan" in html
    assert "<script>" not in html and 'style="' not in html and "onclick" not in html


async def test_unverified_id_blocked(client, db):
    env = await setup_gym(db)
    cust = await near_customer(db, id_status=IdStatus.PENDING)
    await login_as(client, cust)
    r = await client.get(f"/subscribe/{env.gym.slug}")
    assert r.status_code == 200 and "/account/identity" in r.text and "still being verified" in r.text
    r = await subscribe(client, env.gym, "online")
    assert r.status_code == 403
    assert await latest_sub(db, cust.id) is None

    missing = await near_customer(db, id_status=IdStatus.MISSING)
    await login_as(client, missing)
    r = await client.get(f"/subscribe/{env.gym.slug}")
    assert "Upload ID card" in r.text


async def test_suspended_customer_blocked(client, db):
    env = await setup_gym(db)
    cust = await near_customer(db)
    prof = await db.get(CustomerProfile, cust.id)
    prof.activity_suspended = True
    prof.suspension_reason = "Blood pressure reading needs attention"
    await db.commit()
    await login_as(client, cust)
    r = await client.get(f"/subscribe/{env.gym.slug}")
    assert "Gym activities are paused" in r.text
    r = await subscribe(client, env.gym, "online")
    assert r.status_code == 403
    assert await latest_sub(db, cust.id) is None


# ------------------------------------------------------------------ online purchase + server-side price
async def test_online_subscribe_ignores_tampered_price(client, db):
    env = await setup_gym(db)
    cust = await near_customer(db)
    await login_as(client, cust)
    r = await subscribe(client, env.gym, "online_ai", price="1", amount="1", currency="USD")
    assert r.status_code == 303
    sub = await latest_sub(db, cust.id)
    assert r.headers["location"] == f"/checkout/{sub.id}"
    plan = env.plans["online_ai"]
    assert sub.price == plan.price == Decimal("1199") and sub.currency == "INR"
    assert sub.status == SubscriptionStatus.PENDING_PAYMENT and sub.channel == PurchaseChannel.ONLINE_CHECKOUT
    assert sub.created_ip == "127.0.0.1" and sub.branch_id is None


async def test_pending_subscription_is_reused(client, db):
    env = await setup_gym(db)
    cust = await near_customer(db)
    await login_as(client, cust)
    await subscribe(client, env.gym, "online")
    r = await subscribe(client, env.gym, "online")
    assert r.status_code == 303
    count = await db.scalar(select(func.count(Subscription.id)).where(Subscription.customer_id == cust.id))
    assert count == 1


async def test_disabled_plan_hidden_and_rejected(client, db):
    env = await setup_gym(db)
    env.plans["online_trainer"].is_enabled = False
    await db.commit()
    cust = await near_customer(db)
    await login_as(client, cust)
    r = await client.get(f"/subscribe/{env.gym.slug}")
    assert 'value="online_trainer"' not in r.text and 'value="online"' in r.text
    r = await subscribe(client, env.gym, "online_trainer")
    assert r.status_code == 400
    assert await latest_sub(db, cust.id) is None


async def test_online_plans_need_online_open_and_trainer_plans_need_trainer(client, db):
    env = await setup_gym(db, online=False, trainer_modes=())
    cust = await near_customer(db)
    await login_as(client, cust)
    r = await client.get(f"/subscribe/{env.gym.slug}")
    assert 'value="online"' not in r.text and 'value="combo"' not in r.text  # online switched off
    assert 'value="offline"' in r.text and 'value="offline_trainer"' not in r.text  # no trainer
    assert (await subscribe(client, env.gym, "online")).status_code == 400
    assert (await subscribe(client, env.gym, "offline_trainer", branch_id=env.branch.id)).status_code == 400


# ------------------------------------------------------------------ 30 km rule
async def test_offline_near_gps_creates_pending_otp(client, db):
    env = await setup_gym(db)
    cust = await make_customer(db)  # no saved location: GPS posted with the form
    await login_as(client, cust)
    r = await subscribe(client, env.gym, "offline", branch_id=env.branch.id, lat=INDIRANAGAR[0], lon=INDIRANAGAR[1])
    assert r.status_code == 303
    sub = await latest_sub(db, cust.id)
    assert sub.status == SubscriptionStatus.PENDING_OTP and sub.channel == PurchaseChannel.BRANCH_DESK_OTP
    assert sub.branch_id == env.branch.id
    assert sub.customer_latitude == pytest.approx(INDIRANAGAR[0]) and 3 < sub.distance_km < 10


async def test_offline_far_shows_exact_message_and_is_rejected(client, db):
    env = await setup_gym(db)
    cust = await make_customer(db, latitude=MYSURU[0], longitude=MYSURU[1], city="Mysuru")
    await login_as(client, cust)
    r = await client.get(f"/subscribe/{env.gym.slug}")
    assert r.status_code == 200
    assert NOT_NEAR in r.text and VISIT_OWNER in r.text
    assert re.search(r'value="offline" class="sr-only"\s+disabled', r.text)
    r = await subscribe(client, env.gym, "offline", branch_id=env.branch.id)
    assert r.status_code == 400 and NOT_NEAR in r.text and VISIT_OWNER in r.text
    r = await subscribe(client, env.gym, "combo", branch_id=env.branch.id)
    assert r.status_code == 400
    assert await latest_sub(db, cust.id) is None
    # online plans stay available to far-away customers
    assert (await subscribe(client, env.gym, "online")).status_code == 303


async def test_posted_gps_overrides_saved_location(client, db):
    env = await setup_gym(db)
    cust = await make_customer(db, latitude=MYSURU[0], longitude=MYSURU[1])
    await login_as(client, cust)
    r = await client.get(f"/subscribe/{env.gym.slug}?lat={WHITEFIELD[0]}&lon={WHITEFIELD[1]}")
    assert NOT_NEAR not in r.text
    r = await subscribe(client, env.gym, "combo", branch_id=env.branch.id, lat=WHITEFIELD[0], lon=WHITEFIELD[1])
    assert r.status_code == 303


async def test_city_fallback(client, db):
    env = await setup_gym(db, city="Bengaluru")
    cust = await make_customer(db, city="Bangalore")  # alias of Bengaluru, no coordinates
    await login_as(client, cust)
    r = await client.get(f"/subscribe/{env.gym.slug}")
    assert NOT_NEAR not in r.text and "Using your city" in r.text
    assert (await subscribe(client, env.gym, "offline", branch_id=env.branch.id)).status_code == 303

    other = await make_customer(db, city="Mysore")
    await login_as(client, other)
    r = await client.get(f"/subscribe/{env.gym.slug}")
    assert NOT_NEAR in r.text and VISIT_OWNER in r.text
    assert (await subscribe(client, env.gym, "offline", branch_id=env.branch.id)).status_code == 400


async def test_unknown_location_asks_for_location(client, db):
    env = await setup_gym(db)
    cust = await make_customer(db)  # no coordinates, no city
    await login_as(client, cust)
    r = await client.get(f"/subscribe/{env.gym.slug}")
    assert "Share your location" in r.text and "Location needed" in r.text
    assert (await subscribe(client, env.gym, "offline", branch_id=env.branch.id)).status_code == 400
    assert await latest_sub(db, cust.id) is None


async def test_offline_requires_a_published_branch_of_this_gym(client, db):
    env = await setup_gym(db)
    other = await setup_gym(db)
    cust = await near_customer(db)
    await login_as(client, cust)
    r = await subscribe(client, env.gym, "offline", branch_id=other.branch.id)
    assert r.status_code == 400 and "Choose the branch" in r.text
    r = await subscribe(client, env.gym, "offline")
    assert r.status_code == 400


# ------------------------------------------------------------------ desk presence + OTP
async def test_presence_no_shows_directions(client, db):
    env = await setup_gym(db)
    cust = await near_customer(db)
    await login_as(client, cust)
    sub = await start_offline(client, db, env, cust)
    page = await client.get(f"/checkout/{sub.id}")
    assert "Offline memberships are bought at the branch desk" in page.text
    assert "Are you physically present at Koramangala Club?" in page.text
    r = await post_form(client, f"/checkout/{sub.id}/presence", {"present": "no"})
    assert r.status_code == 303 and r.headers["location"] == f"/checkout/{sub.id}?step=directions"
    page = await client.get(r.headers["location"])
    assert "Visit Koramangala Club to finish" in page.text
    assert await db.scalar(select(func.count(OtpChallenge.id))) == 0


async def test_presence_far_gps_blocked_and_flagged(client, db):
    env = await setup_gym(db)
    cust = await near_customer(db)
    await login_as(client, cust)
    sub = await start_offline(client, db, env, cust)
    r = await post_form(client, f"/checkout/{sub.id}/presence", {"present": "yes", "lat": INDIRANAGAR[0], "lon": INDIRANAGAR[1]})
    assert r.status_code == 400 and "Desk purchases need you inside the branch" in r.text
    sub = await latest_sub(db, cust.id)
    assert "presence_far" in sub.fraud_flags and sub.fraud_score > 0
    assert sub.status == SubscriptionStatus.PENDING_OTP
    assert await db.scalar(select(func.count(OtpChallenge.id))) == 0
    assert not [n for n in await notifications_for(db, env.owner.id) if "Desk OTP" in n.title]


async def test_desk_otp_goes_only_to_owner(client, db):
    env = await setup_gym(db)
    cust = await near_customer(db)
    await login_as(client, cust)
    sub = await start_offline(client, db, env, cust)
    r = await post_form(client, f"/checkout/{sub.id}/presence", {"present": "yes"})  # no GPS -> allowed + flagged
    assert r.status_code == 303
    sub = await latest_sub(db, cust.id)
    assert "no_gps" in sub.fraud_flags and sub.physically_present is True
    code = await desk_code_for(db, env.owner.id)
    owner_notes = [n for n in await notifications_for(db, env.owner.id) if n.title.startswith("Desk OTP")]
    assert len(owner_notes) == 1
    body = owner_notes[0].body
    assert cust.full_name in body and "Offline" in body and "1,499.00" in body and owner_notes[0].link.startswith("/owner/desk")
    # the customer never receives the code
    for n in await notifications_for(db, cust.id):
        assert code not in n.body and code not in n.title
    ch = await db.scalar(select(OtpChallenge).where(OtpChallenge.subscription_id == sub.id))
    assert ch.code_hash == hmac_hex(code, "otp") and code not in ch.code_hash
    assert ch.recipient_user_id == env.owner.id and ch.max_attempts == 5
    assert 9 <= (ch.expires_at - utcnow()).total_seconds() / 60 <= 10.1
    page = await client.get(f"/checkout/{sub.id}")
    assert "Enter the desk code" in page.text and code not in page.text


async def test_wrong_otp_then_lockout(client, db):
    env = await setup_gym(db)
    cust = await near_customer(db)
    await login_as(client, cust)
    sub = await start_offline(client, db, env, cust)
    await post_form(client, f"/checkout/{sub.id}/presence", {"present": "yes", "lat": KORAMANGALA[0], "lon": KORAMANGALA[1]})
    code = await desk_code_for(db, env.owner.id)
    wrong = "000000" if code != "000000" else "111111"
    r = await post_form(client, f"/checkout/{sub.id}/otp", {"code": "12ab"})
    assert r.status_code == 400 and "6-digit code" in r.text  # malformed input does not use an attempt
    for left in (4, 3, 2, 1):
        r = await post_form(client, f"/checkout/{sub.id}/otp", {"code": wrong})
        assert r.status_code == 400 and f"{left} {'try' if left == 1 else 'tries'} left" in r.text
    r = await post_form(client, f"/checkout/{sub.id}/otp", {"code": wrong})
    assert r.status_code == 303
    sub = await latest_sub(db, cust.id)
    assert sub.status == SubscriptionStatus.FAILED and "otp_lockout" in sub.fraud_flags
    ev = await db.scalar(select(SecurityEvent).where(SecurityEvent.category == ThreatCategory.FRAUD))
    assert ev is not None and ev.severity == 2 and ev.user_id == cust.id
    assert any(n.title == "Desk purchase locked" for n in await notifications_for(db, env.owner.id))
    # even the right code no longer works
    r = await post_form(client, f"/checkout/{sub.id}/otp", {"code": code})
    assert r.status_code == 303
    assert (await latest_sub(db, cust.id)).status == SubscriptionStatus.FAILED
    page = await client.get(f"/checkout/{sub.id}")
    assert "Purchase locked for security" in page.text


async def test_correct_otp_moves_to_payment(client, db):
    env = await setup_gym(db)
    cust = await near_customer(db)
    await login_as(client, cust)
    sub = await desk_verified(client, db, env, cust)
    assert sub.status == SubscriptionStatus.PENDING_PAYMENT and sub.otp_verified_at is not None
    ch = await db.scalar(select(OtpChallenge).where(OtpChallenge.subscription_id == sub.id))
    assert ch.consumed_at is not None and ch.attempts == 1
    page = await client.get(f"/checkout/{sub.id}")
    assert "Ready to pay" in page.text and "Desk code confirmed" in page.text


async def test_otp_rate_limited_to_three_per_hour(client, db):
    env = await setup_gym(db)
    cust = await near_customer(db)
    await login_as(client, cust)
    sub = await start_offline(client, db, env, cust)
    for _ in range(3):
        r = await post_form(client, f"/checkout/{sub.id}/presence", {"present": "yes"})
        assert r.status_code == 303
    r = await post_form(client, f"/checkout/{sub.id}/presence", {"present": "yes"})
    assert r.status_code == 429 and "3 desk codes in the last hour" in r.text
    assert await db.scalar(select(func.count(OtpChallenge.id))) == 3
    # only the newest code is still open
    open_codes = await db.scalar(select(func.count(OtpChallenge.id)).where(OtpChallenge.expires_at > utcnow()))
    assert open_codes == 1


async def test_pay_requires_desk_code_for_offline(client, db):
    env = await setup_gym(db)
    cust = await near_customer(db)
    await login_as(client, cust)
    sub = await start_offline(client, db, env, cust)
    r = await post_form(client, f"/checkout/{sub.id}/pay", {})
    assert r.status_code == 303 and r.headers["location"] == f"/checkout/{sub.id}"
    assert await db.scalar(select(func.count(Transaction.id))) == 0


# ------------------------------------------------------------------ payments (test gateway)
async def test_test_gateway_approve_activates(client, db):
    env = await setup_gym(db)
    cust = await near_customer(db)
    await login_as(client, cust)
    sub = await desk_verified(client, db, env, cust)
    order_id = await pay_in_test_gateway(client, db, sub, "approve")
    r = await client.get(f"/checkout/return?order_id={order_id}")
    assert r.status_code == 200 and "Payment confirmed" in r.text
    sub = await latest_sub(db, cust.id)
    assert sub.status == SubscriptionStatus.ACTIVE
    today = local_today(cust.timezone)
    assert sub.starts_on == today and sub.ends_on == today + timedelta(days=30)
    tx = await db.scalar(select(Transaction).where(Transaction.provider_order_id == order_id))
    assert tx.status == TxStatus.SUCCESS and tx.completed_at is not None and tx.provider == "test"
    assert tx.amount == Decimal("1499.00") and tx.subscription_id == sub.id
    cust_notes = [n.title for n in await notifications_for(db, cust.id)]
    assert cust_notes.count("Your membership is active") == 1
    assert any(n.title.startswith("New member") for n in await notifications_for(db, env.owner.id))
    # return + reload are idempotent
    r = await client.get(f"/checkout/return?order_id={order_id}")
    assert r.status_code == 200 and "Payment confirmed" in r.text
    assert [n.title for n in await notifications_for(db, cust.id)].count("Your membership is active") == 1
    page = await client.get(f"/checkout/{sub.id}")
    assert "You're in!" in page.text


async def test_test_gateway_decline_fails(client, db):
    env = await setup_gym(db)
    cust = await near_customer(db)
    await login_as(client, cust)
    await subscribe(client, env.gym, "online")
    sub = await latest_sub(db, cust.id)
    order_id = await pay_in_test_gateway(client, db, sub, "decline")
    r = await client.get(f"/checkout/return?order_id={order_id}")
    assert r.status_code == 200 and "Payment failed" in r.text
    sub = await latest_sub(db, cust.id)
    assert sub.status == SubscriptionStatus.FAILED
    tx = await db.scalar(select(Transaction).where(Transaction.provider_order_id == order_id))
    assert tx.status == TxStatus.FAILED
    assert any(n.title == "Payment failed" for n in await notifications_for(db, cust.id))
    # a declined order cannot be approved afterwards
    r = await post_form(client, f"/checkout/{sub.id}/test-gateway", {"order_id": order_id, "decision": "approve"})
    assert r.status_code == 303
    assert (await latest_sub(db, cust.id)).status == SubscriptionStatus.FAILED


async def test_trainer_auto_assigned_least_loaded(client, db):
    env = await setup_gym(db, trainer_modes=(WorkMode.ONLINE, WorkMode.BOTH, WorkMode.OFFLINE))
    busy, free, offline_only = env.trainers
    other_client = await make_customer(db)
    db.add(TrainerClient(employee_id=busy.id, customer_id=other_client.id, mode=ShiftMode.ONLINE))
    await db.commit()
    cust = await near_customer(db)
    await login_as(client, cust)
    await subscribe(client, env.gym, "online_trainer")
    sub = await latest_sub(db, cust.id)
    order_id = await pay_in_test_gateway(client, db, sub)
    await client.get(f"/checkout/return?order_id={order_id}")
    sub = await latest_sub(db, cust.id)
    assert sub.trainer_id == free.id  # least loaded among online-capable trainers (offline-only excluded)
    link = await db.scalar(select(TrainerClient).where(TrainerClient.subscription_id == sub.id))
    assert link.employee_id == free.id and link.mode.value == "online" and link.active
    assert any(n.title == "New client assigned" for n in await notifications_for(db, free.user_id))


async def test_pay_other_customers_order_is_404(client, db, anon_client_factory):
    env = await setup_gym(db)
    owner_of_sub = await near_customer(db)
    await login_as(client, owner_of_sub)
    await subscribe(client, env.gym, "online")
    sub = await latest_sub(db, owner_of_sub.id)
    r = await post_form(client, f"/checkout/{sub.id}/pay", {})
    order_id = r.headers["location"].split("order_id=")[1]

    intruder = await near_customer(db)
    other = anon_client_factory("203.0.113.44")
    await login_as(other, intruder)
    assert (await other.get(f"/checkout/{sub.id}")).status_code == 404
    assert (await post_form(other, f"/checkout/{sub.id}/presence", {"present": "yes"})).status_code == 404
    assert (await post_form(other, f"/checkout/{sub.id}/otp", {"code": "123456"})).status_code == 404
    assert (await post_form(other, f"/checkout/{sub.id}/pay", {})).status_code == 404
    assert (await other.get(f"/checkout/{sub.id}/test-gateway?order_id={order_id}")).status_code == 404
    assert (await post_form(other, f"/checkout/{sub.id}/test-gateway", {"order_id": order_id, "decision": "approve"})).status_code == 404
    assert (await other.get(f"/checkout/return?order_id={order_id}")).status_code == 404
    assert (await post_form(other, f"/me/subscriptions/{sub.id}/cancel", {})).status_code == 404
    assert (await latest_sub(db, owner_of_sub.id)).status == SubscriptionStatus.PENDING_PAYMENT


# ------------------------------------------------------------------ my memberships
async def test_my_subscriptions_page_and_constant_queries(client, db):
    env = await setup_gym(db)
    cust = await near_customer(db)
    await make_subscription(db, cust, env.plans["online_trainer_ai"])
    await login_as(client, cust)
    with QueryCounter() as qc1:
        r = await client.get("/me/subscriptions")
    assert r.status_code == 200 and "Online + Trainer + Ai" in r.text and "days left" in r.text
    for code in ("online", "combo", "offline_ai", "online_ai", "combo_trainer"):
        await make_subscription(db, cust, env.plans[code], branch=env.branch)
    await make_subscription(db, cust, env.plans["offline"], branch=env.branch, status=SubscriptionStatus.PENDING_OTP)
    await make_subscription(db, cust, env.plans["online"], status=SubscriptionStatus.EXPIRED)
    with QueryCounter() as qc2:
        r = await client.get("/me/subscriptions")
    assert r.status_code == 200 and "In progress" in r.text and "History" in r.text
    assert qc2.count == qc1.count


async def test_my_subscriptions_empty_state(client, db):
    cust = await make_customer(db)
    await login_as(client, cust)
    r = await client.get("/me/subscriptions")
    assert r.status_code == 200 and "No memberships yet" in r.text


async def test_cancel_active_subscription(client, db):
    env = await setup_gym(db)
    cust = await near_customer(db)
    sub = await make_subscription(db, cust, env.plans["online_trainer"])
    db.add(TrainerClient(employee_id=env.trainers[0].id, customer_id=cust.id, subscription_id=sub.id, mode=ShiftMode.ONLINE))
    await db.commit()
    await login_as(client, cust)
    r = await post_form(client, f"/me/subscriptions/{sub.id}/cancel", {})
    assert r.status_code == 303
    sub = await latest_sub(db, cust.id)
    assert sub.status == SubscriptionStatus.CANCELLED and sub.cancelled_at is not None
    link = await db.scalar(fresh(select(TrainerClient).where(TrainerClient.subscription_id == sub.id)))
    assert link.active is False
    assert any(n.title == "Membership cancelled" and "not refunded" in n.body for n in await notifications_for(db, cust.id))
    r = await post_form(client, f"/me/subscriptions/{sub.id}/cancel", {})
    assert r.status_code == 303  # second cancel is a no-op with a warning


async def test_renewal_extends_from_current_end(client, db):
    env = await setup_gym(db)
    cust = await near_customer(db)
    current = await make_subscription(db, cust, env.plans["online"])
    await login_as(client, cust)
    r = await client.get(f"/subscribe/{env.gym.slug}")
    assert "You're already a member here" in r.text
    await subscribe(client, env.gym, "online")
    sub = await latest_sub(db, cust.id)
    assert sub.id != current.id
    order_id = await pay_in_test_gateway(client, db, sub)
    await client.get(f"/checkout/return?order_id={order_id}")
    sub = await latest_sub(db, cust.id)
    assert sub.status == SubscriptionStatus.ACTIVE
    assert sub.starts_on == current.ends_on + timedelta(days=1)


async def test_checkout_pages_render_every_state(client, db):
    env = await setup_gym(db)
    cust = await near_customer(db)
    await login_as(client, cust)
    for status in (SubscriptionStatus.PENDING_PAYMENT, SubscriptionStatus.ACTIVE, SubscriptionStatus.EXPIRED,
                   SubscriptionStatus.CANCELLED, SubscriptionStatus.FAILED):
        sub = await make_subscription(db, cust, env.plans["combo"], branch=env.branch, status=status)
        r = await client.get(f"/checkout/{sub.id}")
        assert r.status_code == 200, status
        assert 'style="' not in r.text and "<script>" not in r.text


# ------------------------------------------------------------------ AI independence
async def test_purchase_works_while_ai_is_down(client, db, ai_down):
    env = await setup_gym(db)
    cust = await near_customer(db)
    await login_as(client, cust)
    r = await client.get(f"/subscribe/{env.gym.slug}")
    assert r.status_code == 200 and 'value="online_ai"' in r.text  # AI plans stay on sale; the coach shows "AI unavailable"
    await subscribe(client, env.gym, "online_ai")
    sub = await latest_sub(db, cust.id)
    order_id = await pay_in_test_gateway(client, db, sub)
    r = await client.get(f"/checkout/return?order_id={order_id}")
    assert "Payment confirmed" in r.text
    assert (await latest_sub(db, cust.id)).status == SubscriptionStatus.ACTIVE


async def test_purchase_makes_no_ai_calls(client, db, fake_ai):
    provider = fake_ai(responses=["{}"])
    env = await setup_gym(db)
    cust = await near_customer(db)
    await login_as(client, cust)
    sub = await desk_verified(client, db, env, cust)
    order_id = await pay_in_test_gateway(client, db, sub)
    await client.get(f"/checkout/return?order_id={order_id}")
    assert (await latest_sub(db, cust.id)).status == SubscriptionStatus.ACTIVE
    assert provider.prompts == []  # money and safety decisions here are fully deterministic


# ------------------------------------------------------------------ trainer capacity (load-safety round)
async def _trainer_plan_subscription(db: Any, env: SimpleNamespace, code: str = "online_trainer") -> Subscription:
    """A pending trainer-plan membership of a new member (what activation then assigns a trainer to)."""
    return await make_subscription(db, await make_customer(db), env.plans[code],
                                   status=SubscriptionStatus.PENDING_PAYMENT)


async def _give_clients(db: Any, trainer: Any, n: int) -> None:
    for _ in range(n):
        db.add(TrainerClient(employee_id=trainer.id, customer_id=(await make_customer(db)).id, mode=ShiftMode.ONLINE))
    await db.commit()


async def test_assign_trainer_skips_full_trainers_and_tells_the_owner_once(db, monkeypatch):
    """Before: no cap - the least-loaded trainer took every new member however many clients they already had."""
    from app.services import checkout as svc

    monkeypatch.setattr(get_settings(), "trainer_max_clients", 3)
    env = await setup_gym(db, trainer_modes=(WorkMode.BOTH, WorkMode.BOTH))
    full, spare = env.trainers
    await _give_clients(db, full, 3)
    await _give_clients(db, spare, 2)
    sub = await _trainer_plan_subscription(db, env)
    assert (await svc.assign_trainer(db, sub, env.plans["online_trainer"])).id == spare.id  # the one under the cap
    await db.commit()
    for _ in range(3):  # both trainers are full now: members join without one, the owner hears about it once
        sub = await _trainer_plan_subscription(db, env)
        assert await svc.assign_trainer(db, sub, env.plans["online_trainer"]) is None
        await db.commit()
        assert sub.trainer_id is None
    notes = [n for n in await notifications_for(db, env.owner.id) if "fully booked" in n.title]
    assert len(notes) == 1 and "3 active clients" in notes[0].body
    assert notes[0].link == f"/owner/staff?gym={env.gym.id}&role=trainer"
    assert await db.scalar(select(func.count(TrainerClient.id)).where(TrainerClient.employee_id == spare.id)) == 3
    # a day later the owner is reminded once more
    notes[0].created_at = utcnow() - timedelta(hours=svc.TRAINER_NOTICE_HOURS + 1)
    await db.commit()
    await svc.assign_trainer(db, await _trainer_plan_subscription(db, env), env.plans["online_trainer"])
    await db.commit()
    assert len([n for n in await notifications_for(db, env.owner.id) if "fully booked" in n.title]) == 2


def test_the_trainer_cap_is_the_trainer_max_clients_setting(monkeypatch):
    """The approved per-trainer cap is a setting (TRAINER_MAX_CLIENTS, default 150), read at each assignment."""
    from app.config import Settings
    from app.services import checkout as svc

    assert Settings.model_fields["trainer_max_clients"].default == 150
    monkeypatch.setattr(get_settings(), "trainer_max_clients", 7)
    assert svc.trainer_client_cap() == 7


async def test_the_trainer_load_count_stops_at_the_cap(db, monkeypatch):
    from app.services import checkout as svc
    from tests.test_finance_scale import Sent

    monkeypatch.setattr(get_settings(), "trainer_max_clients", 4)
    env = await setup_gym(db)
    await _give_clients(db, env.trainers[0], 9)
    with Sent() as sent:
        assert await svc.assign_trainer(db, await _trainer_plan_subscription(db, env), env.plans["online_trainer"]) is None
    pick = sent.find("FROM employees", "trainer_clients.employee_id = employees.id")[0][0]
    assert "GROUP BY" not in pick  # no aggregate over every trainer-client link ...
    assert re.search(r"FROM trainer_clients\s+WHERE[^()]*LIMIT", pick), pick  # ... each one's clients up to the cap


async def test_the_trainer_cap_holds_when_members_are_activated_at_the_same_moment(db, monkeypatch):
    """Two activations racing for the last free place: without the per-gym lock both counted 2 clients and both
    assigned the trainer (4 > 3)."""
    from app import db as dbmod
    from app.services import checkout as svc

    monkeypatch.setattr(get_settings(), "trainer_max_clients", 3)
    env = await setup_gym(db)
    trainer = env.trainers[0]
    await _give_clients(db, trainer, 2)
    subs = [await _trainer_plan_subscription(db, env) for _ in range(2)]
    plan_id = env.plans["online_trainer"].id
    first_assigned = asyncio.Event()

    async def activate(sub_id: int, *, hold: bool) -> int | None:
        async with dbmod.get_sessionmaker()() as s:
            sub = await s.get(Subscription, sub_id)
            from app.models import Plan

            trainer_row = await svc.assign_trainer(s, sub, await s.get(Plan, plan_id))
            if hold:
                first_assigned.set()
                await asyncio.sleep(0.3)  # the other activation is running meanwhile
            await s.commit()
            return trainer_row.id if trainer_row else None

    async def second() -> int | None:
        await first_assigned.wait()
        return await activate(subs[1].id, hold=False)

    results = await asyncio.gather(activate(subs[0].id, hold=True), second())
    assert results == [trainer.id, None]
    assert await db.scalar(select(func.count(TrainerClient.id)).where(TrainerClient.employee_id == trainer.id,
                                                                      TrainerClient.active.is_(True))) == 3


async def test_no_matching_trainer_is_reported_once_too(db):
    from app.services import checkout as svc

    env = await setup_gym(db, trainer_modes=(WorkMode.ONLINE,))
    for _ in range(3):  # an offline trainer plan, but the only trainer works online
        sub = await _trainer_plan_subscription(db, env, "offline_trainer")
        assert await svc.assign_trainer(db, sub, env.plans["offline_trainer"]) is None
        await db.commit()
    notes = [n for n in await notifications_for(db, env.owner.id) if n.title.startswith("No trainer available")]
    assert len(notes) == 1 and "at most once a day" in notes[0].body
