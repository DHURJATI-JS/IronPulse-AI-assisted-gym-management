"""WP-L shop payments, order lifecycle and refunds.

* Cashfree PG through WP-D's client (``app.services.cashfree``), imported lazily by ``_cashfree()``: if that module
  is missing or broken, online payments are *unavailable* - unless TEST MODE (no Cashfree keys), which needs no
  gateway and uses the clearly labelled simulated gateway at ``/shop/orders/{id}/test-gateway``.
* ``start_payment``          -> redirect URL (Cashfree JS SDK page or the TEST MODE gateway)
* ``handle_payment_success`` -> idempotent fulfilment: ``SELECT ... FOR UPDATE`` on the order, amount + currency
                                must equal the order total (else flagged, never fulfilled), stock decrement
                                (insufficient stock -> automatic full refund + cancel), coupon use, SHOP_SALE ledger
                                row, purchased cart lines cleared, customer + owner notified.
* ``handle_payment_failure`` -> PAYMENT_FAILED + notify.
* ``sync_with_gateway``      -> server-side verification with ``get_order`` (return URL, retries, stale sweep).
* refunds: ``request_refund`` (customer, 7-day window), ``approve_refund`` / ``reject_refund`` / ``issue_refund``
  (owner), ``cancel_order`` (customer before processing / owner) -> Cashfree refund
  ``POST {pg_base}/orders/{order_id}/refunds`` (or TEST MODE simulation), REFUND ledger row, optional restock.

Shared webhook contract (WP-D owns ``POST /payments/cashfree/webhook``): for order ids starting with ``IPO-`` call
``await handle_payment_success(db, order_id, order_amount, order_currency, cf_payment_id)`` on PAYMENT_SUCCESS and
``await handle_payment_failure(db, order_id, reason=...)`` on PAYMENT_FAILED / USER_DROPPED, then ``await db.commit()``.
Both are idempotent and safe under concurrent deliveries. Functions here never commit; the caller does.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import quote

import httpx
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.utils import clamp, money, utcnow
from app.db import session_scope
from app.models import (
    Branch,
    CartItem,
    Coupon,
    Gym,
    InventoryMovement,
    Product,
    Refund,
    ShopOrder,
    ShopOrderItem,
    Transaction,
    User,
)
from app.models.enums import NotificationCategory, Severity, TxCategory, TxDirection, TxStatus
from app.models.shop import OrderStatus, RefundStatus
from app.services.audit import audit
from app.services.batching import Chunks
from app.services.jobs import periodic
from app.services.notify import notify
from app.services.shop import (
    OWNER_CANCELLABLE,
    PAID_STATUSES,
    UNPAID_STATUSES,
    UNPAID_TTL_HOURS,
    ShopError,
    held_units,
    holds_stock,
    q2,
    refund_request_problem,
    refundable_items,
    unit_refund_amount,
)

log = logging.getLogger("ironpulse.shop.payments")

MISMATCH_MARK = "[payment-review]"
REFUND_TIMEOUT = httpx.Timeout(20.0, connect=8.0)


class PaymentsUnavailable(RuntimeError):
    """Online payments can't be started right now (message is safe to show)."""


# ====================================================================================================== gateway adapter
def _cashfree() -> Any | None:
    """WP-D's Cashfree client, imported lazily so a missing/broken module never breaks the shop."""
    try:
        from app.services import cashfree  # noqa: PLC0415
    except Exception as exc:  # ImportError or a half-written module
        log.warning("Cashfree client unavailable: %s", exc.__class__.__name__)
        return None
    if not all(hasattr(cashfree, name) for name in ("create_order", "get_order")):
        return None
    return cashfree


def payments_mode() -> str:
    """``test`` (no Cashfree keys: simulated gateway) | ``cashfree`` | ``unavailable``."""
    s = get_settings()
    if s.payments_test_mode:
        return "test"
    return "cashfree" if _cashfree() is not None else "unavailable"


def sdk_mode() -> str:
    return "production" if get_settings().cashfree_env == "production" else "sandbox"


async def start_payment(db: AsyncSession, order: ShopOrder, user: User, gym: Gym) -> str:
    """Attach a payment session to a PENDING_PAYMENT order and return where the browser goes next."""
    mode = payments_mode()
    if mode == "test":
        order.payment_provider = "test"
        order.provider_order_id = order.order_number
        return f"/shop/orders/{order.id}/test-gateway"
    if mode == "unavailable":
        raise PaymentsUnavailable("Online payments are temporarily unavailable. Your bag is saved - please try again shortly.")
    cf = _cashfree()
    s = get_settings()
    if order.payment_provider == "cashfree" and order.payment_session_id:
        state = await sync_with_gateway(db, order)
        if state == "pending":
            return f"/shop/orders/{order.id}/pay"
        if state in ("paid", "already_paid"):
            return f"/shop/orders/{order.id}"
        raise PaymentsUnavailable("This payment session has expired. Please place the order again from your bag.")
    phone = user.phone or (order.shipping_address or {}).get("phone")
    try:
        result = await cf.create_order(
            order_id=order.order_number, amount=order.total, currency=order.currency, customer_id=f"ipc{user.id}",
            customer_email=user.email, customer_phone=phone, customer_name=user.full_name,
            return_url=f"{s.public_base_url}/shop/checkout/return?order_id={order.order_number}",
            notify_url=f"{s.public_base_url}/payments/cashfree/webhook",
            note=f"{gym.name} shop order {order.order_number}",
        )
    except Exception as exc:
        log.warning("Cashfree create_order failed for %s: %s", order.order_number, exc.__class__.__name__)
        message = str(exc) if exc.__class__.__name__.startswith("Cashfree") else ""
        raise PaymentsUnavailable(message or "The payment provider could not be reached. Please try again.") from exc
    order.payment_provider = "cashfree"
    order.provider_order_id = order.order_number
    order.payment_session_id = result.payment_session_id
    return f"/shop/orders/{order.id}/pay"


