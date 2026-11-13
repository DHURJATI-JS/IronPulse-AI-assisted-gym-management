"""WP-L gym shop (customer side): catalogue, product pages, AI product Q&A, cart, wishlist, checkout, orders, refunds, reviews.

Routes
  GET  /shop                                   catalogue (q: trigram ILIKE + top-200 semantic, category, gym, sort,
                                               in-stock, 24/page, at most 500 pages; "12,000+" beyond)
  GET  /shop/gym/{slug}                        one gym's store
  GET  /api/shop/stores?q=                     store typeahead for the catalogue's store filter (JSON, <= 20)
  GET  /shop/p/{product_id}[/{slug}]           product page (gallery, safety panel, nutrition, reviews, AI Q&A)
  POST /api/shop/products/{id}/ask             AI Q&A, JSON (signed-in users only; anonymous -> 401)
  POST /api/shop/products/{id}/ask/stream      AI Q&A, Server-Sent Events (same rules; used by the product page)
  GET  /shop/wishlist (24/page)  ·  POST /shop/wishlist/{product_id}/toggle (at most 500 saved)
  GET  /shop/cart  ·  POST /shop/cart/add | /shop/cart/update | /shop/cart/remove  ·  GET /api/shop/cart/count
  GET/POST /shop/checkout?gym={gym_id}         one order per gym (pickup at a verified open branch or delivery)
  GET  /shop/checkout/return?order_id=IPO-...  Cashfree return URL (verified server-side with get_order)
  GET  /shop/orders (20/page)  ·  GET /shop/orders/{id}  ·  GET/POST /shop/orders/{id}/pay  ·  GET/POST /shop/orders/{id}/test-gateway
  POST /shop/orders/{id}/cancel  ·  POST /shop/orders/{id}/refund-request  ·  GET /shop/orders/{id}/receipt
  POST /shop/p/{product_id}/review             verified purchasers only
"""

from __future__ import annotations

import logging
import secrets
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.ai import shop as shop_ai
from app.config import get_settings
from app.core.deps import DB, AuthUser, CurrentUser, NotFound, wants_json
from app.core.pagination import parse_keyset, parse_page
from app.core.templating import redirect, render
from app.core.utils import is_safe_redirect, money, utcnow
from app.models import AuditLog, Gym, Product, ProductReview, ShopOrder, User
from app.models.shop import OrderStatus, ProductCategory
from app.routers.plan_ai import EventStreamResponse
from app.security.net import client_ip
from app.services import shop as svc
from app.services import shop_payments as pay
from app.services.audit import audit

log = logging.getLogger("ironpulse.shop.web")
router = APIRouter()

ORDER_EVENTS_LIMIT = 50  # status events read for an order's timeline (a handful by design)
PRICE_CHANGED = ("Prices, quantities or your coupon changed since this page was opened - please check the new total "
                 "below and place the order again.")

QUESTION_IDEAS: dict[str, tuple[str, ...]] = {
    "supplements": ("Is this good for muscle gain?", "How should I take it?", "Is it safe with my health conditions?",
                    "Does it contain lactose?"),
    "nutrition": ("How much protein is in it?", "Is it good before a workout?", "Is it high in sugar?"),
    "apparel": ("How does it fit?", "Is it good for running?", "How do I wash it?"),
    "accessories": ("What is it best for?", "Is it durable?", "Will it fit in a gym locker?"),
    "wearables": ("Does it track heart rate?", "Is it waterproof?", "How long does the battery last?"),
    "equipment": ("Is it good for home workouts?", "Which exercises can I do with it?", "Is it beginner friendly?"),
    "other": ("What is it best for?", "Is it worth it for a beginner?"),
}


# ====================================================================================================== helpers
def already_closed_message(order: ShopOrder, outcome: str) -> str:
    """A repeated cancel of an order that is already closed (``cancel_order``'s already_* outcomes)."""
    if outcome == "already_refunded":
        return "This order has already been fully refunded - nothing more to cancel."
    if order.paid_at is None:
        return "This order is already cancelled. Nothing was charged."
    if order.refunded_total >= order.total:
        return (f"This order is already cancelled and {money(order.refunded_total, order.currency)} was refunded to "
                "your original payment method (banks usually take 5-7 working days).")
    return "This order is already cancelled. The store is completing your refund."


def _url(base: str, params: dict[str, Any]) -> str:
    return f"{base}?{urlencode(params)}" if params else base


