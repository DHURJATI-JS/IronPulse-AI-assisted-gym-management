"""Money and irreversible flows under two identical requests at the same moment - WITHOUT a shared submit id (two tabs,
two renders of the form, an API client): the submit guard cannot help, so each flow's own natural-key protection (a
unique constraint, a row lock or an advisory lock) must leave exactly one effect.

Covered: shop order placement, the shop's test-gateway payment, a customer's cancel (refund) and refund request, an
owner's refund, a gym-membership purchase and its payment, payroll generation, a staff invitation and account deletion.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select

from app.models import (
    AuditLog,
    Employee,
    InventoryMovement,
    Product,
    Refund,
    SalaryStatement,
    ShopOrder,
    ShopOrderItem,
    Subscription,
    Transaction,
    User,
)
from app.models.enums import SubscriptionStatus, TxCategory
from app.models.shop import OrderStatus
from app.services import auth_flows as af
from tests.conftest import csrf_token, login_as, make_client
from tests.factories import DEFAULT_PASSWORD, make_customer, make_employee, png_bytes
from tests.test_shop import _reset_shop_ai, make_order, make_product, shop_setup  # noqa: F401
from tests.test_shop_payments import bag


async def tabs(user: Any, n: int = 2) -> list[Any]:
    """``n`` tabs of one signed-in browser: one session cookie, one CSRF cookie."""
    first = make_client()
    await login_as(first, user)
    token = await csrf_token(first)
    clients = [first]
    for _ in range(n - 1):
        c = make_client()
        for name, value in first.cookies.items():
            c.cookies.set(name, value)
        clients.append(c)
    for c in clients:
        c.__dict__["csrf"] = token
    return clients


async def at_once(clients: list[Any], url: str, data: dict[str, Any] | None = None, files: Any = None) -> list[Any]:
    async def one(c: Any) -> Any:
        return await c.post(url, data={**(data or {}), "csrf_token": c.__dict__["csrf"]}, files=files)

    try:
        return list(await asyncio.gather(*(one(c) for c in clients)))
    finally:
        for c in clients:
            await c.aclose()


async def count(db: Any, *where: Any, model: Any) -> int:
    return int(await db.scalar(select(func.count()).select_from(model).where(*where)) or 0)


# ============================================================================================ shop
async def test_two_checkouts_at_once_leave_one_unpaid_order(db):
    owner, gym, branch = await shop_setup(db)
    product = await make_product(db, gym, price=Decimal("1000"), stock=10)
    cust = await make_customer(db)
    await bag(db, cust, product, 2)
    rs = await at_once(await tabs(cust), "/shop/checkout", {"gym": gym.id, "fulfillment": "pickup",
                                                             "branch_id": branch.id})
    assert all(r.status_code == 303 for r in rs), [r.text[:300] for r in rs]
    unpaid = await count(db, ShopOrder.customer_id == cust.id,
                         ShopOrder.status.in_([OrderStatus.PENDING_PAYMENT, OrderStatus.PAYMENT_FAILED]),
                         model=ShopOrder)
    assert unpaid == 1  # the later order supersedes the earlier one (cancelled; a late payment is refunded)


async def test_a_payment_confirmed_twice_at_once_is_taken_once(db):
    owner, gym, branch = await shop_setup(db)
    product = await make_product(db, gym, price=Decimal("1000"), stock=10)
    cust = await make_customer(db)
    order = await make_order(db, cust, gym, [(product, 2)], status=OrderStatus.PENDING_PAYMENT, branch=branch)
    oid = order.id
    rs = await at_once(await tabs(cust), f"/shop/orders/{oid}/test-gateway", {"decision": "approve"})
    assert all(r.status_code == 303 for r in rs)
    assert await count(db, Transaction.category == TxCategory.SHOP_SALE, model=Transaction) == 1
    assert await count(db, InventoryMovement.order_id == oid, model=InventoryMovement) == 1
    assert await db.scalar(select(Product.stock).where(Product.id == product.id)) == 8


async def test_a_cancel_sent_twice_at_once_refunds_once(db):
    owner, gym, branch = await shop_setup(db)
    product = await make_product(db, gym, price=Decimal("1000"), stock=10)
    cust = await make_customer(db)
    order = await make_order(db, cust, gym, [(product, 1)], status=OrderStatus.PAID, branch=branch)
    rs = await at_once(await tabs(cust), f"/shop/orders/{order.id}/cancel")
    assert all(r.status_code == 303 for r in rs)
    assert await count(db, Transaction.category == TxCategory.REFUND, model=Transaction) <= 1
    assert await count(db, Refund.order_id == order.id, model=Refund) <= 1


async def test_a_refund_request_sent_twice_at_once_is_one_request(db):
    owner, gym, branch = await shop_setup(db)
    product = await make_product(db, gym)
    cust = await make_customer(db)
    order = await make_order(db, cust, gym, [(product, 2)], status=OrderStatus.COMPLETED, completed_days_ago=1,
                             branch=branch)
    item = await db.scalar(select(ShopOrderItem.id).where(ShopOrderItem.order_id == order.id))
    rs = await at_once(await tabs(cust), f"/shop/orders/{order.id}/refund-request",
                       {f"qty_{item}": 1, "reason": "Arrived damaged and leaking"})
    assert all(r.status_code in (303, 400, 409) for r in rs)
    assert await count(db, Refund.order_id == order.id, model=Refund) == 1


async def test_an_owner_refund_sent_twice_at_once_never_refunds_more_than_was_paid(db):
    owner, gym, branch = await shop_setup(db)
    product = await make_product(db, gym, price=Decimal("1000"), stock=10)
    cust = await make_customer(db)
    order = await make_order(db, cust, gym, [(product, 1)], status=OrderStatus.COMPLETED, completed_days_ago=1,
                             branch=branch)
    oid, total = order.id, order.total
    item = await db.scalar(select(ShopOrderItem.id).where(ShopOrderItem.order_id == oid))
    rs = await at_once(await tabs(owner), f"/owner/shop/orders/{oid}/refund",
                       {f"qty_{item}": 1, "reason": "Goodwill refund for the customer"})
    assert all(r.status_code in (303, 400, 409) for r in rs)
    assert await count(db, Refund.order_id == oid, model=Refund) == 1
    refunded = await db.scalar(select(func.coalesce(func.sum(Transaction.amount), 0))
                               .where(Transaction.category == TxCategory.REFUND))
    assert Decimal(refunded) == total


# ============================================================================================ gym memberships
async def test_a_membership_bought_twice_at_once_is_one_subscription_and_one_payment(db):
    from tests.test_checkout import near_customer, setup_gym

    env = await setup_gym(db)
    cust = await near_customer(db)
    cid = cust.id
    rs = await at_once(await tabs(cust), f"/subscribe/{env.gym.slug}", {"plan": "online"})
    assert all(r.status_code == 303 for r in rs), [r.text[:300] for r in rs]
    subs = (await db.execute(select(Subscription).where(Subscription.customer_id == cid))).scalars().all()
    assert len(subs) == 1, [(s.id, s.status) for s in subs]
    sid = subs[0].id
    pay = await tabs(cust, 1)
    r = await pay[0].post(f"/checkout/{sid}/pay", data={"csrf_token": pay[0].__dict__["csrf"]})
    await pay[0].aclose()
    order_id = r.headers["location"].split("order_id=")[1]
    rs = await at_once(await tabs(cust), f"/checkout/{sid}/test-gateway", {"order_id": order_id, "decision": "approve"})
    assert all(r.status_code == 303 for r in rs)
    db.expire_all()
    sub = await db.get(Subscription, sid)
    assert sub.status == SubscriptionStatus.ACTIVE
    assert await count(db, Transaction.subscription_id == sid, Transaction.category == TxCategory.SUBSCRIPTION,
                       model=Transaction) == 1


# ============================================================================================ payroll / staff
async def test_payroll_generated_twice_at_once_creates_each_statement_once(db):
    from datetime import date, timedelta

    from tests.test_payroll import setup_staff

    owner, gym, emp = await setup_staff(db)
    await make_employee(db, gym, salary="25000", hire_date=date(2020, 3, 1))
    last = (date.today().replace(day=1) - timedelta(days=1))
    rs = await at_once(await tabs(owner), "/owner/payroll/generate", {"month": f"{last:%Y-%m}", "gym": gym.id})
    assert all(r.status_code in (200, 303) for r in rs), [r.status_code for r in rs]
    per_employee = (await db.execute(select(SalaryStatement.employee_id, func.count()).group_by(SalaryStatement.employee_id))).all()
    assert per_employee and all(n == 1 for _, n in per_employee)


async def test_a_staff_invitation_sent_twice_at_once_adds_one_person(db):
    from tests.factories import make_gym, make_owner
    from tests.test_staff import staff_form

    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    files = [("photo", ("me.png", png_bytes((120, 120), "teal"), "image/png"))]
    rs = await at_once(await tabs(owner), f"/owner/gyms/{gym.id}/staff/new", staff_form(work_mode="online"), files=files)
    assert sorted(r.status_code for r in rs)[0] == 303, [r.status_code for r in rs]
    assert await count(db, Employee.invite_email == "rahul.coach@example.com", Employee.deleted_at.is_(None),
                       model=Employee) == 1


# ============================================================================================ account deletion
async def test_an_account_deleted_twice_at_once_is_deleted_once(db):
    """soft_delete_user locks the account row and re-reads deleted_at (it used to check the copy loaded before the
    re-authentication, so two deletions at once both ran: two audit rows, registry writes and revocations twice)."""
    cust = await make_customer(db, email="bye-twice@example.com")
    uid = cust.id
    rs = await at_once(await tabs(cust), "/account/delete", {"password": DEFAULT_PASSWORD, "confirm_text": "DELETE",
                                                             "reason": "privacy", "confirm_identity": "1"})
    assert sorted(r.status_code for r in rs)[0] == 303
    db.expire_all()
    user = await db.get(User, uid)
    assert user.deleted_at is not None and not user.is_active
    deletions = await count(db, AuditLog.entity_type == "user", AuditLog.entity_id == uid,
                            AuditLog.action.like("%delete%"), model=AuditLog)
    assert deletions == 1
    assert af  # the module stays importable for the lockout helpers used elsewhere
