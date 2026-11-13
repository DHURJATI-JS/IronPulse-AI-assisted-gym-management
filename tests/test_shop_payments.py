"""WP-L gym shop - checkout, payments (TEST MODE gateway + Cashfree adapter), idempotent fulfilment, refunds."""

from __future__ import annotations

import json
import re
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select, update

from app.config import get_settings
from app.core.utils import utcnow
from app.models import (
    AuditLog,
    CartItem,
    Coupon,
    InventoryMovement,
    Notification,
    Product,
    Refund,
    ShopOrder,
    ShopOrderItem,
    Transaction,
)
from app.models.enums import TxCategory, TxDirection, TxStatus
from app.models.shop import OrderStatus, RefundStatus
from app.services import shop_payments as pay
from tests.conftest import login_as, post_form
from tests.factories import make_customer
from tests.test_shop import _reset_shop_ai, add_condition, make_minor, make_order, make_product, shop_setup  # noqa: F401


# ====================================================================================================== helpers
async def bag(db, user, product, qty: int) -> None:
    db.add(CartItem(user_id=user.id, product_id=product.id, quantity=qty))
    await db.commit()


async def checkout(client, gym, branch=None, **extra):
    data = {"gym": gym.id, "fulfillment": "pickup" if branch else "delivery", **extra}
    if branch:
        data["branch_id"] = branch.id
    return await post_form(client, "/shop/checkout", data)


def order_id_from(location: str) -> int:
    return int(location.split("/")[3])


async def fresh(db, model, pk):
    return await db.get(model, pk, populate_existing=True)


async def titles(db, user_id: int) -> list[str]:
    return list((await db.execute(select(Notification.title).where(Notification.user_id == user_id)
                                  .order_by(Notification.id))).scalars().all())


async def paid_order(client, db, *, qty: int = 2, stock: int = 10, price: str = "1000", coupon: str | None = None):
    """Customer checks out through the real routes and pays on the TEST MODE gateway."""
    owner, gym, branch = await shop_setup(db)
    product = await make_product(db, gym, name="Gold Whey Protein", price=Decimal(price), stock=stock)
    cust = await make_customer(db)
    await bag(db, cust, product, qty)
    await login_as(client, cust)
    extra = {"coupon": coupon} if coupon else {}
    r = await checkout(client, gym, branch, **extra)
    assert r.status_code == 303 and r.headers["location"].endswith("/test-gateway"), r.text[:500]
    oid = order_id_from(r.headers["location"])
    r = await post_form(client, f"/shop/orders/{oid}/test-gateway", {"decision": "approve"})
    assert r.status_code == 303
    return owner, gym, branch, product, cust, oid


# ====================================================================================================== checkout
async def test_checkout_uses_server_prices_and_creates_pending_order(client, db):
    _, gym, branch = await shop_setup(db)
    p = await make_product(db, gym, price=Decimal("1000"), tax_rate=Decimal("18"))
    cust = await make_customer(db)
    await bag(db, cust, p, 2)
    await login_as(client, cust)
    r = await client.get(f"/shop/checkout?gym={gym.id}")
    assert r.status_code == 200 and "Place order" in r.text and "Test mode" in r.text
    r = await checkout(client, gym, branch, price="1", total="1", unit_price="0.01", subtotal="1")  # tampered fields
    assert r.status_code == 303 and r.headers["location"].endswith("/test-gateway")
    order = await fresh(db, ShopOrder, order_id_from(r.headers["location"]))
    assert order.status == OrderStatus.PENDING_PAYMENT and order.payment_provider == "test"
    assert (order.subtotal, order.tax_total, order.shipping_fee, order.total) == (
        Decimal("2000.00"), Decimal("360.00"), Decimal("0.00"), Decimal("2360.00"))
    assert order.order_number.startswith(f"IPO-{utcnow().year}-") and len(order.order_number.split("-")[2]) == 6
    assert order.customer_label.startswith(cust.full_name) and order.created_ip == "127.0.0.1"
    assert order.pickup_branch_id == branch.id
    item = (await db.execute(select(ShopOrderItem).where(ShopOrderItem.order_id == order.id))).scalar_one()
    assert (item.product_name, item.unit_price, item.quantity, item.line_total) == (
        f"{p.brand} {p.name}", Decimal("1000.00"), 2, Decimal("2360.00"))  # snapshot keeps the brand
    assert await db.scalar(select(Product.stock).where(Product.id == p.id)) == 10  # reserved only when paid
    r = await client.get(r.headers["location"])
    assert r.status_code == 200 and "Test mode - simulated payment" in r.text and "₹2,360.00" in r.text


async def test_checkout_rejects_out_of_stock_and_over_stock(client, db):
    _, gym, branch = await shop_setup(db)
    p = await make_product(db, gym, stock=5)
    cust = await make_customer(db)
    await bag(db, cust, p, 4)
    await login_as(client, cust)
    await db.execute(update(Product).where(Product.id == p.id).values(stock=2))
    await db.commit()
    r = await checkout(client, gym, branch)
    assert r.status_code == 400 and "Only 2 left" in r.text
    await db.execute(update(Product).where(Product.id == p.id).values(stock=0))
    await db.commit()
    r = await checkout(client, gym, branch)
    assert r.status_code == 400 and "Out of stock" in r.text
    assert await db.scalar(select(func.count(ShopOrder.id))) == 0


