"""Super-admin governance (/superadmin/governance, app/services/governance.py): ban an owner, ban an employee, delete a
gym in two steps, lift / restore - each with an impact preview, a typed confirmation + reason, and money failsafes.

Covers: super-admin only (owner / staff / member / anonymous refused) and CSRF; the typed confirmation; preview numbers
(members with paid periods -> pro-rata refunds, staff with unpaid earned days, open orders); an owner ban suspends the
gyms and hides them publicly; an employee ban creates final settlements, ends employments, reassigns or unlinks clients
and revokes sessions; the final gym deletion is blocked while settlements / refunds / orders are open and allowed once
paid or waived (financial records kept); restore during the cooling-off; idempotency and double submits; audit rows and
notifications; the per-admin rate limit; a super-admin can't ban themselves or another super-admin.
"""

from __future__ import annotations

import html
import re
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, update

from app.config import get_settings
from app.core.utils import utcnow
from app.models import (
    AccountBan,
    AuditLog,
    BlockedIdentity,
    Employee,
    GovernanceWaiver,
    Gym,
    IpBan,
    JobPosting,
    Notification,
    Refund,
    SalaryStatement,
    ShopOrder,
    ShopOrderItem,
    Subscription,
    TrainerClient,
    TrainerShift,
    Transaction,
    TrustIpAction,
    User,
    UserSession,
)
from app.models.enums import (
    EmployeeRole,
    EmploymentStatus,
    PurchaseChannel,
    Role,
    ShiftMode,
    StatementStatus,
    SubscriptionStatus,
    TxCategory,
    TxDirection,
    TxStatus,
)
from app.models.shop import OrderStatus
from app.services import governance as G
from app.services import payroll as pr
from tests.conftest import login_as, make_client, post_form, run_jobs
from tests.factories import (
    DEFAULT_PASSWORD,
    make_branch,
    make_customer,
    make_employee,
    make_gym,
    make_owner,
    make_plans,
    make_shift,
    make_user,
    png_bytes,
)

TODAY = date.today()


@pytest.fixture(autouse=True)
def _no_real_ai(ai_down):
    """Jobs these tests run (payroll explanations of the final settlements) never wait for a real AI provider."""
    yield


# ====================================================================== helpers
def flash(r: httpx.Response) -> str:
    from itsdangerous import URLSafeSerializer

    from app.core.templating import FLASH_COOKIE

    raw = r.cookies.get(FLASH_COOKIE)
    if not raw:
        return ""
    data = URLSafeSerializer(get_settings().secret_key, salt="ironpulse-flash").loads(raw)
    return " | ".join(d["m"] for d in data)


def text(r: httpx.Response) -> str:
    return html.unescape(r.text)


async def count(db, model, *where) -> int:
    return int(await db.scalar(select(func.count()).select_from(model).where(*where)) or 0)


async def reload(db, model, ident):
    return await db.get(model, ident, populate_existing=True)


async def admin_client(db, **kw) -> tuple[User, httpx.AsyncClient]:
    admin = await make_user(db, role=Role.SUPERADMIN, full_name="Platform Admin", **kw)
    client = make_client()
    await login_as(client, admin)
    return admin, client


async def membership(db, customer: User, plan, *, starts: date, ends: date, provider: str | None = "test",
                     price: str | None = None) -> Subscription:
    """An ACTIVE membership for [starts, ends] - paid online (TEST MODE) unless ``provider`` says otherwise."""
    sub = Subscription(customer_id=customer.id, gym_id=plan.gym_id, plan_id=plan.id, status=SubscriptionStatus.ACTIVE,
                       channel=PurchaseChannel.ONLINE_CHECKOUT if provider != "cash" else PurchaseChannel.OWNER_MANUAL,
                       price=Decimal(price or plan.price), currency=plan.currency, starts_on=starts, ends_on=ends,
                       activated_at=utcnow())
    db.add(sub)
    await db.flush()
    if provider:
        db.add(Transaction(gym_id=plan.gym_id, direction=TxDirection.INCOMING, category=TxCategory.SUBSCRIPTION,
                           status=TxStatus.SUCCESS, amount=sub.price, currency=sub.currency, description="membership",
                           party_label=customer.full_name, provider=provider, provider_order_id=f"ord-{sub.id}-{provider}",
                           customer_id=customer.id, subscription_id=sub.id, completed_at=utcnow()))
    await db.commit()
    return sub


async def unpaid_statement(db, emp: Employee, *, net: str, start: date, end: date) -> SalaryStatement:
    st = SalaryStatement(employee_id=emp.id, gym_id=emp.gym_id, period_start=start, period_end=end,
                         days_in_period=(end - start).days + 1, days_worked=(end - start).days + 1,
                         monthly_salary=emp.monthly_salary, gross_pay=Decimal(net), net_pay=Decimal(net),
                         currency=emp.currency, country_code=emp.country_code, status=StatementStatus.DUE,
                         due_date=end + timedelta(days=7))
    db.add(st)
    await db.commit()
    return st


