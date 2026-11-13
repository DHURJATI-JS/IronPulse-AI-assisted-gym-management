"""Cashfree Payment Gateway (subscriptions) + Cashfree Payouts (salaries) - async httpx client.

* PG:      ``create_order`` / ``get_order`` against ``settings.cashfree_pg_base`` with the headers
           ``x-client-id``, ``x-client-secret`` and ``x-api-version = settings.cashfree_api_version``.
* Webhook: ``verify_webhook_signature`` - ``x-webhook-signature`` must equal
           ``base64(HMAC_SHA256(secret_key, x-webhook-timestamp + raw_body))`` and the timestamp must be
           within 5 minutes (seconds or milliseconds are accepted).
* Payouts: ``create_payout`` (POST /transfers) / ``get_payout_status`` (GET /transfers?transfer_id=)
           against ``settings.cashfree_payout_base`` with ``x-api-version = settings.cashfree_payout_api_version``.

TEST MODE: without PG keys the checkout uses the local, clearly labelled test gateway (see
``app/routers/checkout.py``); without payout keys ``create_payout`` returns a simulated success with a
``TEST-...`` reference so payroll flows can be exercised end to end.

NOTE: request/response shapes follow Cashfree's public API documentation (PG "orders" API and Payouts V2
"transfers" API). They must be validated end to end with Cashfree **sandbox** keys before going live.
Secrets are never logged.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from urllib.parse import quote

import httpx

from app.config import get_settings
from app.core.utils import to_decimal

log = logging.getLogger("ironpulse.cashfree")

TIMEOUT = httpx.Timeout(20.0, connect=8.0)
WEBHOOK_TOLERANCE_SECONDS = 300
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,45}$")

PAYOUT_SUCCESS = {"SUCCESS", "COMPLETED"}
PAYOUT_FAILED = {"FAILED", "REJECTED", "REVERSED", "CANCELLED", "ERROR"}


class CashfreeError(RuntimeError):
    """Raised for gateway / network errors. ``str(exc)`` is safe to show to users."""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class CashfreeNotConfigured(CashfreeError):
    pass


@dataclass(slots=True)
class OrderResult:
    order_id: str
    order_status: str  # ACTIVE | PAID | EXPIRED | TERMINATED | TERMINATION_REQUESTED
    amount: Decimal | None
    currency: str | None
    payment_session_id: str | None = None
    cf_order_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_paid(self) -> bool:
        return self.order_status.upper() == "PAID"

    @property
    def is_dead(self) -> bool:
        return self.order_status.upper() in {"EXPIRED", "TERMINATED", "TERMINATION_REQUESTED"}


@dataclass(slots=True)
class PayoutResult:
    status: str  # normalised upper-case Cashfree status (SUCCESS, PENDING, RECEIVED, FAILED, ...)
    reference: str | None  # UTR / cf_transfer_id / TEST-... reference
    raw: dict[str, Any] = field(default_factory=dict)
    test_mode: bool = False

    @property
    def succeeded(self) -> bool:
        return self.status in PAYOUT_SUCCESS

    @property
    def failed(self) -> bool:
        return self.status in PAYOUT_FAILED

    @property
    def pending(self) -> bool:
        return not (self.succeeded or self.failed)


# ------------------------------------------------------------------ transport (tests inject a MockTransport)
_transport: httpx.AsyncBaseTransport | None = None


def use_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    """Route every Cashfree HTTP call through ``transport`` (tests: ``httpx.MockTransport``)."""
    global _transport
    _transport = transport


def is_configured() -> bool:
    return get_settings().cashfree_enabled


def payouts_configured() -> bool:
    return get_settings().cashfree_payouts_enabled


def sdk_mode() -> str:
    """Mode string for the Cashfree JS SDK: ``Cashfree({mode})``."""
    return "production" if get_settings().cashfree_env == "production" else "sandbox"


def new_order_id(subscription_id: int) -> str:
    """``IPS{sid}-{random}`` - unique, <= 45 chars, only [A-Za-z0-9-] as Cashfree requires."""
    return f"IPS{int(subscription_id)}-{secrets.token_hex(5).upper()}"


def _amount(value: Any) -> float:
    amt = to_decimal(value)
    if amt <= 0:
        raise ValueError("Amount must be greater than zero")
    return float(amt)


def _pg_headers() -> dict[str, str]:
    s = get_settings()
    if not s.cashfree_enabled:
        raise CashfreeNotConfigured("Online payments are not configured (Cashfree keys are missing).")
    return {
        "x-client-id": str(s.cashfree_app_id),
        "x-client-secret": str(s.cashfree_secret_key),
        "x-api-version": s.cashfree_api_version,
        "Content-Type": "application/json",
    }


def _payout_headers() -> dict[str, str]:
    s = get_settings()
    if not s.cashfree_payouts_enabled:
        raise CashfreeNotConfigured("Payouts are not configured (Cashfree payout keys are missing).")
    return {
        "x-client-id": str(s.cashfree_payout_client_id),
        "x-client-secret": str(s.cashfree_payout_client_secret),
        "x-api-version": s.cashfree_payout_api_version,
        "Content-Type": "application/json",
    }


async def _request(base_url: str, method: str, path: str, *, headers: dict[str, str], json: Any = None,
                   params: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=TIMEOUT, transport=_transport,
                                     headers={"Accept": "application/json"}) as client:
            response = await client.request(method, path, headers=headers, json=json, params=params)
    except httpx.HTTPError as exc:
        log.warning("cashfree %s %s failed: %s", method, path, exc.__class__.__name__)
        raise CashfreeError("The payment provider could not be reached. Please try again in a moment.") from exc
    try:
        data = response.json()
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {"data": data}
    if response.status_code >= 400:
        code = str(data.get("code") or data.get("type") or "") or None
        log.warning("cashfree %s %s -> HTTP %s (%s)", method, path, response.status_code, code)
        message = data.get("message") if isinstance(data.get("message"), str) else None
        raise CashfreeError(message or f"The payment provider returned an error ({response.status_code}).",
                            status=response.status_code, code=code)
    return data


def _order(data: dict[str, Any], fallback_id: str = "") -> OrderResult:
    amount = data.get("order_amount")
    try:
        amt = to_decimal(amount) if amount is not None else None
    except ValueError:
        amt = None
    return OrderResult(
        order_id=str(data.get("order_id") or fallback_id),
        order_status=str(data.get("order_status") or "UNKNOWN").upper(),
        amount=amt,
        currency=(str(data["order_currency"]).upper() if data.get("order_currency") else None),
        payment_session_id=data.get("payment_session_id"),
        cf_order_id=str(data["cf_order_id"]) if data.get("cf_order_id") is not None else None,
        raw=data,
    )


# ------------------------------------------------------------------ Payment Gateway
async def create_order(
    *,
    order_id: str,
    amount: Decimal,
    currency: str,
    customer_id: str,
    customer_email: str | None,
    customer_phone: str | None,
    customer_name: str | None,
    return_url: str,
    notify_url: str,
    note: str | None = None,
) -> OrderResult:
    """POST /orders -> ``payment_session_id`` for the JS SDK checkout."""
    if not _ID_RE.match(order_id):
        raise ValueError("order_id must be 3-45 characters of letters, digits, '-' or '_'")
    digits = re.sub(r"\D", "", customer_phone or "")
    payload = {
        "order_id": order_id,
        "order_amount": _amount(amount),
        "order_currency": currency.upper(),
        "customer_details": {
            "customer_id": re.sub(r"[^A-Za-z0-9_-]", "", customer_id)[:50] or "customer",
            "customer_email": customer_email or None,
            # Cashfree requires a phone number; the last 10 digits work for Indian numbers.
            "customer_phone": digits[-10:] if len(digits) >= 10 else "9999999999",
            "customer_name": (customer_name or "")[:100] or None,
        },
        "order_meta": {"return_url": return_url, "notify_url": notify_url},
        "order_note": (note or "")[:200] or None,
    }
    data = await _request(get_settings().cashfree_pg_base, "POST", "/orders", headers=_pg_headers(), json=payload)
    result = _order(data, order_id)
    if not result.payment_session_id:
        raise CashfreeError("The payment provider did not return a payment session.")
    return result


async def get_order(order_id: str) -> OrderResult:
    """GET /orders/{order_id} - the source of truth for "was this order paid?"."""
    if not _ID_RE.match(order_id):
        raise ValueError("invalid order_id")
    data = await _request(get_settings().cashfree_pg_base, "GET", f"/orders/{quote(order_id, safe='')}",
                          headers=_pg_headers())
    return _order(data, order_id)


def sign_webhook(raw_body: bytes, timestamp: str, secret: str) -> str:
    """base64(HMAC_SHA256(secret, timestamp + raw_body)) - what Cashfree sends in x-webhook-signature."""
    mac = hmac.new(secret.encode(), str(timestamp).encode() + raw_body, hashlib.sha256).digest()
    return base64.b64encode(mac).decode()


def verify_webhook_signature(
    raw_body: bytes,
    timestamp: str | None,
    signature: str | None,
    *,
    secret: str | None = None,
    now: float | None = None,
    tolerance_seconds: int = WEBHOOK_TOLERANCE_SECONDS,
) -> bool:
    """Constant-time signature check plus a +/- 5 minute timestamp window (replay protection)."""
    secret = secret if secret is not None else get_settings().cashfree_secret_key
    if not secret or not timestamp or not signature:
        return False
    ts_text = str(timestamp).strip()
    try:
        ts = int(ts_text)
    except ValueError:
        return False
    ts_seconds = ts / 1000.0 if ts > 10**11 else float(ts)  # Cashfree sends milliseconds
    current = time.time() if now is None else now
    if abs(current - ts_seconds) > tolerance_seconds:
        return False
    expected = sign_webhook(raw_body, ts_text, secret)
    return hmac.compare_digest(expected.encode(), signature.strip().encode())


# ------------------------------------------------------------------ Payouts (salaries)
def _beneficiary(beneficiary: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    name = str(beneficiary.get("beneficiary_name") or beneficiary.get("name") or "").strip()
    if not name:
        raise ValueError("Beneficiary name is required")
    vpa = str(beneficiary.get("vpa") or beneficiary.get("upi") or "").strip()
    account = str(beneficiary.get("bank_account_number") or beneficiary.get("account_number") or "").strip()
    ifsc = str(beneficiary.get("bank_ifsc") or beneficiary.get("ifsc") or "").strip().upper()
    if vpa:
        return "upi", {"beneficiary_name": name[:100], "beneficiary_instrument_details": {"vpa": vpa}}
    if account and ifsc:
        return "banktransfer", {
            "beneficiary_name": name[:100],
            "beneficiary_instrument_details": {"bank_account_number": account, "bank_ifsc": ifsc},
        }
    raise ValueError("Beneficiary needs a UPI ID (vpa) or a bank account number + IFSC")


def _payout(data: dict[str, Any]) -> PayoutResult:
    status = str(data.get("status") or data.get("transfer_status") or "PENDING").upper()
    ref = data.get("transfer_utr") or data.get("utr") or data.get("cf_transfer_id") or data.get("reference_id")
    return PayoutResult(status=status, reference=str(ref) if ref is not None else None, raw=data)


async def create_payout(
    transfer_id: str,
    amount: Decimal | str | float,
    currency: str,
    beneficiary: dict[str, Any],
    *,
    remarks: str | None = None,
) -> PayoutResult:
    """POST /transfers (Payouts V2). ``beneficiary``: {beneficiary_name, bank_account_number + bank_ifsc} or
    {beneficiary_name, vpa}. TEST MODE (no payout keys): simulated SUCCESS with a ``TEST-...`` reference."""
    if not _ID_RE.match(transfer_id):
        raise ValueError("transfer_id must be 3-45 characters of letters, digits, '-' or '_'")
    value = _amount(amount)
    mode, details = _beneficiary(beneficiary)
    if not payouts_configured():
        reference = f"TEST-{secrets.token_hex(6).upper()}"
        return PayoutResult(
            status="SUCCESS", reference=reference, test_mode=True,
            raw={"transfer_id": transfer_id, "status": "SUCCESS", "simulated": True, "transfer_mode": mode,
                 "transfer_amount": value, "transfer_currency": currency.upper(), "reference": reference},
        )
    payload = {
        "transfer_id": transfer_id,
        "transfer_amount": value,
        "transfer_currency": currency.upper(),
        "transfer_mode": mode,
        "beneficiary_details": details,
        "transfer_remarks": (remarks or "Salary")[:70],
    }
    data = await _request(get_settings().cashfree_payout_base, "POST", "/transfers", headers=_payout_headers(),
                          json=payload)
    return _payout(data)


async def get_payout_status(transfer_id: str) -> PayoutResult:
    """GET /transfers?transfer_id=... (TEST MODE: simulated SUCCESS)."""
    if not _ID_RE.match(transfer_id):
        raise ValueError("invalid transfer_id")
    if not payouts_configured():
        return PayoutResult(status="SUCCESS", reference=f"TEST-{transfer_id}"[:80], test_mode=True,
                            raw={"transfer_id": transfer_id, "status": "SUCCESS", "simulated": True})
    data = await _request(get_settings().cashfree_payout_base, "GET", "/transfers", headers=_payout_headers(),
                          params={"transfer_id": transfer_id})
    return _payout(data)