async def test_checkout_coupon_rules(client, db):
    _, gym, branch = await shop_setup(db)
    p = await make_product(db, gym, price=Decimal("1000"), tax_rate=Decimal("18"))
    now = utcnow()
    db.add_all([
        Coupon(gym_id=gym.id, code="OLD10", percent_off=10, valid_until=now - timedelta(days=1)),
        Coupon(gym_id=gym.id, code="BIG500", amount_off=Decimal("500"), min_order_total=Decimal("5000")),
        Coupon(gym_id=gym.id, code="ONCE", percent_off=20, max_uses=1, used_count=1),
        Coupon(gym_id=gym.id, code="PAUSED", percent_off=5, is_active=False),
        Coupon(gym_id=gym.id, code="SAVE10", percent_off=10, max_uses=5),
    ])
    await db.commit()
    cust = await make_customer(db)
    await bag(db, cust, p, 2)
    await login_as(client, cust)
    for code, text in (("OLD10", "expired"), ("BIG500", "minimum order"), ("ONCE", "usage limit"), ("PAUSED", "no longer active"),
                       ("NOPE", "isn")):
        r = await checkout(client, gym, branch, coupon=code)
        assert r.status_code == 400 and text in r.text, code
    assert await db.scalar(select(func.count(ShopOrder.id))) == 0
    r = await checkout(client, gym, branch, coupon="save10")
    assert r.status_code == 303
    order = await fresh(db, ShopOrder, order_id_from(r.headers["location"]))
    # 10% off 2000 = 200; tax 18% on the discounted 1800 = 324
    assert (order.discount_total, order.tax_total, order.total, order.coupon_code) == (
        Decimal("200.00"), Decimal("324.00"), Decimal("2124.00"), "SAVE10")


async def test_checkout_blocks_minors_from_adult_products(client, db):
    _, gym, branch = await shop_setup(db)
    pre = await make_product(db, gym, name="Ignite Pre-Workout", age_restricted=True)
    minor = await make_minor(db)
    await bag(db, minor, pre, 1)  # e.g. added before the product became 18+
    await login_as(client, minor)
    r = await checkout(client, gym, branch)
    assert r.status_code == 400 and "Adults only (18+)" in r.text
    assert await db.scalar(select(func.count(ShopOrder.id))) == 0


async def test_checkout_fulfilment_rules_and_delivery_fee(client, db):
    _, gym, branch = await shop_setup(db)
    cheap = await make_product(db, gym, price=Decimal("500"), tax_rate=Decimal("0"), fulfillment="delivery")
    cust = await make_customer(db)
    await bag(db, cust, cheap, 1)
    await login_as(client, cust)
    r = await checkout(client, gym, branch)  # pickup not offered for a delivery-only product
    assert r.status_code == 400
    r = await checkout(client, gym, None, ship_name="A", ship_phone="12", ship_line1="x", ship_city="", ship_postal="!")
    assert r.status_code == 400 and "Enter the street address" in r.text and "postal" in r.text
    r = await checkout(client, gym, None, ship_name="Asha Rao", ship_phone="98765 43210", ship_line1="12 MG Road, Indiranagar",
                       ship_city="Bengaluru", ship_postal="560038")
    assert r.status_code == 303
    order = await fresh(db, ShopOrder, order_id_from(r.headers["location"]))
    assert order.fulfillment == "delivery" and order.shipping_fee == Decimal("49.00") and order.total == Decimal("549.00")
    assert order.shipping_address["city"] == "Bengaluru" and order.pickup_branch_id is None
    await bag(db, cust, await make_product(db, gym, price=Decimal("600"), tax_rate=Decimal("0")), 1)
    r = await checkout(client, gym, None, ship_name="Asha Rao", ship_phone="98765 43210", ship_line1="12 MG Road, Indiranagar",
                       ship_city="Bengaluru", ship_postal="560038")
    newer = await fresh(db, ShopOrder, order_id_from(r.headers["location"]))
    assert newer.shipping_fee == Decimal("0.00") and newer.total == Decimal("1100.00")  # free from ₹999
    assert (await fresh(db, ShopOrder, order.id)).status == OrderStatus.CANCELLED  # superseded unpaid order


async def test_checkout_requires_safety_acknowledgement_when_warned(client, db):
    _, gym, branch = await shop_setup(db)
    whey = await make_product(db, gym, name="Gold Whey Protein", allergens=["milk"])
    cust = await make_customer(db)
    await add_condition(db, cust, "Milk")
    await bag(db, cust, whey, 1)
    await login_as(client, cust)
    r = await client.get(f"/shop/checkout?gym={gym.id}")
    assert "Safety notes for your items" in r.text and "Contains milk / dairy" in r.text
    r = await checkout(client, gym, branch)
    assert r.status_code == 400 and "read the safety notes" in r.text
    r = await checkout(client, gym, branch, safety_ack="1")
    assert r.status_code == 303


