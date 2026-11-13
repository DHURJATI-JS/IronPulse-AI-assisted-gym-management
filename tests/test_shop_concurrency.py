"""The gym shop when many people buy at the same moment (the owner: "stress test the shop for purchasing items at the
same time by multiple users"). Every scenario fires real HTTP requests in parallel - one client per member - through
the checkout, the TEST MODE gateway and the bag:

* the last units: more buyers than stock pay at once -> exactly the stock is sold, stock never goes below zero, every
  other buyer is refunded automatically and told, the ledger and the inventory log add up;
* a coupon with a use limit redeemed by many buyers at once -> never more uses than allowed (unpaid orders hold a use);
* the same member double-submitting checkout -> one unpaid order per store, never two;
* parallel adds to the bag / wishlist at their limits -> the limits hold.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select

from app.models import (
    CartItem,
    Coupon,
    InventoryMovement,
    Notification,
    Product,
    ProductReview,
    Refund,
    ShopOrder,
    Transaction,
    WishlistItem,
)
from app.models.enums import TxCategory
from app.models.shop import OrderStatus
from app.services import shop as svc
from tests.conftest import login_as, make_client, post_form
from tests.factories import make_customer
from tests.test_shop import make_order, make_product, shop_setup
from tests.test_shop_payments import bag, checkout, order_id_from


async def scalar(db: Any, stmt: Any) -> Any:
    return await db.scalar(stmt.execution_options(populate_existing=True))


async def members(db: Any, n: int) -> list[Any]:
    return [await make_customer(db) for _ in range(n)]


async def signed_in(users: list[Any]) -> list[Any]:
    clients = [make_client() for _ in users]
    for c, u in zip(clients, users):
        await login_as(c, u)
    return clients


async def close(clients: list[Any]) -> None:
    for c in clients:
        await c.aclose()


async def test_more_buyers_than_stock_never_oversell_and_nobody_is_charged_then_refunded(db):
    """F20: 8 buyers race for the last 3 units. Checkout now holds the units, so exactly 3 reach the payment page and
    the other 5 are told at checkout - before any money moves - instead of being charged, refunded 5-7 days later and
    the owner e-mailed once per lost race. Stock never goes below zero; the ledger and the inventory log add up."""
    owner, gym, branch = await shop_setup(db)
    product = await make_product(db, gym, name="Last Tubs", price=Decimal("1500"), stock=3, low_stock_threshold=1)
    buyers = await members(db, 8)
    for b in buyers:
        await bag(db, b, product, 1)
    clients = await signed_in(buyers)
    try:
        placed = await asyncio.gather(*(checkout(c, gym, branch) for c in clients))
        winners = [(c, order_id_from(r.headers["location"])) for c, r in zip(clients, placed) if r.status_code == 303]
        losers = [r for r in placed if r.status_code != 303]
        assert len(winners) == 3 and len(losers) == 5, [r.status_code for r in placed]
        assert all(r.status_code == 400 and "being bought by other members right now" in r.text for r in losers)
        paid = await asyncio.gather(*(post_form(c, f"/shop/orders/{oid}/test-gateway", {"decision": "approve"})
                                      for c, oid in winners))
        assert all(r.status_code == 303 for r in paid)
    finally:
        await close(clients)
    statuses = (await db.execute(select(ShopOrder.status).execution_options(populate_existing=True))).scalars().all()
    assert sorted(s.value for s in statuses) == ["paid"] * 3  # the losers never got an order to pay
    assert await scalar(db, select(Product.stock).where(Product.id == product.id)) == 0  # never below zero
    assert await scalar(db, select(func.sum(InventoryMovement.change)).where(
        InventoryMovement.product_id == product.id, InventoryMovement.reason == "sale")) == -3
    assert await scalar(db, select(func.count(Refund.id))) == 0  # nobody was charged and then refunded
    sales = await scalar(db, select(func.count(Transaction.id)).where(Transaction.category == TxCategory.SHOP_SALE))
    assert sales == 3
    noise = await scalar(db, select(func.count(Notification.id)).where(
        Notification.title.in_(("Order cancelled - refund issued", "Shop order auto-refunded"))))
    assert noise == 0  # no refund notices, no owner e-mail per lost race


async def test_parallel_reviews_by_one_purchaser_never_fail(db):
    """F22: one purchaser's review posted from several tabs (or a retry) at the same moment - each request found no
    review and inserted one, and the losers hit the unique key: HTTP 500. Now they are taken one after the other: one
    review, every request answered normally."""
    owner, gym, branch = await shop_setup(db)
    product = await make_product(db, gym)
    (buyer,) = await members(db, 1)
    await make_order(db, buyer, gym, [(product, 1)], status=OrderStatus.COMPLETED, completed_days_ago=1)
    clients = await signed_in([buyer] * 10)
    try:
        rs = await asyncio.gather(*(post_form(c, f"/shop/p/{product.id}/review",
                                              {"rating": 5 - i % 3, "title": f"Tab {i}", "body": "Solid kettlebell."})
                                    for i, c in enumerate(clients)))
    finally:
        await close(clients)
    assert [r.status_code for r in rs] == [303] * 10
    assert await scalar(db, select(func.count(ProductReview.id)).where(ProductReview.product_id == product.id)) == 1
    assert await scalar(db, select(Product.review_count).where(Product.id == product.id)) == 1


async def test_a_limited_coupon_redeemed_by_many_at_once_is_never_overused(db):
    owner, gym, branch = await shop_setup(db)
    product = await make_product(db, gym, price=Decimal("2000"), stock=50)
    db.add(Coupon(gym_id=gym.id, code="FIRST2", percent_off=20, max_uses=2, is_active=True))
    await db.commit()
    buyers = await members(db, 6)
    for b in buyers:
        await bag(db, b, product, 1)
    clients = await signed_in(buyers)
    try:
        placed = await asyncio.gather(*(checkout(c, gym, branch, coupon="FIRST2") for c in clients))
        winners = [(c, order_id_from(r.headers["location"])) for c, r in zip(clients, placed) if r.status_code == 303]
        losers = [r for r in placed if r.status_code != 303]
        assert len(winners) == 2 and len(losers) == 4
        assert all(r.status_code == 400 and "FIRST2 has been fully used" in r.text for r in losers)
        await asyncio.gather(*(post_form(c, f"/shop/orders/{oid}/test-gateway", {"decision": "approve"})
                               for c, oid in winners))
    finally:
        await close(clients)
    assert await scalar(db, select(Coupon.used_count).where(Coupon.code == "FIRST2")) == 2
    assert await scalar(db, select(func.count(ShopOrder.id)).where(ShopOrder.coupon_code == "FIRST2")) == 2


async def test_a_double_submitted_checkout_leaves_one_unpaid_order(db):
    owner, gym, branch = await shop_setup(db)
    product = await make_product(db, gym, stock=10)
    (buyer,) = await members(db, 1)
    await bag(db, buyer, product, 2)
    clients = await signed_in([buyer, buyer, buyer])
    try:
        results = await asyncio.gather(*(checkout(c, gym, branch) for c in clients))
    finally:
        await close(clients)
    assert all(r.status_code == 303 for r in results)
    open_orders = await scalar(db, select(func.count(ShopOrder.id)).where(
        ShopOrder.customer_id == buyer.id, ShopOrder.status.in_(svc.UNPAID_STATUSES)))
    assert open_orders == 1  # the later submits superseded the earlier ones, one after the other


async def test_parallel_adds_to_a_nearly_full_bag_never_pass_its_limit(db, monkeypatch):
    monkeypatch.setattr(svc, "MAX_CART_LINES", 3)
    owner, gym, branch = await shop_setup(db)
    products = [await make_product(db, gym) for _ in range(6)]
    (buyer,) = await members(db, 1)
    for p in products[:2]:
        await bag(db, buyer, p, 1)
    clients = await signed_in([buyer] * 4)
    try:
        await asyncio.gather(*(post_form(c, "/shop/cart/add", {"product_id": p.id, "quantity": 1})
                               for c, p in zip(clients, products[2:])))
    finally:
        await close(clients)
    assert await scalar(db, select(func.count(CartItem.id)).where(CartItem.user_id == buyer.id)) == 3


async def test_parallel_wishlist_saves_at_the_limit(db, monkeypatch):
    monkeypatch.setattr(svc, "WISHLIST_MAX", 2)
    owner, gym, branch = await shop_setup(db)
    products = [await make_product(db, gym) for _ in range(5)]
    (buyer,) = await members(db, 1)
    clients = await signed_in([buyer] * 5)
    try:
        await asyncio.gather(*(post_form(c, f"/shop/wishlist/{p.id}/toggle", {}) for c, p in zip(clients, products)))
    finally:
        await close(clients)
    assert await scalar(db, select(func.count(WishlistItem.id)).where(WishlistItem.user_id == buyer.id)) == 2
