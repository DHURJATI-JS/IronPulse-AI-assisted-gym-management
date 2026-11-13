"""The AI guard's tier follows the member's plan at once (docs/ai-guard-and-queue.md section 3, "Entitlement").

A customer whose active plan includes AI is judged with the bigger 'member_ai' quotas (app/ai/policy.py), everyone
else with 'member'. The guard caches that answer per process for ENTITLEMENT_TTL (5 minutes), so
app/services/checkout.py calls ``guard.forget_entitlement(customer_id)`` right after the COMMIT of every change:
activation (online checkout through the test gateway, the Cashfree webhook, a desk purchase after its OTP, the owner's
cash registration), cancellation and the daily expiry sweep. Without those hooks every test here would still see the
old tier for five minutes. A transaction that rolls back forgets nothing.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.ai import policy as ai_policy
from app.ai.guard import guard
from app.ai.owner import AIOwner
from app.config import get_settings
from app.db import QueryCounter
from app.models import Subscription
from app.models.enums import SubscriptionStatus
from app.services import checkout as svc
from tests.conftest import login_as, post_form
from tests.factories import make_customer, make_subscription
from tests.test_cashfree import cf_keys, cf_mock, signed_headers, start_cashfree_order, webhook_body  # noqa: F401
from tests.test_checkout import desk_verified, latest_sub, near_customer, pay_in_test_gateway, setup_gym, subscribe


@pytest.fixture(autouse=True)
def _guard_on(monkeypatch):
    """The guard as configured by default (whatever the local .env says)."""
    monkeypatch.setattr(get_settings(), "ai_guard_enabled", True)
    monkeypatch.setattr(get_settings(), "ai_quota_scale", 1.0)


async def tier(user) -> str:
    """The tier the guard judges this customer's next AI request with - the one the admin's guard.status shows."""
    uid = user if isinstance(user, int) else user.id
    admission = await guard.admit(AIOwner(user_id=uid, role="customer", ip="198.51.100.30"), "assistant")
    assert admission is not None
    await guard.settle(admission, ok=False)  # only a probe: the quota unit is given back
    assert (await guard.status(uid))["tier"] == admission.tier
    return admission.tier


async def test_online_purchase_through_the_test_gateway(client, db):
    env = await setup_gym(db)
    cust = await near_customer(db)
    assert await tier(cust) == "member"  # cached from here on
    await login_as(client, cust)
    assert (await subscribe(client, env.gym, "online_ai")).status_code == 303
    sub = await latest_sub(db, cust.id)
    assert sub.status == SubscriptionStatus.PENDING_PAYMENT
    assert await tier(cust) == "member"  # a purchase that is not paid yet changes nothing
    await pay_in_test_gateway(client, db, sub)
    assert (await latest_sub(db, cust.id)).status == SubscriptionStatus.ACTIVE
    assert await tier(cust) == "member_ai"
    assert (await guard.status(cust.id))["limits"]["chat"] == list(ai_policy.QUOTAS["member_ai"]["chat"])


async def test_desk_purchase_after_the_otp(client, db):
    env = await setup_gym(db)
    cust = await near_customer(db)
    assert await tier(cust) == "member"
    await login_as(client, cust)
    sub = await desk_verified(client, db, env, cust, "offline_ai")
    assert sub.status == SubscriptionStatus.PENDING_PAYMENT  # the desk code confirmed presence, not a membership
    assert await tier(cust) == "member"
    await pay_in_test_gateway(client, db, sub)
    assert await tier(cust) == "member_ai"


async def test_cashfree_webhook_activation(client, db, cf_keys, cf_mock, anon_client_factory):  # noqa: F811
    env, cust, sub, order_id, _ = await start_cashfree_order(client, db, cf_mock, plan="online_ai")
    assert await tier(cust) == "member"
    body = webhook_body(order_id, amount=float(sub.price))
    r = await anon_client_factory("198.51.100.20").post("/payments/cashfree/webhook", content=body,
                                                       headers=signed_headers(body))
    assert r.status_code == 200 and r.json() == {"ok": True, "result": "success"}
    assert await tier(cust) == "member_ai"


async def test_owner_cash_registration(client, db):
    env = await setup_gym(db)
    cust = await make_customer(db, email="walkin-ai@example.com")
    assert await tier(cust) == "member"
    # the member chose the plan in their app; the desk records the cash for it (it never enrols a stranger)
    await make_subscription(db, cust, env.plans["online_ai"], status=SubscriptionStatus.PENDING_PAYMENT)
    await login_as(client, env.owner)
    r = await post_form(client, "/owner/subscriptions/manual", {"customer": cust.email,
                                                               "plan_id": env.plans["online_ai"].id})
    assert r.status_code == 303, r.text[:400]
    assert await tier(cust) == "member_ai"


async def test_a_plan_without_ai_keeps_the_member_tier(client, db):
    env = await setup_gym(db)
    cust = await make_customer(db, email="walkin-plain@example.com")
    assert await tier(cust) == "member"
    await make_subscription(db, cust, env.plans["online"], status=SubscriptionStatus.PENDING_PAYMENT)  # chosen in the app
    await login_as(client, env.owner)
    r = await post_form(client, "/owner/subscriptions/manual", {"customer": cust.email, "plan_id": env.plans["online"].id})
    assert r.status_code == 303, r.text[:400]
    assert await tier(cust) == "member"


async def test_cancelling_the_ai_plan(client, db):
    env = await setup_gym(db)
    cust = await near_customer(db)
    sub = await make_subscription(db, cust, env.plans["online_ai"])
    assert await tier(cust) == "member_ai"
    await login_as(client, cust)
    r = await post_form(client, f"/me/subscriptions/{sub.id}/cancel", {})
    assert r.status_code == 303
    assert (await latest_sub(db, cust.id)).status == SubscriptionStatus.CANCELLED
    assert await tier(cust) == "member"


async def test_daily_expiry_sweep(db):
    env = await setup_gym(db)
    cust = await make_customer(db)
    sub = await make_subscription(db, cust, env.plans["combo_ai"])
    assert await tier(cust) == "member_ai"
    sub.ends_on = date.today() - timedelta(days=1)  # the membership ran out overnight
    await db.commit()
    assert await svc.expire_due_subscriptions(db) == 1
    await db.commit()
    assert await tier(cust) == "member"


async def test_only_a_committed_change_forgets_the_cached_tier(db):
    env = await setup_gym(db)
    cust = await make_customer(db)
    sub = await make_subscription(db, cust, env.plans["online_ai"], status=SubscriptionStatus.PENDING_PAYMENT)
    sub_id, uid = sub.id, cust.id  # a rollback expires every loaded object
    assert await tier(uid) == "member"
    await svc.activate_subscription(db, await db.get(Subscription, sub_id))
    await db.rollback()  # the activation never happened
    with QueryCounter() as cached:
        assert await tier(uid) == "member"
    assert cached.count == 0  # still the cached answer: a rolled-back change forgets nothing
    await svc.activate_subscription(db, await db.get(Subscription, sub_id))
    await db.commit()
    with QueryCounter() as fresh:
        assert await tier(uid) == "member_ai"
    assert fresh.count == 1  # forgotten at the COMMIT: one new lookup


async def test_deleting_the_account_forgets_the_cached_tier_once_it_commits(db):
    """soft_delete_user cancels the subscriptions with one bulk UPDATE (no activate/cancel service call), so it has
    its own after-commit hook: an account restored within the cache window is not judged with the old AI tier."""
    from app.services.accounts import soft_delete_user

    env = await setup_gym(db)
    cust = await make_customer(db)
    await make_subscription(db, cust, env.plans["online_ai"])
    uid = cust.id
    assert await tier(uid) == "member_ai"  # cached from here on
    await soft_delete_user(db, cust, reason="test")
    with QueryCounter() as cached:
        assert await tier(uid) == "member_ai"
    assert cached.count == 0  # not committed yet: nothing is forgotten
    await db.commit()
    with QueryCounter() as fresh:
        assert await tier(uid) == "member"
    assert fresh.count == 1  # forgotten at the COMMIT: one new lookup sees the cancelled subscription