async def paid_order(db, gym: Gym, customer: User, *, total: str, status: OrderStatus = OrderStatus.PAID) -> ShopOrder:
    n = await count(db, ShopOrder) + 1
    order = ShopOrder(order_number=f"IPO-T{gym.id}-{n}", gym_id=gym.id, customer_id=customer.id,
                      customer_label=customer.full_name, status=status, fulfillment="pickup", subtotal=Decimal(total),
                      total=Decimal(total), currency="INR", payment_provider="test", provider_order_id=f"IPO-T{gym.id}-{n}",
                      paid_at=utcnow() if status != OrderStatus.PENDING_PAYMENT else None)
    db.add(order)
    await db.flush()
    db.add(ShopOrderItem(order_id=order.id, product_id=None, product_name="Whey 1 kg", unit_price=Decimal(total),
                         quantity=1, line_total=Decimal(total)))
    await db.commit()
    return order


async def world(db) -> dict[str, Any]:
    """An owner's gym with two online-paid members (one mid-period, one not yet started), one cash member, three
    working staff (one with an unpaid statement), trainer clients, a shift, an open paid order and a job posting."""
    owner = await make_owner(db, email="gym.owner@example.com", full_name="Owner Olivia")
    gym = await make_gym(db, owner, name="Iron Temple Fitness")
    branch = await make_branch(db, gym)
    plans = {p.code: p for p in await make_plans(db, gym)}
    m1 = await make_customer(db, email="member.one@example.com", full_name="Member One")
    m2 = await make_customer(db, email="member.two@example.com", full_name="Member Two")
    m3 = await make_customer(db, email="member.cash@example.com", full_name="Member Cash")
    s1 = await membership(db, m1, plans["online"], starts=TODAY - timedelta(days=10), ends=TODAY + timedelta(days=19),
                          price="3000")
    s2 = await membership(db, m2, plans["online"], starts=TODAY + timedelta(days=5), ends=TODAY + timedelta(days=34),
                          price="1000")
    s3 = await membership(db, m3, plans["offline"], starts=TODAY - timedelta(days=5), ends=TODAY + timedelta(days=24),
                          provider="cash", price="1500")
    t1 = await make_user(db, role=Role.STAFF, email="trainer.one@example.com", full_name="Trainer One")
    t2 = await make_user(db, role=Role.STAFF, email="trainer.two@example.com", full_name="Trainer Two")
    t3 = await make_user(db, role=Role.STAFF, email="desk.three@example.com", full_name="Desk Three")
    e1 = await make_employee(db, gym, user=t1, salary="30000")
    e2 = await make_employee(db, gym, user=t2, salary="24000")
    e3 = await make_employee(db, gym, user=t3, role=EmployeeRole.RECEPTIONIST, salary="18000")
    st3 = await unpaid_statement(db, e3, net="5000.00", start=TODAY - timedelta(days=40), end=TODAY - timedelta(days=31))
    db.add(TrainerClient(employee_id=e1.id, customer_id=m1.id, subscription_id=s1.id, mode=ShiftMode.ONLINE))
    await db.commit()
    await make_shift(db, e1, branch=branch)
    order = await paid_order(db, gym, m2, total="1200.00")
    job = JobPosting(gym_id=gym.id, title="Strength Coach Vacancy", role=EmployeeRole.TRAINER,
                     employment_type="full_time", location="Bengaluru", currency="INR", requirements="CPR certified")
    db.add(job)
    await db.commit()
    return dict(owner=owner, gym=gym, branch=branch, plans=plans, members=(m1, m2, m3), subs=(s1, s2, s3),
                staff=(t1, t2, t3), emps=(e1, e2, e3), st3=st3, order=order, job=job)


def ban_form(target: User, reason: str, note: str = "Documented fraud against several members.", **extra: str) -> dict:
    return {"reason": reason, "note": note, "confirm": target.email, **extra}


# ====================================================================== access
async def test_super_admin_only(db):
    w = await world(db)
    gym, owner = w["gym"], w["owner"]
    urls = ["/superadmin/governance", f"/superadmin/governance/gyms/{gym.id}", f"/superadmin/governance/users/{owner.id}"]
    async with make_client() as anon:
        for url in urls:
            r = await anon.get(url)
            assert r.status_code == 303 and r.headers["location"].startswith("/auth/login"), url
        r = await post_form(anon, f"/superadmin/governance/users/{owner.id}/ban", ban_form(owner, "terms_violation"))
        assert r.status_code in (303, 401)
    for person in (owner, w["staff"][0], w["members"][0]):
        async with make_client() as c:
            await login_as(c, person)
            for url in urls:
                assert (await c.get(url)).status_code == 403, (person.role, url)
            r = await post_form(c, f"/superadmin/governance/gyms/{gym.id}/delete",
                                {"reason": "closed", "note": "x" * 20, "confirm": gym.name})
            assert r.status_code == 403
    assert await count(db, AccountBan) == 0 and (await reload(db, Gym, gym.id)).is_active


