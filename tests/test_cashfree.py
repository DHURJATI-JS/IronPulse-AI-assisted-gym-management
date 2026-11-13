"""WP-D: Cashfree PG/Payouts client (request shapes, errors, TEST MODE), webhook signatures, and the checkout
flow with Cashfree configured (order creation, SDK page, server-side return verification, webhook)."""

from __future__ import annotations

import json
import time
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.models import SecurityEvent, Transaction
from app.models.enums import SubscriptionStatus, ThreatCategory, TxStatus
from app.services import cashfree
from tests.conftest import login_as, post_form
from tests.test_checkout import fresh, latest_sub, near_customer, notifications_for, setup_gym, subscribe

SECRET = "cf_test_secret_key_123"


@pytest.fixture
def cf_keys(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "cashfree_app_id", "TEST_APP_ID")
    monkeypatch.setattr(s, "cashfree_secret_key", SECRET)
    monkeypatch.setattr(s, "cashfree_env", "sandbox")
    return s


@pytest.fixture
def cf_mock():
    """httpx MockTransport for every Cashfree call: state["routes"][(METHOD, path)] = fn(request) -> Response."""
    state: dict[str, Any] = {"requests": [], "routes": {}}

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        fn = state["routes"].get((request.method, request.url.path))
        if fn is None:
            return httpx.Response(404, json={"message": "not mocked", "code": "not_found"})
        return fn(request)

    cashfree.use_transport(httpx.MockTransport(handler))
    yield state
    cashfree.use_transport(None)


def order_json(order_id: str, status: str = "ACTIVE", amount: Any = 799.0, currency: str = "INR") -> dict[str, Any]:
    return {"cf_order_id": 2149460581, "order_id": order_id, "order_status": status, "order_amount": amount,
            "order_currency": currency, "payment_session_id": f"session_{order_id}"}