def _login_url(next_url: str) -> str:
    return "/auth/login?next=" + quote(next_url if is_safe_redirect(next_url) else "/shop", safe="")


def _next(form: Any, fallback: str) -> str:
    nxt = form.get("next") if form is not None else None
    return nxt if isinstance(nxt, str) and is_safe_redirect(nxt) else fallback


def _int(value: Any, default: int = 0) -> int:
    """An integer form/query value; junk - or a number too large for a BIGINT id, which would fail the query - gives
    ``default``."""
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return n if -(2**63) < n < 2**63 else default


def _json_or_redirect(request: Request, url: str, message: str | None, category: str = "success", *,
                      status: int = 200, data: dict[str, Any] | None = None):
    if wants_json(request):
        return JSONResponse({"ok": status < 400, "message": message, "detail": message, **(data or {})}, status_code=status)
    return redirect(url, message, category)


async def _cart_count(db: DB, user: User | None) -> int:
    return await svc.cart_count(db, user) if svc.can_shop(user) else 0


async def _public_product(db: DB, product_id: int, user: User | None) -> tuple[Product, bool]:
    """Product with gym/images/branch loaded. Inactive products are visible only to their owner (preview)."""
    product = await svc.load_product(db, product_id)
    if product is None:
        raise NotFound("Product not found.")
    owner_view = bool(user is not None and product.gym is not None and product.gym.owner_id == user.id)
    if not svc.is_public(product) and not owner_view:
        raise NotFound("This product is no longer available.")
    return product, owner_view


# ====================================================================================================== catalogue
async def _catalogue(request: Request, db: DB, user: User | None, *, gym: Gym | None = None):
    """Bounded work per view: one page of products (``search_products``: capped count, windowed pager links), the
    category counts and the top stores from the in-process cache, and on a store page no store list at all."""
    filters = svc.parse_filters(request.query_params)
    if gym is not None:
        filters.gym_id = gym.id
    page = await svc.search_products(db, filters, user=user)
    counts = await svc.category_counts(db, gym.id if gym else None)
    stores: list[svc.StoreOption] = []
    more_stores = False
    if gym is None:  # a store's own page has no store filter: no stores query there
        stores, more_stores = await svc.top_stores(db)
        if filters.gym_id and all(s.id != filters.gym_id for s in stores):  # a store found with the typeahead
            selected = await svc.store_option(db, filters.gym_id)
            if selected is not None:
                stores = sorted([*stores, selected], key=lambda s: (s.name.lower(), s.id))
    base = f"/shop/gym/{gym.slug}" if gym else "/shop"
    keep = {k: v for k, v in filters.params(page=1).items() if k != "gym" or gym is None}
    no_cat = {k: v for k, v in keep.items() if k != "category"}
    categories = [
        {"value": c.value, "label": svc.CATEGORY_INFO[c][0], "icon": svc.CATEGORY_INFO[c][1], "count": counts.get(c.value, 0),
         "href": _url(base, {**no_cat, **({"category": c.value} if filters.category != c.value else {})})}
        for c in ProductCategory if counts.get(c.value, 0) or filters.category == c.value
    ]
    return render(request, "shop/index.html", {
        "cards": page.items, "total": page.total, "page": page, "filters": filters, "keep": keep,
        "categories": categories, "all_href": _url(base, no_cat), "stores": stores, "more_stores": more_stores,
        "store_search_base": _url("/shop", {k: v for k, v in keep.items() if k != "gym"}),
        "store": gym, "base": base, "sorts": svc.SORTS, "cart_count": await _cart_count(db, user),
        "can_shop": svc.can_shop(user), "clear_href": base, "total_products": sum(counts.values()),
    })


@router.get("/shop")
async def catalogue(request: Request, db: DB, user: CurrentUser):
    return await _catalogue(request, db, user)


@router.get("/shop/gym/{slug}")
async def gym_store(slug: str, request: Request, db: DB, user: CurrentUser):
    gym = await db.scalar(select(Gym).where(Gym.slug == slug[:140], Gym.is_active.is_(True)))
    if gym is None:
        raise NotFound("Store not found.")
    return await _catalogue(request, db, user, gym=gym)