async def sync_with_gateway(db: AsyncSession, order: ShopOrder) -> str:
    """Ask Cashfree (never the browser) whether an order was paid, and apply the verified outcome."""
    cf = _cashfree()
    if cf is None or order.payment_provider != "cashfree":
        return "unavailable"
    try:
        res = await cf.get_order(order.order_number)
    except Exception as exc:
        log.warning("Cashfree get_order failed for %s: %s", order.order_number, exc.__class__.__name__)
        return "unavailable"
    if res.is_paid:
        ref = f"cf_order:{res.cf_order_id}" if getattr(res, "cf_order_id", None) else None
        return await handle_payment_success(db, order.order_number, res.amount, res.currency, ref, source="gateway")
    if res.is_dead:
        return await handle_payment_failure(db, order.order_number, reason=f"gateway order {res.order_status.lower()}")
    return "pending"


# ====================================================================================================== payment outcomes
async def _lock_order(db: AsyncSession, order_number: str) -> ShopOrder | None:
    return await db.scalar(
        select(ShopOrder).where(ShopOrder.order_number == order_number).with_for_update()
        .execution_options(populate_existing=True)
    )


async def _items(db: AsyncSession, order_id: int) -> list[ShopOrderItem]:
    return list((await db.execute(
        select(ShopOrderItem).where(ShopOrderItem.order_id == order_id).order_by(ShopOrderItem.id)
        .execution_options(populate_existing=True)
    )).scalars().all())


async def _lock_products(db: AsyncSession, ids: Sequence[int | None]) -> dict[int, Product]:
    wanted = sorted({i for i in ids if i})
    if not wanted:
        return {}
    rows = (await db.execute(
        select(Product).where(Product.id.in_(wanted)).order_by(Product.id).with_for_update()
        .execution_options(populate_existing=True)
    )).scalars().all()
    return {p.id: p for p in rows}


async def _gym(db: AsyncSession, gym_id: int | None) -> Gym | None:
    return await db.get(Gym, gym_id) if gym_id else None


def _sale_tx(order: ShopOrder, gym_name: str, payment_id: str | None) -> Transaction:
    now = utcnow()
    return Transaction(
        gym_id=order.gym_id, direction=TxDirection.INCOMING, category=TxCategory.SHOP_SALE, status=TxStatus.SUCCESS,
        amount=order.total, currency=order.currency,
        description=clamp(f"Shop order {order.order_number} · {gym_name}", 255),
        party_label=order.customer_label, provider=order.payment_provider or "test",
        provider_order_id=order.order_number, provider_payment_id=clamp(payment_id, 64) if payment_id else None,
        customer_id=order.customer_id, completed_at=now,
        meta={"shop_order_id": order.id, "order_number": order.order_number, "kind": "shop_sale"},
    )


async def handle_payment_success(
    db: AsyncSession,
    order_number: str,
    amount: Any,
    currency: str | None,
    provider_payment_id: str | None,
    *,
    source: str = "webhook",
) -> str:
    """Apply a VERIFIED payment. Idempotent. Returns paid | already_paid | mismatch | refunded_out_of_stock |
    refunded_cancelled | not_found. The caller commits."""
    order = await _lock_order(db, order_number)
    if order is None:
        log.warning("payment for unknown shop order %s (%s)", order_number, source)
        return "not_found"
    if order.paid_at is not None or order.status in PAID_STATUSES:
        return "already_paid"
    try:
        paid = q2(amount)
    except ValueError:
        paid = None
    if paid is None or paid != q2(order.total) or (currency or "").upper() != order.currency.upper():
        return await _flag_mismatch(db, order, amount, currency, provider_payment_id, source)

    gym = await _gym(db, order.gym_id)
    gym_name = gym.name if gym else "Gym shop"
    items = await _items(db, order.id)
    was_cancelled = order.status == OrderStatus.CANCELLED
    held_here = holds_stock(order)  # its units are still held for it (services/shop.py "stock holds")
    order.stock_held_until = None  # the hold ends here, whatever happens next
    order.paid_at = utcnow()
    order.payment_provider = order.payment_provider or ("cashfree" if source in ("webhook", "gateway") else "test")
    order.status = OrderStatus.PAID
    sale = _sale_tx(order, gym_name, provider_payment_id)
    db.add(sale)
    await db.flush()

    if was_cancelled:
        await _auto_refund(db, order, items, gym, "The order had been cancelled before your payment arrived.")
        return "refunded_cancelled"

    products = await _lock_products(db, [i.product_id for i in items])
    # a live hold converts into the sale (only an owner's stock cut can make it short); a lapsed one may only take units
    # no other open checkout holds - else this payment is refunded rather than another buyer's held units sold twice
    others = {} if held_here else await held_units(db, products, exclude_order_id=order.id)
    short = [i for i in items if i.product_id not in products
             or products[i.product_id].stock - others.get(i.product_id, 0) < i.quantity]
    if short:
        names = ", ".join(i.product_name for i in short[:3])
        await _auto_refund(db, order, items, gym, f"Sorry - {names} sold out before your payment was confirmed.")
        return "refunded_out_of_stock"

    low_notes = []
    for item in items:
        product = products[item.product_id]
        before = product.stock
        product.stock = before - item.quantity
        db.add(InventoryMovement(product_id=product.id, change=-item.quantity, reason="sale", order_id=order.id))
        if product.stock <= product.low_stock_threshold < before:
            low_notes.append(f"{product.name} ({product.stock} left)")
    if order.coupon_code and order.gym_id:
        coupon = await db.scalar(select(Coupon).where(Coupon.gym_id == order.gym_id, Coupon.code == order.coupon_code)
                                 .with_for_update())
        if coupon is not None:
            coupon.used_count += 1
    if order.customer_id:
        await db.execute(delete(CartItem).where(CartItem.user_id == order.customer_id,
                                                CartItem.product_id.in_([i.product_id for i in items if i.product_id])))
    await _notify_paid(db, order, items, gym, low_notes)
    return "paid"


