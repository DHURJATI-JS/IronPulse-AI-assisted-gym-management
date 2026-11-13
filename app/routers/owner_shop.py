"""WP-L gym shop (owner side): dashboard, products (stock, images), orders (fulfilment, cancellations, refunds),
coupons and review moderation. Every query is scoped to the owner's own gyms - foreign ids are 404.

Routes
  GET  /owner/shop                                       dashboard (SQL aggregates only)
  GET  /owner/shop/products                              all products of my gyms (gym / category / status / search)
  GET/POST /owner/gyms/{gym_id}/shop/products/new        create (with up to 8 photos)
  GET/POST /owner/shop/products/{product_id}             edit
  POST /owner/shop/products/{product_id}/stock           restock / remove / set (InventoryMovement + audit)
  POST /owner/shop/products/{product_id}/images          add photos (<= 8 in total)
  POST /owner/shop/products/{product_id}/images/{image_id}/delete | /move
  POST /owner/shop/products/{product_id}/status          activate / deactivate
  POST /owner/shop/products/{product_id}/delete          only when the product never appeared in an order
  GET  /owner/shop/orders  ·  GET /owner/shop/orders/{order_id}
  POST /owner/shop/orders/{order_id}/status | /cancel | /refund
  POST /owner/shop/refunds/{refund_id}/approve | /reject
  GET/POST /owner/shop/coupons  ·  POST /owner/shop/coupons/{coupon_id}/toggle
  GET  /owner/shop/reviews  ·  POST /owner/shop/reviews/{review_id}/toggle
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import contains_eager, joinedload
from starlette.datastructures import UploadFile

from app.core import caps
from app.core.deps import DB, NotFound, OwnerUser, get_owned_gym
from app.core.pagination import paginate, parse_page
from app.core.templating import redirect, render
from app.core.utils import clamp, safe_zone, to_decimal, utcnow
from app.models import (
    AuditLog,
    Coupon,
    Gym,
    InventoryMovement,
    Product,
    ProductImage,
    ProductReview,
    ShopOrder,
    ShopOrderItem,
    Transaction,
    User,
)
from app.models.shop import OrderStatus, ProductCategory, RefundStatus
from app.services import shop as svc
from app.services import shop_payments as pay
from app.services import quotas, storage
from app.services.audit import audit

log = logging.getLogger("ironpulse.shop.owner")
router = APIRouter()

ORDERS_PER_PAGE = 30
PRODUCTS_PER_PAGE = 50
COUPONS_PER_PAGE = 30
REVIEWS_PER_PAGE = 30
ORDER_EVENTS_LIMIT = 50  # audit events read for one order's timeline (ix_audit_logs_entity serves the lookup)
FILTER_DATE_MIN = date(2000, 1, 1)  # list date filters are clamped (date + 1 day must never overflow)
STATUS_FILTERS = {
    "": "All",
    "to_fulfil": "To fulfil",
    "pending_payment": "Awaiting payment",
    "refund_requested": "Refund requested",
    "completed": "Completed",
    "cancelled": "Cancelled",
}


# ====================================================================================================== helpers
def _int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _txt(form: Any, key: str, n: int = 500) -> str:
    v = form.get(key) if form is not None else None
    return v.strip()[:n] if isinstance(v, str) else ""


def _url(base: str, params: dict[str, Any]) -> str:
    clean = {k: v for k, v in params.items() if v not in ("", None)}
    return f"{base}?{urlencode(clean)}" if clean else base


async def _gyms(db: DB, owner: User) -> list[Gym]:
    return list((await db.execute(select(Gym).where(Gym.owner_id == owner.id).order_by(Gym.name, Gym.id))).scalars().all())


async def _product(db: DB, product_id: int, owner: User) -> Product:
    product = await svc.get_owned_product(db, product_id, owner)
    if product is None:
        raise NotFound("Product not found.")
    return product


async def _order(db: DB, order_id: int, owner: User) -> ShopOrder:
    order = await svc.load_order(db, order_id, owner_id=owner.id)
    if order is None:
        raise NotFound("Order not found.")
    return order


def _back(product: Product, anchor: str = "") -> str:
    return f"/owner/shop/products/{product.id}{anchor}"


# ====================================================================================================== dashboard
@router.get("/owner/shop")
async def dashboard(request: Request, db: DB, owner: OwnerUser):
    gyms = await _gyms(db, owner)
    data = await svc.owner_dashboard(db, owner) if gyms else None
    return render(request, "owner/shop/dashboard.html", {"gyms": gyms, "d": data, "tab": "overview"})


# ====================================================================================================== products
@router.get("/owner/shop/products")
async def products_list(request: Request, db: DB, owner: OwnerUser):
    qp = request.query_params
    gyms = await _gyms(db, owner)
    gym_id = _int(qp.get("gym")) or None
    category = qp.get("category") or ""
    status = qp.get("status") or ""
    q = clamp(qp.get("q") or "", 80)
    where: list[Any] = [Gym.owner_id == owner.id]
    if gym_id:
        where.append(Product.gym_id == gym_id)
    if category in {c.value for c in ProductCategory}:
        where.append(Product.category == ProductCategory(category))
    if status == "active":
        where.append(Product.is_active.is_(True))
    elif status == "hidden":
        where.append(Product.is_active.is_(False))
    elif status == "low":
        where += [Product.is_active.is_(True), Product.stock <= Product.low_stock_threshold]
    elif status == "out":
        where.append(Product.stock <= 0)
    if q:
        pattern = svc._like(q)
        where.append(or_(Product.name.ilike(pattern, escape="\\"), Product.brand.ilike(pattern, escape="\\"),
                         Product.sku.ilike(pattern, escape="\\")))
    p = await paginate(
        db,
        select(Product, svc.cover_key_col()).join(Gym, Gym.id == Product.gym_id).options(contains_eager(Product.gym))
        .where(*where).order_by(Product.is_active.desc(), Product.updated_at.desc(), Product.id.desc()),
        page=parse_page(request).page, per_page=PRODUCTS_PER_PAGE,
        count_stmt=select(func.count(Product.id)).join(Gym, Gym.id == Product.gym_id).where(*where),
    )
    params = {"gym": gym_id or "", "category": category, "status": status, "q": q}
    return render(request, "owner/shop/products.html", {
        "rows": p.items, "total": p.total, "gyms": gyms, "f": params, "p": p, "page": p.page, "pages": p.pages,
        "tab": "products", "categories": [(c.value, svc.CATEGORY_INFO[c][0]) for c in ProductCategory],
    })


def _form_ctx(gym: Gym, branches: list, *, product: Product | None, values: dict | None = None,
              errors: dict | None = None, movements: list | None = None, has_orders: bool = False) -> dict[str, Any]:
    p = product
    v = values or ({
        "name": p.name, "category": p.category, "brand": p.brand, "description": p.description, "price": p.price,
        "compare_at_price": p.compare_at_price, "tax_rate": p.tax_rate, "sku": p.sku, "stock": p.stock,
        "low_stock_threshold": p.low_stock_threshold, "is_active": p.is_active, "is_supplement": p.is_supplement,
        "age_restricted": p.age_restricted, "allergens": p.allergens or [], "caution_tags": p.caution_tags or [],
        "ingredients": p.ingredients, "nutrition_facts": p.nutrition_facts or {}, "fulfillment": p.fulfillment,
        "pickup_branch_id": p.pickup_branch_id,
    } if p else {"category": ProductCategory.SUPPLEMENTS, "tax_rate": Decimal("18"), "stock": 0, "low_stock_threshold": 5,
                 "is_active": True, "allergens": [], "caution_tags": [], "nutrition_facts": {}, "fulfillment": "both"})
    return {
        "gym": gym, "branches": branches, "product": product, "v": v, "errors": errors or {}, "tab": "products",
        "categories": [(c.value, svc.CATEGORY_INFO[c][0]) for c in ProductCategory],
        "allergens": svc.ALLERGENS, "caution_tags": svc.CAUTION_TAGS, "common_tags": svc.COMMON_CAUTION_TAGS,
        "nutrition_fields": svc.NUTRITION_FIELDS, "fulfillment_labels": svc.FULFILLMENT_LABELS,
        "max_images": caps.limit("product_images"), "movements": movements or [], "has_orders": has_orders,
        "inferred": svc.effective_caution_tags(product) if product else {},
    }


def _uploads(form: Any, key: str = "images") -> list[UploadFile]:
    return [f for f in form.getlist(key) if isinstance(f, UploadFile) and f.filename]


async def _save_images(db: DB, product: Product, files: list[UploadFile], start: int) -> tuple[int, list[str]]:
    """Store the photos, then charge them to the gym's storage quota (app/services/quotas.py) - photos that would pass
    it are deleted again and reported. The caller holds the product's photo cap (lock order: narrow, then broad)."""
    stored_files: list[storage.StoredFile] = []
    problems: list[str] = []
    for upload in files:
        try:
            stored_files.append(await storage.save_public_image(upload, kind="products", max_side=1600))
        except storage.UploadError as exc:
            problems.append(f"{upload.filename}: {exc}")
    if stored_files:
        try:
            await quotas.reserve_gym(db, product.gym_id, sum(s.size for s in stored_files))
        except caps.CapReached as exc:
            for s in stored_files:
                await storage.delete_key(s.key)
            return 0, [*problems, str(exc)]
    for i, stored in enumerate(stored_files):
        db.add(ProductImage(product_id=product.id, image_key=stored.key, alt=clamp(product.name, 160), sort_order=start + i,
                            size_bytes=stored.size))
    return len(stored_files), problems