# ====================================================================================================== payment outcomes
async def test_test_gateway_payment_fulfils_order(client, db):
    owner, gym, branch = await shop_setup(db)
    p = await make_product(db, gym, name="Gold Whey Protein", price=Decimal("1000"), stock=5, low_stock_threshold=3)
    other = await make_product(db, gym, name="Keep In Bag", stock=9)
    db.add(Coupon(gym_id=gym.id, code="SAVE10", percent_off=10))
    await db.commit()
    cust = await make_customer(db)
    await bag(db, cust, p, 2)
    await login_as(client, cust)
    r = await checkout(client, gym, branch, coupon="SAVE10")
    oid = order_id_from(r.headers["location"])
    await bag(db, cust, other, 1)  # added after checkout -> must stay in the bag
    r = await post_form(client, f"/shop/orders/{oid}/test-gateway", {"decision": "approve"})
    assert r.status_code == 303 and r.headers["location"] == f"/shop/orders/{oid}"
    order = await fresh(db, ShopOrder, oid)
    assert order.status == OrderStatus.PAID and order.paid_at is not None
    assert await db.scalar(select(Product.stock).where(Product.id == p.id)) == 3
    mv = (await db.execute(select(InventoryMovement).where(InventoryMovement.order_id == oid))).scalar_one()
    assert (mv.change, mv.reason) == (-2, "sale")
    assert await db.scalar(select(Coupon.used_count).where(Coupon.code == "SAVE10")) == 1
    tx = (await db.execute(select(Transaction).where(Transaction.category == TxCategory.SHOP_SALE))).scalar_one()
    assert (tx.direction, tx.status, tx.amount, tx.gym_id, tx.customer_id) == (
        TxDirection.INCOMING, TxStatus.SUCCESS, order.total, gym.id, cust.id)
    assert tx.provider == "test" and tx.provider_order_id == order.order_number and tx.provider_payment_id.startswith("TEST-PAY-")
    assert tx.party_label == order.customer_label
    assert [c for (c,) in (await db.execute(select(CartItem.product_id).where(CartItem.user_id == cust.id))).all()] == [other.id]
    assert "Order confirmed" in await titles(db, cust.id)
    owner_titles = await titles(db, owner.id)
    assert "New shop order" in owner_titles
    body = await db.scalar(select(Notification.body).where(Notification.user_id == owner.id, Notification.title == "New shop order"))
    assert "Low stock now: Gold Whey Protein (3 left)" in body
    r = await client.get(f"/shop/orders/{oid}")
    assert r.status_code == 200 and "Payment received" in r.text and "Changed your mind?" in r.text


async def test_payment_success_is_idempotent(client, db):
    _, gym, _, product, cust, oid = await paid_order(client, db, qty=2, stock=10)
    order = await fresh(db, ShopOrder, oid)
    again = await pay.handle_payment_success(db, order.order_number, order.total, order.currency, "dup-1")
    await db.commit()
    assert again == "already_paid"
    assert await db.scalar(select(Product.stock).where(Product.id == product.id)) == 8
    assert await db.scalar(select(func.count(Transaction.id)).where(Transaction.category == TxCategory.SHOP_SALE)) == 1
    assert await db.scalar(select(func.count(InventoryMovement.id)).where(InventoryMovement.order_id == oid)) == 1
    assert await pay.handle_payment_success(db, "IPO-1999-000000", "1", "INR", None) == "not_found"


async def test_amount_mismatch_is_flagged_and_not_fulfilled(client, db):
    owner, gym, branch = await shop_setup(db)
    p = await make_product(db, gym, stock=10)
    cust = await make_customer(db)
    await bag(db, cust, p, 1)
    await login_as(client, cust)
    oid = order_id_from((await checkout(client, gym, branch)).headers["location"])
    order = await fresh(db, ShopOrder, oid)
    outcome = await pay.handle_payment_success(db, order.order_number, order.total - Decimal("1"), "INR", "cf_pay_1")
    await db.commit()
    assert outcome == "mismatch"
    order = await fresh(db, ShopOrder, oid)
    assert order.status == OrderStatus.PAYMENT_FAILED and order.paid_at is None and pay.MISMATCH_MARK in order.notes
    assert await db.scalar(select(Product.stock).where(Product.id == p.id)) == 10
    assert await db.scalar(select(func.count(Transaction.id))) == 0
    assert "Shop payment needs review" in await titles(db, owner.id)
    assert await db.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == "shop.payment.mismatch")) == 1
    assert await pay.handle_payment_success(db, order.order_number, order.total, "USD", "x") == "mismatch"  # currency too
    await db.commit()
    assert (await titles(db, owner.id)).count("Shop payment needs review") == 1  # flagged once
    r = await client.get(f"/shop/orders/{oid}")
    assert "Payment under review" in r.text and "Pay now" not in r.text


async def test_declined_payment_marks_failed_and_can_be_retried(client, db):
    _, gym, branch = await shop_setup(db)
    p = await make_product(db, gym)
    cust = await make_customer(db)
    await bag(db, cust, p, 1)
    await login_as(client, cust)
    oid = order_id_from((await checkout(client, gym, branch)).headers["location"])
    await post_form(client, f"/shop/orders/{oid}/test-gateway", {"decision": "decline"})
    assert (await fresh(db, ShopOrder, oid)).status == OrderStatus.PAYMENT_FAILED
    assert "Payment failed" in await titles(db, cust.id)
    r = await post_form(client, f"/shop/orders/{oid}/pay", {})
    assert r.status_code == 303 and r.headers["location"] == f"/shop/orders/{oid}/test-gateway"
    await post_form(client, f"/shop/orders/{oid}/test-gateway", {"decision": "approve"})
    assert (await fresh(db, ShopOrder, oid)).status == OrderStatus.PAID