async def _notify_paid(db: AsyncSession, order: ShopOrder, items: Sequence[ShopOrderItem], gym: Gym | None,
                       low_notes: list[str]) -> None:
    units = sum(i.quantity for i in items)
    gym_name = gym.name if gym else "the gym"
    if order.fulfillment == "pickup":
        branch = await db.get(Branch, order.pickup_branch_id) if order.pickup_branch_id else None
        how = f"We'll let you know when it's ready for pickup{f' at {branch.name}' if branch else ''}."
    else:
        how = "We'll let you know when it ships."
    if order.customer_id:
        await notify(db, order.customer_id, "Order confirmed",
                     f"Thanks! {gym_name} received your payment of {money(order.total, order.currency)} for order "
                     f"{order.order_number} ({units} item{'s' if units != 1 else ''}). {how}",
                     category=NotificationCategory.PAYMENT, severity=Severity.SUCCESS, link=f"/shop/orders/{order.id}")
    if gym is not None:
        extra = f" Low stock now: {', '.join(low_notes)}." if low_notes else ""
        who = order.customer_label.split(" <")[0]
        await notify(db, gym.owner_id, "New shop order",
                     f"{who} paid {money(order.total, order.currency)} for {units} item{'s' if units != 1 else ''} "
                     f"(order {order.order_number}, {'pickup' if order.fulfillment == 'pickup' else 'delivery'}).{extra}",
                     category=NotificationCategory.PAYMENT, severity=Severity.SUCCESS, link=f"/owner/shop/orders/{order.id}")


async def _flag_mismatch(db: AsyncSession, order: ShopOrder, amount: Any, currency: str | None,
                         payment_id: str | None, source: str) -> str:
    if MISMATCH_MARK in (order.notes or ""):
        return "mismatch"
    line = (f"{MISMATCH_MARK} {utcnow():%Y-%m-%d %H:%M} UTC: gateway reported {amount} {currency or '?'} "
            f"but the order total is {order.total} {order.currency} (payment {payment_id or 'n/a'}, via {source}). "
            "Not fulfilled - verify in the payment dashboard and refund it there.")
    order.notes = (order.notes + "\n" if order.notes else "") + line
    order.stock_held_until = None
    if order.status != OrderStatus.CANCELLED:
        order.status = OrderStatus.PAYMENT_FAILED
    await audit(db, None, "shop.payment.mismatch", "shop_order", order.id, gym_id=order.gym_id,
                detail={"expected": str(order.total), "currency": order.currency, "reported": str(amount),
                        "reported_currency": currency, "payment_id": payment_id, "source": source})
    gym = await _gym(db, order.gym_id)
    if gym is not None:
        await notify(db, gym.owner_id, "Shop payment needs review",
                     f"A payment for order {order.order_number} didn't match the order total "
                     f"({money(order.total, order.currency)}). The order was NOT fulfilled - please check it.",
                     category=NotificationCategory.SECURITY, severity=Severity.DANGER, link=f"/owner/shop/orders/{order.id}")
    if order.customer_id:
        await notify(db, order.customer_id, "We couldn't confirm your payment",
                     f"Your payment for order {order.order_number} couldn't be matched to the order total, so it isn't "
                     "being prepared yet. The store has been notified and will contact you.",
                     category=NotificationCategory.PAYMENT, severity=Severity.WARNING, link=f"/shop/orders/{order.id}")
    return "mismatch"


async def handle_payment_failure(db: AsyncSession, order_number: str, *, reason: str = "", source: str = "webhook") -> str:
    """Payment declined / dropped. Idempotent; never touches paid orders. Returns failed | ignored | not_found."""
    order = await _lock_order(db, order_number)
    if order is None:
        return "not_found"
    if order.status != OrderStatus.PENDING_PAYMENT:
        return "ignored"
    order.status = OrderStatus.PAYMENT_FAILED
    order.stock_held_until = None  # released for other buyers; "Pay again" takes the units again while they're free
    if order.customer_id:
        await notify(db, order.customer_id, "Payment failed",
                     f"Your payment of {money(order.total, order.currency)} for order {order.order_number} didn't go "
                     "through and no money was taken. You can try again from the order page.",
                     category=NotificationCategory.PAYMENT, severity=Severity.WARNING, link=f"/shop/orders/{order.id}")
    log.info("shop order %s payment failed (%s): %s", order_number, source, reason)
    return "failed"


# ====================================================================================================== refunds
@dataclass(slots=True)
class GatewayRefund:
    ok: bool
    refund_id: str | None
    error: str | None = None
    status: str | None = None
    simulated: bool = False


_refund_transport: httpx.AsyncBaseTransport | None = None


def use_refund_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    """Route Cashfree refund calls through ``transport`` (tests: ``httpx.MockTransport``)."""
    global _refund_transport
    _refund_transport = transport


async def cashfree_refund(order_id: str, refund_id: str, amount: Decimal, note: str) -> GatewayRefund:
    """POST {pg_base}/orders/{order_id}/refunds  {"refund_amount", "refund_id", "refund_note"}."""
    s = get_settings()
    if not s.cashfree_enabled:
        return GatewayRefund(False, None, "Cashfree keys are not configured - refund this payment in the Cashfree dashboard.")
    headers = {
        "x-client-id": str(s.cashfree_app_id), "x-client-secret": str(s.cashfree_secret_key),
        "x-api-version": s.cashfree_api_version, "Content-Type": "application/json", "Accept": "application/json",
    }
    payload = {"refund_amount": float(q2(amount)), "refund_id": refund_id, "refund_note": clamp(note, 100) or "Refund"}
    url = f"{s.cashfree_pg_base}/orders/{quote(order_id, safe='')}/refunds"
    try:
        async with httpx.AsyncClient(timeout=REFUND_TIMEOUT, transport=_refund_transport) as client:
            response = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        log.warning("Cashfree refund %s failed: %s", refund_id, exc.__class__.__name__)
        return GatewayRefund(False, None, "The payment provider could not be reached. Please try again.")
    try:
        data = response.json()
    except ValueError:
        data = {}
    data = data if isinstance(data, dict) else {}
    if response.status_code >= 400:
        message = data.get("message") if isinstance(data.get("message"), str) else None
        return GatewayRefund(False, None, message or f"The payment provider rejected the refund ({response.status_code}).")
    status = str(data.get("refund_status") or "PENDING").upper()
    if status in ("CANCELLED", "FAILED", "REJECTED"):
        return GatewayRefund(False, None, f"The payment provider reported the refund as {status.lower()}.", status)
    ref = data.get("cf_refund_id") or data.get("refund_id") or refund_id
    return GatewayRefund(True, clamp(str(ref), 64), None, status)