async def test_csrf_is_required(db):
    w = await world(db)
    admin, c = await admin_client(db)
    async with c:
        r = await c.post(f"/superadmin/governance/users/{w['owner'].id}/ban",
                         data=ban_form(w["owner"], "terms_violation", member_policy="keep"))
        assert r.status_code == 403
    assert await count(db, AccountBan) == 0


# ====================================================================== preview
async def test_preview_numbers_are_accurate(db):
    w = await world(db)
    admin, c = await admin_client(db)
    impact = (await G.gyms_impact(db, [w["gym"]], as_of=TODAY))[w["gym"].id]
    # members: 3000 x 19/30 unused + 1000 (not started yet) + 1500 x 24/30 (cash) = 1900 + 1000 + 1200
    assert impact.members == 3 and impact.memberships == 3
    assert impact.refunds_due == {"INR": Decimal("4100.00")}
    assert (impact.online_refunds, impact.manual_refunds) == (2, 1)
    assert G.pro_rata(Decimal("3000"), TODAY - timedelta(days=10), TODAY + timedelta(days=19), TODAY) == Decimal("1900.00")
    # staff: earned pay = the unpaid statement + what each one earned since (payroll's own per-person preview)
    expected = Decimal("5000.00")
    for emp in w["emps"]:
        acc = await pr.accrued_preview(db, await reload(db, Employee, emp.id))
        expected += acc.net if acc else Decimal("0")
    assert impact.staff == impact.staff_working == 3
    assert impact.earned_pay == {"INR": expected} and impact.unpaid_statements == 1
    assert (impact.open_orders, impact.order_refunds) == (1, {"INR": Decimal("1200.00")})
    assert (impact.clients, impact.open_jobs) == (1, 1)
    async with c:
        r = await c.get(f"/superadmin/governance/gyms/{w['gym'].id}")
        assert r.status_code == 200
        body = text(r)
        assert "₹4,100.00" in body and G.money_text(impact.earned_pay) in body and "₹1,200.00" in body
        assert "A dry run" in body or "dry run" in body
        r = await c.get(f"/superadmin/governance/users/{w['owner'].id}")
        assert r.status_code == 200 and "₹4,100.00" in text(r)
        r = await c.get("/superadmin/governance")
        assert r.status_code == 200 and "Iron Temple Fitness" in text(r)
        users = await c.get(f"/superadmin/users?q={w['owner'].email}")
        assert f"/superadmin/governance/users/{w['owner'].id}" in users.text
    assert await count(db, AuditLog, AuditLog.action.like("governance.%")) == 0  # a preview changes nothing
    assert (await reload(db, Gym, w["gym"].id)).is_active


# ====================================================================== ban an owner
async def test_typed_confirmation_and_reason_are_required(db):
    w = await world(db)
    admin, c = await admin_client(db)
    owner, gym = w["owner"], w["gym"]
    async with c:
        r = await post_form(c, f"/superadmin/governance/users/{owner.id}/ban",
                            {**ban_form(owner, "terms_violation", member_policy="keep"), "confirm": "someone@else.com"})
        assert r.status_code == 400 and "Type the account" in text(r)
        r = await post_form(c, f"/superadmin/governance/users/{owner.id}/ban",
                            {**ban_form(owner, "made_up", member_policy="keep")})
        assert r.status_code == 400 and "Choose a reason" in text(r)
        r = await post_form(c, f"/superadmin/governance/users/{owner.id}/ban", ban_form(owner, "terms_violation", note="short",
                                                                                        member_policy="keep"))
        assert r.status_code == 400
        r = await post_form(c, f"/superadmin/governance/gyms/{gym.id}/delete",
                            {"reason": "closed", "note": "The owner closed the gym for good.", "confirm": "Iron Temple"})
        assert r.status_code == 400 and "Type the gym" in text(r)
    assert await count(db, AccountBan) == 0
    assert (await reload(db, Gym, gym.id)).deletion_requested_at is None