async def test_stock_shortage_at_confirmation_refunds_and_cancels(client, db):
    owner, gym, branch = await shop_setup(db)
    p = await make_product(db, gym, stock=5)
    cust = await make_customer(db)
    await bag(db, cust, p, 3)
    await login_as(client, cust)
    oid = order_id_from((await checkout(client, gym, branch)).headers["location"])
    await db.execute(update(Product).where(Product.id == p.id).values(stock=1))  # sold elsewhere meanwhile
    await db.commit()
    await post_form(client, f"/shop/orders/{oid}/test-gateway", {"decision": "approve"})
    order = await fresh(db, ShopOrder, oid)
    assert order.status == OrderStatus.CANCELLED and order.refunded_total == order.total and order.paid_at is not None
    assert await db.scalar(select(Product.stock).where(Product.id == p.id)) == 1
    refund = (await db.execute(select(Refund).where(Refund.order_id == oid))).scalar_one()
    assert refund.status == RefundStatus.COMPLETED and refund.amount == order.total and refund.provider_refund_id.startswith("TEST-RF-")
    kinds = dict((await db.execute(select(Transaction.category, Transaction.amount))).all())
    assert kinds == {TxCategory.SHOP_SALE: order.total, TxCategory.REFUND: order.total}
    assert "Order cancelled - refund issued" in await titles(db, cust.id)
    assert "Shop order auto-refunded" in await titles(db, owner.id)


async def test_late_payment_for_cancelled_order_is_refunded(client, db):
    _, gym, branch = await shop_setup(db)
    p = await make_product(db, gym, stock=5)
    cust = await make_customer(db)
    await bag(db, cust, p, 1)
    await login_as(client, cust)
    oid = order_id_from((await checkout(client, gym, branch)).headers["location"])
    r = await post_form(client, f"/shop/orders/{oid}/cancel", {})
    assert (await fresh(db, ShopOrder, oid)).status == OrderStatus.CANCELLED
    order = await fresh(db, ShopOrder, oid)
    assert await pay.handle_payment_success(db, order.order_number, order.total, "INR", "late") == "refunded_cancelled"
    await db.commit()
    order = await fresh(db, ShopOrder, oid)
    assert order.status == OrderStatus.CANCELLED and order.refunded_total == order.total
    assert await db.scalar(select(Product.stock).where(Product.id == p.id)) == 5


# ====================================================================================================== cancellations & refunds
async def test_customer_cancel_refunds_and_restocks(client, db):
    owner, gym, _, product, cust, oid = await paid_order(client, db, qty=2, stock=10)
    assert await db.scalar(select(Product.stock).where(Product.id == product.id)) == 8
    r = await post_form(client, f"/shop/orders/{oid}/cancel", {})
    assert r.status_code == 303
    order = await fresh(db, ShopOrder, oid)
    assert order.status == OrderStatus.CANCELLED and order.cancelled_at and order.refunded_total == order.total
    assert await db.scalar(select(Product.stock).where(Product.id == product.id)) == 10
    assert await db.scalar(select(InventoryMovement.change).where(InventoryMovement.reason == "cancel")) == 2
    refund_tx = (await db.execute(select(Transaction).where(Transaction.category == TxCategory.REFUND))).scalar_one()
    assert (refund_tx.direction, refund_tx.status, refund_tx.amount) == (TxDirection.OUTGOING, TxStatus.SUCCESS, order.total)
    assert "Order cancelled" in await titles(db, cust.id) and "Shop order cancelled" in await titles(db, owner.id)
    r = await post_form(client, f"/shop/orders/{oid}/cancel", {})  # second click: no double refund
    assert await db.scalar(select(func.count(Transaction.id)).where(Transaction.category == TxCategory.REFUND)) == 1


async def test_customer_cannot_cancel_once_processing(client, db, anon_client_factory):
    owner, gym, _, product, cust, oid = await paid_order(client, db)
    oc = anon_client_factory("127.0.0.1")
    await login_as(oc, owner)
    await post_form(oc, f"/owner/shop/orders/{oid}/status", {"action": "processing"})
    await post_form(client, f"/shop/orders/{oid}/cancel", {})
    assert (await fresh(db, ShopOrder, oid)).status == OrderStatus.PROCESSING
    assert await db.scalar(select(func.count(Refund.id))) == 0


async def test_refund_request_window_and_validation(client, db):
    owner, gym, branch = await shop_setup(db)
    p = await make_product(db, gym)
    cust = await make_customer(db)
    await login_as(client, cust)
    old = await make_order(db, cust, gym, [(p, 1)], status=OrderStatus.COMPLETED, completed_days_ago=8, branch=branch)
    item = await db.scalar(select(ShopOrderItem.id).where(ShopOrderItem.order_id == old.id))
    await post_form(client, f"/shop/orders/{old.id}/refund-request", {f"qty_{item}": 1, "reason": "Arrived damaged and leaking"})
    assert await db.scalar(select(func.count(Refund.id))) == 0
    recent = await make_order(db, cust, gym, [(p, 2)], status=OrderStatus.COMPLETED, completed_days_ago=2, branch=branch)
    item = await db.scalar(select(ShopOrderItem.id).where(ShopOrderItem.order_id == recent.id))
    await post_form(client, f"/shop/orders/{recent.id}/refund-request", {f"qty_{item}": 1, "reason": "short"})
    await post_form(client, f"/shop/orders/{recent.id}/refund-request", {f"qty_{item}": 3, "reason": "Arrived damaged and leaking"})
    assert await db.scalar(select(func.count(Refund.id))) == 0
    r = await post_form(client, f"/shop/orders/{recent.id}/refund-request", {f"qty_{item}": 1, "reason": "Arrived damaged and leaking"})
    assert r.status_code == 303
    refund = (await db.execute(select(Refund))).scalar_one()
    assert refund.status == RefundStatus.REQUESTED and refund.amount == Decimal("1180.00") and refund.requested_by_id == cust.id
    assert (await fresh(db, ShopOrder, recent.id)).status == OrderStatus.REFUND_REQUESTED
    assert "Refund requested" in await titles(db, owner.id)
    stranger = await make_customer(db)
    await login_as(client, stranger)
    r = await client.get(f"/shop/orders/{recent.id}")
    assert r.status_code == 404