@router.get("/api/shop/stores")
async def store_search(request: Request, db: DB):
    """Store typeahead of the catalogue's store filter (public, like the catalogue): at most 20 active stores whose name
    contains ``?q=`` (2-60 characters; shorter -> none). One indexed statement, cached per query for a minute."""
    stores = await svc.search_stores(db, request.query_params.get("q"))
    return {"stores": [{"id": s.id, "name": s.name, "slug": s.slug} for s in stores],
            "limit": svc.STORE_SEARCH_LIMIT}


@router.get("/shop/p/{product_id}")
async def product_short(product_id: int, db: DB, user: CurrentUser):
    product, _ = await _public_product(db, product_id, user)
    return redirect(svc.product_url(product), status_code=301)


@router.get("/shop/p/{product_id}/{slug}")
async def product_page(product_id: int, slug: str, request: Request, db: DB, user: CurrentUser):
    product, owner_view = await _public_product(db, product_id, user)
    if slug != product.slug:
        return redirect(svc.product_url(product), status_code=301)
    conditions = await svc.load_conditions(db, user)
    safety = svc.product_safety(product, user, conditions)
    rating = await svc.rating_summary(db, product.id)
    before = parse_keyset(request).before_id  # the window size is the server's (REVIEWS_PAGE_SIZE), not ?limit=
    reviews = await svc.product_reviews(db, product.id, before_id=before)
    can_review = bool(user is not None and svc.can_shop(user) and await svc.has_purchased(db, user.id, product.id))
    my_review = None
    if can_review:
        my_review = await db.scalar(select(ProductReview).where(ProductReview.product_id == product.id,
                                                                ProductReview.user_id == user.id))
    related = await svc.product_cards(
        db, [*svc.public_product_clauses(), Product.gym_id == product.gym_id, Product.id != product.id],
        order=[(Product.category == product.category).desc(), (Product.stock > 0).desc(), Product.created_at.desc()],
        limit=4, user=user)
    pickup = product.fulfillment in ("pickup", "both")
    branches, pickup_total = [], 0
    if pickup and product.pickup_branch:
        branches, pickup_total = [product.pickup_branch], 1
    elif pickup:
        branches, pickup_total = await svc.pickup_preview(db, product.gym_id)  # 3 names + a count, not every branch
    fee, free_from = svc.delivery_rule(product.currency)
    here = svc.product_url(product)
    return render(request, "shop/product.html", {
        "product": product, "gym": product.gym, "images": product.images, "owner_view": owner_view,
        "rating": rating, "reviews": reviews.items, "my_review": my_review, "can_review": can_review,
        "older_reviews_href": f"{here}?before_id={reviews.next_cursor}#reviews" if reviews.has_more else None,
        "newest_reviews_href": f"{here}#reviews" if before is not None else None,
        "safety": safety, "wished": await svc.is_wished(db, user, product.id), "related": related,
        "nutrition": svc.nutrition_rows(product), "serving_size": (product.nutrition_facts or {}).get("serving_size"),
        "allergen_labels": [svc.ALLERGENS[a] for a in product.allergens or [] if a in svc.ALLERGENS],
        "caution_labels": [svc.CAUTION_TAGS.get(t, t) for t in svc.effective_caution_tags(product)],
        "fulfillment_label": svc.FULFILLMENT_LABELS.get(product.fulfillment, product.fulfillment),
        "pickup_branches": branches, "pickup_total": pickup_total,
        "delivery": product.fulfillment in ("delivery", "both"), "delivery_fee": fee, "free_from": free_from,
        "percent_off": svc.percent_off(product), "stock_state": svc.stock_state(product),
        "max_qty": max(1, min(svc.MAX_QTY, product.stock)), "can_shop": svc.can_shop(user),
        "purchase_problem": svc.purchase_problem(product, user) if user is not None else None,
        "cart_count": await _cart_count(db, user), "ai_feature": True,
        "question_ideas": QUESTION_IDEAS.get(product.category.value, QUESTION_IDEAS["other"]),
        "image_urls": [f"/media/{img.image_key}" for img in product.images if img.image_key.startswith("public/")],
    })


# ====================================================================================================== AI Q&A
class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=shop_ai.MAX_QUESTION)


async def _ask_context(db: DB, product_id: int, user: User, body: AskIn) -> tuple[Any, str] | JSONResponse:
    """Product + member context for one question. Per-user limits are not checked here: the AI guard's 'qa' quotas,
    duplicate and spam checks run inside the AI call (cached answers cost nothing and are never refused)."""
    question = shop_ai.sanitize_question(body.question)
    if not question:
        return JSONResponse({"error": "Invalid input", "detail": "Type a question about the product."}, status_code=422)
    product = await svc.load_product(db, product_id)
    if product is None or not svc.is_public(product):
        raise NotFound("Product not found.")
    return await shop_ai.build_context(db, product, user), question


