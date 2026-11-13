"""WP-D: default plan seeding, owner plan editor (validation, audit, ownership), plan availability rules,
front desk (codes, N+1 guard), cash registrations at the desk, daily expiry + reminders."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from markupsafe import escape
from sqlalchemy import func, select

from app.core.utils import utcnow
from app.db import QueryCounter
from app.models import (
    AuditLog,
    CustomerProfile,
    Notification,
    OtpChallenge,
    Plan,
    Subscription,
    TrainerClient,
    Transaction,
)
from app.models.enums import (
    EmploymentStatus,
    IdStatus,
    PlanMode,
    PurchaseChannel,
    ShiftMode,
    SubscriptionStatus,
    TxStatus,
    WorkMode,
)
from app.services import checkout as svc
from tests.conftest import login_as, post_form
from tests.factories import (
    INDIRANAGAR,
    make_branch,
    make_customer,
    make_employee,
    make_gym,
    make_owner,
    make_subscription,
)
from tests.test_checkout import fresh, near_customer, notifications_for, setup_gym, start_offline, subscribe

RANGES = {"INR": (799, 3999), "USD": (15, 79), "GBP": (12, 65), "AED": (59, 299), "EUR": (14, 70)}


# ------------------------------------------------------------------ seeding
async def test_seed_creates_12_plans_and_is_idempotent(db):
    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    plans = await svc.seed_default_plans(db, gym)
    await db.commit()
    assert sorted(p.code for p in plans) == sorted(svc.PLAN_CODES) and len(plans) == 12
    by_code = {p.code: p for p in plans}
    assert by_code["combo_trainer_ai"].includes_trainer and by_code["combo_trainer_ai"].includes_ai
    assert by_code["online"].mode == PlanMode.ONLINE and not by_code["online"].includes_trainer
    assert by_code["offline_ai"].mode == PlanMode.OFFLINE and by_code["offline_ai"].includes_ai
    assert all(p.duration_days == 30 and p.is_enabled and p.description and p.currency == "INR" for p in plans)
    assert [p.sort_order for p in plans] == list(range(12))
    # cheapest online, dearest combo + trainer + AI, more features never cheaper
    assert by_code["online"].price == Decimal("799.00") and by_code["combo_trainer_ai"].price == Decimal("3999.00")
    assert by_code["online"].price < by_code["online_ai"].price < by_code["online_trainer_ai"].price
    # owner edits survive a re-seed; nothing is duplicated
    by_code["online"].price = Decimal("999.00")
    await db.commit()
    again = await svc.seed_default_plans(db, gym)
    await db.commit()
    assert len(again) == 12
    assert await db.scalar(select(func.count(Plan.id)).where(Plan.gym_id == gym.id)) == 12
    assert {p.code: p for p in again}["online"].price == Decimal("999.00")


@pytest.mark.parametrize("currency", sorted(RANGES))
async def test_seed_prices_fit_currency_ranges(db, currency):
    owner = await make_owner(db)
    gym = await make_gym(db, owner, currency=currency, country={"INR": "IN", "USD": "US", "GBP": "GB", "AED": "AE", "EUR": "DE"}[currency])
    plans = await svc.seed_default_plans(db, gym)
    lo, hi = RANGES[currency]
    for p in plans:
        assert Decimal(lo) <= p.price <= Decimal(hi), (p.code, p.price)
        assert p.currency == currency


async def test_seed_partial_adds_only_missing(db):
    env = await setup_gym(db)  # factory plans (12)
    await db.execute(Plan.__table__.delete().where(Plan.gym_id == env.gym.id, Plan.code == "combo_ai"))
    await db.commit()
    plans = await svc.seed_default_plans(db, env.gym)
    await db.commit()
    assert len(plans) == 12
    assert {p.code: p for p in plans}["online"].price == Decimal("799")  # untouched factory price


# ------------------------------------------------------------------ availability rules (unit)
async def test_availability_rules(db):
    owner = await make_owner(db)
    gym = await make_gym(db, owner, online=True, offline=True)
    plans = {p.code: p for p in await svc.seed_default_plans(db, gym)}
    await db.commit()
    av = await svc.gym_availability(db, gym)
    assert av.unavailable_reason(plans["online"]) is None
    assert av.unavailable_reason(plans["offline"]) == "No published branch yet"
    assert av.unavailable_reason(plans["combo"]) == "No published branch yet"
    assert av.unavailable_reason(plans["online_trainer"]) == "No active trainer for this mode"

    await make_branch(db, gym, photos=2)  # unpublished: too few photos
    assert (await svc.gym_availability(db, gym)).unavailable_reason(plans["offline"]) == "No published branch yet"
    await make_branch(db, gym)
    await make_employee(db, gym, work_mode=WorkMode.OFFLINE)
    await make_employee(db, gym, work_mode=WorkMode.ONLINE, status=EmploymentStatus.SUSPENDED, user=await make_customer(db))
    av = await svc.gym_availability(db, gym)
    assert av.unavailable_reason(plans["offline_trainer"]) is None
    assert av.unavailable_reason(plans["combo_trainer"]) is None  # any active trainer serves combo
    assert av.unavailable_reason(plans["online_trainer"]) == "No active trainer for this mode"  # suspended one ignored

    gym.online_open = False
    assert av.unavailable_reason(plans["online"]) == "Online training is closed"
    assert av.unavailable_reason(plans["combo"]) == "Online training is closed"
    gym.online_enabled = False
    assert av.unavailable_reason(plans["online_ai"]) == "Online training is switched off"
    plans["offline"].is_enabled = False
    assert av.unavailable_reason(plans["offline"]) == "Disabled"


# ------------------------------------------------------------------ owner plans page
async def test_plans_page_empty_state_and_seed(client, db):
    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    await login_as(client, owner)
    r = await client.get(f"/owner/gyms/{gym.id}/plans")
    assert r.status_code == 200 and "Seed default plans" in r.text and "No plans yet" in r.text
    r = await post_form(client, f"/owner/gyms/{gym.id}/plans/seed", {})
    assert r.status_code == 303 and r.headers["location"] == f"/owner/gyms/{gym.id}/plans"
    assert await db.scalar(select(func.count(Plan.id)).where(Plan.gym_id == gym.id)) == 12
    assert await db.scalar(select(AuditLog).where(AuditLog.action == "plans.seed")) is not None
    r = await client.get(f"/owner/gyms/{gym.id}/plans")
    assert r.status_code == 200 and "Plans &amp; pricing" in r.text
    assert r.text.count('data-component="plan-editor"') == 12
    assert "No published branch yet" in r.text and "On sale" in r.text
    assert 'style="' not in r.text and "<script>" not in r.text
    r = await post_form(client, f"/owner/gyms/{gym.id}/plans/seed", {})
    assert r.status_code == 303 and await db.scalar(select(func.count(Plan.id))) == 12


async def test_plans_page_other_owner_is_404(client, db):
    env = await setup_gym(db)
    intruder = await make_owner(db)
    await login_as(client, intruder)
    assert (await client.get(f"/owner/gyms/{env.gym.id}/plans")).status_code == 404
    assert (await post_form(client, f"/owner/gyms/{env.gym.id}/plans/seed", {})).status_code == 404
    plan = env.plans["online"]
    r = await post_form(client, f"/owner/plans/{plan.id}", {"price": "1", "is_enabled": "1"})
    assert r.status_code == 404
    await db.refresh(plan)
    assert plan.price == Decimal("799")


async def test_customer_cannot_manage_plans(client, db):
    env = await setup_gym(db)
    cust = await make_customer(db)
    await login_as(client, cust)
    assert (await client.get(f"/owner/gyms/{env.gym.id}/plans")).status_code == 403
    assert (await client.get("/owner/desk")).status_code == 403


@pytest.mark.parametrize("raw,message", [
    ("0", "more than zero"),
    ("-5", "numbers only"),
    ("abc", "numbers only"),
    ("12.345", "2 decimal places"),
    ("10000000.01", "10,000,000"),
    ("", "Enter a price"),
    ("1e5", "numbers only"),
])
async def test_plan_price_validation(client, db, raw, message):
    env = await setup_gym(db)
    plan = env.plans["online"]
    await login_as(client, env.owner)
    r = await post_form(client, f"/owner/plans/{plan.id}", {"price": raw, "description": "x", "is_enabled": "1"})
    assert r.status_code == 400 and message in r.text
    assert 'aria-invalid="true"' in r.text
    await db.refresh(plan)
    assert plan.price == Decimal("799")


async def test_plan_update_saves_audits_and_spares_existing_subscriptions(client, db):
    env = await setup_gym(db)
    plan = env.plans["online"]
    cust = await make_customer(db)
    existing = await make_subscription(db, cust, plan)
    await login_as(client, env.owner)
    r = await post_form(client, f"/owner/plans/{plan.id}", {"price": "1,249.50", "description": "Train anywhere."})
    assert r.status_code == 303 and r.headers["location"].endswith(f"#plan-{plan.id}")
    await db.refresh(plan)
    assert plan.price == Decimal("1249.50") and plan.description == "Train anywhere." and plan.is_enabled is False
    log = await db.scalar(select(AuditLog).where(AuditLog.action == "plans.update"))
    assert log.detail["price"] == ["799.00", "1249.50"] and log.detail["is_enabled"] == [True, False]
    await db.refresh(existing)
    assert existing.price == Decimal("799")  # price changes never touch existing subscriptions
    r = await post_form(client, f"/owner/plans/{plan.id}", {"price": "10000000", "description": "Train anywhere.", "is_enabled": "on"})
    assert r.status_code == 303
    await db.refresh(plan)
    assert plan.price == Decimal("10000000.00") and plan.is_enabled
    r = await post_form(client, f"/owner/plans/{plan.id}", {"price": "x" * 5, "description": "d" * 700})
    assert r.status_code == 400 and "under 600 characters" in r.text


# ------------------------------------------------------------------ front desk
async def _offline_challenge(client, db, env):
    cust = await near_customer(db)
    await login_as(client, cust)
    sub = await start_offline(client, db, env, cust)
    r = await post_form(client, f"/checkout/{sub.id}/presence", {"present": "yes"})
    assert r.status_code == 303
    return cust, sub


async def test_desk_shows_codes_and_constant_queries(client, db, anon_client_factory):
    env = await setup_gym(db)
    cust, sub = await _offline_challenge(client, db, env)
    owner_client = anon_client_factory("127.0.0.1")
    await login_as(owner_client, env.owner)
    with QueryCounter() as qc1:
        r = await owner_client.get("/owner/desk")
    assert r.status_code == 200
    notes = await notifications_for(db, env.owner.id)
    code = next(n.body.split("Code: ")[1][:6] for n in notes if n.title.startswith("Desk OTP"))
    assert code in r.text and cust.full_name in r.text and "Koramangala Club" in r.text
    assert "No gps" in r.text or "no gps" in r.text  # fraud flag shown to the owner
    for _ in range(4):
        await _offline_challenge(client, db, env)
    with QueryCounter() as qc2:
        r = await owner_client.get("/owner/desk")
    assert r.status_code == 200 and r.text.count('data-component="countdown"') == 5
    assert qc2.count == qc1.count
    # another owner sees none of these codes
    other = await make_owner(db)
    await login_as(owner_client, other)
    r = await owner_client.get("/owner/desk")
    assert code not in r.text and "Nobody is waiting" in r.text


async def test_desk_hides_consumed_and_expired_codes(client, db, anon_client_factory):
    env = await setup_gym(db)
    cust, sub = await _offline_challenge(client, db, env)
    ch = await db.scalar(select(OtpChallenge).where(OtpChallenge.subscription_id == sub.id))
    ch.expires_at = utcnow() - timedelta(seconds=1)
    await db.commit()
    owner_client = anon_client_factory("127.0.0.1")
    await login_as(owner_client, env.owner)
    r = await owner_client.get("/owner/desk")
    assert "Nobody is waiting" in r.text


async def test_manual_cash_subscription(client, db, anon_client_factory):
    env = await setup_gym(db, trainer_modes=(WorkMode.OFFLINE,))
    cust = await make_customer(db, email="walkin@example.com", latitude=12.29, longitude=76.63)  # far away: desk is the way
    # at the desk the member chooses the plan in their own app (their phone's GPS is at the branch): their consent
    member = anon_client_factory("127.0.0.1")
    await login_as(member, cust)
    r = await subscribe(member, env.gym, "offline_trainer", branch_id=env.branch.id, lat=env.branch.latitude,
                        lon=env.branch.longitude)
    assert r.status_code == 303, r.text[:400]
    await login_as(client, env.owner)
    page = await client.get("/owner/desk")
    assert "Register a cash membership" in page.text
    plan = env.plans["offline_trainer"]
    r = await post_form(client, "/owner/subscriptions/manual", {"customer": "WalkIn@example.com", "plan_id": plan.id,
                                                               "branch_id": env.branch.id, "note": "Receipt 42"})
    assert r.status_code == 303, r.text[:400]
    sub = await db.scalar(fresh(select(Subscription).where(Subscription.customer_id == cust.id)))
    assert sub.status == SubscriptionStatus.ACTIVE and sub.channel == PurchaseChannel.OWNER_MANUAL
    assert sub.branch_id == env.branch.id and sub.price == plan.price
    tx = await db.scalar(select(Transaction).where(Transaction.subscription_id == sub.id))
    assert tx.provider == "cash" and tx.status == TxStatus.SUCCESS and tx.created_by_id == env.owner.id
    assert tx.amount == plan.price and tx.completed_at is not None and tx.meta == {"note": "Receipt 42"}
    assert sub.trainer_id == env.trainers[0].id
    link = await db.scalar(select(TrainerClient).where(TrainerClient.subscription_id == sub.id))
    assert link.mode == ShiftMode.OFFLINE
    assert await db.scalar(select(AuditLog).where(AuditLog.action == "subscriptions.manual")) is not None
    assert any(n.title == "Your membership is active" for n in await notifications_for(db, cust.id))
    # the owner registered it, so the owner doesn't get a "new member" ping for their own action
    assert not any(n.title.startswith("New member") for n in await notifications_for(db, env.owner.id))
    page = await client.get("/owner/desk")
    assert "Recent desk sales" in page.text and cust.full_name in page.text and "Cash" in page.text


async def test_manual_cash_subscription_validation(client, db):
    env = await setup_gym(db)
    other = await setup_gym(db)
    unverified = await make_customer(db, email="pending@example.com", id_status=IdStatus.PENDING)
    ok = await make_customer(db, email="ok@example.com")
    for member in (unverified, ok):  # both registered by this owner under Members (consent recorded there)
        profile = await db.scalar(select(CustomerProfile).where(CustomerProfile.user_id == member.id))
        profile.registered_by_id = env.owner.id
    await db.commit()
    await login_as(client, env.owner)
    url = "/owner/subscriptions/manual"
    r = await post_form(client, url, {"customer": "", "plan_id": env.plans["online"].id})
    assert r.status_code == 400 and "e-mail address or phone" in r.text
    r = await post_form(client, url, {"customer": "nobody@example.com", "plan_id": env.plans["online"].id})
    assert r.status_code == 400 and str(escape(svc.DESK_NEEDS_MEMBER.format(gym=env.gym.name))) in r.text
    r = await post_form(client, url, {"customer": unverified.email, "plan_id": env.plans["online"].id})
    assert r.status_code == 400 and "verified yet" in r.text
    r = await post_form(client, url, {"customer": ok.email, "plan_id": env.plans["offline"].id})
    assert r.status_code == 400 and "Choose the branch" in r.text
    r = await post_form(client, url, {"customer": ok.email, "plan_id": env.plans["offline"].id, "branch_id": other.branch.id})
    assert r.status_code == 400 and "Choose the branch" in r.text  # foreign branch
    r = await post_form(client, url, {"customer": ok.email, "plan_id": other.plans["online"].id})
    assert r.status_code == 400 and "Choose one of your plans" in r.text  # foreign plan
    env.plans["online"].is_enabled = False
    await db.commit()
    r = await post_form(client, url, {"customer": ok.email, "plan_id": env.plans["online"].id})
    assert r.status_code == 400 and "disabled" in r.text
    assert await db.scalar(select(func.count(Subscription.id))) == 0


# ------------------------------------------------------------------ the desk never enrols a stranger (cross-gym isolation)
DESK_URL = "/owner/subscriptions/manual"


async def test_the_desk_cannot_enrol_another_gyms_member_and_open_their_health_record(client, db, anon_client_factory):
    """F01: gym A's owner typed the e-mail or phone of gym B's member into the cash desk form - the membership was
    active at once, with no consent, and A could read (and overwrite) the member's health record. The desk now only
    completes a purchase the member chose themselves, in their own app."""
    a, b = await setup_gym(db), await setup_gym(db)
    khalid = await near_customer(db, email="khalid@example.com", phone="+91 98450 11111", full_name="Khalid Mansoori",
                                 conditions=["Type 2 diabetes"])
    await make_subscription(db, khalid, b.plans["online"])  # a member of gym B only
    await login_as(client, a.owner)
    for ref in ("khalid@example.com", "+91 98450 11111", "9845011111"):
        r = await post_form(client, DESK_URL, {"customer": ref, "plan_id": a.plans["online"].id})
        assert r.status_code == 400 and str(escape(svc.DESK_NEEDS_MEMBER.format(gym=a.gym.name))) in r.text
        assert "Khalid Mansoori" not in r.text and "diabetes" not in r.text.lower()
    assert await db.scalar(select(func.count(Subscription.id)).where(Subscription.gym_id == a.gym.id)) == 0
    assert await db.scalar(select(func.count(Transaction.id))) == 0
    assert (await client.get(f"/owner/customers/{khalid.id}")).status_code == 404  # still nothing to read ...
    assert (await client.get(f"/profile/{khalid.id}")).status_code == 404
    r = await post_form(client, f"/owner/customers/{khalid.id}/health", {"health_notes": "owned"})
    assert r.status_code == 404  # ... or to overwrite
    assert [n.title for n in await notifications_for(db, khalid.id)] == []  # and nothing was sent in their name

    # the member chooses the plan in their own app: now the desk records the cash for that purchase
    member = anon_client_factory("127.0.0.1")
    await login_as(member, khalid)
    assert (await subscribe(member, a.gym, "online")).status_code == 303
    r = await post_form(client, DESK_URL, {"customer": "khalid@example.com", "plan_id": a.plans["online"].id})
    assert r.status_code == 303, r.text[:400]
    subs = (await db.execute(fresh(select(Subscription).where(Subscription.gym_id == a.gym.id)))).scalars().all()
    assert [(s.status, s.channel) for s in subs] == [(SubscriptionStatus.ACTIVE, PurchaseChannel.OWNER_MANUAL)]
    # now a current member of gym A: a renewal or plan change at A's desk needs no new step
    r = await post_form(client, DESK_URL, {"customer": "khalid@example.com", "plan_id": a.plans["online_trainer"].id})
    assert r.status_code == 303


async def test_a_member_who_cancelled_or_never_finished_is_a_stranger_to_the_desk_again(client, db):
    a = await setup_gym(db)
    left = await near_customer(db, email="left@example.com")
    await make_subscription(db, left, a.plans["online"], status=SubscriptionStatus.CANCELLED)
    other_plan = await near_customer(db, email="other-plan@example.com")
    await make_subscription(db, other_plan, a.plans["online"], status=SubscriptionStatus.PENDING_PAYMENT)
    await login_as(client, a.owner)
    neutral = str(escape(svc.DESK_NEEDS_MEMBER.format(gym=a.gym.name)))
    r = await post_form(client, DESK_URL, {"customer": left.email, "plan_id": a.plans["online"].id})
    assert r.status_code == 400 and neutral in r.text
    r = await post_form(client, DESK_URL, {"customer": other_plan.email, "plan_id": a.plans["combo"].id,
                                           "branch_id": a.branch.id})
    assert r.status_code == 400 and neutral in r.text  # they chose the online plan, not this one
    assert await db.scalar(select(func.count(Subscription.id)).where(
        Subscription.channel == PurchaseChannel.OWNER_MANUAL)) == 0


async def test_desk_refusals_never_describe_a_stranger(client, db):
    """F02: the desk's refusals named another gym's member, their other gym, their ID status and their safety pause -
    a silent person lookup across gyms. Anyone who hasn't chosen this gym, and an e-mail with no account, now get ONE
    identical answer, and nothing is written or sent."""
    a, b = await setup_gym(db), await setup_gym(db)
    sophie = await near_customer(db, email="sophie@example.com", full_name="Sophie Martin")
    await make_subscription(db, sophie, b.plans["offline"], branch=b.branch)  # in person at gym B
    tushar = await near_customer(db, email="tushar@example.com", full_name="Tushar Malik")
    await make_subscription(db, tushar, b.plans["online"])
    (await db.scalar(select(CustomerProfile).where(CustomerProfile.user_id == tushar.id))).activity_suspended = True
    kavya = await near_customer(db, email="kavya@example.com", full_name="Kavya Menon", id_status=IdStatus.PENDING)
    await db.commit()
    await login_as(client, a.owner)
    neutral = str(escape(svc.DESK_NEEDS_MEMBER.format(gym=a.gym.name)))
    probes = [("nobody@example.com", "online"), ("sophie@example.com", "offline"), ("sophie@example.com", "combo"),
              ("sophie@example.com", "online"), ("tushar@example.com", "online"), ("kavya@example.com", "online")]
    for ref, code in probes:
        data = {"customer": ref, "plan_id": a.plans[code].id}
        if code != "online":
            data["branch_id"] = a.branch.id
        r = await post_form(client, DESK_URL, data)
        assert r.status_code == 400 and neutral in r.text, (ref, code)
        for secret in ("Sophie Martin", "Tushar Malik", "Kavya Menon", b.gym.name, "in-person member of",
                       "paused for safety", "verified yet", "(pending)", "No customer account"):
            assert secret not in r.text, (ref, code, secret)
    assert await db.scalar(select(func.count(Subscription.id)).where(Subscription.gym_id == a.gym.id)) == 0
    assert await db.scalar(select(func.count(Notification.id)).where(
        Notification.user_id.in_([sophie.id, tushar.id, kavya.id]))) == 0


# ------------------------------------------------------------------ daily lifecycle
async def test_expiry_job_and_reminders(db):
    env = await setup_gym(db)
    cust = await make_customer(db)
    today = date.today()
    ended = await make_subscription(db, cust, env.plans["online"])
    ended.starts_on, ended.ends_on = today - timedelta(days=31), today - timedelta(days=1)
    soon = await make_subscription(db, cust, env.plans["online_ai"])
    soon.starts_on, soon.ends_on = today - timedelta(days=27), today + timedelta(days=3)
    later = await make_subscription(db, cust, env.plans["combo"])
    later.ends_on = today + timedelta(days=10)
    db.add(TrainerClient(employee_id=env.trainers[0].id, customer_id=cust.id, subscription_id=ended.id, mode=ShiftMode.ONLINE))
    await db.commit()

    assert await svc.expire_due_subscriptions(db, today) == 1
    assert await svc.send_expiry_reminders(db, today) == 1
    await db.commit()
    assert await svc.send_expiry_reminders(db, today) == 0  # idempotent
    assert await svc.expire_due_subscriptions(db, today) == 0
    await db.commit()

    rows = {s.id: s.status for s in (await db.execute(fresh(select(Subscription)))).scalars()}
    assert rows[ended.id] == SubscriptionStatus.EXPIRED
    assert rows[soon.id] == SubscriptionStatus.ACTIVE and rows[later.id] == SubscriptionStatus.ACTIVE
    link = await db.scalar(fresh(select(TrainerClient).where(TrainerClient.subscription_id == ended.id)))
    assert link.active is False
    titles = [n.title for n in await notifications_for(db, cust.id)]
    assert titles.count("Your membership has expired") == 1 and titles.count(svc.REMINDER_TITLE) == 1


async def test_reminder_skipped_after_renewal_and_stale_pending_cancelled(db):
    env = await setup_gym(db)
    cust = await make_customer(db)
    today = date.today()
    soon = await make_subscription(db, cust, env.plans["online"])
    soon.ends_on = today + timedelta(days=3)
    renewal = await make_subscription(db, cust, env.plans["online"])
    renewal.starts_on, renewal.ends_on = today + timedelta(days=4), today + timedelta(days=34)
    stale = await make_subscription(db, cust, env.plans["online_ai"], status=SubscriptionStatus.PENDING_PAYMENT)
    stale.created_at = utcnow() - timedelta(days=8)
    await db.commit()
    assert await svc.send_expiry_reminders(db, today) == 0
    assert await svc.cancel_stale_pending(db) == 1
    await db.commit()
    s = await db.scalar(fresh(select(Subscription).where(Subscription.id == stale.id)))
    assert s.status == SubscriptionStatus.CANCELLED


async def test_periodic_job_is_registered():
    from app.services.jobs import registered_periodic

    import app.routers.checkout  # noqa: F401 - the router imports the service that registers it

    assert "subscriptions_daily" in registered_periodic()


async def test_owner_plans_page_constant_queries(client, db):
    env = await setup_gym(db)
    await login_as(client, env.owner)
    with QueryCounter() as qc1:
        r = await client.get(f"/owner/gyms/{env.gym.id}/plans")
    assert r.status_code == 200
    for i in range(6):
        c = await make_customer(db)
        await make_subscription(db, c, env.plans[svc.PLAN_CODES[i]], branch=env.branch)
    await make_branch(db, env.gym, coords=INDIRANAGAR)
    with QueryCounter() as qc2:
        r = await client.get(f"/owner/gyms/{env.gym.id}/plans")
    assert r.status_code == 200 and "1 active member" in r.text
    assert qc2.count == qc1.count
    assert await db.scalar(select(func.count(Notification.id))) == 0
