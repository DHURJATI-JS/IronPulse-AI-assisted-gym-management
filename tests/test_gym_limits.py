"""Gyms per person (app/services/gym_limits.py): at most 3 gyms with an online-capable membership (online or combo) and
1 gym with an in-person-capable one (gym or combo) - distinct gyms over active, suspended and unfinished purchases,
on every join path (member checkout, the owner's desk registration), race-safe, re-checked before payment and at
activation, grandfathered, and shown on the plan cards, "My memberships" and the member's slot line."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select, update

from app import db as dbmod
from app.core import caps
from app.models import Notification, Subscription, Transaction
from app.models.enums import PlanMode, SubscriptionStatus, TxCategory, TxDirection, TxStatus
from app.services import checkout as svc
from app.services import gym_limits
from tests.conftest import login_as, make_client, post_form
from tests.factories import make_subscription
from tests.test_checkout import latest_sub, near_customer, notifications_for, setup_gym, subscribe


async def gyms(db: Any, n: int) -> list[Any]:
    return [await setup_gym(db) for _ in range(n)]


async def count_subs(db: Any, customer_id: int, *statuses: SubscriptionStatus) -> int:
    stmt = select(func.count(Subscription.id)).where(Subscription.customer_id == customer_id)
    if statuses:
        stmt = stmt.where(Subscription.status.in_(statuses))
    return int(await db.scalar(stmt.execution_options(populate_existing=True)) or 0)


# ------------------------------------------------------------------ the rule
async def test_a_fourth_online_gym_is_refused_naming_the_three(client, db):
    envs = await gyms(db, 4)
    cust = await near_customer(db)
    for env in envs[:3]:
        await make_subscription(db, cust, env.plans["online"])
    await login_as(client, cust)
    r = await subscribe(client, envs[3].gym, "online")
    assert r.status_code == 409, r.text[:300]
    for env in envs[:3]:
        assert env.gym.name in r.text
    assert "at most 3 gyms at a time" in r.text and 'href="/me/subscriptions"' in r.text
    assert await count_subs(db, cust.id) == 3  # nothing was created
    slots = await gym_limits.gym_slots(db, cust.id)
    assert (slots.online_used, slots.in_person_used) == (3, 0)


async def test_a_second_in_person_gym_is_refused_but_online_there_is_fine(client, db):
    a, b = await gyms(db, 2)
    cust = await near_customer(db)
    await make_subscription(db, cust, a.plans["offline"], branch=a.branch)
    await login_as(client, cust)
    r = await subscribe(client, b.gym, "offline", branch_id=b.branch.id)
    assert r.status_code == 409 and a.gym.name in r.text and "at most 1 gym at a time" in r.text
    assert "Online plans here are still open to you" in r.text
    r = await subscribe(client, b.gym, "online")
    assert r.status_code == 303
    assert (await latest_sub(db, cust.id)).gym_id == b.gym.id


async def test_a_combo_uses_both_slots(client, db):
    a, b, c, d = await gyms(db, 4)
    cust = await near_customer(db)
    await make_subscription(db, cust, a.plans["combo"], branch=a.branch)
    slots = await gym_limits.gym_slots(db, cust.id)
    assert slots.summary == "Online gyms 1/3 · In-person gym 1/1"
    await login_as(client, cust)
    assert (await subscribe(client, b.gym, "offline", branch_id=b.branch.id)).status_code == 409
    assert (await subscribe(client, b.gym, "combo", branch_id=b.branch.id)).status_code == 409
    assert (await subscribe(client, b.gym, "online")).status_code == 303
    assert (await subscribe(client, c.gym, "online_ai")).status_code == 303
    r = await subscribe(client, d.gym, "online")  # a (combo), b and c hold the 3 online slots
    assert r.status_code == 409 and all(e.gym.name in r.text for e in (a, b, c))


async def test_same_gym_renewal_and_switch_never_count_but_an_upgrade_needs_the_in_person_slot(client, db):
    a, b, c, d = await gyms(db, 4)
    cust = await near_customer(db)
    for env in (a, b, c):
        await make_subscription(db, cust, env.plans["online"])
    await make_subscription(db, cust, d.plans["offline"], branch=d.branch)
    await login_as(client, cust)
    assert (await subscribe(client, a.gym, "online")).status_code == 303  # renewal of the same plan
    assert (await subscribe(client, a.gym, "online_trainer_ai")).status_code == 303  # switch at the same gym
    r = await subscribe(client, a.gym, "combo", branch_id=a.branch.id)  # online -> combo: needs the in-person slot
    assert r.status_code == 409 and d.gym.name in r.text
    r = await subscribe(client, d.gym, "combo", branch_id=d.branch.id)  # offline -> combo: needs a 4th online slot
    assert r.status_code == 409 and "online" in r.text
    assert (await subscribe(client, d.gym, "offline_trainer", branch_id=d.branch.id)).status_code == 303


async def test_expired_cancelled_and_failed_memberships_never_count(client, db):
    envs = await gyms(db, 4)
    cust = await near_customer(db)
    old = await make_subscription(db, cust, envs[0].plans["online"])
    old.ends_on = date.today() - timedelta(days=1)  # ended yesterday, the hourly job has not run yet
    for env, status in zip(envs[1:3], (SubscriptionStatus.CANCELLED, SubscriptionStatus.FAILED)):
        await make_subscription(db, cust, env.plans["combo"], branch=env.branch, status=status)
    await make_subscription(db, cust, envs[3].plans["online"], status=SubscriptionStatus.EXPIRED)
    await db.commit()
    assert (await gym_limits.gym_slots(db, cust.id)).summary == "Online gyms 0/3 · In-person gym 0/1"


async def test_a_suspended_membership_holds_its_slot(db):
    a, b = await gyms(db, 2)
    cust = await near_customer(db)
    await make_subscription(db, cust, a.plans["offline"], branch=a.branch, status=SubscriptionStatus.SUSPENDED)
    assert await gym_limits.join_refusal(db, cust.id, b.gym.id, PlanMode.OFFLINE)
    await db.rollback()


# ------------------------------------------------------------------ race safety
async def test_parallel_checkouts_at_the_limit_never_pass_it(db):
    envs = await gyms(db, 5)
    cust = await near_customer(db)
    await make_subscription(db, cust, envs[0].plans["online"])
    await make_subscription(db, cust, envs[1].plans["online"])
    clients = [make_client() for _ in envs[2:]]
    try:
        for c in clients:
            await login_as(c, cust)
        results = await asyncio.gather(*(subscribe(c, env.gym, "online") for c, env in zip(clients, envs[2:])))
    finally:
        for c in clients:
            await c.aclose()
    codes = sorted(r.status_code for r in results)
    assert codes == [303, 409, 409], codes  # one slot was left: exactly one of the three got it
    assert await count_subs(db, cust.id, *gym_limits.COUNTED) == 3
    assert (await gym_limits.gym_slots(db, cust.id)).online_used == 3


async def test_a_join_waits_for_a_parallel_join_of_the_same_person_and_counts_it(db):
    """The second join blocks on the person's lock until the first commits, then sees its unfinished purchase."""
    a, b, c = await gyms(db, 3)
    cust = await near_customer(db)
    await make_subscription(db, cust, a.plans["online"])
    await make_subscription(db, cust, b.plans["online"])
    first_in = asyncio.Event()

    async def join(env: Any, *, hold: bool) -> str:
        async with dbmod.get_sessionmaker()() as s:
            from app.models import Gym, Plan, User

            user, gym = await s.get(User, cust.id), await s.get(Gym, env.gym.id)
            plan = await s.get(Plan, env.plans["online"].id)
            try:
                await svc.start_subscription(s, customer=user, gym=gym, plan=plan,
                                             availability=await svc.gym_availability(s, gym), branch_id=None,
                                             location=svc.resolve_location(user), ip="127.0.0.1")
            except svc.PurchaseError as exc:
                return f"refused: {exc}"
            if hold:
                first_in.set()
                await asyncio.sleep(0.3)  # the other join runs meanwhile and must wait for this commit
            await s.commit()
            return "joined"

    async def second() -> str:
        await first_in.wait()
        return await join(c, hold=False)

    d = await setup_gym(db)
    results = await asyncio.gather(join(d, hold=True), second())
    assert results[0] == "joined" and results[1].startswith("refused") and d.gym.name in results[1]