async def test_owner_partial_refund_amounts_quantities_ledger_and_restock(client, db, anon_client_factory):
    owner, gym, branch = await shop_setup(db)
    a = await make_product(db, gym, name="Whey A", price=Decimal("1000"), tax_rate=Decimal("18"), stock=10)
    b = await make_product(db, gym, name="Shaker B", price=Decimal("500"), tax_rate=Decimal("12"), stock=10)
    cust = await make_customer(db)
    order = await make_order(db, cust, gym, [(a, 2), (b, 1)], status=OrderStatus.COMPLETED, completed_days_ago=1,
                             discount=Decimal("250"), branch=branch)
    assert order.total == Decimal("2628.00")  # 2500 - 250 + tax(324 + 54)
    items = {i.product_id: i for i in (await db.execute(select(ShopOrderItem).where(ShopOrderItem.order_id == order.id))).scalars()}
    assert (items[a.id].line_total, items[b.id].line_total) == (Decimal("2124.00"), Decimal("504.00"))
    await login_as(client, cust)
    await post_form(client, f"/shop/orders/{order.id}/refund-request", {f"qty_{items[a.id].id}": 2, "reason": "One tub was opened"})
    refund = (await db.execute(select(Refund))).scalar_one()
    oc = anon_client_factory("127.0.0.1")
    await login_as(oc, owner)
    r = await oc.get(f"/owner/shop/orders/{order.id}")
    assert r.status_code == 200 and "One tub was opened" in r.text
    # owner approves only 1 of the 2 requested units, with restock
    r = await post_form(oc, f"/owner/shop/refunds/{refund.id}/approve", {f"qty_{items[a.id].id}": 1, "restock": "1"})
    assert r.status_code == 303
    refund = await fresh(db, Refund, refund.id)
    order = await fresh(db, ShopOrder, order.id)
    item_a = await fresh(db, ShopOrderItem, items[a.id].id)
    assert refund.status == RefundStatus.COMPLETED and refund.amount == Decimal("1062.00")
    assert item_a.refunded_quantity == 1 and order.refunded_total == Decimal("1062.00")
    assert order.status == OrderStatus.PARTIALLY_REFUNDED
    assert await db.scalar(select(Product.stock).where(Product.id == a.id)) == 11
    assert await db.scalar(select(InventoryMovement.reason).where(InventoryMovement.product_id == a.id)) == "refund_restock"
    tx = (await db.execute(select(Transaction).where(Transaction.category == TxCategory.REFUND))).scalar_one()
    assert tx.amount == Decimal("1062.00") and tx.direction == TxDirection.OUTGOING and refund.transaction_id == tx.id
    assert "Refund processed" in await titles(db, cust.id)
    # refund the rest without restock -> fully refunded, totals add up exactly
    r = await post_form(oc, f"/owner/shop/orders/{order.id}/refund",
                        {f"qty_{items[a.id].id}": 1, f"qty_{items[b.id].id}": 1, "reason": "Goodwill refund for the rest"})
    order = await fresh(db, ShopOrder, order.id)
    assert order.status == OrderStatus.REFUNDED and order.refunded_total == order.total
    assert await db.scalar(select(func.sum(Transaction.amount)).where(Transaction.category == TxCategory.REFUND)) == order.total
    assert await db.scalar(select(Product.stock).where(Product.id == b.id)) == 10
    r = await post_form(oc, f"/owner/shop/orders/{order.id}/refund", {f"qty_{items[b.id].id}": 1, "reason": "Trying to refund twice"})
    assert await db.scalar(select(func.count(Refund.id))) == 2  # nothing left to refund


async def test_reject_refund_requires_a_note(client, db, anon_client_factory):
    owner, gym, branch = await shop_setup(db)
    p = await make_product(db, gym)
    cust = await make_customer(db)
    order = await make_order(db, cust, gym, [(p, 1)], status=OrderStatus.COMPLETED, completed_days_ago=1, branch=branch)
    item = await db.scalar(select(ShopOrderItem.id).where(ShopOrderItem.order_id == order.id))
    await login_as(client, cust)
    await post_form(client, f"/shop/orders/{order.id}/refund-request", {f"qty_{item}": 1, "reason": "Changed my mind about it"})
    refund_id = await db.scalar(select(Refund.id))
    oc = anon_client_factory("127.0.0.1")
    await login_as(oc, owner)
    await post_form(oc, f"/owner/shop/refunds/{refund_id}/reject", {"note": "No"})
    assert (await fresh(db, Refund, refund_id)).status == RefundStatus.REQUESTED
    await post_form(oc, f"/owner/shop/refunds/{refund_id}/reject", {"note": "Opened supplements can't be returned for hygiene reasons."})
    refund = await fresh(db, Refund, refund_id)
    assert refund.status == RefundStatus.REJECTED and refund.decided_by_id == owner.id
    assert (await fresh(db, ShopOrder, order.id)).status == OrderStatus.COMPLETED
    assert "Refund request declined" in await titles(db, cust.id)
    other_owner, _, _ = await shop_setup(db)
    await login_as(oc, other_owner)
    r = await post_form(oc, f"/owner/shop/refunds/{refund_id}/approve", {"full": "1"})
    assert r.status_code == 404


# ====================================================================================================== Cashfree
@pytest.fixture
def cashfree_keys(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "cashfree_app_id", "TEST_APP_ID")
    monkeypatch.setattr(s, "cashfree_secret_key", "TEST_SECRET")
    yield s