@router.get("/owner/gyms/{gym_id}/shop/products/new")
async def product_new(gym_id: int, request: Request, db: DB, owner: OwnerUser):
    gym = await get_owned_gym(db, gym_id, owner)
    branches = await svc.pickup_branches(db, gym.id)
    return render(request, "owner/shop/product_form.html", _form_ctx(gym, branches, product=None))


@router.post("/owner/gyms/{gym_id}/shop/products/new")
async def product_create(gym_id: int, request: Request, db: DB, owner: OwnerUser):
    gym = await get_owned_gym(db, gym_id, owner)
    branches = await svc.pickup_branches(db, gym.id)
    form = await request.form()
    values, errors = svc.parse_product_form(form, branches=branches, creating=True)
    files = _uploads(form)
    if len(files) > caps.limit("product_images"):
        errors["images"] = f"Add at most {caps.limit('product_images')} photos."
    status = 400
    if not errors:  # products per gym shop, race-safe (held until this request commits the product)
        try:
            await caps.enforce(db, "products_per_gym", gym.id, select(Product.id).where(Product.gym_id == gym.id))
        except caps.CapReached as exc:
            errors, status = {"name": str(exc)}, exc.status_code
    if errors:
        return render(request, "owner/shop/product_form.html", _form_ctx(gym, branches, product=None, values=values, errors=errors),
                      status_code=status)
    product = Product(gym_id=gym.id, currency=gym.currency, slug=await svc.unique_product_slug(db, gym.id, values["name"]),
                      **values)
    svc.refresh_embedding(product)
    db.add(product)
    await db.flush()
    if product.stock:
        db.add(InventoryMovement(product_id=product.id, change=product.stock, reason="restock", actor_id=owner.id,
                                 note="Opening stock"))
    saved, problems = await _save_images(db, product, files, 0)
    await audit(db, owner, "shop.product.create", "product", product.id, gym_id=gym.id, request=request,
                detail={"name": product.name, "price": str(product.price), "stock": product.stock, "images": saved})
    await db.commit()
    msg = f"{product.name} is {'live in your shop' if product.is_active else 'saved (hidden)'}."
    if problems:
        return redirect(_back(product), msg + " Some photos were skipped: " + "; ".join(problems)[:200], "warning")
    return redirect(_back(product), msg, "success")