async def gateway_refund(order: ShopOrder, refund: Refund, amount: Decimal) -> GatewayRefund:
    """Refund through the provider that took the payment (TEST MODE payments are refunded by simulation)."""
    if order.payment_provider == "cashfree":
        return await cashfree_refund(order.order_number, f"RF-{order.order_number}-{refund.id}", amount,
                                     f"Refund for order {order.order_number}")
    return GatewayRefund(True, f"TEST-RF-{secrets.token_hex(5).upper()}", None, "SUCCESS", simulated=True)


def _post_completion_status(order: ShopOrder) -> OrderStatus:
    if order.refunded_total >= order.total:
        return OrderStatus.REFUNDED
    if order.refunded_total > 0:
        return OrderStatus.PARTIALLY_REFUNDED
    return OrderStatus.COMPLETED if order.completed_at else OrderStatus.PAID


def _refund_amount(order: ShopOrder, items: Sequence[ShopOrderItem], quantities: dict[int, int]) -> Decimal:
    amount = sum((unit_refund_amount(i, quantities.get(i.id, 0)) for i in items), q2(0))
    all_units = all(i.refunded_quantity + quantities.get(i.id, 0) >= i.quantity for i in items)
    if all_units:  # everything returned -> refund the remaining balance (delivery fee, rounding residue)
        amount = order.total - order.refunded_total
    return q2(amount)


async def _execute_refund(
    db: AsyncSession, order: ShopOrder, items: Sequence[ShopOrderItem], refund: Refund, *,
    quantities: dict[int, int], restock: bool, actor: User | None, final_status: OrderStatus | None = None,
    movement_reason: str = "refund_restock", notify_customer: bool = True,
) -> GatewayRefund:
    remaining = order.total - order.refunded_total
    amount = q2(refund.amount)
    if amount <= 0 or amount > remaining:
        raise ShopError(f"The refund must be between {money(0.01, order.currency)} and {money(remaining, order.currency)}.")
    refund.status = RefundStatus.PROCESSING
    result = await gateway_refund(order, refund, amount)
    now = utcnow()
    refund.processed_at = now
    if actor is not None:
        refund.decided_by_id = actor.id
    if not result.ok:
        refund.status = RefundStatus.FAILED
        refund.decision_note = clamp(f"{refund.decision_note + ' · ' if refund.decision_note else ''}Gateway: {result.error}", 1000)
        gym = await _gym(db, order.gym_id)
        if gym is not None:
            await notify(db, gym.owner_id, "Refund failed",
                         f"The refund of {money(amount, order.currency)} for order {order.order_number} failed: {result.error}",
                         category=NotificationCategory.PAYMENT, severity=Severity.DANGER, link=f"/owner/shop/orders/{order.id}")
        return result
    refund.status = RefundStatus.COMPLETED
    refund.provider_refund_id = result.refund_id
    refund.restock = restock
    refund.items = [{"order_item_id": i.id, "quantity": quantities[i.id]} for i in items if quantities.get(i.id)]
    for item in items:
        item.refunded_quantity = min(item.quantity, item.refunded_quantity + quantities.get(item.id, 0))
    if restock:
        products = await _lock_products(db, [i.product_id for i in items if quantities.get(i.id)])
        for item in items:
            q = quantities.get(item.id, 0)
            product = products.get(item.product_id) if item.product_id else None
            if q and product is not None:
                product.stock += q
                db.add(InventoryMovement(product_id=product.id, change=q, reason=movement_reason, order_id=order.id,
                                         actor_id=actor.id if actor else None, note=f"Refund {refund.id}"))
    order.refunded_total = q2(order.refunded_total + amount)
    order.status = final_status or _post_completion_status(order)
    tx = Transaction(
        gym_id=order.gym_id, direction=TxDirection.OUTGOING, category=TxCategory.REFUND, status=TxStatus.SUCCESS,
        amount=amount, currency=order.currency,
        description=clamp(f"Refund for shop order {order.order_number}", 255), party_label=order.customer_label,
        provider=order.payment_provider or "test", provider_reference=clamp(result.refund_id, 80),
        customer_id=order.customer_id, created_by_id=actor.id if actor else None, completed_at=now,
        meta={"shop_order_id": order.id, "order_number": order.order_number, "refund_id": refund.id,
              "kind": "shop_refund", "simulated": result.simulated, "gateway_status": result.status},
    )
    db.add(tx)
    await db.flush()
    refund.transaction_id = tx.id
    if notify_customer and order.customer_id:
        await notify(db, order.customer_id, "Refund processed",
                     f"{money(amount, order.currency)} for order {order.order_number} is on its way back to your original "
                     "payment method (banks usually take 5-7 working days).",
                     category=NotificationCategory.PAYMENT, severity=Severity.SUCCESS, link=f"/shop/orders/{order.id}")
    return result