async def test_owner_ban_suspends_gyms_and_hides_them(db):
    w = await world(db)
    admin, c = await admin_client(db)
    owner, gym = w["owner"], w["gym"]
    pending = Subscription(customer_id=w["members"][0].id, gym_id=gym.id, plan_id=w["plans"]["combo"].id,
                           status=SubscriptionStatus.PENDING_PAYMENT, channel=PurchaseChannel.ONLINE_CHECKOUT,
                           price=Decimal("1999"), currency="INR")
    db.add(pending)
    await db.commit()
    async with make_client() as anon:  # visible before
        assert (await anon.get(f"/gyms/{gym.slug}")).status_code == 200
        assert "Strength Coach Vacancy" in text(await anon.get("/jobs"))
    owner_client = make_client()
    await login_as(owner_client, owner)
    async with c, owner_client:
        r = await post_form(c, f"/superadmin/governance/users/{owner.id}/ban",
                            ban_form(owner, "terms_violation", member_policy="keep"))
        assert r.status_code == 303, text(r)[:400]
        assert "banned" in flash(r) and "1 gym is suspended" in flash(r)
        assert (await owner_client.get("/owner")).status_code in (303, 401)  # signed out
    ban = await db.scalar(select(AccountBan).where(AccountBan.user_id == owner.id))
    assert ban.source == "platform" and ban.lifted_at is None and ban.created_by_id == admin.id
    assert not (await reload(db, User, owner.id)).is_active
    g = await reload(db, Gym, gym.id)
    assert not g.is_active and g.suspension_kind == "owner_ban" and g.suspended_at is not None and g.archived_at is None
    assert (await reload(db, Subscription, pending.id)).status == SubscriptionStatus.CANCELLED  # no new memberships
    for s in w["subs"]:  # members keep access until their paid period ends
        assert (await reload(db, Subscription, s.id)).status == SubscriptionStatus.ACTIVE
    for e in w["emps"]:  # earned pay protected: nothing ends, nothing is voided
        assert (await reload(db, Employee, e.id)).status == EmploymentStatus.ACTIVE
    assert (await reload(db, SalaryStatement, w["st3"].id)).status == StatementStatus.DUE
    async with make_client() as anon:  # hidden from public pages, search, checkout, the jobs board
        assert (await anon.get(f"/gyms/{gym.slug}")).status_code == 404
        assert gym.name not in text(await anon.get("/gyms"))
        assert "Strength Coach Vacancy" not in text(await anon.get("/jobs"))
        assert (await anon.get(f"/subscribe/{gym.slug}")).status_code in (303, 404)
    assert await count(db, UserSession, UserSession.user_id == owner.id, UserSession.revoked_at.is_(None)) == 0
    assert await count(db, BlockedIdentity, BlockedIdentity.user_id == owner.id) >= 1  # the identity registry
    assert await count(db, AuditLog, AuditLog.action == "governance.owner_ban") == 1
    assert await count(db, AuditLog, AuditLog.action == "governance.gym_suspended", AuditLog.gym_id == gym.id) == 1
    for person in (*w["staff"], *w["members"], owner):
        assert await count(db, Notification, Notification.user_id == person.id) >= 1, person.email
    assert await count(db, Notification, Notification.user_id == w["members"][0].id,
                       Notification.body.like("%stays valid%")) == 1


async def test_owner_ban_with_refunds_pays_members_back_pro_rata(db):
    w = await world(db)
    admin, c = await admin_client(db)
    owner = w["owner"]
    async with c:
        r = await post_form(c, f"/superadmin/governance/users/{owner.id}/ban",
                            ban_form(owner, "payment_fraud", member_policy="refund"))
        assert r.status_code == 303 and "refund" in flash(r)
    await run_jobs()
    s1, s2, s3 = [await reload(db, Subscription, s.id) for s in w["subs"]]
    assert s1.status == s2.status == SubscriptionStatus.CANCELLED
    assert s3.status == SubscriptionStatus.ACTIVE  # paid in cash: refunded by hand from the desk
    refunds = {t.subscription_id: t for t in (await db.execute(
        select(Transaction).where(Transaction.category == TxCategory.REFUND))).scalars()}
    assert refunds[s1.id].amount == Decimal("1900.00") and refunds[s2.id].amount == Decimal("1000.00")
    assert all(t.status == TxStatus.SUCCESS and t.direction == TxDirection.OUTGOING for t in refunds.values())
    assert await count(db, Notification, Notification.user_id == w["members"][0].id,
                       Notification.title == "Membership refunded") == 1
    # a severe reason also banned recent public addresses (none here: the admin's own / non-public are spared)
    assert await count(db, IpBan) == 0