# ------------------------------------------------------------------ every path
async def test_the_owner_desk_registration_obeys_the_limit(client, db):
    a, b = await gyms(db, 2)
    cust = await near_customer(db)
    await make_subscription(db, cust, a.plans["offline"], branch=a.branch)
    # the member chose gym B's online plan in their app (the desk only completes what the member chose - F01)
    await make_subscription(db, cust, b.plans["online"], status=SubscriptionStatus.PENDING_PAYMENT)
    await login_as(client, b.owner)
    r = await post_form(client, "/owner/subscriptions/manual", {"customer": cust.email, "plan_id": b.plans["online"].id})
    assert r.status_code == 303
    assert await count_subs(db, cust.id, SubscriptionStatus.ACTIVE) == 2
    # now gym B's member: moving up to an in-person plan needs the in-person slot that gym A holds - refused, and the
    # refusal never names gym A to gym B's owner (another gym's members are private - F02)
    r = await post_form(client, "/owner/subscriptions/manual",
                        {"customer": cust.email, "plan_id": b.plans["offline"].id, "branch_id": b.branch.id})
    assert r.status_code == 400 and "as many memberships as one person can" in r.text and "My memberships" in r.text
    assert a.gym.name not in r.text and "in-person member of" not in r.text
    assert await count_subs(db, cust.id) == 2