async def test_cashfree_refund_request_shape_and_failure(client, db, anon_client_factory, cashfree_keys):
    owner, gym, branch = await shop_setup(db)
    p = await make_product(db, gym, stock=5)
    cust = await make_customer(db)
    order = await make_order(db, cust, gym, [(p, 2)], status=OrderStatus.PAID, provider="cashfree", branch=branch)
    item = await db.scalar(select(ShopOrderItem.id).where(ShopOrderItem.order_id == order.id))
    seen: list[httpx.Request] = []

    def ok(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"cf_refund_id": "cfr_777", "refund_status": "PENDING"})

    oc = anon_client_factory("127.0.0.1")
    await login_as(oc, owner)
    pay.use_refund_transport(httpx.MockTransport(ok))
    try:
        await post_form(oc, f"/owner/shop/orders/{order.id}/refund", {f"qty_{item}": 1, "reason": "Customer returned one tub", "restock": "1"})
    finally:
        pay.use_refund_transport(None)
    req = seen[0]
    assert str(req.url) == f"{cashfree_keys.cashfree_pg_base}/orders/{order.order_number}/refunds"
    assert req.headers["x-client-id"] == "TEST_APP_ID" and req.headers["x-client-secret"] == "TEST_SECRET"
    assert req.headers["x-api-version"] == cashfree_keys.cashfree_api_version
    body = json.loads(req.content)
    assert body["refund_amount"] == 1180.0 and body["refund_id"].startswith(f"RF-{order.order_number}-") and body["refund_note"]
    refund = (await db.execute(select(Refund).execution_options(populate_existing=True))).scalar_one()
    assert refund.status == RefundStatus.COMPLETED and refund.provider_refund_id == "cfr_777"

    def fail(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "refund amount exceeds balance"})

    pay.use_refund_transport(httpx.MockTransport(fail))
    try:
        await post_form(oc, f"/owner/shop/orders/{order.id}/refund", {f"qty_{item}": 1, "reason": "Second tub returned too"})
    finally:
        pay.use_refund_transport(None)
    failed = (await db.execute(select(Refund).where(Refund.status == RefundStatus.FAILED))).scalar_one()
    assert "exceeds balance" in failed.decision_note
    assert (await fresh(db, ShopOrder, order.id)).refunded_total == Decimal("1180.00")
    assert "Refund failed" in await titles(db, owner.id)


class FakeCashfree:
    """Stand-in for WP-D's app.services.cashfree (create_order / get_order)."""

    def __init__(self) -> None:
        self.created: list[dict] = []
        self.status = "ACTIVE"
        self.amount = None

    async def create_order(self, **kw):
        self.created.append(kw)
        return SimpleNamespace(payment_session_id="session_abc123", order_id=kw["order_id"], cf_order_id="cf_1")

    async def get_order(self, order_id):
        return SimpleNamespace(order_id=order_id, order_status=self.status, amount=self.amount, currency="INR",
                               cf_order_id="cf_1", is_paid=self.status == "PAID",
                               is_dead=self.status in ("EXPIRED", "TERMINATED"))


async def test_cashfree_checkout_uses_adapter_and_verifies_return(client, db, cashfree_keys, monkeypatch):
    fake = FakeCashfree()
    monkeypatch.setattr(pay, "_cashfree", lambda: fake)
    _, gym, branch = await shop_setup(db)
    p = await make_product(db, gym, stock=4)
    cust = await make_customer(db)
    await bag(db, cust, p, 1)
    await login_as(client, cust)
    r = await checkout(client, gym, branch)
    oid = order_id_from(r.headers["location"])
    assert r.headers["location"] == f"/shop/orders/{oid}/pay"
    order = await fresh(db, ShopOrder, oid)
    sent = fake.created[0]
    assert sent["order_id"] == order.order_number and sent["amount"] == order.total
    assert sent["return_url"].endswith(f"/shop/checkout/return?order_id={order.order_number}")
    assert sent["notify_url"].endswith("/payments/cashfree/webhook")
    assert order.payment_provider == "cashfree" and order.payment_session_id == "session_abc123"
    r = await client.get(f"/shop/orders/{oid}/pay")
    assert r.status_code == 200 and 'src="https://sdk.cashfree.com/js/v3/cashfree.js"' in r.text and "nonce=" in r.text
    assert "https://sdk.cashfree.com" in r.headers["content-security-policy"]
    assert (await client.get(f"/shop/orders/{oid}/test-gateway")).status_code == 404  # no simulation with real keys
    r = await client.get(f"/shop/checkout/return?order_id={order.order_number}")  # still pending at the gateway
    assert (await fresh(db, ShopOrder, oid)).status == OrderStatus.PENDING_PAYMENT
    fake.status, fake.amount = "PAID", order.total  # the redirect is never trusted: verified with get_order
    r = await client.get(f"/shop/checkout/return?order_id={order.order_number}")
    assert r.status_code == 303
    assert (await fresh(db, ShopOrder, oid)).status == OrderStatus.PAID
    assert await db.scalar(select(Product.stock).where(Product.id == p.id)) == 3


async def test_payments_unavailable_when_cashfree_client_is_missing(client, db, cashfree_keys, monkeypatch):
    monkeypatch.setattr(pay, "_cashfree", lambda: None)
    assert pay.payments_mode() == "unavailable"
    _, gym, branch = await shop_setup(db)
    p = await make_product(db, gym)
    cust = await make_customer(db)
    await bag(db, cust, p, 1)
    await login_as(client, cust)
    r = await checkout(client, gym, branch)
    oid = order_id_from(r.headers["location"])
    assert r.headers["location"] == f"/shop/orders/{oid}"
    order = await fresh(db, ShopOrder, oid)
    assert order.status == OrderStatus.PENDING_PAYMENT and order.payment_provider is None