# ====================================================================== ban an employee
async def test_employee_ban_settles_pay_ends_employments_and_revokes_sessions(db):
    w = await world(db)
    admin, c = await admin_client(db)
    trainer, e1 = w["staff"][0], w["emps"][0]
    other_owner = await make_owner(db, email="second.owner@example.com")
    other_gym = await make_gym(db, other_owner, name="Second Gym")
    e_other = await make_employee(db, other_gym, user=trainer, salary="12000")
    replacement = await make_employee(db, other_gym, salary="20000")
    m = await make_customer(db, email="client.two@example.com")
    sub = await membership(db, m, (await make_plans(db, other_gym))[5], starts=TODAY, ends=TODAY + timedelta(days=29))
    db.add(TrainerClient(employee_id=e_other.id, customer_id=m.id, subscription_id=sub.id, mode=ShiftMode.ONLINE))
    db.add(UserSession(id="s" * 64, user_id=trainer.id, expires_at=utcnow() + timedelta(days=1), ip="93.184.216.34"))
    await db.commit()
    staff_client = make_client()
    await login_as(staff_client, trainer)
    async with c, staff_client:
        r = await c.get(f"/superadmin/governance/users/{trainer.id}")
        assert r.status_code == 200 and "Second Gym" in text(r) and "Move them to" in text(r)
        r = await post_form(c, f"/superadmin/governance/users/{trainer.id}/ban",
                            {**ban_form(trainer, "theft_fraud", note="Took cash from members at the front desk."),
                             f"reassign_{e_other.id}": str(replacement.id)})
        assert r.status_code == 303, text(r)[:400]
        assert "2 employments ended" in flash(r) and "2 final settlements created" in flash(r)
        assert (await staff_client.get("/staff")).status_code in (303, 401)  # signed out everywhere
    ban = await db.scalar(select(AccountBan).where(AccountBan.user_id == trainer.id))
    assert ban.source == "platform"
    for emp in (e1, e_other):
        fresh = await reload(db, Employee, emp.id)
        assert fresh.status == EmploymentStatus.TERMINATED and fresh.removal_reason == "theft_fraud"
        fs = await pr.final_settlement_of(db, emp.id)
        assert fs is not None and fs.status == StatementStatus.DUE and fs.net_pay > 0  # earned pay never lost
    assert await count(db, TrainerShift, TrainerShift.employee_id == e1.id) == 0  # shifts cancelled
    assert await count(db, TrainerClient, TrainerClient.employee_id == e1.id, TrainerClient.active.is_(True)) == 0
    moved = await db.scalar(select(TrainerClient).where(TrainerClient.customer_id == m.id))
    assert moved.employee_id == replacement.id and moved.active  # reassigned to the trainer the admin picked
    assert await count(db, UserSession, UserSession.user_id == trainer.id, UserSession.revoked_at.is_(None)) == 0
    # SEVERE: the account's recent public address banned through trust & safety (with its exclusions)
    assert await count(db, TrustIpAction, TrustIpAction.user_id == trainer.id, TrustIpAction.action == "banned") == 1
    assert await count(db, IpBan, IpBan.ip == "93.184.216.34") == 1
    assert await count(db, Notification, Notification.user_id == w["owner"].id,
                       Notification.title.like("%ended by the platform%")) == 1
    assert await count(db, Notification, Notification.user_id == w["members"][0].id,
                       Notification.title == "Trainer change") == 1  # told and unlinked
    assert await count(db, Notification, Notification.user_id == m.id, Notification.title == "Your trainer has changed") == 1
    assert await count(db, AuditLog, AuditLog.action == "governance.employee_ban") == 1


async def test_a_non_severe_employee_ban_never_bans_addresses(db):
    w = await world(db)
    admin, c = await admin_client(db)
    trainer = w["staff"][1]
    db.add(UserSession(id="t" * 64, user_id=trainer.id, expires_at=utcnow() + timedelta(days=1), ip="93.184.216.35"))
    await db.commit()
    async with c:
        r = await post_form(c, f"/superadmin/governance/users/{trainer.id}/ban",
                            ban_form(trainer, "policy_violation", note="Repeatedly ignored the safety policy."))
        assert r.status_code == 303
    assert await count(db, IpBan) == 0 and await count(db, TrustIpAction) == 0


async def test_a_super_admin_can_never_ban_themselves_or_another_admin(db):
    admin, c = await admin_client(db, email="first.admin@example.com")
    other = await make_user(db, role=Role.SUPERADMIN, email="second.admin@example.com")
    async with c:
        r = await post_form(c, f"/superadmin/governance/users/{admin.id}/ban", ban_form(admin, "terms_violation"))
        assert r.status_code == 403 and "your own account" in text(r)
        r = await post_form(c, f"/superadmin/governance/users/{other.id}/ban", ban_form(other, "terms_violation"))
        assert r.status_code == 403 and "can't be banned" in text(r)
    assert await G.ban_refusal(db, other, admin) is not None
    await db.execute(update(User).where(User.id == other.id).values(is_active=False))
    await db.commit()
    assert "last platform admin" in await G.ban_refusal(db, other, admin)  # the last active one
    assert await count(db, AccountBan) == 0