async def _edit_ctx(db: DB, product: Product, **kw: Any) -> dict[str, Any]:
    branches = await svc.pickup_branches(db, product.gym_id)
    movements = list((await db.execute(
        select(InventoryMovement).where(InventoryMovement.product_id == product.id)
        .order_by(InventoryMovement.created_at.desc(), InventoryMovement.id.desc()).limit(10)
    )).scalars().all())
    return _form_ctx(product.gym, branches, product=product, movements=movements,
                     has_orders=await svc.product_has_orders(db, product.id), **kw)


@router.get("/owner/shop/products/{product_id}")
async def product_edit(product_id: int, request: Request, db: DB, owner: OwnerUser):
    product = await _product(db, product_id, owner)
    return render(request, "owner/shop/product_form.html", await _edit_ctx(db, product))


@router.post("/owner/shop/products/{product_id}")
async def product_update(product_id: int, request: Request, db: DB, owner: OwnerUser):
    product = await _product(db, product_id, owner)
    branches = await svc.pickup_branches(db, product.gym_id)
    form = await request.form()
    values, errors = svc.parse_product_form(form, branches=branches, creating=False)
    if errors:
        return render(request, "owner/shop/product_form.html", await _edit_ctx(db, product, values=values, errors=errors),
                      status_code=400)
    before = {"price": product.price, "compare_at_price": product.compare_at_price, "tax_rate": product.tax_rate}
    changed = [k for k, v in values.items() if getattr(product, k) != v]
    for k in changed:
        setattr(product, k, values[k])
    if "name" in changed:
        product.slug = await svc.unique_product_slug(db, product.gym_id, product.name, exclude_id=product.id)
    if set(changed) & {"name", "brand", "category", "description", "ingredients"}:
        svc.refresh_embedding(product)
    if changed:
        await audit(db, owner, "shop.product.update", "product", product.id, gym_id=product.gym_id, request=request,
                    detail={"fields": changed})
    price_changes = {k: {"from": str(before[k]) if before[k] is not None else None,
                         "to": str(values[k]) if values[k] is not None else None} for k in before if k in changed}
    if price_changes:
        await audit(db, owner, "shop.product.price", "product", product.id, gym_id=product.gym_id, request=request,
                    detail=price_changes)
    await db.commit()
    return redirect(_back(product), "Changes saved." if changed else "Nothing changed.", "success" if changed else "info")