@router.post("/api/shop/products/{product_id}/ask")
async def ask_product(product_id: int, body: AskIn, db: DB, user: AuthUser):
    """Signed-in users only (AuthUser: anonymous callers get 401 JSON). A guard refusal answers with the contract JSON
    (429/409/422 + ``code``, ``retry_after`` and a Retry-After header); 'busy' answers from the product listing."""
    prepared = await _ask_context(db, product_id, user, body)
    if isinstance(prepared, JSONResponse):
        return prepared
    ctx, question = prepared
    answer = shop_ai.answer_question(ctx, question)  # reads the ORM objects now; the AI call never touches the DB
    await db.close()  # no pooled connection is held while the request waits for the AI (queue + provider)
    return JSONResponse(await answer)


@router.post("/api/shop/products/{product_id}/ask/stream")
async def ask_product_stream(product_id: int, body: AskIn, db: DB, user: AuthUser):
    """Same as /ask but streamed as Server-Sent Events (start -> delta* -> done; a guard refusal is a ``blocked``
    event followed by ``done`` with ``blocked: true``)."""
    prepared = await _ask_context(db, product_id, user, body)
    if isinstance(prepared, JSONResponse):
        return prepared
    ctx, question = prepared
    events = shop_ai.stream_events(ctx, question)  # reads the ORM objects now; the generator never touches the DB
    await db.close()  # release the pooled connection for the length of the stream (objects stay loaded, detached)
    return EventStreamResponse(events)


# ====================================================================================================== wishlist
@router.get("/shop/wishlist")
async def wishlist(request: Request, db: DB, user: AuthUser):
    page = await svc.wishlist_page(db, user, parse_page(request).page)  # 24 per page, never the whole list
    return render(request, "shop/wishlist.html", {"cards": page.items, "page": page, "cart_count": await _cart_count(db, user),
                                                  "can_shop": svc.can_shop(user)})


@router.post("/shop/wishlist/{product_id}/toggle")
async def wishlist_toggle(product_id: int, request: Request, db: DB, user: CurrentUser):
    form = None if wants_json(request) else await request.form()
    product = await svc.load_product(db, product_id)
    if product is None or (not svc.is_public(product) and not await svc.is_wished(db, user, product_id)):
        raise NotFound("Product not found.")
    back = _next(form, svc.product_url(product))
    if user is None:
        if wants_json(request):
            return JSONResponse({"error": "Please sign in", "detail": "Sign in to save favourites.",
                                 "login": _login_url(back)}, status_code=401)
        return redirect(_login_url(back), "Sign in to save products to your wishlist.", "info")
    try:
        wished = await svc.toggle_wishlist(db, user, product)
    except svc.ShopError as exc:  # the wishlist is full (nothing was written)
        return _json_or_redirect(request, back, str(exc), "error", status=400)
    await db.commit()
    msg = f"Saved {product.name} to your wishlist." if wished else f"Removed {product.name} from your wishlist."
    return _json_or_redirect(request, back, msg, data={"wishlisted": wished})


# ====================================================================================================== cart
@router.get("/api/shop/cart/count")
async def cart_count_api(db: DB, user: CurrentUser):
    return {"count": await _cart_count(db, user), "signed_in": user is not None}


def _coupon_params(request: Request) -> dict[int, str]:
    out: dict[int, str] = {}
    for key, value in request.query_params.items():
        if key.startswith("coupon_") and value:
            gid = _int(key[7:])
            if gid:
                out[gid] = value
    return out


@router.get("/shop/cart")
async def cart_page(request: Request, db: DB, user: AuthUser):
    groups = await svc.load_cart(db, user, coupons=_coupon_params(request)) if svc.can_shop(user) else []
    units = sum(ln.quantity for g in groups for ln in g.lines)
    return render(request, "shop/cart.html", {"groups": groups, "can_shop": svc.can_shop(user), "cart_count": units,
                                              "units": units, "max_qty": svc.MAX_QTY})