async def test_ban_is_idempotent_and_double_submit_safe(db):
    w = await world(db)
    admin, c = await admin_client(db)
    owner = w["owner"]
    async with c:
        page = await c.get(f"/superadmin/governance/users/{owner.id}")
        form = re.search(r'action="/superadmin/governance/users/\d+/ban".*?name="submit_id" value="([^"]+)"', page.text, re.S)
        assert form is not None
        data = {**ban_form(owner, "terms_violation", member_policy="keep"), "submit_id": form.group(1)}
        first = await post_form(c, f"/superadmin/governance/users/{owner.id}/ban", data)
        again = await post_form(c, f"/superadmin/governance/users/{owner.id}/ban", data)  # a double click
        assert first.status_code == again.status_code == 303
        assert first.headers["location"] == again.headers["location"]
        later = await post_form(c, f"/superadmin/governance/users/{owner.id}/ban",
                                ban_form(owner, "terms_violation", member_policy="keep"))  # a second tab
        assert later.status_code == 303 and "already banned" in flash(later)
    assert await count(db, AccountBan, AccountBan.user_id == owner.id) == 1
    assert await count(db, AuditLog, AuditLog.action == "governance.owner_ban") == 1


async def test_lift_owner_ban_reopens_the_gyms(db):
    w = await world(db)
    admin, c = await admin_client(db)
    owner, gym = w["owner"], w["gym"]
    async with c:
        await post_form(c, f"/superadmin/governance/users/{owner.id}/ban", ban_form(owner, "terms_violation",
                                                                                    member_policy="keep"))
        r = await post_form(c, f"/superadmin/governance/users/{owner.id}/lift", {"note": "no"})
        assert r.status_code == 400
        r = await post_form(c, f"/superadmin/governance/users/{owner.id}/lift", {"note": "Appeal accepted after review."})
        assert r.status_code == 303 and "1 gym opened again" in flash(r)
        again = await post_form(c, f"/superadmin/governance/users/{owner.id}/lift", {"note": "Appeal accepted after review."})
        assert "not banned" in flash(again)
    assert (await reload(db, User, owner.id)).is_active
    g = await reload(db, Gym, gym.id)
    assert g.is_active and g.suspended_at is None and g.suspension_kind is None
    assert await count(db, Notification, Notification.user_id == w["members"][0].id,
                       Notification.title == "Your gym is open again") == 1
    assert await count(db, AuditLog, AuditLog.action == "governance.ban_lifted") == 1


async def test_destructive_actions_are_rate_limited(db, monkeypatch):
    w = await world(db)
    admin, c = await admin_client(db)
    monkeypatch.setattr(get_settings(), "governance_actions_per_hour", 1)
    async with c:
        r = await post_form(c, f"/superadmin/governance/users/{w['staff'][1].id}/ban",
                            ban_form(w["staff"][1], "policy_violation", note="Repeatedly ignored the safety policy."))
        assert r.status_code == 303 and "banned" in flash(r)
        r = await post_form(c, f"/superadmin/governance/users/{w['owner'].id}/ban",
                            ban_form(w["owner"], "terms_violation", member_policy="keep"))
        assert r.status_code == 303 and "limit" in flash(r)
    assert await count(db, AccountBan) == 1


async def test_payroll_keeps_recording_pay_of_a_suspended_gym(db):
    w = await world(db)
    admin, c = await admin_client(db)
    async with c:
        await post_form(c, f"/superadmin/governance/users/{w['owner'].id}/ban", ban_form(w["owner"], "terms_violation",
                                                                                         member_policy="keep"))
    first_of_next = (TODAY.replace(day=1) + timedelta(days=32)).replace(day=1)
    before = await count(db, SalaryStatement, SalaryStatement.gym_id == w["gym"].id)
    assert await pr.run_monthly_generation(db, today=first_of_next) >= 3
    assert await count(db, SalaryStatement, SalaryStatement.gym_id == w["gym"].id) >= before + 3