@router.post("/owner/shop/products/{product_id}/stock")
async def product_stock(product_id: int, request: Request, db: DB, owner: OwnerUser):
    product = await _product(db, product_id, owner)
    form = await request.form()
    mode = _txt(form, "mode", 10) or "restock"
    qty = _int(form.get("quantity"), -1)
    note = " ".join(_txt(form, "reason", 255).split())
    if len(note) < 3:
        return redirect(_back(product, "#stock"), "Give a short reason for the stock change (e.g. “New delivery”).", "error")
    if mode == "set":
        if not 0 <= qty <= svc.MAX_STOCK:
            return redirect(_back(product, "#stock"), f"Enter a stock level between 0 and {svc.MAX_STOCK}.", "error")
        change = qty - product.stock
    else:
        if not 1 <= qty <= svc.MAX_STOCK:
            return redirect(_back(product, "#stock"), "Enter a quantity of at least 1.", "error")
        change = qty if mode == "restock" else -qty
    if change == 0:
        return redirect(_back(product, "#stock"), "Stock is already at that level.", "info")
    before = product.stock
    back = _back(product, "#stock")
    try:  # manual stock changes per product per day (sales, refunds and cancellations are not counted)
        await caps.enforce(db, "stock_changes_per_product_day", product.id, select(InventoryMovement.id).where(
            InventoryMovement.product_id == product.id, InventoryMovement.reason.in_(("restock", "adjustment")),
            InventoryMovement.actor_id.is_not(None), InventoryMovement.created_at >= caps.since()))
    except caps.CapReached as exc:
        return redirect(back, str(exc), "error")
    try:
        await svc.adjust_stock(db, product, change, reason="restock" if mode == "restock" else "adjustment", actor=owner,
                               note=note)
    except svc.ShopError as exc:
        return redirect(back, str(exc), "error")
    await audit(db, owner, "shop.product.stock", "product", product.id, gym_id=product.gym_id, request=request,
                detail={"from": before, "to": before + change, "change": change, "mode": mode, "reason": note})
    await db.commit()
    return redirect(back, f"Stock updated: {before} → {before + change}.", "success")


@router.post("/owner/shop/products/{product_id}/images")
async def product_images(product_id: int, request: Request, db: DB, owner: OwnerUser):
    product = await _product(db, product_id, owner)
    form = await request.form()
    files = _uploads(form)
    if not files:
        return redirect(_back(product, "#photos"), "Choose at least one photo.", "error")
    # photos per product, race-safe: the cap's lock is held until this request commits its photos
    room = await caps.room(db, "product_images", product.id, select(ProductImage.id).where(ProductImage.product_id == product.id))
    max_images = caps.limit("product_images")
    if room <= 0:
        return redirect(_back(product, "#photos"), str(caps.CapReached("product_images")), "error")
    start = max((img.sort_order for img in product.images), default=-1) + 1
    saved, problems = await _save_images(db, product, files[:room], start)
    await audit(db, owner, "shop.product.images", "product", product.id, gym_id=product.gym_id, request=request,
                detail={"added": saved})
    await db.commit()
    msg = f"Added {saved} photo{'s' if saved != 1 else ''}."
    if len(files) > room:
        msg += f" Only {room} more fit (max {max_images})."
    if problems:
        msg += " Skipped: " + "; ".join(problems)
    return redirect(_back(product, "#photos"), clamp(msg, 290), "warning" if problems or len(files) > room else "success")


@router.post("/owner/shop/products/{product_id}/images/{image_id}/delete")
async def product_image_delete(product_id: int, image_id: int, request: Request, db: DB, owner: OwnerUser):
    product = await _product(db, product_id, owner)
    image = next((i for i in product.images if i.id == image_id), None)
    if image is None:
        raise NotFound("Photo not found.")
    await db.execute(delete(ProductImage).where(ProductImage.id == image.id))
    await audit(db, owner, "shop.product.image_delete", "product", product.id, gym_id=product.gym_id, request=request,
                detail={"image_id": image.id})
    await db.commit()
    await storage.delete_key(image.image_key)
    return redirect(_back(product, "#photos"), "Photo removed.", "success")


@router.post("/owner/shop/products/{product_id}/images/{image_id}/move")
async def product_image_move(product_id: int, image_id: int, request: Request, db: DB, owner: OwnerUser):
    product = await _product(db, product_id, owner)
    form = await request.form()
    images = sorted(product.images, key=lambda i: (i.sort_order, i.id))
    idx = next((k for k, i in enumerate(images) if i.id == image_id), None)
    if idx is None:
        raise NotFound("Photo not found.")
    direction = _txt(form, "direction", 10)
    img = images.pop(idx)
    if direction == "first":
        images.insert(0, img)
    elif direction == "up":
        images.insert(max(0, idx - 1), img)
    else:
        images.insert(min(len(images), idx + 1), img)
    for n, i in enumerate(images):
        i.sort_order = n
    await db.commit()
    return redirect(_back(product, "#photos"), "Cover photo updated." if direction == "first" else "Photo order updated.", "success")


