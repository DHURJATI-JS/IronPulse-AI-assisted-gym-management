"""Integration: the shared signed Cashfree webhook dispatches gym-shop orders (IPO-*) to the shop service."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from decimal import Decimal

from app.config import get_settings
from app.models import ShopOrder
from app.models.shop import OrderStatus
from tests.conftest import login_as, make_client
from tests.factories import make_customer
from tests.test_shop import make_product, shop_setup
from tests.test_shop_payments import bag, checkout, fresh, order_id_from


def _signed(body: dict, secret: str) -> tuple[bytes, dict]:
    raw = json.dumps(body).encode()
    ts = str(int(time.time()))
    sig = base64.b64encode(hmac.new(secret.encode(), ts.encode() + raw, hashlib.sha256).digest()).decode()
    return raw, {"x-webhook-timestamp": ts, "x-webhook-signature": sig, "content-type": "application/json"}


async def test_shop_order_paid_via_shared_webhook(client, db, monkeypatch):
    owner, gym, branch = await shop_setup(db)
    product = await make_product(db, gym, name="Creatine Monohydrate", price=Decimal("899"), stock=5)
    cust = await make_customer(db)
    await bag(db, cust, product, 1)
    await login_as(client, cust)
    r = await checkout(client, gym, branch)
    assert r.status_code == 303
    order = await fresh(db, ShopOrder, order_id_from(r.headers["location"]))
    assert order.status == OrderStatus.PENDING_PAYMENT

    settings = get_settings()
    monkeypatch.setattr(settings, "cashfree_app_id", "TEST_APP")
    monkeypatch.setattr(settings, "cashfree_secret_key", "test-webhook-secret")
    event = {"type": "PAYMENT_SUCCESS_WEBHOOK", "data": {
        "order": {"order_id": order.order_number, "order_amount": float(order.total), "order_currency": order.currency},
        "payment": {"cf_payment_id": 987654, "payment_status": "SUCCESS", "payment_amount": float(order.total),
                    "payment_currency": order.currency}}}
    raw, headers = _signed(event, "test-webhook-secret")
    async with make_client("54.0.0.1") as cashfree:  # the webhook comes from Cashfree, no session or CSRF
        r = await cashfree.post("/payments/cashfree/webhook", content=raw, headers=headers)
        assert r.status_code == 200 and r.json()["result"] == "paid", r.text
        r = await cashfree.post("/payments/cashfree/webhook", content=raw, headers=headers)  # replay
        assert r.json()["result"] == "already_paid"
        bad = dict(headers, **{"x-webhook-signature": "AAAA"})
        r = await cashfree.post("/payments/cashfree/webhook", content=raw, headers=bad)
        assert r.status_code == 401
    order = await fresh(db, ShopOrder, order.id)
    assert order.status == OrderStatus.PAID and order.paid_at is not None