# ====================================================================== delete a gym
async def test_gym_deletion_is_blocked_until_every_obligation_is_settled(db, monkeypatch):
    w = await world(db)
    admin, c = await admin_client(db)
    gym = w["gym"]
    e1, e2, e3 = w["emps"]
    # the payout rail of TEST MODE whatever this machine's .env holds (no money ever moves in tests)
    monkeypatch.setattr(pr, "payout_mode", lambda st, emp: ("test", "TEST MODE: simulates a Cashfree payout."))
    ledger_before = await count(db, Transaction)
    async with c:
        r = await post_form(c, f"/superadmin/governance/gyms/{gym.id}/delete",
                            {"reason": "closed", "note": "The owner closed the gym for good.", "confirm": " iron temple FITNESS "})
        assert r.status_code == 303, text(r)[:300]
        g = await reload(db, Gym, gym.id)
        assert not g.is_active and g.deletion_requested_at is not None and g.suspension_kind == "deletion"
        assert 6.9 < (g.delete_after - g.deletion_requested_at).total_seconds() / 86400 <= 7.0
        final = {"note": "Everything settled - closing the gym.", "confirm": gym.name}
        r = await post_form(c, f"/superadmin/governance/gyms/{gym.id}/finalize", final)
        assert r.status_code == 409 and "cooling-off" in text(r)
        await db.execute(update(Gym).where(Gym.id == gym.id).values(delete_after=utcnow() - timedelta(minutes=1)))
        await db.commit()
        r = await post_form(c, f"/superadmin/governance/gyms/{gym.id}/finalize", final)
        assert r.status_code == 409
        body = text(r)
        assert "3 employees without a final settlement" in body and "3 active memberships" in body
        assert "1 open shop order" in body and "unpaid salary" in body

        # staff: final settlements (nothing paid yet), then a payout, a recorded proof and a waiver
        r = await post_form(c, f"/superadmin/governance/gyms/{gym.id}/settlements",
                            {"last_day": TODAY.isoformat(), "confirm_money": "1"})
        assert "3 final settlements created" in flash(r)
        s1, s2 = [await pr.final_settlement_of(db, e.id) for e in (e1, e2)]
        r = await post_form(c, f"/superadmin/governance/statements/{s1.id}/payout", {"confirm_money": "1"})
        assert "TEST MODE: payout" in flash(r), flash(r)  # payments_test_mode: simulated, counted as paid
        r = await post_form(c, f"/superadmin/governance/statements/{s2.id}/proof",
                            {"reference": "HDFC0012345678", "amount": str(s2.net_pay), "paid_on": TODAY.isoformat()},
                            files={"proof": ("transfer.png", png_bytes(text="paid"), "image/png")})
        assert "recorded" in flash(r), flash(r)
        r = await post_form(c, f"/superadmin/governance/employees/{e3.id}/waive", {"note": "too short"})
        assert "Explain" in flash(r)
        r = await post_form(c, f"/superadmin/governance/employees/{e3.id}/waive",
                            {"note": "Paid in cash by the owner on 3 Nov, signed receipt on file."})
        assert "waived" in flash(r)

        # members: online ones refunded through the refund path, the cash one recorded by hand
        r = await post_form(c, f"/superadmin/governance/gyms/{gym.id}/refunds", {"confirm_money": "1"})
        assert "background" in flash(r)
        r = await post_form(c, f"/superadmin/governance/gyms/{gym.id}/orders", {"confirm_money": "1"})
        assert "background" in flash(r)
        await run_jobs()
        cash = w["subs"][2]
        r = await post_form(c, f"/superadmin/governance/subscriptions/{cash.id}/manual-refund",
                            {"reference": "CASH-REF-77", "note": "Handed back in cash at the desk, receipt signed."})
        assert "Manual refund" in flash(r), flash(r)

        r = await post_form(c, f"/superadmin/governance/gyms/{gym.id}/finalize", final)
        assert r.status_code == 303, text(r)[:600]
        assert "deleted" in flash(r)
    g = await reload(db, Gym, gym.id)
    assert g.archived_at is not None and not g.is_active
    for e in (e1, e2, e3):
        assert (await reload(db, Employee, e.id)).status == EmploymentStatus.TERMINATED
    assert not (await reload(db, JobPosting, w["job"].id)).is_open
    assert (await reload(db, ShopOrder, w["order"].id)).status == OrderStatus.CANCELLED
    assert await count(db, Refund, Refund.order_id == w["order"].id) == 1
    for s in w["subs"]:
        assert (await reload(db, Subscription, s.id)).status == SubscriptionStatus.CANCELLED
    refunds = await count(db, Transaction, Transaction.category == TxCategory.REFUND)
    assert refunds == 4  # 2 online memberships + 1 cash membership + 1 order
    # financial records are never deleted: the gym row, its statements and the whole ledger stay
    assert await count(db, Transaction) >= ledger_before + refunds
    assert await count(db, SalaryStatement, SalaryStatement.gym_id == gym.id) >= 4
    assert (await reload(db, SalaryStatement, w["st3"].id)) is not None
    waiver = await db.scalar(select(GovernanceWaiver).where(GovernanceWaiver.employee_id == e3.id))
    assert waiver is not None and waiver.amount >= Decimal("5000.00") and waiver.actor_id == admin.id
    actions = set((await db.execute(select(AuditLog.action).where(AuditLog.action.like("governance.%")))).scalars())
    assert {"governance.gym_delete_scheduled", "governance.settlements_created", "governance.payout",
            "governance.payment_proof", "governance.pay_waived", "governance.refunds_queued",
            "governance.orders_queued", "governance.manual_refund", "governance.gym_archived"} <= actions
    owner_notes = (await db.execute(select(Notification.title).where(Notification.user_id == w["owner"].id))).scalars().all()
    assert any("scheduled for deletion" in t for t in owner_notes) and any("was deleted" in t for t in owner_notes)
    for staff in w["staff"]:
        assert await count(db, Notification, Notification.user_id == staff.id, Notification.title.like("%closed%")) == 1


