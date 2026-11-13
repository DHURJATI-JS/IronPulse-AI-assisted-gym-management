"""WP-L gym shop - owner side: product CRUD (+ images, stock, embeddings), ownership (404 for foreign ids),
coupons, review moderation, dashboard aggregates, order fulfilment, low-stock digest, sample catalogue."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select

from app.db import QueryCounter
from app.models import (
    AuditLog,
    Coupon,
    InventoryMovement,
    Notification,
    Product,
    ProductImage,
    ProductReview,
    ShopOrder,
)
from app.models.shop import OrderStatus, RefundStatus
from app.services import shop as svc
from app.services import storage
from app.services.jobs import registered_periodic
from tests.conftest import login_as, post_form
from tests.factories import make_customer, png_bytes
from tests.test_shop import _reset_shop_ai, add_image, make_order, make_product, shop_setup  # noqa: F401

PRODUCT_FORM = {
    "name": "Gold Whey Protein 1 kg", "brand": "PulseFuel", "category": "supplements",
    "description": "Fast-absorbing whey isolate with 25 g protein per scoop.", "price": "2999", "compare_at_price": "3499",
    "tax_rate": "18", "sku": "WHEY-1KG", "stock": "12", "low_stock_threshold": "4", "fulfillment": "both",
    "is_active": "1", "is_supplement": "1", "serving_size": "1 scoop (30 g)", "protein_g": "25", "calories": "120",
    "ingredients": "Whey protein isolate (milk), cocoa, soy lecithin.",
}


async def form_with_lists(client, url, data, lists=None, files=None):
    """post_form with repeated keys (checkbox groups) and optional files."""
    from tests.conftest import csrf_token

    payload: dict = {k: str(v) for k, v in data.items()}
    payload.update({k: list(values) for k, values in (lists or {}).items()})  # repeated keys
    payload["csrf_token"] = await csrf_token(client)
    return await client.post(url, data=payload, files=files)


async def titles(db, user_id):
    return list((await db.execute(select(Notification.title).where(Notification.user_id == user_id))).scalars().all())


# ====================================================================================================== products
async def test_owner_creates_product_with_images_safety_and_embedding(client, db):
    owner, gym, _ = await shop_setup(db)
    await login_as(client, owner)
    r = await client.get(f"/owner/gyms/{gym.id}/shop/products/new")
    assert r.status_code == 200 and "Contains allergens" in r.text
    files = [("images", ("front.png", png_bytes((120, 120), "white"), "image/png")),
             ("images", ("label.png", png_bytes((120, 120), "blue"), "image/png"))]
    r = await form_with_lists(client, f"/owner/gyms/{gym.id}/shop/products/new", PRODUCT_FORM,
                              {"allergens": ["milk", "soy", "bogus"], "caution_tags": ["kidney", "not-a-tag"]}, files)
    assert r.status_code == 303, r.text[:1500]
    product = (await db.execute(select(Product))).scalar_one()
    assert (product.price, product.compare_at_price, product.tax_rate, product.currency) == (
        Decimal("2999.00"), Decimal("3499.00"), Decimal("18.00"), "INR")
    assert product.allergens == ["milk", "soy"] and product.caution_tags == ["kidney"]
    assert product.nutrition_facts == {"serving_size": "1 scoop (30 g)", "calories": 120, "protein_g": 25}
    assert product.slug == "gold-whey-protein-1-kg" and product.stock == 12 and product.is_supplement
    assert await db.scalar(select(func.count(Product.id)).where(Product.embedding.is_not(None))) == 1
    images = (await db.execute(select(ProductImage).order_by(ProductImage.sort_order))).scalars().all()
    assert len(images) == 2 and all(storage.path_for(i.image_key).is_file() for i in images)
    mv = (await db.execute(select(InventoryMovement))).scalar_one()
    assert (mv.change, mv.reason, mv.actor_id) == (12, "restock", owner.id)
    assert await db.scalar(select(AuditLog.action).where(AuditLog.entity_type == "product")) == "shop.product.create"
    r = await client.get("/shop?q=protein+isolate")  # semantic + ILIKE search sees the new product
    assert "Gold Whey Protein 1 kg" in r.text
    r = await form_with_lists(client, f"/owner/gyms/{gym.id}/shop/products/new", PRODUCT_FORM)
    assert await db.scalar(select(Product.slug).where(Product.id != product.id)) == "gold-whey-protein-1-kg-2"


async def test_product_form_validation(client, db):
    owner, gym, _ = await shop_setup(db)
    await login_as(client, owner)
    bad = {**PRODUCT_FORM, "price": "-5", "compare_at_price": "10", "tax_rate": "60", "description": "short",
           "protein_g": "lots", "sku": "bad sku!"}
    r = await form_with_lists(client, f"/owner/gyms/{gym.id}/shop/products/new", bad)
    assert r.status_code == 400
    for text in ("Price: Must be between", "Tax %: Must be between", "at least 10 characters", "Protein (g)", "SKU:"):
        assert text in r.text, text
    r = await form_with_lists(client, f"/owner/gyms/{gym.id}/shop/products/new", {**PRODUCT_FORM, "price": "4000"})
    assert r.status_code == 400 and "MRP (compare-at price) must be at least the selling price" in r.text
    assert await db.scalar(select(func.count(Product.id))) == 0


async def test_ownership_is_enforced(client, db, anon_client_factory):
    owner, gym, _ = await shop_setup(db)
    intruder, other_gym, _ = await shop_setup(db)
    p = await make_product(db, gym, name="Owner One Whey")
    await login_as(client, intruder)
    assert (await client.get(f"/owner/shop/products/{p.id}")).status_code == 404
    assert (await post_form(client, f"/owner/shop/products/{p.id}", PRODUCT_FORM)).status_code == 404
    assert (await post_form(client, f"/owner/shop/products/{p.id}/stock", {"mode": "set", "quantity": 0, "reason": "steal"})).status_code == 404
    assert (await post_form(client, f"/owner/shop/products/{p.id}/delete", {})).status_code == 404
    assert (await client.get(f"/owner/gyms/{gym.id}/shop/products/new")).status_code == 404
    r = await client.get("/owner/shop/products")
    assert "Owner One Whey" not in r.text
    cust = await make_customer(db)
    c2 = anon_client_factory("127.0.0.1")
    await login_as(c2, cust)
    assert (await c2.get("/owner/shop")).status_code == 403
    assert await db.scalar(select(Product.stock).where(Product.id == p.id)) == 10


async def test_edit_product_audits_price_changes(client, db):
    owner, gym, _ = await shop_setup(db)
    p = await make_product(db, gym, name="Strength Powder 250 g", price=Decimal("899"))
    await login_as(client, owner)
    r = await client.get(f"/owner/shop/products/{p.id}")
    assert r.status_code == 200 and "Automatically flagged" not in r.text
    form = {**PRODUCT_FORM, "name": "Creatine Monohydrate 250 g", "price": "799", "compare_at_price": "999",
            "description": "Micronised creatine monohydrate for strength."}
    r = await form_with_lists(client, f"/owner/shop/products/{p.id}", form)
    assert r.status_code == 303
    p = await db.get(Product, p.id, populate_existing=True)
    assert p.price == Decimal("799.00") and p.slug == "creatine-monohydrate-250-g" and p.stock == 10  # stock not editable here
    detail = await db.scalar(select(AuditLog.detail).where(AuditLog.action == "shop.product.price"))
    assert detail["price"] == {"from": "899.00", "to": "799.00"}
    r = await client.get(f"/owner/shop/products/{p.id}")
    assert "Automatically flagged" in r.text and "contains creatine" in r.text


async def test_image_limit_remove_and_reorder(client, db):
    owner, gym, _ = await shop_setup(db)
    p = await make_product(db, gym)
    imgs = [await add_image(db, p, i) for i in range(7)]
    await login_as(client, owner)
    files = [("images", (f"{i}.png", png_bytes((40, 40), "red"), "image/png")) for i in range(3)]
    r = await form_with_lists(client, f"/owner/shop/products/{p.id}/images", {}, files=files)
    assert r.status_code == 303
    assert await db.scalar(select(func.count(ProductImage.id))) == svc.MAX_IMAGES
    r = await post_form(client, f"/owner/shop/products/{p.id}/images/{imgs[0].id}/delete", {})
    assert r.status_code == 303 and await db.scalar(select(func.count(ProductImage.id))) == 7
    last = await db.scalar(select(ProductImage.id).order_by(ProductImage.sort_order.desc(), ProductImage.id.desc()).limit(1))
    await post_form(client, f"/owner/shop/products/{p.id}/images/{last}/move", {"direction": "first"})
    order = (await db.execute(select(ProductImage.id).order_by(ProductImage.sort_order))).scalars().all()
    assert order[0] == last and len(order) == 7
    other, _, _ = await shop_setup(db)
    await login_as(client, other)
    assert (await post_form(client, f"/owner/shop/products/{p.id}/images/{last}/delete", {})).status_code == 404


async def test_stock_adjustments_record_movements(client, db):
    owner, gym, _ = await shop_setup(db)
    p = await make_product(db, gym, stock=10)
    await login_as(client, owner)
    await post_form(client, f"/owner/shop/products/{p.id}/stock", {"mode": "restock", "quantity": 10, "reason": "New delivery"})
    await post_form(client, f"/owner/shop/products/{p.id}/stock", {"mode": "remove", "quantity": 3, "reason": "Damaged tubs"})
    await post_form(client, f"/owner/shop/products/{p.id}/stock", {"mode": "set", "quantity": 5, "reason": "Stock count"})
    await post_form(client, f"/owner/shop/products/{p.id}/stock", {"mode": "remove", "quantity": 9, "reason": "Too many"})
    await post_form(client, f"/owner/shop/products/{p.id}/stock", {"mode": "restock", "quantity": 4, "reason": ""})
    assert await db.scalar(select(Product.stock).where(Product.id == p.id)) == 5
    moves = (await db.execute(select(InventoryMovement.change, InventoryMovement.reason, InventoryMovement.note)
                              .order_by(InventoryMovement.id))).all()
    assert moves == [(10, "restock", "New delivery"), (-3, "adjustment", "Damaged tubs"), (-12, "adjustment", "Stock count")]
    assert await db.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == "shop.product.stock")) == 3


async def test_products_in_orders_are_hidden_not_deleted(client, db):
    owner, gym, _ = await shop_setup(db)
    sold = await make_product(db, gym, name="Sold Product")
    fresh_product = await make_product(db, gym, name="Never Sold")
    cust = await make_customer(db)
    await make_order(db, cust, gym, [(sold, 1)])
    await login_as(client, owner)
    await post_form(client, f"/owner/shop/products/{sold.id}/delete", {})
    assert await db.scalar(select(Product.is_active).where(Product.id == sold.id)) is False
    await post_form(client, f"/owner/shop/products/{fresh_product.id}/delete", {})
    assert await db.scalar(select(func.count(Product.id)).where(Product.id == fresh_product.id)) == 0
    await post_form(client, f"/owner/shop/products/{sold.id}/status", {"action": "activate"})
    assert await db.scalar(select(Product.is_active).where(Product.id == sold.id)) is True


# ====================================================================================================== coupons & reviews
async def test_coupons_create_validate_and_toggle(client, db):
    owner, gym, _ = await shop_setup(db)
    _, other_gym, _ = await shop_setup(db)
    await login_as(client, owner)
    ok = {"gym_id": gym.id, "code": "strong-10", "kind": "percent", "percent_off": "10", "min_order_total": "999",
          "max_uses": "50", "is_active": "1"}
    r = await post_form(client, "/owner/shop/coupons", ok)
    assert r.status_code == 303
    c = (await db.execute(select(Coupon))).scalar_one()
    assert (c.code, c.percent_off, c.amount_off, c.min_order_total, c.max_uses, c.is_active) == (
        "STRONG-10", 10, None, Decimal("999.00"), 50, True)
    for bad, text in (({**ok}, "already exists"), ({**ok, "code": "X"}, "3-32 characters"),
                      ({**ok, "code": "BIG", "percent_off": "95"}, "between 1 and 90"),
                      ({**ok, "code": "PAST", "valid_until": "2020-01-01"}, "can&#39;t be in the past"),
                      ({**ok, "code": "THEIRS", "gym_id": other_gym.id}, "Choose one of your gyms"),
                      ({**ok, "code": "AMT", "kind": "amount", "amount_off": "0"}, "greater than zero")):
        r = await post_form(client, "/owner/shop/coupons", bad)
        assert r.status_code == 400 and text in r.text, text
    r = await post_form(client, "/owner/shop/coupons", {**ok, "code": "FLAT200", "kind": "amount", "amount_off": "200",
                                                         "percent_off": ""})
    assert await db.scalar(select(Coupon.amount_off).where(Coupon.code == "FLAT200")) == Decimal("200.00")
    await post_form(client, f"/owner/shop/coupons/{c.id}/toggle", {})
    assert await db.scalar(select(Coupon.is_active).where(Coupon.id == c.id)) is False
    r = await client.get("/owner/shop/coupons")
    assert "STRONG-10" in r.text and "Paused" in r.text
    intruder, _, _ = await shop_setup(db)
    await login_as(client, intruder)
    assert (await post_form(client, f"/owner/shop/coupons/{c.id}/toggle", {})).status_code == 404


async def test_review_moderation(client, db, anon_client_factory):
    owner, gym, _ = await shop_setup(db)
    p = await make_product(db, gym, name="Moderated Whey")
    cust = await make_customer(db)
    review = ProductReview(product_id=p.id, user_id=cust.id, rating=1, title="Spam spam", body="Buy elsewhere!!!")
    db.add(review)
    await db.commit()
    await login_as(client, owner)
    r = await client.get("/owner/shop/reviews?show=low")
    assert "Spam spam" in r.text
    await post_form(client, f"/owner/shop/reviews/{review.id}/toggle", {})
    assert await db.scalar(select(ProductReview.is_hidden).where(ProductReview.id == review.id)) is True
    guest = anon_client_factory("203.0.113.70")
    r = await guest.get(f"/shop/p/{p.id}/{p.slug}")
    assert "Spam spam" not in r.text and "No reviews yet" in r.text
    assert (await svc.rating_summary(db, p.id)).count == 0
    assert await db.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == "shop.review.hide")) == 1
    intruder, _, _ = await shop_setup(db)
    await login_as(client, intruder)
    assert (await post_form(client, f"/owner/shop/reviews/{review.id}/toggle", {})).status_code == 404


# ====================================================================================================== dashboard & orders
async def test_dashboard_aggregates(client, db):
    owner, gym, branch = await shop_setup(db)
    whey = await make_product(db, gym, name="Dashboard Whey", price=Decimal("1000"), tax_rate=Decimal("0"), stock=2,
                              low_stock_threshold=3)
    cust = await make_customer(db)
    paid = await make_order(db, cust, gym, [(whey, 2)], status=OrderStatus.PAID, branch=branch)
    from app.core.utils import utcnow

    await db.execute(ShopOrder.__table__.update().where(ShopOrder.id == paid.id).values(paid_at=utcnow()))
    done = await make_order(db, cust, gym, [(whey, 1)], status=OrderStatus.COMPLETED, completed_days_ago=1, branch=branch)
    from app.models import Refund

    db.add(Refund(order_id=done.id, amount=Decimal("1000"), reason="Leaking tub", status=RefundStatus.REQUESTED))
    await db.commit()
    data = await svc.owner_dashboard(db, owner)
    assert data["sales"][0].today == Decimal("2000.00") and data["to_fulfil"] == 1
    assert [p.name for p, _ in data["low_stock"]] == ["Dashboard Whey"]
    assert len(data["refunds"]) == 1 and data["top"][0].product_name == "Dashboard Whey"
    await login_as(client, owner)
    r = await client.get("/owner/shop")
    assert r.status_code == 200 and "₹2,000.00" in r.text and "Leaking tub" in r.text and "Dashboard Whey" in r.text


async def test_owner_order_flow_notifies_customer(client, db, anon_client_factory):
    owner, gym, branch = await shop_setup(db)
    p = await make_product(db, gym)
    cust = await make_customer(db)
    pickup = await make_order(db, cust, gym, [(p, 1)], status=OrderStatus.PAID, branch=branch)
    delivery = await make_order(db, cust, gym, [(p, 1)], status=OrderStatus.PAID, fulfillment="delivery")
    await login_as(client, owner)
    r = await client.get(f"/owner/shop/orders/{pickup.id}")
    assert r.status_code == 200 and "Mark processing" in r.text
    await post_form(client, f"/owner/shop/orders/{pickup.id}/status", {"action": "shipped"})  # wrong for pickup
    assert await db.scalar(select(ShopOrder.status).where(ShopOrder.id == pickup.id)) == OrderStatus.PAID
    await post_form(client, f"/owner/shop/orders/{pickup.id}/status", {"action": "processing"})
    await post_form(client, f"/owner/shop/orders/{pickup.id}/status", {"action": "ready", "note": "Front desk after 5 pm"})
    assert await db.scalar(select(ShopOrder.status).where(ShopOrder.id == pickup.id)) == OrderStatus.READY_FOR_PICKUP
    body = await db.scalar(select(Notification.body).where(Notification.user_id == cust.id, Notification.title == "Ready for pickup"))
    assert branch.name in body and "Front desk after 5 pm" in body
    await post_form(client, f"/owner/shop/orders/{pickup.id}/status", {"action": "completed"})
    assert await db.scalar(select(ShopOrder.completed_at).where(ShopOrder.id == pickup.id)) is not None
    await post_form(client, f"/owner/shop/orders/{delivery.id}/status", {"action": "shipped", "note": "DTDC D123456789"})
    assert "Your order has shipped" in await titles(db, cust.id) and "Order completed" in await titles(db, cust.id)
    c2 = anon_client_factory("127.0.0.1")
    await login_as(c2, cust)
    r = await c2.get(f"/shop/orders/{delivery.id}")
    assert "DTDC D123456789" in r.text and "Shipping update" in r.text
    r = await c2.get(f"/shop/orders/{pickup.id}")
    assert "Return or refund" in r.text and "Request refund" in r.text
    assert await db.scalar(select(func.count(AuditLog.id)).where(AuditLog.action.like("shop.order.%"))) == 4


async def test_owner_cancel_refunds_and_restocks(client, db):
    owner, gym, branch = await shop_setup(db)
    p = await make_product(db, gym, stock=3)
    cust = await make_customer(db)
    order = await make_order(db, cust, gym, [(p, 2)], status=OrderStatus.PROCESSING, branch=branch)
    await login_as(client, owner)
    r = await post_form(client, f"/owner/shop/orders/{order.id}/cancel", {"reason": "Supplier recall"})
    assert r.status_code == 303
    o = await db.get(ShopOrder, order.id, populate_existing=True)
    assert o.status == OrderStatus.CANCELLED and o.refunded_total == o.total
    assert await db.scalar(select(Product.stock).where(Product.id == p.id)) == 5
    assert "Order cancelled" in await titles(db, cust.id)


async def test_owner_orders_list_filters_and_constant_queries(client, db):
    owner, gym, branch = await shop_setup(db)
    p = await make_product(db, gym)
    cust = await make_customer(db)
    first = await make_order(db, cust, gym, [(p, 1)], status=OrderStatus.PAID, branch=branch)
    await make_order(db, cust, gym, [(p, 2)], status=OrderStatus.COMPLETED, completed_days_ago=1, branch=branch)
    await login_as(client, owner)
    await client.get("/owner/shop/orders")
    with QueryCounter() as small:
        r = await client.get("/owner/shop/orders")
    assert r.status_code == 200 and first.order_number in r.text
    for _ in range(10):
        await make_order(db, await make_customer(db), gym, [(p, 1)], status=OrderStatus.PAID, branch=branch)
    with QueryCounter() as big:
        assert (await client.get("/owner/shop/orders")).status_code == 200
    assert big.count == small.count
    r = await client.get(f"/owner/shop/orders?q={first.order_number}")
    assert r.text.count("/owner/shop/orders/") >= 1 and first.order_number in r.text
    r = await client.get("/owner/shop/orders?status=completed")
    assert first.order_number not in r.text
    r = await client.get("/owner/shop/orders?status=to_fulfil")
    assert first.order_number in r.text
    intruder, _, _ = await shop_setup(db)
    await login_as(client, intruder)
    r = await client.get("/owner/shop/orders")
    assert first.order_number not in r.text
    assert (await client.get(f"/owner/shop/orders/{first.id}")).status_code == 404


# ====================================================================================================== background + samples
async def test_low_stock_digest_is_sent_at_most_daily(db):
    owner, gym, _ = await shop_setup(db)
    await make_product(db, gym, name="Almost Gone", stock=1, low_stock_threshold=3)
    await make_product(db, gym, name="Plenty", stock=50, low_stock_threshold=3)
    await make_product(db, gym, name="Hidden Low", stock=0, is_active=False)
    assert "shop_low_stock_digest" in registered_periodic() and "shop_expire_unpaid" in registered_periodic()
    assert await svc.send_low_stock_digests(db) == 1
    await db.commit()
    body = await db.scalar(select(Notification.body).where(Notification.user_id == owner.id))
    assert "Almost Gone (1 left)" in body and "Plenty" not in body and "Hidden Low" not in body
    assert await svc.send_low_stock_digests(db) == 0  # throttled
    await db.commit()
    assert await db.scalar(select(func.count(Notification.id)).where(Notification.title == svc.LOW_STOCK_TITLE)) == 1