async def _auto_refund(db: AsyncSession, order: ShopOrder, items: Sequence[ShopOrderItem], gym: Gym | None, reason: str) -> None:
    """Paid but can't be fulfilled -> refund everything, cancel, tell both sides."""
    refund = Refund(order_id=order.id, amount=q2(order.total - order.refunded_total), reason=reason,
                    items=[{"order_item_id": i.id, "quantity": i.quantity - i.refunded_quantity} for i in items],
                    restock=False, status=RefundStatus.APPROVED, decision_note="Automatic refund")
    db.add(refund)
    await db.flush()
    quantities = {i.id: i.quantity - i.refunded_quantity for i in items}
    result = await _execute_refund(db, order, items, refund, quantities=quantities, restock=False, actor=None,
                                   final_status=OrderStatus.CANCELLED, notify_customer=False)
    order.status = OrderStatus.CANCELLED
    order.cancelled_at = utcnow()
    if order.customer_id:
        tail = ("A full refund of " + money(refund.amount, order.currency) + " is on its way.") if result.ok else \
            "The store will refund you shortly."
        await notify(db, order.customer_id, "Order cancelled - refund issued",
                     f"{reason} Order {order.order_number} was cancelled. {tail}",
                     category=NotificationCategory.PAYMENT, severity=Severity.WARNING, link=f"/shop/orders/{order.id}")
    if gym is not None:
        await notify(db, gym.owner_id, "Shop order auto-refunded",
                     f"Order {order.order_number} was paid but couldn't be fulfilled ({reason}) and was "
                     f"{'refunded automatically' if result.ok else 'cancelled - the automatic refund FAILED, refund it manually'}.",
                     category=NotificationCategory.PAYMENT, severity=Severity.WARNING if result.ok else Severity.DANGER,
                     link=f"/owner/shop/orders/{order.id}")


def parse_quantities(form: Any, items: Sequence[ShopOrderItem], *, prefix: str = "qty_") -> dict[int, int]:
    out: dict[int, int] = {}
    for item in items:
        raw = form.get(f"{prefix}{item.id}")
        if raw in (None, ""):
            continue
        try:
            value = int(str(raw).strip())
        except ValueError as exc:
            raise ShopError(f"Enter a whole number of units for {item.product_name}.") from exc
        if value < 0:
            raise ShopError("Quantities can't be negative.")
        if value:
            out[item.id] = value
    return out


async def lock_order_for_update(db: AsyncSession, order_id: int) -> tuple[ShopOrder, list[ShopOrderItem], list[Refund]]:
    order = await db.scalar(select(ShopOrder).where(ShopOrder.id == order_id).with_for_update()
                            .execution_options(populate_existing=True))
    if order is None:
        raise ShopError("Order not found.")
    items = await _items(db, order.id)
    refunds = list((await db.execute(select(Refund).where(Refund.order_id == order.id).order_by(Refund.id)
                                     .with_for_update().execution_options(populate_existing=True))).scalars().all())
    return order, items, refunds


async def request_refund(db: AsyncSession, order_id: int, user: User, quantities: dict[int, int], reason: str) -> Refund:
    """Customer refund/return request (within 7 days of completion)."""
    order, items, refunds = await lock_order_for_update(db, order_id)
    if order.customer_id != user.id:
        raise ShopError("Order not found.")
    problem = refund_request_problem(order, items=items, refunds=refunds)
    if problem:
        raise ShopError(problem)
    reason = " ".join((reason or "").split())
    if len(reason) < 10:
        raise ShopError("Please describe the reason in at least 10 characters.")
    available = {item.id: left for item, left in refundable_items(order, items, refunds)}
    chosen = {iid: q for iid, q in quantities.items() if q > 0}
    if not chosen:
        raise ShopError("Choose at least one item to return.")
    for iid, q in chosen.items():
        if iid not in available or q > available[iid]:
            raise ShopError("One of the quantities is more than you can return.")
    amount = _refund_amount(order, items, chosen)
    refund = Refund(order_id=order.id, amount=amount, reason=clamp(reason, 1000),
                    items=[{"order_item_id": iid, "quantity": q} for iid, q in chosen.items()], restock=True,
                    status=RefundStatus.REQUESTED, requested_by_id=user.id)
    db.add(refund)
    order.status = OrderStatus.REFUND_REQUESTED
    await db.flush()
    gym = await _gym(db, order.gym_id)
    if gym is not None:
        await notify(db, gym.owner_id, "Refund requested",
                     f"{order.customer_label.split(' <')[0]} asked for a refund of {money(amount, order.currency)} on order "
                     f"{order.order_number}: “{clamp(reason, 140)}”",
                     category=NotificationCategory.PAYMENT, severity=Severity.WARNING, link=f"/owner/shop/orders/{order.id}")
    await audit(db, user, "shop.refund.request", "shop_order", order.id, gym_id=order.gym_id,
                detail={"refund_id": refund.id, "amount": str(amount), "items": refund.items})
    return refund


async def _lock_refund(db: AsyncSession, refund_id: int, owner: User) -> tuple[Refund, ShopOrder, list[ShopOrderItem], list[Refund]]:
    row = await db.scalar(select(Refund.order_id).join(ShopOrder, ShopOrder.id == Refund.order_id)
                          .join(Gym, Gym.id == ShopOrder.gym_id).where(Refund.id == refund_id, Gym.owner_id == owner.id))
    if row is None:
        raise LookupError("refund not found")
    order, items, refunds = await lock_order_for_update(db, row)
    refund = next(r for r in refunds if r.id == refund_id)
    return refund, order, items, refunds