@router.post("/shop/cart/add")
async def cart_add(request: Request, db: DB, user: CurrentUser):
    form = None
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            raw = await request.json()
        except ValueError:
            raw = {}
        data: dict[str, Any] = raw if isinstance(raw, dict) else {}
    else:
        form = await request.form()
        data = dict(form)
    product = await svc.load_product(db, _int(data.get("product_id")))
    if product is None:
        raise NotFound("Product not found.")
    back = _next(form, svc.product_url(product))
    if user is None:
        if wants_json(request):
            return JSONResponse({"error": "Please sign in", "detail": "Sign in to add items to your bag.",
                                 "login": _login_url(svc.product_url(product))}, status_code=401)
        return redirect(_login_url(svc.product_url(product)), "Sign in to add items to your bag.", "info")
    try:
        qty, note = await svc.add_to_cart(db, user, product, _int(data.get("quantity"), 1))
    except svc.ShopError as exc:
        return _json_or_redirect(request, back, str(exc), "error", status=400)
    await db.commit()
    count = await svc.cart_count(db, user)
    msg = note or f"Added {product.name} to your bag."
    return _json_or_redirect(request, back, msg, "info" if note else "success",
                             data={"count": count, "quantity": qty, "product": product.name})


@router.post("/shop/cart/update")
async def cart_update(request: Request, db: DB, user: AuthUser):
    form = await request.form()
    back = _next(form, "/shop/cart")
    try:
        qty, note = await svc.update_cart_quantity(db, user, _int(form.get("product_id")), _int(form.get("quantity"), 1))
    except svc.ShopError as exc:
        return _json_or_redirect(request, back, str(exc), "error", status=400)
    await db.commit()
    return _json_or_redirect(request, back, note or ("Bag updated." if qty else "Removed from your bag."),
                             "info" if note else "success", data={"quantity": qty, "count": await svc.cart_count(db, user)})


@router.post("/shop/cart/remove")
async def cart_remove(request: Request, db: DB, user: AuthUser):
    form = await request.form()
    removed = await svc.remove_from_cart(db, user, _int(form.get("product_id")))
    await db.commit()
    return _json_or_redirect(request, _next(form, "/shop/cart"), "Removed from your bag." if removed else None,
                             data={"count": await svc.cart_count(db, user)})


# ====================================================================================================== checkout
async def _checkout_context(db: DB, user: User, plan: svc.CheckoutPlan, *, errors: dict[str, str] | None = None,
                            form: Any = None) -> dict[str, Any]:
    fee, free_from = svc.delivery_rule(plan.gym.currency)
    return {
        "plan": plan, "gym": plan.gym, "errors": errors or {}, "form": form or {}, "quote_fp": svc.quote_fingerprint(plan),
        "delivery_fee": fee, "free_from": free_from, "payments_mode": pay.payments_mode(),
        "cart_count": await svc.cart_count(db, user), "warnings": [(p, r) for p, r in plan.safety if r.warnings],
        "safety_tone": "danger" if any(r.tone == "danger" for _, r in plan.safety) else "warning",
    }


@router.get("/shop/checkout")
async def checkout_page(request: Request, db: DB, user: AuthUser):
    if not svc.can_shop(user):
        return redirect("/shop", "Shopping is available on member accounts.", "info")
    gym_id = _int(request.query_params.get("gym"))
    if not gym_id:
        lines = await svc.load_cart_lines(db, user)
        gyms = {ln.product.gym_id for ln in lines}
        if len(gyms) == 1:
            return redirect(f"/shop/checkout?gym={gyms.pop()}")
        return redirect("/shop/cart", "Choose which store to check out from." if gyms else "Your bag is empty.", "info")
    plan = await svc.build_checkout(db, user, gym_id, coupon_code=request.query_params.get("coupon", ""),
                                    fulfillment=request.query_params.get("fulfillment", ""))
    if plan is None:
        return redirect("/shop/cart", "There's nothing from that store in your bag.", "info")
    return render(request, "shop/checkout.html", await _checkout_context(db, user, plan))