async def test_an_unfinished_purchase_expiring_frees_its_slot(client, db):
    envs = await gyms(db, 4)
    cust = await near_customer(db)
    pending = [await make_subscription(db, cust, env.plans["online"], status=SubscriptionStatus.PENDING_PAYMENT)
               for env in envs[:3]]
    await login_as(client, cust)
    r = await subscribe(client, envs[3].gym, "online")
    assert r.status_code == 409 and "(unfinished purchase)" in r.text
    await db.execute(update(Subscription).where(Subscription.id.in_([p.id for p in pending]))
                     .values(created_at=func.now() - timedelta(days=8)))
    await db.commit()
    assert await svc.cancel_stale_pending(db) == 3
    assert (await subscribe(client, envs[3].gym, "online")).status_code == 303


async def test_grandfathered_members_keep_everything_and_only_new_gyms_are_refused(client, db):
    envs = await gyms(db, 6)
    cust = await near_customer(db)
    for env in envs[:5]:
        await make_subscription(db, cust, env.plans["online"])
    await make_subscription(db, cust, envs[0].plans["offline"], branch=envs[0].branch)
    await make_subscription(db, cust, envs[1].plans["offline"], branch=envs[1].branch)
    assert (await gym_limits.gym_slots(db, cust.id)).summary == "Online gyms 5/3 · In-person gym 2/1"
    await login_as(client, cust)
    assert (await subscribe(client, envs[5].gym, "online")).status_code == 409
    assert (await subscribe(client, envs[2].gym, "online_ai")).status_code == 303  # a gym they already hold
    assert await count_subs(db, cust.id, SubscriptionStatus.ACTIVE) == 7  # nothing cancelled
    page = await client.get("/me/subscriptions")
    assert "Online gyms 5/3" in page.text and "In-person gym 2/1" in page.text


# ------------------------------------------------------------------ payment and activation re-check
async def _old_pending_over_the_limit(db: Any) -> tuple[Any, Any, Any]:
    """3 online gyms held, plus an unfinished purchase at a 4th gym from before the limit (inserted directly)."""
    envs = await gyms(db, 4)
    cust = await near_customer(db)
    for env in envs[:3]:
        await make_subscription(db, cust, env.plans["online"])
    pending = await make_subscription(db, cust, envs[3].plans["online"], status=SubscriptionStatus.PENDING_PAYMENT)
    return cust, envs[3], pending


async def test_pay_is_refused_before_any_money_moves(client, db):
    cust, env, pending = await _old_pending_over_the_limit(db)
    await login_as(client, cust)
    r = await post_form(client, f"/checkout/{pending.id}/pay", {})
    assert r.status_code == 409 and "at most 3 gyms at a time" in r.text
    assert await db.scalar(select(func.count(Transaction.id)).where(Transaction.subscription_id == pending.id)) == 0