async def approve_refund(db: AsyncSession, refund_id: int, owner: User, *, quantities: dict[int, int] | None,
                         restock: bool, full: bool = False, note: str = "", request: Any = None) -> GatewayRefund:
    """Owner approves a requested refund (as requested, partial quantities, or everything remaining)."""
    refund, order, items, refunds = await _lock_refund(db, refund_id, owner)
    if refund.status != RefundStatus.REQUESTED:
        raise ShopError(f"This refund was already {refund.status.value.replace('_', ' ')}.")
    others_open = [r for r in refunds if r.id != refund.id and r.status in (RefundStatus.REQUESTED, RefundStatus.PROCESSING)]
    if others_open:
        raise ShopError("Another refund on this order is still open - decide that one first.")
    left = {i.id: i.quantity - i.refunded_quantity for i in items}
    if full:
        chosen = {iid: q for iid, q in left.items() if q > 0}
    elif quantities is None:
        chosen = {}
        for entry in refund.items or []:
            try:
                chosen[int(entry["order_item_id"])] = int(entry["quantity"])
            except (KeyError, TypeError, ValueError):
                continue
    else:
        chosen = {iid: q for iid, q in quantities.items() if q > 0}
    if not chosen:
        raise ShopError("Choose at least one unit to refund.")
    for iid, q in chosen.items():
        if iid not in left or q > left[iid]:
            raise ShopError("A quantity is more than what is left to refund on that item.")
    refund.amount = _refund_amount(order, items, chosen)
    if note:
        refund.decision_note = clamp(" ".join(note.split()), 1000)
    result = await _execute_refund(db, order, items, refund, quantities=chosen, restock=restock, actor=owner)
    if not result.ok:
        order.status = _post_completion_status(order)
    await audit(db, owner, "shop.refund.approve", "shop_order", order.id, gym_id=order.gym_id, request=request,
                detail={"refund_id": refund.id, "amount": str(refund.amount), "items": refund.items, "restock": restock,
                        "ok": result.ok, "error": result.error})
    return result


async def reject_refund(db: AsyncSession, refund_id: int, owner: User, note: str, *, request: Any = None) -> Refund:
    note = " ".join((note or "").split())
    if len(note) < 10:
        raise ShopError("Explain the decision to the customer in at least 10 characters.")
    refund, order, items, refunds = await _lock_refund(db, refund_id, owner)
    if refund.status != RefundStatus.REQUESTED:
        raise ShopError(f"This refund was already {refund.status.value.replace('_', ' ')}.")
    refund.status = RefundStatus.REJECTED
    refund.decision_note = clamp(note, 1000)
    refund.decided_by_id = owner.id
    refund.processed_at = utcnow()
    if order.status == OrderStatus.REFUND_REQUESTED:
        order.status = _post_completion_status(order)
    if order.customer_id:
        await notify(db, order.customer_id, "Refund request declined",
                     f"{(await _gym(db, order.gym_id)).name if order.gym_id else 'The store'} declined your refund request "
                     f"for order {order.order_number}: {note}",
                     category=NotificationCategory.PAYMENT, severity=Severity.WARNING, link=f"/shop/orders/{order.id}")
    await audit(db, owner, "shop.refund.reject", "shop_order", order.id, gym_id=order.gym_id, request=request,
                detail={"refund_id": refund.id, "note": note})
    return refund


async def issue_refund(db: AsyncSession, order_id: int, owner: User, quantities: dict[int, int], *, restock: bool,
                       reason: str, request: Any = None) -> GatewayRefund:
    """Owner-initiated (partial) refund of a paid order, without a customer request."""
    order, items, refunds = await lock_order_for_update(db, order_id)
    if order.paid_at is None or order.status in (OrderStatus.CANCELLED, OrderStatus.REFUNDED):
        raise ShopError("Only paid orders that aren't fully refunded can be refunded.")
    if any(r.status in (RefundStatus.REQUESTED, RefundStatus.PROCESSING) for r in refunds):
        raise ShopError("This order has an open refund request - approve or decline it instead.")
    reason = " ".join((reason or "").split())
    if len(reason) < 10:
        raise ShopError("Give a reason of at least 10 characters (the customer sees it).")
    left = {i.id: i.quantity - i.refunded_quantity for i in items}
    chosen = {iid: q for iid, q in quantities.items() if q > 0}
    if not chosen:
        raise ShopError("Choose at least one unit to refund.")
    for iid, q in chosen.items():
        if iid not in left or q > left[iid]:
            raise ShopError("A quantity is more than what is left to refund on that item.")
    refund = Refund(order_id=order.id, amount=_refund_amount(order, items, chosen), reason=clamp(reason, 1000),
                    items=[{"order_item_id": iid, "quantity": q} for iid, q in chosen.items()], restock=restock,
                    status=RefundStatus.APPROVED, requested_by_id=owner.id)
    db.add(refund)
    await db.flush()
    result = await _execute_refund(db, order, items, refund, quantities=chosen, restock=restock, actor=owner)
    await audit(db, owner, "shop.refund.issue", "shop_order", order.id, gym_id=order.gym_id, request=request,
                detail={"refund_id": refund.id, "amount": str(refund.amount), "items": refund.items, "restock": restock,
                        "ok": result.ok})
    return result