@router.post("/owner/shop/products/{product_id}/status")
async def product_status(product_id: int, request: Request, db: DB, owner: OwnerUser):
    product = await _product(db, product_id, owner)
    form = await request.form()
    product.is_active = _txt(form, "action", 12) == "activate"
    await audit(db, owner, "shop.product.activate" if product.is_active else "shop.product.deactivate", "product",
                product.id, gym_id=product.gym_id, request=request)
    await db.commit()
    return redirect(_back(product), "Product is live in your shop." if product.is_active else "Product hidden from customers.",
                    "success")


@router.post("/owner/shop/products/{product_id}/delete")
async def product_delete(product_id: int, request: Request, db: DB, owner: OwnerUser):
    product = await _product(db, product_id, owner)
    if await svc.product_has_orders(db, product.id):
        product.is_active = False
        await audit(db, owner, "shop.product.deactivate", "product", product.id, gym_id=product.gym_id, request=request,
                    detail={"reason": "delete requested but product has orders"})
        await db.commit()
        return redirect(_back(product), "This product appears in orders, so it was hidden instead of deleted (order history keeps it).",
                        "info")
    keys = [i.image_key for i in product.images]
    name = product.name
    await audit(db, owner, "shop.product.delete", "product", product.id, gym_id=product.gym_id, request=request,
                detail={"name": name})
    await db.execute(delete(Product).where(Product.id == product.id))
    await db.commit()
    for key in keys:
        await storage.delete_key(key)
    return redirect("/owner/shop/products", f"Deleted {name}.", "success")


# ====================================================================================================== orders
@router.get("/owner/shop/orders")
async def orders_list(request: Request, db: DB, owner: OwnerUser):
    qp = request.query_params
    gyms = await _gyms(db, owner)
    status = qp.get("status") or ""
    gym_id = _int(qp.get("gym")) or None
    q = clamp(qp.get("q") or "", 60)
    tz = safe_zone(owner.timezone)
    d_from = _filter_date(qp.get("from"))
    d_to = _filter_date(qp.get("to"))
    where: list[Any] = [Gym.owner_id == owner.id]
    if status == "to_fulfil":
        where.append(ShopOrder.status.in_([OrderStatus.PAID, OrderStatus.PROCESSING, OrderStatus.READY_FOR_PICKUP,
                                           OrderStatus.SHIPPED]))
    elif status in {s.value for s in OrderStatus}:
        where.append(ShopOrder.status == OrderStatus(status))
    if gym_id:
        where.append(ShopOrder.gym_id == gym_id)
    if q:
        pattern = svc._like(q)
        where.append(or_(ShopOrder.order_number.ilike(pattern, escape="\\"), ShopOrder.customer_label.ilike(pattern, escape="\\")))
    if d_from:
        where.append(ShopOrder.created_at >= datetime.combine(d_from, time.min, tzinfo=tz))
    if d_to:
        where.append(ShopOrder.created_at < datetime.combine(d_to + timedelta(days=1), time.min, tzinfo=tz))
    units = (select(func.coalesce(func.sum(ShopOrderItem.quantity), 0)).where(ShopOrderItem.order_id == ShopOrder.id)
             .correlate(ShopOrder).scalar_subquery())
    p = await paginate(
        db,
        select(ShopOrder, units).join(Gym, Gym.id == ShopOrder.gym_id).options(contains_eager(ShopOrder.gym))
        .where(*where).order_by(ShopOrder.created_at.desc(), ShopOrder.id.desc()),
        page=parse_page(request).page, per_page=ORDERS_PER_PAGE,
        count_stmt=select(func.count(ShopOrder.id)).join(Gym, Gym.id == ShopOrder.gym_id).where(*where),
    )
    counts = dict((await db.execute(
        select(ShopOrder.status, func.count(ShopOrder.id)).join(Gym, Gym.id == ShopOrder.gym_id)
        .where(Gym.owner_id == owner.id).group_by(ShopOrder.status))).all())
    params = {"status": status, "gym": gym_id or "", "q": q, "from": d_from.isoformat() if d_from else "",
              "to": d_to.isoformat() if d_to else ""}
    return render(request, "owner/shop/orders.html", {
        "rows": p.items, "total": p.total, "gyms": gyms, "f": params, "p": p, "page": p.page, "pages": p.pages,
        "tab": "orders", "status_filters": STATUS_FILTERS, "counts": {getattr(k, "value", k): v for k, v in counts.items()},
        "status_links": {k: _url("/owner/shop/orders", {**params, "status": k}) for k in STATUS_FILTERS},
        "all_statuses": [(s.value, svc.ORDER_STATUS_INFO[s][0]) for s in OrderStatus],
    })


def _date(raw: Any) -> date | None:
    try:
        return date.fromisoformat(str(raw)) if raw else None
    except ValueError:
        return None


def _filter_date(raw: Any) -> date | None:
    """A list filter date, clamped to FILTER_DATE_MIN .. a year from now (?to=9999-12-31 must not overflow)."""
    d = _date(str(raw).strip()[:10]) if raw else None
    if d is None:
        return None
    return min(max(d, FILTER_DATE_MIN), date.today() + timedelta(days=366))