# ====================================================================================================== receipts & housekeeping
async def test_receipt_is_printable_and_private(client, db, anon_client_factory):
    owner, gym, _, product, cust, oid = await paid_order(client, db)
    order = await fresh(db, ShopOrder, oid)
    r = await client.get(f"/shop/orders/{oid}/receipt")
    assert r.status_code == 200 and order.order_number in r.text and "GST / tax @ 18%" in r.text
    assert 'class="shop-receipt' in r.text and 'data-component="shop-print"' in r.text and gym.name in r.text
    oc = anon_client_factory("127.0.0.1")
    await login_as(oc, owner)
    assert (await oc.get(f"/shop/orders/{oid}/receipt")).status_code == 200
    await login_as(oc, await make_customer(db))
    assert (await oc.get(f"/shop/orders/{oid}/receipt")).status_code == 404


async def test_stale_unpaid_orders_expire(db):
    _, gym, branch = await shop_setup(db)
    p = await make_product(db, gym)
    cust = await make_customer(db)
    stale = await make_order(db, cust, gym, [(p, 1)], status=OrderStatus.PENDING_PAYMENT)
    fresh_order = await make_order(db, cust, gym, [(p, 1)], status=OrderStatus.PENDING_PAYMENT)
    await db.execute(update(ShopOrder).where(ShopOrder.id == stale.id).values(created_at=utcnow() - timedelta(days=2)))
    await db.commit()
    assert await pay.expire_unpaid_orders(db) == 1
    await db.commit()
    assert (await fresh(db, ShopOrder, stale.id)).status == OrderStatus.CANCELLED
    assert (await fresh(db, ShopOrder, fresh_order.id)).status == OrderStatus.PENDING_PAYMENT


# ====================================================================================================== stock holds (F20)
def flash_of(r) -> str:
    """The flash message(s) a redirect set (decoded from the signed cookie)."""
    from itsdangerous import URLSafeSerializer

    from app.core.templating import FLASH_COOKIE

    raw = r.cookies.get(FLASH_COOKIE)
    if not raw:
        return ""
    return " | ".join(d["m"] for d in URLSafeSerializer(get_settings().secret_key, salt="ironpulse-flash").loads(raw))


async def two_buyers(db, anon_client_factory, *, stock: int = 1):
    _, gym, branch = await shop_setup(db)
    p = await make_product(db, gym, name="Last Kettlebell", stock=stock)
    a, b = await make_customer(db), await make_customer(db)
    clients = []
    for buyer in (a, b):
        await bag(db, buyer, p, 1)
        c = anon_client_factory("127.0.0.1")
        await login_as(c, buyer)
        clients.append(c)
    return gym, branch, p, clients


async def test_checkout_holds_the_units_so_a_second_buyer_is_told_before_paying(client, db, anon_client_factory):
    """F20: checkout held nothing - two buyers of the last unit were both sent to pay, and the slower one was charged and
    then refunded. Now the first checkout holds the unit and the second buyer is told at checkout."""
    gym, branch, p, (ca, cb) = await two_buyers(db, anon_client_factory)
    ra = await checkout(ca, gym, branch)
    assert ra.status_code == 303 and ra.headers["location"].endswith("/test-gateway")
    order = await fresh(db, ShopOrder, order_id_from(ra.headers["location"]))
    assert order.stock_held_until is not None and order.stock_held_until > utcnow()
    rb = await checkout(cb, gym, branch)
    assert rb.status_code == 400 and "being bought by other members right now" in rb.text
    assert await db.scalar(select(func.count(ShopOrder.id))) == 1  # the second buyer has nothing to pay
    assert await db.scalar(select(Product.stock).where(Product.id == p.id)) == 1  # held, not sold: stock unchanged
    await post_form(ca, f"/shop/orders/{order.id}/test-gateway", {"decision": "approve"})
    order = await fresh(db, ShopOrder, order.id)
    assert order.status == OrderStatus.PAID and order.stock_held_until is None  # the hold became the sale
    assert await db.scalar(select(Product.stock).where(Product.id == p.id)) == 0
    assert await db.scalar(select(func.count(Refund.id))) == 0


async def test_a_lapsed_hold_frees_the_unit_and_a_late_payment_never_takes_another_buyers_hold(client, db,
                                                                                            anon_client_factory):
    gym, branch, p, (ca, cb) = await two_buyers(db, anon_client_factory)
    oa = order_id_from((await checkout(ca, gym, branch)).headers["location"])
    await db.execute(update(ShopOrder).where(ShopOrder.id == oa).values(stock_held_until=utcnow() - timedelta(seconds=1)))
    await db.commit()  # A's STOCK_HOLD_MINUTES are over without a payment
    rb = await checkout(cb, gym, branch)
    assert rb.status_code == 303  # B can buy the unit now
    ob = order_id_from(rb.headers["location"])
    # A's payment arrives late anyway: the unit B holds is never sold twice - A is refunded in full, automatically
    r = await post_form(ca, f"/shop/orders/{oa}/test-gateway", {"decision": "approve"})
    assert "sold out" in flash_of(r)
    late = await fresh(db, ShopOrder, oa)
    assert late.status == OrderStatus.CANCELLED and late.refunded_total == late.total and late.stock_held_until is None
    await post_form(cb, f"/shop/orders/{ob}/test-gateway", {"decision": "approve"})
    assert (await fresh(db, ShopOrder, ob)).status == OrderStatus.PAID
    assert await db.scalar(select(Product.stock).where(Product.id == p.id)) == 0