@router.post("/shop/checkout")
async def checkout_submit(request: Request, db: DB, user: AuthUser):
    if not svc.can_shop(user):
        return redirect("/shop", "Shopping is available on member accounts.", "info")
    form = await request.form()
    gym_id = _int(form.get("gym"))
    fulfillment = str(form.get("fulfillment") or "")
    coupon = str(form.get("coupon") or "")
    plan = await svc.build_checkout(db, user, gym_id, coupon_code=coupon, fulfillment=fulfillment)
    if plan is None:
        return redirect("/shop/cart", "There's nothing from that store in your bag.", "info")
    # what the page showed (a client without it gets place_order's checks only); a bag line that became unavailable is
    # place_order's to report - nothing is placed then anyway
    shown = str(form.get("quote") or "")[:64]
    if shown and not plan.problems and shown != svc.quote_fingerprint(plan):  # not a secret: a plain comparison
        errors = {"cart": PRICE_CHANGED}
        if plan.coupon_code and plan.coupon is None:
            errors["coupon"] = plan.coupon_error or "That coupon can't be used."
        return render(request, "shop/checkout.html", await _checkout_context(db, user, plan, errors=errors, form=form),
                      status_code=409)
    try:
        order = await svc.place_order(db, user, plan, fulfillment=fulfillment, branch_id=_int(form.get("branch_id")) or None,
                                      form=form, safety_ack=str(form.get("safety_ack") or "") in ("1", "on"),
                                      ip=client_ip(request))
    except svc.CheckoutError as exc:  # raised before anything is written - re-render with the same plan
        return render(request, "shop/checkout.html", await _checkout_context(db, user, plan, errors=exc.errors, form=form),
                      status_code=400)
    await db.commit()
    try:
        url = await pay.start_payment(db, order, user, plan.gym)
    except pay.PaymentsUnavailable as exc:
        await db.commit()
        return redirect(f"/shop/orders/{order.id}", str(exc), "warning")
    await db.commit()
    return redirect(url)


@router.get("/shop/checkout/return")
async def checkout_return(request: Request, db: DB, user: AuthUser):
    number = (request.query_params.get("order_id") or "")[:24]
    order = None
    if number:
        order = await db.scalar(select(ShopOrder).where(ShopOrder.order_number == number, ShopOrder.customer_id == user.id))
    if order is None:
        return redirect("/shop/orders", "We couldn't find that order." if number else None, "warning")
    if order.paid_at is not None:
        return redirect(f"/shop/orders/{order.id}", "Payment received - thank you!", "success")
    state = await pay.sync_with_gateway(db, order)
    await db.commit()
    messages = {
        "paid": ("Payment received - thank you! Your order is confirmed.", "success"),
        "already_paid": ("Payment received - thank you!", "success"),
        "failed": ("The payment didn't go through and no money was taken. You can try again.", "error"),
        "pending": ("We're waiting for the payment provider to confirm your payment. Refresh in a moment.", "info"),
        "mismatch": ("We couldn't match your payment to this order. The store has been notified.", "warning"),
        "refunded_out_of_stock": ("An item sold out before your payment was confirmed, so the order was refunded.", "warning"),
        "refunded_cancelled": ("This order had been cancelled, so your payment is being refunded.", "warning"),
    }
    msg, cat = messages.get(state, ("We couldn't check the payment right now - we'll keep trying.", "warning"))
    return redirect(f"/shop/orders/{order.id}", msg, cat)


# ====================================================================================================== orders
async def _my_order(db: DB, order_id: int, user: User) -> ShopOrder:
    order = await svc.load_order(db, order_id, customer_id=user.id)
    if order is None:
        raise NotFound("Order not found.")
    return order


async def order_events(db: DB, order_id: int) -> list[AuditLog]:
    """The order's status events, oldest first (the timeline needs each status' first event), at most
    ORDER_EVENTS_LIMIT - an index range read of ix_audit_logs_entity."""
    return list((await db.execute(
        select(AuditLog).where(AuditLog.entity_type == "shop_order", AuditLog.entity_id == order_id)
        .order_by(AuditLog.created_at, AuditLog.id).limit(ORDER_EVENTS_LIMIT)
    )).scalars().all())


@router.get("/shop/orders")
async def orders_page(request: Request, db: DB, user: AuthUser):
    page = await svc.customer_orders(db, user, parse_page(request).page)  # 20 per page: older orders stay reachable
    covers = await svc.order_covers(db, [i.product_id for o in page.items for i in o.items[:4]])
    return render(request, "shop/orders.html", {"orders": page.items, "page": page, "covers": covers,
                                                "cart_count": await _cart_count(db, user),
                                                "status_info": svc.ORDER_STATUS_INFO})