async def _order_events(db: DB, order_id: int) -> list[AuditLog]:
    """The order's status events, oldest first (the timeline needs each status' first event), at most
    ORDER_EVENTS_LIMIT."""
    return list((await db.execute(
        select(AuditLog).where(AuditLog.entity_type == "shop_order", AuditLog.entity_id == order_id)
        .order_by(AuditLog.created_at, AuditLog.id).limit(ORDER_EVENTS_LIMIT)
    )).scalars().all())


@router.get("/owner/shop/orders/{order_id}")
async def order_detail(order_id: int, request: Request, db: DB, owner: OwnerUser):
    order = await _order(db, order_id, owner)
    events = await _order_events(db, order.id)
    covers = await svc.order_covers(db, [i.product_id for i in order.items])
    tx_ids = [r.transaction_id for r in order.refunds if r.transaction_id]
    txs = list((await db.execute(
        select(Transaction).where(or_(Transaction.provider_order_id == order.order_number, Transaction.id.in_(tx_ids or [0])))
        .order_by(Transaction.created_at)
    )).scalars().all())
    open_refunds = [r for r in order.refunds if r.status == RefundStatus.REQUESTED]
    items_by_id = {i.id: i for i in order.items}
    left = {i.id: i.quantity - i.refunded_quantity for i in order.items}
    return render(request, "owner/shop/order.html", {
        "order": order, "items": order.items, "quote": svc.quote_for_order(order, order.items), "tab": "orders",
        "timeline": svc.order_timeline(order, events), "events": events, "covers": covers, "txs": txs,
        "open_refunds": open_refunds, "items_by_id": items_by_id, "left": left,
        "unit_amounts": {i.id: (i.line_total / i.quantity) for i in order.items},
        "can_cancel": order.status in svc.OWNER_CANCELLABLE,
        "can_issue": order.paid_at is not None and order.status not in (OrderStatus.CANCELLED, OrderStatus.REFUNDED)
                     and not open_refunds and any(v > 0 for v in left.values()),
        "flagged": pay.MISMATCH_MARK in (order.notes or ""),
    })


@router.post("/owner/shop/orders/{order_id}/status")
async def order_status(order_id: int, request: Request, db: DB, owner: OwnerUser):
    await _order(db, order_id, owner)
    form = await request.form()
    try:
        target = await pay.advance_order(db, order_id, owner, _txt(form, "action", 20), note=_txt(form, "note", 300),
                                         request=request)
    except svc.ShopError as exc:
        return redirect(f"/owner/shop/orders/{order_id}", str(exc), "error")
    await db.commit()
    return redirect(f"/owner/shop/orders/{order_id}", f"Order marked as {svc.order_status_label(target).lower()} - the customer was notified.",
                    "success")


@router.post("/owner/shop/orders/{order_id}/cancel")
async def order_cancel(order_id: int, request: Request, db: DB, owner: OwnerUser):
    await _order(db, order_id, owner)
    form = await request.form()
    try:
        outcome = await pay.cancel_order(db, order_id, owner, by_owner=True, reason=_txt(form, "reason", 300), request=request)
    except svc.ShopError as exc:
        return redirect(f"/owner/shop/orders/{order_id}", str(exc), "error")
    await db.commit()
    if outcome == "already_cancelled":  # a second tab or a resubmit: nothing changed
        return redirect(f"/owner/shop/orders/{order_id}", "This order is already cancelled - nothing changed.", "info")
    if outcome == "already_refunded":
        return redirect(f"/owner/shop/orders/{order_id}", "This order is already fully refunded - nothing to cancel.",
                        "info")
    if outcome == "refund_failed":
        return redirect(f"/owner/shop/orders/{order_id}#refunds", "The refund failed, so the order was NOT cancelled - "
                        "see the failed refund below.", "error")
    return redirect(f"/owner/shop/orders/{order_id}",
                    "Order cancelled and fully refunded; stock restored." if outcome == "refunded" else "Order cancelled.", "success")


@router.post("/owner/shop/orders/{order_id}/refund")
async def order_refund(order_id: int, request: Request, db: DB, owner: OwnerUser):
    order = await _order(db, order_id, owner)
    form = await request.form()
    try:
        quantities = pay.parse_quantities(form, order.items)
        result = await pay.issue_refund(db, order.id, owner, quantities, restock=bool(form.get("restock")),
                                        reason=_txt(form, "reason", 1000), request=request)
    except svc.ShopError as exc:
        return redirect(f"/owner/shop/orders/{order_id}#refunds", str(exc), "error")
    await db.commit()
    if not result.ok:
        return redirect(f"/owner/shop/orders/{order_id}#refunds", f"The refund failed: {result.error}", "error")
    return redirect(f"/owner/shop/orders/{order_id}#refunds", "Refund issued - the customer was notified.", "success")