async def test_restore_during_the_cooling_off(db):
    w = await world(db)
    admin, c = await admin_client(db)
    gym = w["gym"]
    async with c:
        await post_form(c, f"/superadmin/governance/gyms/{gym.id}/delete",
                        {"reason": "duplicate", "note": "Listed twice by mistake.", "confirm": gym.name})
        await post_form(c, f"/superadmin/governance/gyms/{gym.id}/settlements",
                        {"last_day": TODAY.isoformat(), "confirm_money": "1"})
        voidable = [await pr.final_settlement_of(db, e.id) for e in w["emps"]]
        assert all(s is not None for s in voidable)
        r = await post_form(c, f"/superadmin/governance/gyms/{gym.id}/restore", {"note": "The listing is legitimate."})
        assert r.status_code == 303 and "open again" in flash(r)
        r = await post_form(c, f"/superadmin/governance/gyms/{gym.id}/restore", {"note": "The listing is legitimate."})
        assert "No deletion" in flash(r)  # idempotent
    g = await reload(db, Gym, gym.id)
    assert g.is_active and g.deletion_requested_at is None and g.delete_after is None and g.suspended_at is None
    for s in voidable:  # unpaid settlements the deletion created are voided - employment goes on
        assert (await reload(db, SalaryStatement, s.id)).status == StatementStatus.VOID
    for e in w["emps"]:
        assert (await reload(db, Employee, e.id)).status == EmploymentStatus.ACTIVE
    async with make_client() as anon:
        assert (await anon.get(f"/gyms/{gym.slug}")).status_code == 200
    assert await count(db, AuditLog, AuditLog.action == "governance.gym_restored") == 1


async def test_restoring_a_banned_owners_gym_keeps_it_suspended(db):
    w = await world(db)
    admin, c = await admin_client(db)
    owner, gym = w["owner"], w["gym"]
    async with c:
        await post_form(c, f"/superadmin/governance/users/{owner.id}/ban", ban_form(owner, "terms_violation",
                                                                                    member_policy="keep"))
        await post_form(c, f"/superadmin/governance/gyms/{gym.id}/delete",
                        {"reason": "owner_banned", "note": "The owner is banned for fraud.", "confirm": gym.name})
        r = await post_form(c, f"/superadmin/governance/gyms/{gym.id}/restore", {"note": "Deletion not needed yet."})
        assert "stays suspended" in flash(r)
    g = await reload(db, Gym, gym.id)
    assert not g.is_active and g.suspension_kind == "owner_ban" and g.deletion_requested_at is None


async def test_the_settlement_desk_without_payouts_shows_to_pay(db, monkeypatch):
    w = await world(db)
    admin, c = await admin_client(db)
    gym = w["gym"]
    monkeypatch.setattr(pr, "payout_mode", lambda st, emp: (None, "Cashfree Payouts are not configured - pay by bank "
                                                                  "transfer and upload the proof."))
    async with c:
        await post_form(c, f"/superadmin/governance/gyms/{gym.id}/delete",
                        {"reason": "closed", "note": "The owner closed the gym for good.", "confirm": gym.name})
        r = await c.get(f"/superadmin/governance/gyms/{gym.id}")
        assert r.status_code == 200
        body = text(r)
        assert "Settlement desk" in body and "To pay" in body and "Record proof" in body
        assert "Pay via Cashfree" not in body and "Pay (test mode)" not in body
        r = await post_form(c, f"/superadmin/governance/statements/{w['st3'].id}/payout", {"confirm_money": "1"})
        assert "not configured" in flash(r)  # money never moves without a payout rail: proof recording instead
    assert (await reload(db, SalaryStatement, w["st3"].id)).status == StatementStatus.DUE


async def test_money_moves_only_with_an_explicit_confirmation(db):
    w = await world(db)
    admin, c = await admin_client(db)
    gym = w["gym"]
    async with c:
        await post_form(c, f"/superadmin/governance/gyms/{gym.id}/delete",
                        {"reason": "closed", "note": "The owner closed the gym for good.", "confirm": gym.name})
        r = await post_form(c, f"/superadmin/governance/statements/{w['st3'].id}/payout", {})
        assert "Tick the box" in flash(r)
        r = await post_form(c, f"/superadmin/governance/gyms/{gym.id}/refunds", {})
        assert "Tick the box" in flash(r)
        r = await post_form(c, f"/superadmin/governance/gyms/{gym.id}/orders", {})
        assert "Tick the box" in flash(r)
    await run_jobs()
    assert (await reload(db, SalaryStatement, w["st3"].id)).status == StatementStatus.DUE
    assert await count(db, Transaction, Transaction.category == TxCategory.REFUND) == 0
    assert (await reload(db, ShopOrder, w["order"].id)).status == OrderStatus.PAID