async def cancel_order(db: AsyncSession, order_id: int, actor: User, *, by_owner: bool, reason: str = "",
                       request: Any = None) -> str:
    """Cancel an order. Paid orders get a full refund + restock. Returns cancelled | refunded | refund_failed
    (the gateway refused the refund: the order is unchanged and the FAILED refund is kept) | already_cancelled |
    already_refunded (a repeated cancel - a second tab, a resubmit, a stale page: nothing changes). The caller commits."""
    order, items, refunds = await lock_order_for_update(db, order_id)
    if not by_owner and order.customer_id != actor.id:
        raise ShopError("Order not found.")
    if order.status == OrderStatus.CANCELLED:
        return "already_cancelled"
    if order.status == OrderStatus.REFUNDED:
        return "already_refunded"
    if order.status in UNPAID_STATUSES:
        order.status, order.cancelled_at, order.stock_held_until = OrderStatus.CANCELLED, utcnow(), None
        await audit(db, actor, "shop.order.cancel", "shop_order", order.id, gym_id=order.gym_id, request=request,
                    detail={"from": "unpaid", "to": "cancelled", "by_owner": by_owner})
        if by_owner and order.customer_id:
            await notify(db, order.customer_id, "Order cancelled",
                         f"Order {order.order_number} was cancelled by the store before payment. {reason}".strip(),
                         category=NotificationCategory.PAYMENT, severity=Severity.INFO, link=f"/shop/orders/{order.id}")
        return "cancelled"
    allowed = OWNER_CANCELLABLE if by_owner else (OrderStatus.PAID,)
    if order.status not in allowed:
        if by_owner:
            raise ShopError("Orders that have shipped or completed can't be cancelled - issue a refund instead.")
        raise ShopError("This order is already being prepared, so it can't be cancelled here. Contact the store.")
    if any(r.status in (RefundStatus.REQUESTED, RefundStatus.PROCESSING) for r in refunds):
        raise ShopError("Resolve the open refund request first.")
    quantities = {i.id: i.quantity - i.refunded_quantity for i in items if i.quantity > i.refunded_quantity}
    remaining = q2(order.total - order.refunded_total)
    why = " ".join((reason or "").split()) or ("Cancelled by the store" if by_owner else "Cancelled by the customer")
    if remaining > 0:
        refund = Refund(order_id=order.id, amount=remaining, reason=clamp(why, 1000),
                        items=[{"order_item_id": iid, "quantity": q} for iid, q in quantities.items()], restock=True,
                        status=RefundStatus.APPROVED, requested_by_id=actor.id)
        db.add(refund)
        await db.flush()
        result = await _execute_refund(db, order, items, refund, quantities=quantities, restock=True, actor=actor,
                                       final_status=OrderStatus.CANCELLED, movement_reason="cancel", notify_customer=False)
        if not result.ok:  # nothing was refunded: keep the order as it was, keep the FAILED refund for the record
            return "refund_failed"
    previous = order.status
    order.status, order.cancelled_at = OrderStatus.CANCELLED, utcnow()
    await audit(db, actor, "shop.order.cancel", "shop_order", order.id, gym_id=order.gym_id, request=request,
                detail={"from": previous.value, "to": "cancelled", "by_owner": by_owner, "refunded": str(remaining),
                        "reason": why})
    gym = await _gym(db, order.gym_id)
    if order.customer_id:
        await notify(db, order.customer_id, "Order cancelled",
                     f"Order {order.order_number} was cancelled{' by the store' if by_owner else ''}. "
                     f"{money(remaining, order.currency)} is being refunded to your original payment method.",
                     category=NotificationCategory.PAYMENT, severity=Severity.INFO, link=f"/shop/orders/{order.id}")
    if gym is not None and not by_owner:
        await notify(db, gym.owner_id, "Shop order cancelled",
                     f"{order.customer_label.split(' <')[0]} cancelled order {order.order_number}; "
                     f"{money(remaining, order.currency)} was refunded and the stock restored.",
                     category=NotificationCategory.PAYMENT, severity=Severity.INFO, link=f"/owner/shop/orders/{order.id}")
    return "refunded"


async def platform_close_order(db: AsyncSession, order_id: int, actor: User, *, reason: str) -> str:
    """The platform closes the store (app/services/governance.py): an open order ends with the buyer's money back.
    Unpaid or still cancellable orders go through ``cancel_order`` as the store (full refund + restock); a shipped order
    or one with a refund request gets the rest of its total refunded (the open request is closed as superseded, no
    restock). Returns ``cancel_order``'s outcome, or refunded | refund_failed | in_flight (a refund still processing at
    the gateway: left for review) | nothing. The caller commits."""
    order, items, refunds = await lock_order_for_update(db, order_id)
    if order.status in (OrderStatus.CANCELLED, OrderStatus.REFUNDED, OrderStatus.COMPLETED):
        return "nothing"
    if any(r.status == RefundStatus.PROCESSING for r in refunds):
        return "in_flight"
    requested = [r for r in refunds if r.status == RefundStatus.REQUESTED]
    if not requested and (order.status in UNPAID_STATUSES or order.status in OWNER_CANCELLABLE):
        return await cancel_order(db, order_id, actor, by_owner=True, reason=reason)
    now = utcnow()
    for r in requested:
        r.status = RefundStatus.REJECTED
        r.decision_note = "Superseded: the store closed and the order was refunded in full."
        r.decided_by_id, r.processed_at = actor.id, now
    remaining = q2(order.total - order.refunded_total)
    if order.paid_at is None or remaining <= 0:
        order.status = OrderStatus.CANCELLED if order.paid_at is None else _post_completion_status(order)
        return "nothing"
    quantities = {i.id: i.quantity - i.refunded_quantity for i in items if i.quantity > i.refunded_quantity}
    refund = Refund(order_id=order.id, amount=remaining, reason=clamp(reason, 1000),
                    items=[{"order_item_id": iid, "quantity": qty} for iid, qty in quantities.items()], restock=False,
                    status=RefundStatus.APPROVED, requested_by_id=actor.id)
    db.add(refund)
    await db.flush()
    result = await _execute_refund(db, order, items, refund, quantities=quantities, restock=False, actor=actor,
                                   final_status=OrderStatus.REFUNDED)
    await audit(db, actor, "shop.order.platform_refund", "shop_order", order.id, gym_id=order.gym_id,
                detail={"refund_id": refund.id, "amount": str(remaining), "ok": result.ok, "error": result.error,
                        "by": "platform"})
    return "refunded" if result.ok else "refund_failed"


# ====================================================================================================== fulfilment (owner)
TRANSITIONS: dict[str, tuple[tuple[OrderStatus, ...], OrderStatus]] = {
    "processing": ((OrderStatus.PAID,), OrderStatus.PROCESSING),
    "ready": ((OrderStatus.PAID, OrderStatus.PROCESSING), OrderStatus.READY_FOR_PICKUP),
    "shipped": ((OrderStatus.PAID, OrderStatus.PROCESSING), OrderStatus.SHIPPED),
    "completed": ((OrderStatus.READY_FOR_PICKUP, OrderStatus.SHIPPED), OrderStatus.COMPLETED),
}