async def _refund_order_id(db: DB, refund_id: int, owner: User) -> int:
    from app.models import Refund

    oid = await db.scalar(select(Refund.order_id).join(ShopOrder, ShopOrder.id == Refund.order_id)
                          .join(Gym, Gym.id == ShopOrder.gym_id).where(Refund.id == refund_id, Gym.owner_id == owner.id))
    if oid is None:
        raise NotFound("Refund not found.")
    return oid


@router.post("/owner/shop/refunds/{refund_id}/approve")
async def refund_approve(refund_id: int, request: Request, db: DB, owner: OwnerUser):
    order_id = await _refund_order_id(db, refund_id, owner)
    order = await _order(db, order_id, owner)
    form = await request.form()
    full = _txt(form, "full", 5) == "1"
    try:
        quantities = None if full else pay.parse_quantities(form, order.items)
        result = await pay.approve_refund(db, refund_id, owner, quantities=quantities, restock=bool(form.get("restock")),
                                          full=full, note=_txt(form, "note", 1000), request=request)
    except svc.ShopError as exc:
        return redirect(f"/owner/shop/orders/{order_id}#refunds", str(exc), "error")
    await db.commit()
    if not result.ok:
        return redirect(f"/owner/shop/orders/{order_id}#refunds", f"The refund failed: {result.error}", "error")
    return redirect(f"/owner/shop/orders/{order_id}#refunds", "Refund approved and processed - the customer was notified.", "success")


@router.post("/owner/shop/refunds/{refund_id}/reject")
async def refund_reject(refund_id: int, request: Request, db: DB, owner: OwnerUser):
    order_id = await _refund_order_id(db, refund_id, owner)
    form = await request.form()
    try:
        await pay.reject_refund(db, refund_id, owner, _txt(form, "note", 1000), request=request)
    except svc.ShopError as exc:
        return redirect(f"/owner/shop/orders/{order_id}#refunds", str(exc), "error")
    await db.commit()
    return redirect(f"/owner/shop/orders/{order_id}#refunds", "Refund request declined - the customer was notified.", "success")


# ====================================================================================================== coupons
async def _coupon_page(request: Request, db: DB, owner: User, *, errors: dict | None = None, form: Any = None,
                       status_code: int = 200):
    gyms = await _gyms(db, owner)
    p = await paginate(
        db,
        select(Coupon, Gym.name, Gym.currency).join(Gym, Gym.id == Coupon.gym_id)
        .where(Gym.owner_id == owner.id).order_by(Coupon.is_active.desc(), Coupon.created_at.desc(), Coupon.id.desc()),
        page=parse_page(request).page, per_page=COUPONS_PER_PAGE,
        count_stmt=select(func.count(Coupon.id)).join(Gym, Gym.id == Coupon.gym_id).where(Gym.owner_id == owner.id),
    )
    return render(request, "owner/shop/coupons.html", {"gyms": gyms, "coupons": p.items, "p": p, "errors": errors or {},
                                                       "form": form or {}, "tab": "coupons", "now": utcnow()},
                  status_code=status_code)


@router.get("/owner/shop/coupons")
async def coupons_page(request: Request, db: DB, owner: OwnerUser):
    return await _coupon_page(request, db, owner)