async def test_paying_again_takes_the_units_again_or_cancels_before_any_money_moves(client, db, anon_client_factory):
    gym, branch, p, (ca, cb) = await two_buyers(db, anon_client_factory)
    oa = order_id_from((await checkout(ca, gym, branch)).headers["location"])
    await post_form(ca, f"/shop/orders/{oa}/test-gateway", {"decision": "decline"})
    declined = await fresh(db, ShopOrder, oa)
    assert declined.status == OrderStatus.PAYMENT_FAILED and declined.stock_held_until is None  # released
    ob = order_id_from((await checkout(cb, gym, branch)).headers["location"])  # B takes the free unit
    r = await post_form(ca, f"/shop/orders/{oa}/pay", {})  # A tries again: the unit is gone - told BEFORE paying
    assert r.status_code == 303 and r.headers["location"] == f"/shop/orders/{oa}"
    assert "sold out while this order was waiting for payment" in flash_of(r) and "Nothing was charged" in flash_of(r)
    assert (await fresh(db, ShopOrder, oa)).status == OrderStatus.CANCELLED
    assert await db.scalar(select(func.count(Transaction.id))) == 0 and await db.scalar(select(func.count(Refund.id))) == 0
    # B declines too and pays again while the unit is free: the hold is taken again and the payment goes through
    await post_form(cb, f"/shop/orders/{ob}/test-gateway", {"decision": "decline"})
    r = await post_form(cb, f"/shop/orders/{ob}/pay", {})
    assert r.headers["location"] == f"/shop/orders/{ob}/test-gateway"
    assert (await fresh(db, ShopOrder, ob)).stock_held_until > utcnow()
    await post_form(cb, f"/shop/orders/{ob}/test-gateway", {"decision": "approve"})
    assert (await fresh(db, ShopOrder, ob)).status == OrderStatus.PAID


# ====================================================================================================== price changes (F23)
def quote_of(html: str) -> str:
    m = re.search(r'name="quote" value="([0-9a-f]{32})"', html)
    assert m, "the checkout form carries the quote it showed"
    return m.group(1)


async def test_checkout_never_places_an_order_at_a_total_the_buyer_did_not_see(client, db):
    """F23: the owner raised a price (or the bag changed in another tab) after the checkout page was shown, and "Place
    order" went straight to the payment page at the new total. Now the page is shown again with the new total."""
    _, gym, branch = await shop_setup(db)
    p = await make_product(db, gym, price=Decimal("100"), tax_rate=Decimal("0"))
    cust = await make_customer(db)
    await bag(db, cust, p, 2)
    await login_as(client, cust)
    shown = quote_of((await client.get(f"/shop/checkout?gym={gym.id}")).text)
    await db.execute(update(Product).where(Product.id == p.id).values(price=Decimal("900")))
    await db.commit()
    r = await checkout(client, gym, branch, quote=shown)
    assert r.status_code == 409 and "changed since this page was opened" in r.text and "₹1,800.00" in r.text
    assert await db.scalar(select(func.count(ShopOrder.id))) == 0
    r = await checkout(client, gym, branch, quote=quote_of(r.text))  # the buyer saw the new total and confirms it
    assert r.status_code == 303
    assert (await fresh(db, ShopOrder, order_id_from(r.headers["location"]))).total == Decimal("1800.00")
    # the bag changed in another tab after the page was shown (the unpaid order above keeps its bag lines)
    shown = quote_of((await client.get(f"/shop/checkout?gym={gym.id}")).text)
    await db.execute(update(CartItem).where(CartItem.user_id == cust.id).values(quantity=5))
    await db.commit()
    r = await checkout(client, gym, branch, quote=shown)
    assert r.status_code == 409 and "₹4,500.00" in r.text
    r = await checkout(client, gym, branch, quote="ünïcode-garbage")  # anything else: the same answer, never a 500
    assert r.status_code == 409


# ====================================================================================================== repeated cancel (F26)
async def test_cancelling_an_already_cancelled_order_says_so(client, db, anon_client_factory):
    """F26: a second cancel of a cancelled, refunded order (a second tab, a resubmit) said "already being prepared -
    contact the store". It now says what happened, and changes nothing."""
    owner, gym, branch, product, cust, oid = await paid_order(client, db, qty=1, price="349")
    tab2 = anon_client_factory("127.0.0.1")
    await login_as(tab2, cust)
    r = await post_form(client, f"/shop/orders/{oid}/cancel", {})
    assert flash_of(r) == "Order cancelled - your refund is on its way."
    r = await post_form(tab2, f"/shop/orders/{oid}/cancel", {})
    message = flash_of(r)
    assert "already cancelled" in message and "refunded" in message and "being prepared" not in message
    assert await db.scalar(select(func.count(Refund.id))) == 1
    oc = anon_client_factory("127.0.0.1")
    await login_as(oc, owner)
    r = await post_form(oc, f"/owner/shop/orders/{oid}/cancel", {"reason": "Customer asked"})
    assert "already cancelled" in flash_of(r) and "shipped or completed" not in flash_of(r)
    # an unpaid order cancelled twice
    await bag(db, cust, product, 1)
    unpaid = order_id_from((await checkout(client, gym, branch)).headers["location"])
    await post_form(client, f"/shop/orders/{unpaid}/cancel", {})
    r = await post_form(tab2, f"/shop/orders/{unpaid}/cancel", {})
    assert flash_of(r) == "This order is already cancelled. Nothing was charged."