def mock_create_order(state: dict[str, Any]) -> None:
    def create(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(200, json=order_json(body["order_id"], amount=body["order_amount"]))

    state["routes"][("POST", "/pg/orders")] = create


def signed_headers(body: bytes, *, ts: str | None = None, secret: str = SECRET) -> dict[str, str]:
    ts = ts or str(int(time.time() * 1000))
    return {"x-webhook-timestamp": ts, "x-webhook-signature": cashfree.sign_webhook(body, ts, secret),
            "content-type": "application/json"}


def webhook_body(order_id: str, amount: Any = 799.0, currency: str = "INR", kind: str = "PAYMENT_SUCCESS_WEBHOOK",
                 status: str = "SUCCESS") -> bytes:
    return json.dumps({
        "type": kind, "event_time": "2026-11-10T10:00:00+05:30",
        "data": {
            "order": {"order_id": order_id, "order_amount": amount, "order_currency": currency},
            "payment": {"cf_payment_id": 5114915372, "payment_status": status, "payment_amount": amount,
                        "payment_currency": currency, "payment_message": "Simulated"},
            "customer_details": {"customer_id": "ipc1"},
        },
    }).encode()


async def start_cashfree_order(client: Any, db: Any, cf_mock: dict[str, Any], plan: str = "online"):
    env = await setup_gym(db)
    cust = await near_customer(db, phone="+91 98765 43210")
    await login_as(client, cust)
    await subscribe(client, env.gym, plan)
    sub = await latest_sub(db, cust.id)
    mock_create_order(cf_mock)
    r = await post_form(client, f"/checkout/{sub.id}/pay", {})
    assert r.status_code == 303, r.text[:300]
    order_id = r.headers["location"].split("order_id=")[1]
    return env, cust, sub, order_id, r.headers["location"]


# ------------------------------------------------------------------ webhook signatures (unit)
def test_webhook_signature_valid_invalid_stale():
    body = b'{"type":"PAYMENT_SUCCESS_WEBHOOK"}'
    now = time.time()
    ts_ms = str(int(now * 1000))
    sig = cashfree.sign_webhook(body, ts_ms, SECRET)
    assert cashfree.verify_webhook_signature(body, ts_ms, sig, secret=SECRET, now=now)
    ts_s = str(int(now))
    assert cashfree.verify_webhook_signature(body, ts_s, cashfree.sign_webhook(body, ts_s, SECRET), secret=SECRET, now=now)
    assert not cashfree.verify_webhook_signature(body + b" ", ts_ms, sig, secret=SECRET, now=now)  # tampered body
    assert not cashfree.verify_webhook_signature(body, ts_ms, sig, secret="other-secret", now=now)
    assert not cashfree.verify_webhook_signature(body, ts_ms, "bm90LWEtc2lnbmF0dXJl", secret=SECRET, now=now)
    assert not cashfree.verify_webhook_signature(body, ts_ms, sig, secret=SECRET, now=now + 301)  # stale
    assert not cashfree.verify_webhook_signature(body, ts_ms, sig, secret=SECRET, now=now - 301)  # from the future
    assert cashfree.verify_webhook_signature(body, ts_ms, sig, secret=SECRET, now=now + 299)
    assert not cashfree.verify_webhook_signature(body, None, sig, secret=SECRET)
    assert not cashfree.verify_webhook_signature(body, "not-a-number", sig, secret=SECRET)
    assert not cashfree.verify_webhook_signature(body, ts_ms, sig, secret="")


def test_webhook_signature_needs_configured_secret(monkeypatch):
    monkeypatch.setattr(get_settings(), "cashfree_secret_key", None)
    body = b"{}"
    ts = str(int(time.time()))
    assert not cashfree.verify_webhook_signature(body, ts, cashfree.sign_webhook(body, ts, SECRET))
    monkeypatch.setattr(get_settings(), "cashfree_secret_key", SECRET)
    assert cashfree.verify_webhook_signature(body, ts, cashfree.sign_webhook(body, ts, SECRET))


# ------------------------------------------------------------------ PG client
async def test_create_order_request_shape(cf_keys, cf_mock):
    mock_create_order(cf_mock)
    result = await cashfree.create_order(
        order_id="IPS7-ABCDEF1234", amount=Decimal("1499.00"), currency="inr", customer_id="ipc 42!",
        customer_email="a@example.com", customer_phone="+91 98765-43210", customer_name="Asha",
        return_url="http://testserver/checkout/return?order_id=IPS7-ABCDEF1234",
        notify_url="http://testserver/payments/cashfree/webhook", note="Pulse - Online",
    )
    req = cf_mock["requests"][-1]
    assert str(req.url) == "https://sandbox.cashfree.com/pg/orders" and req.method == "POST"
    assert req.headers["x-client-id"] == "TEST_APP_ID" and req.headers["x-client-secret"] == SECRET
    assert req.headers["x-api-version"] == get_settings().cashfree_api_version
    body = json.loads(req.content)
    assert body["order_id"] == "IPS7-ABCDEF1234" and body["order_amount"] == 1499.0 and body["order_currency"] == "INR"
    assert body["customer_details"] == {"customer_id": "ipc42", "customer_email": "a@example.com",
                                        "customer_phone": "9876543210", "customer_name": "Asha"}
    assert body["order_meta"]["return_url"].endswith("/checkout/return?order_id=IPS7-ABCDEF1234")
    assert body["order_meta"]["notify_url"].endswith("/payments/cashfree/webhook")
    assert result.payment_session_id == "session_IPS7-ABCDEF1234" and result.order_status == "ACTIVE"
    assert result.amount == Decimal("1499.00") and not result.is_paid


async def test_get_order_and_errors(cf_keys, cf_mock):
    cf_mock["routes"][("GET", "/pg/orders/IPS7-PAID")] = lambda r: httpx.Response(200, json=order_json("IPS7-PAID", "PAID", 1499))
    order = await cashfree.get_order("IPS7-PAID")
    assert order.is_paid and order.amount == Decimal("1499.00") and order.currency == "INR"

    cf_mock["routes"][("GET", "/pg/orders/IPS7-BOOM")] = lambda r: httpx.Response(500, json={"message": "upstream", "code": "x"})
    with pytest.raises(cashfree.CashfreeError) as exc:
        await cashfree.get_order("IPS7-BOOM")
    assert exc.value.status == 500

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    cf_mock["routes"][("GET", "/pg/orders/IPS7-DOWN")] = down
    with pytest.raises(cashfree.CashfreeError, match="could not be reached"):
        await cashfree.get_order("IPS7-DOWN")
    with pytest.raises(ValueError):
        await cashfree.get_order("bad id with spaces")


async def test_pg_not_configured_raises():
    with pytest.raises(cashfree.CashfreeNotConfigured):
        await cashfree.get_order("IPS1-ABCDEF")


# ------------------------------------------------------------------ Payouts
async def test_payout_test_mode_simulates_success():
    assert not cashfree.payouts_configured()
    res = await cashfree.create_payout("SAL-7-202611", Decimal("30000"), "INR",
                                       {"beneficiary_name": "Ravi", "bank_account_number": "001122334455", "bank_ifsc": "HDFC0000001"})
    assert res.succeeded and res.test_mode and res.reference.startswith("TEST-")
    status = await cashfree.get_payout_status("SAL-7-202611")
    assert status.succeeded and status.reference.startswith("TEST-")
    with pytest.raises(ValueError):
        await cashfree.create_payout("SAL-1", 0, "INR", {"name": "Ravi", "vpa": "ravi@upi"})
    with pytest.raises(ValueError):
        await cashfree.create_payout("SAL-1", 10, "INR", {"name": "Ravi"})
    with pytest.raises(ValueError):
        await cashfree.create_payout("bad id!", 10, "INR", {"name": "Ravi", "vpa": "ravi@upi"})


async def test_payout_configured_request_shape(monkeypatch, cf_mock):
    s = get_settings()
    monkeypatch.setattr(s, "cashfree_payout_client_id", "PAYOUT_ID")
    monkeypatch.setattr(s, "cashfree_payout_client_secret", "PAYOUT_SECRET")
    cf_mock["routes"][("POST", "/payout/transfers")] = lambda r: httpx.Response(
        200, json={"transfer_id": json.loads(r.content)["transfer_id"], "cf_transfer_id": "CF-9001", "status": "RECEIVED"})
    res = await cashfree.create_payout("SAL-7-202611", "30000.50", "INR",
                                       {"beneficiary_name": "Ravi Kumar", "bank_account_number": "001122334455", "bank_ifsc": "hdfc0000001"},
                                       remarks="November salary")
    req = cf_mock["requests"][-1]
    assert str(req.url) == "https://sandbox.cashfree.com/payout/transfers"
    assert req.headers["x-client-id"] == "PAYOUT_ID" and req.headers["x-client-secret"] == "PAYOUT_SECRET"
    assert req.headers["x-api-version"] == s.cashfree_payout_api_version
    body = json.loads(req.content)
    assert body == {
        "transfer_id": "SAL-7-202611", "transfer_amount": 30000.5, "transfer_currency": "INR", "transfer_mode": "banktransfer",
        "beneficiary_details": {"beneficiary_name": "Ravi Kumar", "beneficiary_instrument_details": {
            "bank_account_number": "001122334455", "bank_ifsc": "HDFC0000001"}},
        "transfer_remarks": "November salary",
    }
    assert res.pending and res.status == "RECEIVED" and res.reference == "CF-9001" and not res.test_mode

    await cashfree.create_payout("SAL-8", 100, "INR", {"name": "Ravi", "vpa": "ravi@okbank"})
    body = json.loads(cf_mock["requests"][-1].content)
    assert body["transfer_mode"] == "upi" and body["beneficiary_details"]["beneficiary_instrument_details"] == {"vpa": "ravi@okbank"}

    cf_mock["routes"][("GET", "/payout/transfers")] = lambda r: httpx.Response(
        200, json={"transfer_id": r.url.params["transfer_id"], "status": "SUCCESS", "transfer_utr": "UTR123456"})
    status = await cashfree.get_payout_status("SAL-7-202611")
    assert status.succeeded and status.reference == "UTR123456"
    assert cf_mock["requests"][-1].url.params["transfer_id"] == "SAL-7-202611"


# ------------------------------------------------------------------ checkout with Cashfree configured
async def test_pay_creates_cashfree_order_and_sdk_page(client, db, cf_keys, cf_mock):
    env, cust, sub, order_id, location = await start_cashfree_order(client, db, cf_mock)
    assert location == f"/checkout/{sub.id}/gateway?order_id={order_id}" and order_id.startswith(f"IPS{sub.id}-")
    body = json.loads(cf_mock["requests"][-1].content)
    assert body["order_amount"] == 799.0 and body["order_currency"] == "INR"
    assert body["order_meta"]["return_url"] == f"{get_settings().public_base_url}/checkout/return?order_id={order_id}"
    assert body["order_meta"]["notify_url"] == f"{get_settings().public_base_url}/payments/cashfree/webhook"
    assert body["customer_details"]["customer_phone"] == "9876543210"
    tx = await db.scalar(select(Transaction).where(Transaction.provider_order_id == order_id))
    assert tx.provider == "cashfree" and tx.status == TxStatus.PENDING and tx.amount == Decimal("799.00")
    assert tx.payment_session_id == f"session_{order_id}" and tx.customer_id == cust.id
    page = await client.get(location)
    assert page.status_code == 200
    assert 'src="https://sdk.cashfree.com/js/v3/cashfree.js"' in page.text and 'nonce="' in page.text
    assert f'data-session-id="session_{order_id}"' in page.text and 'data-mode="sandbox"' in page.text
    assert "https://sdk.cashfree.com" in page.headers["content-security-policy"]
    # the simulated gateway only exists in TEST MODE
    assert (await client.get(f"/checkout/{sub.id}/test-gateway?order_id={order_id}")).status_code == 404


async def test_pay_gateway_error_is_shown(client, db, cf_keys, cf_mock):
    env = await setup_gym(db)
    cust = await near_customer(db)
    await login_as(client, cust)
    await subscribe(client, env.gym, "online")
    sub = await latest_sub(db, cust.id)
    cf_mock["routes"][("POST", "/pg/orders")] = lambda r: httpx.Response(401, json={"message": "authentication failed", "code": "request_failed"})
    r = await post_form(client, f"/checkout/{sub.id}/pay", {})
    assert r.status_code == 502 and "Payment couldn" in r.text and "authentication failed" in r.text
    assert await db.scalar(select(func.count(Transaction.id))) == 0


async def test_return_verifies_order_server_side(client, db, cf_keys, cf_mock):
    env, cust, sub, order_id, _ = await start_cashfree_order(client, db, cf_mock)
    state = {"status": "ACTIVE"}
    cf_mock["routes"][("GET", f"/pg/orders/{order_id}")] = lambda r: httpx.Response(200, json=order_json(order_id, state["status"]))
    r = await client.get(f"/checkout/return?order_id={order_id}")
    assert r.status_code == 200 and "Payment not completed yet" in r.text
    assert (await latest_sub(db, cust.id)).status == SubscriptionStatus.PENDING_PAYMENT
    state["status"] = "PAID"
    r = await client.get(f"/checkout/return?order_id={order_id}")
    assert r.status_code == 200 and "Payment confirmed" in r.text
    assert (await latest_sub(db, cust.id)).status == SubscriptionStatus.ACTIVE
    r = await client.get(f"/checkout/return?order_id={order_id}")
    assert "Payment confirmed" in r.text
    titles = [n.title for n in await notifications_for(db, cust.id)]
    assert titles.count("Your membership is active") == 1


async def test_return_amount_mismatch_not_activated(client, db, cf_keys, cf_mock):
    env, cust, sub, order_id, _ = await start_cashfree_order(client, db, cf_mock)
    cf_mock["routes"][("GET", f"/pg/orders/{order_id}")] = lambda r: httpx.Response(200, json=order_json(order_id, "PAID", 1.0))
    r = await client.get(f"/checkout/return?order_id={order_id}")
    assert r.status_code == 200 and "couldn" in r.text
    sub = await latest_sub(db, cust.id)
    assert sub.status == SubscriptionStatus.PENDING_PAYMENT and "amount_mismatch" in sub.fraud_flags
    tx = await db.scalar(fresh(select(Transaction).where(Transaction.provider_order_id == order_id)))
    assert tx.status == TxStatus.FAILED and tx.meta["amount_mismatch"]["expected"] == "799.00"
    ev = await db.scalar(select(SecurityEvent).where(SecurityEvent.category == ThreatCategory.FRAUD))
    assert ev is not None and order_id in ev.detail
    assert any(n.title == "Payment needs review" for n in await notifications_for(db, env.owner.id))


async def test_webhook_activates_and_is_idempotent(client, db, cf_keys, cf_mock, anon_client_factory):
    env, cust, sub, order_id, _ = await start_cashfree_order(client, db, cf_mock)
    hook = anon_client_factory("198.51.100.20")
    body = webhook_body(order_id)
    r = await hook.post("/payments/cashfree/webhook", content=body, headers=signed_headers(body))
    assert r.status_code == 200 and r.json() == {"ok": True, "result": "success"}
    sub = await latest_sub(db, cust.id)
    assert sub.status == SubscriptionStatus.ACTIVE
    tx = await db.scalar(fresh(select(Transaction).where(Transaction.provider_order_id == order_id)))
    assert tx.status == TxStatus.SUCCESS and tx.provider_payment_id == "5114915372" and tx.completed_at is not None
    r = await hook.post("/payments/cashfree/webhook", content=body, headers=signed_headers(body))
    assert r.status_code == 200 and r.json()["result"] == "success"
    assert [n.title for n in await notifications_for(db, cust.id)].count("Your membership is active") == 1
    # the customer's return afterwards just shows the result (no second activation)
    cf_mock["routes"][("GET", f"/pg/orders/{order_id}")] = lambda r: httpx.Response(200, json=order_json(order_id, "PAID"))
    page = await client.get(f"/checkout/return?order_id={order_id}")
    assert "Payment confirmed" in page.text
    assert [n.title for n in await notifications_for(db, cust.id)].count("Your membership is active") == 1


async def test_webhook_rejects_bad_and_stale_signatures(client, db, cf_keys, cf_mock, anon_client_factory):
    env, cust, sub, order_id, _ = await start_cashfree_order(client, db, cf_mock)
    hook = anon_client_factory("198.51.100.21")
    body = webhook_body(order_id)
    bad = signed_headers(body, secret="wrong-secret")
    r = await hook.post("/payments/cashfree/webhook", content=body, headers=bad)
    assert r.status_code == 401
    old_ts = str(int((time.time() - 600) * 1000))
    r = await hook.post("/payments/cashfree/webhook", content=body, headers=signed_headers(body, ts=old_ts))
    assert r.status_code == 401
    tampered = webhook_body(order_id, amount=1.0)
    r = await hook.post("/payments/cashfree/webhook", content=tampered, headers=signed_headers(body))
    assert r.status_code == 401
    r = await hook.post("/payments/cashfree/webhook", content=body, headers={"content-type": "application/json"})
    assert r.status_code == 401
    assert (await latest_sub(db, cust.id)).status == SubscriptionStatus.PENDING_PAYMENT


async def test_webhook_amount_mismatch_flags_fraud(client, db, cf_keys, cf_mock, anon_client_factory):
    env, cust, sub, order_id, _ = await start_cashfree_order(client, db, cf_mock)
    hook = anon_client_factory("198.51.100.22")
    body = webhook_body(order_id, amount=79.0)
    r = await hook.post("/payments/cashfree/webhook", content=body, headers=signed_headers(body))
    assert r.status_code == 200 and r.json()["result"] == "mismatch"
    sub = await latest_sub(db, cust.id)
    assert sub.status != SubscriptionStatus.ACTIVE and "amount_mismatch" in sub.fraud_flags
    ev = await db.scalar(select(SecurityEvent).where(SecurityEvent.category == ThreatCategory.FRAUD))
    assert ev is not None and "mismatch" in ev.detail
    body = webhook_body(order_id, currency="USD")
    r = await hook.post("/payments/cashfree/webhook", content=body, headers=signed_headers(body))
    assert r.status_code == 200 and (await latest_sub(db, cust.id)).status != SubscriptionStatus.ACTIVE


async def test_webhook_failed_payment_unknown_order_and_unconfigured(client, db, cf_keys, cf_mock, anon_client_factory, monkeypatch):
    env, cust, sub, order_id, _ = await start_cashfree_order(client, db, cf_mock)
    hook = anon_client_factory("198.51.100.23")
    body = webhook_body(order_id, kind="PAYMENT_FAILED_WEBHOOK", status="FAILED")
    r = await hook.post("/payments/cashfree/webhook", content=body, headers=signed_headers(body))
    assert r.status_code == 200 and r.json()["result"] == "noted"
    tx = await db.scalar(fresh(select(Transaction).where(Transaction.provider_order_id == order_id)))
    assert tx.status == TxStatus.PENDING and tx.meta["last_failure"]["status"] == "FAILED"  # retryable on Cashfree
    body = webhook_body("IPS999-NOPE")
    r = await hook.post("/payments/cashfree/webhook", content=body, headers=signed_headers(body))
    assert r.status_code == 200 and r.json()["result"] == "ignored"
    monkeypatch.setattr(get_settings(), "cashfree_secret_key", None)
    r = await hook.post("/payments/cashfree/webhook", content=body, headers=signed_headers(body))
    assert r.status_code == 503