@router.post("/owner/shop/coupons")
async def coupon_create(request: Request, db: DB, owner: OwnerUser):
    form = await request.form()
    errors: dict[str, str] = {}
    gym = None
    gid = _int(form.get("gym_id"))
    if gid:
        gym = await db.scalar(select(Gym).where(Gym.id == gid, Gym.owner_id == owner.id))
    if gym is None:
        errors["gym_id"] = "Choose one of your gyms."
    code = svc.normalize_code(form.get("code"))
    if not svc.CODE_RE.match(code):
        errors["code"] = "Use 3-32 characters: A-Z, 0-9 and dashes (e.g. STRONG10)."
    elif gym is not None and await db.scalar(select(Coupon.id).where(Coupon.gym_id == gym.id, Coupon.code == code)):
        errors["code"] = f"{code} already exists for this gym."
    kind = _txt(form, "kind", 10)
    percent = amount = None
    if kind == "amount":
        try:
            amount = to_decimal(_txt(form, "amount_off", 20))
            if not Decimal("0.01") <= amount <= svc.MAX_PRICE:
                raise ValueError
        except ValueError:
            errors["amount_off"] = "Enter a discount amount greater than zero."
    else:
        percent = _int(form.get("percent_off"))
        if not 1 <= percent <= 90:
            errors["percent_off"] = "Percent off must be between 1 and 90."
    min_total = None
    raw_min = _txt(form, "min_order_total", 20)
    if raw_min:
        try:
            min_total = to_decimal(raw_min)
            if min_total < 0 or min_total > svc.MAX_PRICE:
                raise ValueError
        except ValueError:
            errors["min_order_total"] = "Enter a valid minimum order total (or leave it empty)."
    valid_until = None
    raw_until = _txt(form, "valid_until", 12)
    if raw_until:
        d = _date(raw_until)
        tz = safe_zone(owner.timezone)
        if d is None:
            errors["valid_until"] = "Enter a valid date."
        elif d < datetime.now(tz).date():
            errors["valid_until"] = "The expiry date can't be in the past."
        else:
            valid_until = datetime.combine(d, time(23, 59, 59), tzinfo=tz)
    max_uses = None
    raw_max = _txt(form, "max_uses", 10)
    if raw_max:
        max_uses = _int(raw_max, -1)
        if not 1 <= max_uses <= 1_000_000:
            errors["max_uses"] = "Max uses must be at least 1 (or leave it empty for unlimited)."
    status = 400
    if gym is not None and not errors:  # coupons per gym shop, race-safe
        try:
            await caps.enforce(db, "coupons_per_gym", gym.id, select(Coupon.id).where(Coupon.gym_id == gym.id))
        except caps.CapReached as exc:
            errors["code"], status = str(exc), exc.status_code
    if errors:
        return await _coupon_page(request, db, owner, errors=errors, form=form, status_code=status)
    coupon = Coupon(gym_id=gym.id, code=code, percent_off=percent if kind != "amount" else None,
                    amount_off=amount if kind == "amount" else None, min_order_total=min_total, valid_until=valid_until,
                    max_uses=max_uses, is_active=bool(form.get("is_active")))
    db.add(coupon)
    await db.flush()
    await audit(db, owner, "shop.coupon.create", "coupon", coupon.id, gym_id=gym.id, request=request,
                detail={"code": code, "percent_off": percent, "amount_off": str(amount) if amount else None,
                        "min_order_total": str(min_total) if min_total else None, "max_uses": max_uses})
    await db.commit()
    return redirect("/owner/shop/coupons", f"Coupon {code} created.", "success")


@router.post("/owner/shop/coupons/{coupon_id}/toggle")
async def coupon_toggle(coupon_id: int, request: Request, db: DB, owner: OwnerUser):
    coupon = await db.scalar(select(Coupon).join(Gym, Gym.id == Coupon.gym_id).where(Coupon.id == coupon_id,
                                                                                    Gym.owner_id == owner.id))
    if coupon is None:
        raise NotFound("Coupon not found.")
    coupon.is_active = not coupon.is_active
    await audit(db, owner, "shop.coupon.toggle", "coupon", coupon.id, gym_id=coupon.gym_id, request=request,
                detail={"active": coupon.is_active})
    await db.commit()
    return redirect("/owner/shop/coupons", f"{coupon.code} {'activated' if coupon.is_active else 'paused'}.", "success")


# ====================================================================================================== reviews
@router.get("/owner/shop/reviews")
async def reviews_page(request: Request, db: DB, owner: OwnerUser):
    show = request.query_params.get("show") or ""
    show = show if show in ("hidden", "low") else ""
    where: list[Any] = [Gym.owner_id == owner.id]
    if show == "hidden":
        where.append(ProductReview.is_hidden.is_(True))
    elif show == "low":
        where.append(ProductReview.rating <= 2)
    p = await paginate(
        db,
        select(ProductReview, Product.id, Product.name, Product.slug, Gym.name)
        .join(Product, Product.id == ProductReview.product_id).join(Gym, Gym.id == Product.gym_id)
        .options(joinedload(ProductReview.user)).where(*where)
        .order_by(ProductReview.created_at.desc(), ProductReview.id.desc()),
        page=parse_page(request).page, per_page=REVIEWS_PER_PAGE,
        count_stmt=select(func.count(ProductReview.id)).join(Product, Product.id == ProductReview.product_id)
        .join(Gym, Gym.id == Product.gym_id).where(*where),
    )
    return render(request, "owner/shop/reviews.html", {"rows": p.items, "p": p, "show": show, "tab": "reviews"})


@router.post("/owner/shop/reviews/{review_id}/toggle")
async def review_toggle(review_id: int, request: Request, db: DB, owner: OwnerUser):
    review = await db.scalar(select(ProductReview).join(Product, Product.id == ProductReview.product_id)
                             .join(Gym, Gym.id == Product.gym_id).where(ProductReview.id == review_id, Gym.owner_id == owner.id))
    if review is None:
        raise NotFound("Review not found.")
    review.is_hidden = not review.is_hidden
    gym_id = await db.scalar(select(Product.gym_id).where(Product.id == review.product_id))
    await audit(db, owner, "shop.review.hide" if review.is_hidden else "shop.review.unhide", "product_review", review.id,
                gym_id=gym_id, request=request)
    await db.commit()
    return redirect("/owner/shop/reviews", "Review hidden from the product page." if review.is_hidden else "Review visible again.",
                    "success")