@router.get("/shop/orders/{order_id}")
async def order_page(order_id: int, request: Request, db: DB, user: AuthUser):
    order = await _my_order(db, order_id, user)
    events = await order_events(db, order.id)
    covers = await svc.order_covers(db, [i.product_id for i in order.items])
    shipping_notes = [ln[len("Shipped: "):] for ln in (order.notes or "").splitlines() if ln.startswith("Shipped: ")]
    flagged = pay.MISMATCH_MARK in (order.notes or "")
    return render(request, "shop/order.html", {
        "order": order, "items": order.items, "quote": svc.quote_for_order(order, order.items),
        "timeline": svc.order_timeline(order, events), "covers": covers, "refundable": svc.refundable_items(order),
        "refund_problem": svc.refund_request_problem(order), "refund_window_ends": svc.refund_window_ends(order),
        "can_cancel": order.status in (OrderStatus.PAID, OrderStatus.PENDING_PAYMENT, OrderStatus.PAYMENT_FAILED),
        "can_pay": order.status in svc.UNPAID_STATUSES and not flagged, "under_review": flagged,
        "payments_mode": pay.payments_mode(), "shipping_notes": shipping_notes, "status_info": svc.ORDER_STATUS_INFO,
        "refund_info": svc.REFUND_STATUS_INFO, "cart_count": await _cart_count(db, user),
        "window_days": svc.REFUND_WINDOW_DAYS,
    })


@router.get("/shop/orders/{order_id}/pay")
async def order_pay_page(order_id: int, request: Request, db: DB, user: AuthUser):
    order = await _my_order(db, order_id, user)
    if order.status not in svc.UNPAID_STATUSES:
        return redirect(f"/shop/orders/{order.id}")
    if order.payment_provider == "test" or pay.payments_mode() == "test":
        return redirect(f"/shop/orders/{order.id}/test-gateway")
    if order.payment_provider != "cashfree" or not order.payment_session_id:
        return redirect(f"/shop/orders/{order.id}", "Start the payment from your order page.", "info")
    return render(request, "shop/pay.html", {"order": order, "sdk_mode": pay.sdk_mode()})


@router.post("/shop/orders/{order_id}/pay")
async def order_pay_retry(order_id: int, db: DB, user: AuthUser):
    order = await svc.load_order(db, order_id, customer_id=user.id, lock=True)  # the stock hold is decided under it
    if order is None:
        raise NotFound("Order not found.")
    if order.status not in svc.UNPAID_STATUSES:
        return redirect(f"/shop/orders/{order.id}", "This order doesn't need a payment.", "info")
    if pay.MISMATCH_MARK in (order.notes or ""):
        return redirect(f"/shop/orders/{order.id}", "This order's payment is under review by the store.", "warning")
    # no money moves for units that are no longer there: a lapsed hold is taken again while the units are free
    gone = await svc.renew_stock_hold(db, order)
    if gone:
        order.status, order.cancelled_at, order.stock_held_until = OrderStatus.CANCELLED, utcnow(), None
        await audit(db, user, "shop.order.cancel", "shop_order", order.id, gym_id=order.gym_id,
                    detail={"from": "unpaid", "to": "cancelled", "by_owner": False, "reason": "sold out before payment",
                            "items": gone[:10]})
        await db.commit()
        names = ", ".join(gone[:3]) + (f" and {len(gone) - 3} more" if len(gone) > 3 else "")
        return redirect(f"/shop/orders/{order.id}", f"Sorry - {names} sold out while this order was waiting for "
                        "payment, so the order was cancelled. Nothing was charged.", "warning")
    if order.status == OrderStatus.PAYMENT_FAILED:
        order.status = OrderStatus.PENDING_PAYMENT
    try:
        url = await pay.start_payment(db, order, user, order.gym)
    except pay.PaymentsUnavailable as exc:
        await db.commit()
        return redirect(f"/shop/orders/{order.id}", str(exc), "warning")
    await db.commit()
    return redirect(url)


@router.get("/shop/orders/{order_id}/test-gateway")
async def test_gateway_page(order_id: int, request: Request, db: DB, user: AuthUser):
    if not get_settings().payments_test_mode:
        raise NotFound()
    order = await _my_order(db, order_id, user)
    if order.status not in svc.UNPAID_STATUSES or order.payment_provider != "test":
        return redirect(f"/shop/orders/{order.id}")
    return render(request, "shop/test_gateway.html", {"order": order, "items": order.items})