async def test_a_payment_that_slipped_through_is_held_for_review_never_activated(db):
    cust, env, pending = await _old_pending_over_the_limit(db)
    tx = Transaction(gym_id=env.gym.id, direction=TxDirection.INCOMING, category=TxCategory.SUBSCRIPTION,
                     status=TxStatus.PENDING, amount=pending.price, currency=pending.currency, description="test",
                     party_label=cust.full_name, customer_id=cust.id, subscription_id=pending.id, provider="test",
                     provider_order_id=f"IPS{pending.id}-T1")
    db.add(tx)
    await db.commit()
    async with dbmod.get_sessionmaker()() as s:
        locked = await svc.lock_transaction(s, tx.provider_order_id, customer_id=cust.id)
        outcome = await svc.settle_order(s, locked, paid=True, amount=locked.amount, currency=locked.currency)
        await s.commit()
    assert outcome == "review"
    sub = await db.scalar(select(Subscription).where(Subscription.id == pending.id)
                          .execution_options(populate_existing=True))
    paid = await db.scalar(select(Transaction).where(Transaction.id == tx.id).execution_options(populate_existing=True))
    assert sub.status == SubscriptionStatus.FAILED and "gym_limit" in sub.fraud_flags and sub.activated_at is None
    assert paid.status == TxStatus.SUCCESS and paid.meta.get("gym_limit")  # booked: the gym refunds it
    owner_notes = [n.title for n in await notifications_for(db, env.owner.id)]
    member_notes = [n for n in await notifications_for(db, cust.id)]
    assert "Payment to refund: membership over the gym limit" in owner_notes
    assert any(n.title == "Payment received - membership not activated" and "refund" in n.body for n in member_notes)
    assert (await gym_limits.gym_slots(db, cust.id)).online_used == 3


async def test_a_normal_payment_still_activates(client, db):
    from tests.test_checkout import pay_in_test_gateway

    a, b = await gyms(db, 2)
    cust = await near_customer(db)
    await make_subscription(db, cust, a.plans["offline"], branch=a.branch)
    await login_as(client, cust)
    assert (await subscribe(client, b.gym, "online")).status_code == 303
    sub = await latest_sub(db, cust.id)
    await pay_in_test_gateway(client, db, sub)
    page = await client.get(f"/checkout/return?order_id=IPS{sub.id}-", follow_redirects=False)
    assert page.status_code in (200, 404)
    assert (await latest_sub(db, cust.id)).status == SubscriptionStatus.ACTIVE


# ------------------------------------------------------------------ what the member sees
async def test_plan_cards_show_the_reason_instead_of_subscribe(client, db):
    a, b = await gyms(db, 2)
    cust = await near_customer(db)
    await make_subscription(db, cust, a.plans["offline"], branch=a.branch)
    anon = await client.get(f"/gyms/{b.gym.slug}?lat=12.9716&lon=77.6412")
    assert anon.status_code == 200 and "Limit reached" not in anon.text
    await login_as(client, cust)
    page = await client.get(f"/gyms/{b.gym.slug}")
    assert page.status_code == 200
    assert "Limit reached: you&#39;re already an in-person member of 1 gym" in page.text and a.gym.name in page.text
    assert f'/subscribe/{b.gym.slug}?plan=online"' in page.text  # online plans still lead to checkout
    sub_page = await client.get(f"/subscribe/{b.gym.slug}")
    assert "In-person gym 1/1" in sub_page.text and 'data-gym-limit' in sub_page.text
    assert 'value="offline" class="sr-only"  disabled' in sub_page.text or 'value="offline" class="sr-only" disabled' \
        in sub_page.text.replace("  ", " ")


async def test_the_limits_are_settings(monkeypatch, db):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "max_online_gyms", 5)
    monkeypatch.setattr(get_settings(), "max_offline_gyms", 2)
    assert (caps.limit("online_gyms"), caps.limit("offline_gyms")) == (5, 2)
    assert gym_limits.GymSlots([], [], gym_limits.max_online(), gym_limits.max_in_person()).summary == \
        "Online gyms 0/5 · In-person gyms 0/2"


async def test_normal_use_never_hits_the_limit(client, db):
    """A member with one gym (combo) renews, switches and adds two online gyms without a single refusal."""
    a, b, c = await gyms(db, 3)
    cust = await near_customer(db)
    await login_as(client, cust)
    for gym, code, extra in ((a.gym, "combo", {"branch_id": a.branch.id}), (a.gym, "combo_ai", {"branch_id": a.branch.id}),
                             (b.gym, "online", {}), (c.gym, "online_trainer", {})):
        r = await subscribe(client, gym, code, **extra)
        assert r.status_code == 303, (code, r.text[:300])
    notes = await db.scalar(select(func.count(Notification.id)).where(Notification.user_id == cust.id))
    assert notes is not None