async def advance_order(db: AsyncSession, order_id: int, owner: User, action: str, *, note: str = "",
                        request: Any = None) -> OrderStatus:
    """processing -> ready_for_pickup (pickup) | shipped (delivery, tracking note) -> completed. Notifies the customer."""
    if action not in TRANSITIONS:
        raise ShopError("Unknown action.")
    order, items, refunds = await lock_order_for_update(db, order_id)
    sources, target = TRANSITIONS[action]
    if order.status not in sources:
        raise ShopError(f"Can't mark an order that is “{order.status.value.replace('_', ' ')}” as {target.value.replace('_', ' ')}.")
    if action == "ready" and order.fulfillment != "pickup":
        raise ShopError("This is a delivery order - mark it as shipped instead.")
    if action == "shipped" and order.fulfillment != "delivery":
        raise ShopError("This is a pickup order - mark it as ready for pickup instead.")
    note = clamp(" ".join((note or "").split()), 300)
    previous = order.status
    order.status = target
    if target == OrderStatus.COMPLETED:
        order.completed_at = utcnow()
    if action == "shipped" and note:
        order.notes = (order.notes + "\n" if order.notes else "") + f"Shipped: {note}"
    await audit(db, owner, f"shop.order.{target.value}", "shop_order", order.id, gym_id=order.gym_id, request=request,
                detail={"from": previous.value, "to": target.value, "note": note or None})
    if order.customer_id:
        if target == OrderStatus.READY_FOR_PICKUP:
            branch = await db.get(Branch, order.pickup_branch_id) if order.pickup_branch_id else None
            where = f"{branch.name}, {branch.address_line1}, {branch.city}" if branch else "the gym"
            await notify(db, order.customer_id, "Ready for pickup",
                         f"Your order {order.order_number} is ready for pickup at {where}. Show the order number at the desk."
                         + (f" Note: {note}" if note else ""),
                         category=NotificationCategory.PAYMENT, severity=Severity.SUCCESS, link=f"/shop/orders/{order.id}")
        elif target == OrderStatus.SHIPPED:
            await notify(db, order.customer_id, "Your order has shipped",
                         f"Order {order.order_number} is on its way." + (f" Tracking: {note}" if note else ""),
                         category=NotificationCategory.PAYMENT, severity=Severity.SUCCESS, link=f"/shop/orders/{order.id}")
        elif target == OrderStatus.COMPLETED:
            await notify(db, order.customer_id, "Order completed",
                         f"Enjoy your purchase! Order {order.order_number} is complete. You can review your items or request "
                         f"a return within 7 days from the order page.",
                         category=NotificationCategory.PAYMENT, severity=Severity.SUCCESS, link=f"/shop/orders/{order.id}")
        elif target == OrderStatus.PROCESSING:
            await notify(db, order.customer_id, "We're preparing your order",
                         f"{order.order_number} is being prepared by the store.",
                         category=NotificationCategory.PAYMENT, severity=Severity.INFO, link=f"/shop/orders/{order.id}",
                         email=False)
    return target


# ====================================================================================================== stale unpaid orders
EXPIRE_BUDGET_S = 120.0  # per run (every 30 min): gateway checks are slow, the next run carries on
_KEEP_STATES = ("paid", "already_paid", "mismatch", "refunded_out_of_stock", "refunded_cancelled")


async def expire_unpaid_orders(db: AsyncSession, *, older_than_hours: int = UNPAID_TTL_HOURS, limit: int = 50) -> int:
    """Cancel unpaid orders after a day (Cashfree orders are verified with the gateway first). Returns cancelled count.

    Candidates are read in keyset chunks of ``limit`` (id > the last one seen), so orders that stay unpaid on purpose
    can never block newer ones, until the backlog is done or EXPIRE_BUDGET_S is spent. Orders flagged for payment
    review (MISMATCH_MARK: the gateway reported a different amount) are left to the owner and never polled again.
    Each order is settled in its own short transaction, and the gateway is asked BEFORE that transaction begins - no
    database transaction waits on an HTTP call. ``db`` only holds the periodic's lock."""
    cutoff = utcnow() - timedelta(hours=older_than_hours)
    batch = max(1, int(limit))
    run = Chunks(holder=db, budget_s=EXPIRE_BUDGET_S)
    cancelled = last_id = 0
    while await run.next():
        async with session_scope() as s:
            orders = (await s.execute(
                select(ShopOrder)
                .where(ShopOrder.status.in_(UNPAID_STATUSES), ShopOrder.created_at < cutoff, ShopOrder.id > last_id,
                       or_(ShopOrder.notes.is_(None), ~ShopOrder.notes.contains(MISMATCH_MARK, autoescape=True)))
                .order_by(ShopOrder.id).limit(batch)
            )).scalars().all()
        for order in orders:  # detached snapshots: no transaction is open while the gateway is asked
            if run.out_of_time:
                return cancelled  # the next run carries on (settled orders have left the candidates)
            last_id = order.id
            if await _expire_one(order):
                cancelled += 1
        run.done(len(orders), batch)
    return cancelled


async def _expire_one(order: ShopOrder) -> bool:
    """One stale order: ask the gateway (no transaction open yet), then settle it in a short transaction."""
    async with session_scope() as s:
        if order.payment_provider == "cashfree":
            # sync_with_gateway calls Cashfree first and touches the database only with the verified outcome
            if await sync_with_gateway(s, order) in _KEEP_STATES:
                return False
        locked = await _lock_order(s, order.order_number)
        if locked is not None and locked.status in UNPAID_STATUSES and locked.paid_at is None \
                and MISMATCH_MARK not in (locked.notes or ""):
            locked.status, locked.cancelled_at, locked.stock_held_until = OrderStatus.CANCELLED, utcnow(), None
            return True
    return False


@periodic("shop_expire_unpaid", every_seconds=1800, initial_delay=150)
async def _expire_unpaid(db: AsyncSession) -> None:
    await expire_unpaid_orders(db)