@router.post("/shop/orders/{order_id}/test-gateway")
async def test_gateway_submit(order_id: int, request: Request, db: DB, user: AuthUser):
    if not get_settings().payments_test_mode:
        raise NotFound()
    order = await _my_order(db, order_id, user)
    form = await request.form()
    if order.status not in svc.UNPAID_STATUSES or order.payment_provider != "test":
        return redirect(f"/shop/orders/{order.id}", "This order isn't waiting for a payment.", "info")
    if form.get("decision") == "approve":
        state = await pay.handle_payment_success(db, order.order_number, order.total, order.currency,
                                                 f"TEST-PAY-{secrets.token_hex(5).upper()}", source="test")
    else:
        state = await pay.handle_payment_failure(db, order.order_number, reason="declined on the test gateway", source="test")
    await db.commit()
    messages = {
        "paid": ("Payment approved (test mode). Your order is confirmed!", "success"),
        "failed": ("Payment declined (test mode). No money was taken - you can try again.", "error"),
        "refunded_out_of_stock": ("An item sold out before the payment was confirmed - the order was refunded.", "warning"),
        "refunded_cancelled": ("This order had been cancelled - the payment was refunded.", "warning"),
    }
    msg, cat = messages.get(state, (None, "info"))
    return redirect(f"/shop/orders/{order.id}", msg, cat)


@router.post("/shop/orders/{order_id}/cancel")
async def order_cancel(order_id: int, request: Request, db: DB, user: AuthUser):
    order = await _my_order(db, order_id, user)
    try:
        outcome = await pay.cancel_order(db, order_id, user, by_owner=False, request=request)
    except svc.ShopError as exc:  # validation errors happen before any write
        return redirect(f"/shop/orders/{order_id}", str(exc), "error")
    await db.commit()
    if outcome in ("already_cancelled", "already_refunded"):  # a second tab, a resubmit: say what really happened
        return redirect(f"/shop/orders/{order_id}", already_closed_message(order, outcome), "info")
    if outcome == "refund_failed":
        return redirect(f"/shop/orders/{order_id}", "The refund couldn't be processed, so the order was not cancelled. "
                        "The store has been notified.", "error")
    msg = "Order cancelled - your refund is on its way." if outcome == "refunded" else "Order cancelled."
    return redirect(f"/shop/orders/{order_id}", msg, "success")


@router.post("/shop/orders/{order_id}/refund-request")
async def order_refund_request(order_id: int, request: Request, db: DB, user: AuthUser):
    order = await _my_order(db, order_id, user)
    form = await request.form()
    try:
        quantities = pay.parse_quantities(form, order.items)
        await pay.request_refund(db, order.id, user, quantities, str(form.get("reason") or ""))
    except svc.ShopError as exc:
        return redirect(f"/shop/orders/{order_id}#refund", str(exc), "error")
    await db.commit()
    return redirect(f"/shop/orders/{order.id}", "Refund requested - the store will review it shortly.", "success")


@router.get("/shop/orders/{order_id}/receipt")
async def order_receipt(order_id: int, request: Request, db: DB, user: AuthUser):
    order = await svc.load_order(db, order_id, customer_id=user.id)
    if order is None:
        order = await svc.load_order(db, order_id, owner_id=user.id)
    if order is None:
        raise NotFound("Order not found.")
    if order.paid_at is None:
        return redirect(f"/shop/orders/{order.id}", "A receipt is available once the order is paid.", "info")
    back = f"/shop/orders/{order.id}" if order.customer_id == user.id else f"/owner/shop/orders/{order.id}"
    return render(request, "shop/receipt.html", {"order": order, "items": order.items, "back": back,
                                                 "quote": svc.quote_for_order(order, order.items)})


# ====================================================================================================== reviews
@router.post("/shop/p/{product_id}/review")
async def review_submit(product_id: int, request: Request, db: DB, user: AuthUser):
    product = await svc.load_product(db, product_id)
    if product is None or not svc.is_public(product):
        raise NotFound("Product not found.")
    form = await request.form()
    back = svc.product_url(product) + "#reviews"
    try:
        await svc.save_review(db, user, product, rating=_int(form.get("rating")), title=str(form.get("title") or ""),
                              body=str(form.get("body") or ""))
    except svc.ShopError as exc:
        return redirect(back, str(exc), "error")
    await db.commit()
    return redirect(back, "Thanks for your review!", "success")
