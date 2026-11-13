"""Small, dependency-free helpers shared by every module."""

from __future__ import annotations

import hashlib
import re
import secrets
import unicodedata
from datetime import UTC, date, datetime, time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# ------------------------------------------------------------------ time


def utcnow() -> datetime:
    return datetime.now(UTC)


def safe_zone(tz: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(tz or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def is_valid_timezone(tz: str | None) -> bool:
    if not tz:
        return False
    try:
        ZoneInfo(tz)
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False


def local_now(tz: str | None) -> datetime:
    return datetime.now(safe_zone(tz))


def local_today(tz: str | None) -> date:
    return local_now(tz).date()


def to_local(dt: datetime | None, tz: str | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(safe_zone(tz))


def parse_hhmm(value: str | None) -> time | None:
    if not value:
        return None
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", value)
    if not m:
        raise ValueError("Use HH:MM (24-hour)")
    hh, mm = int(m.group(1)), int(m.group(2))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError("Use HH:MM (24-hour)")
    return time(hh, mm)


def minutes(t: time) -> int:
    return t.hour * 60 + t.minute


# ------------------------------------------------------------------ phones
DIAL_CODES = {
    "IN": "91", "US": "1", "CA": "1", "GB": "44", "AE": "971", "AU": "61", "SG": "65", "DE": "49",
    "FR": "33", "IT": "39", "ES": "34", "NL": "31", "IE": "353", "NZ": "64", "ZA": "27", "SA": "966",
    "QA": "974", "KW": "965", "OM": "968", "BH": "973", "PK": "92", "BD": "880", "LK": "94", "NP": "977",
    "MY": "60", "ID": "62", "PH": "63", "TH": "66", "VN": "84", "JP": "81", "KR": "82", "CN": "86",
    "BR": "55", "MX": "52", "NG": "234", "KE": "254", "EG": "20", "TR": "90", "SE": "46", "NO": "47",
    "DK": "45", "FI": "358", "CH": "41", "AT": "43", "BE": "32", "PT": "351", "PL": "48",
}


def normalize_phone(raw: str | None, country_code: str | None = None) -> str | None:
    """Return an E.164-style ``+<digits>`` string, or None for empty input. Raises ValueError if invalid.

    Used to enforce one account per phone number (including soft-deleted accounts for 30 days)."""
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    plus = raw.startswith("+") or raw.startswith("00")
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("00"):
        digits = digits[2:]
    if not plus:
        digits = digits.lstrip("0")
        dial = DIAL_CODES.get((country_code or "").upper())
        if dial and not (len(digits) > 10 and digits.startswith(dial)):
            digits = dial + digits
    if not 8 <= len(digits) <= 15:
        raise ValueError("Enter a valid phone number (with country code if outside your country)")
    return "+" + digits


# ------------------------------------------------------------------ text
def slugify(text: str, max_len: int = 120) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:max_len].strip("-") or "item"


def initials(name: str | None) -> str:
    parts = [p for p in re.split(r"\s+", (name or "").strip()) if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def clamp(text: str | None, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


# bidi embeddings / overrides / isolates (U+202A-U+202E, U+2066-U+2069): a name carrying one reorders the rest of every
# line it is shown in ("pay <RLO>gnp.exe" reads "pay exe.png"); the marks LRM / RLM (U+200E / U+200F) are kept
BIDI_CONTROLS = dict.fromkeys(map(ord, "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"))


def strip_bidi(text: str) -> str:
    """``text`` without bidi embedding / override / isolate controls (names: registration, profile, staff records)."""
    return text.translate(BIDI_CONTROLS)


def mask_middle(value: str | None, keep_end: int = 4, keep_start: int = 0) -> str:
    if not value:
        return ""
    v = re.sub(r"\s+", "", value)
    if len(v) <= keep_end + keep_start:
        return "•" * len(v)
    return v[:keep_start] + "•" * (len(v) - keep_end - keep_start) + v[-keep_end:]


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


def numeric_code(length: int = 6) -> str:
    return "".join(str(secrets.randbelow(10)) for _ in range(length))


def is_safe_redirect(url: str | None) -> bool:
    """Only same-site relative paths are allowed as ?next= targets (prevents open redirects)."""
    if not url or not url.startswith("/") or url.startswith("//") or "\\" in url:
        return False
    parts = urlsplit(url)
    return not parts.scheme and not parts.netloc


# ------------------------------------------------------------------ money
CURRENCY_SYMBOLS = {
    "INR": "₹", "USD": "$", "EUR": "€", "GBP": "£", "AED": "AED ", "AUD": "A$", "CAD": "C$", "SGD": "S$",
    "JPY": "¥", "CNY": "¥", "SAR": "SAR ", "QAR": "QAR ", "NZD": "NZ$", "ZAR": "R", "CHF": "CHF ",
    "PKR": "Rs ", "LKR": "Rs ", "BDT": "৳", "NPR": "Rs ", "MYR": "RM", "PHP": "₱", "THB": "฿", "BRL": "R$",
    "MXN": "MX$", "KES": "KSh ", "NGN": "₦", "SEK": "kr ", "NOK": "kr ", "DKK": "kr ", "KWD": "KD ",
    "OMR": "OMR ", "BHD": "BD ", "IDR": "Rp ", "VND": "₫", "KRW": "₩", "TRY": "₺", "EGP": "E£", "PLN": "zł ",
}

COUNTRY_CURRENCY = {
    "IN": "INR", "US": "USD", "GB": "GBP", "AE": "AED", "AU": "AUD", "CA": "CAD", "SG": "SGD", "DE": "EUR",
    "FR": "EUR", "IT": "EUR", "ES": "EUR", "NL": "EUR", "IE": "EUR", "PT": "EUR", "BE": "EUR", "AT": "EUR",
    "FI": "EUR", "SA": "SAR", "QA": "QAR", "KW": "KWD", "OM": "OMR", "BH": "BHD", "NZ": "NZD", "ZA": "ZAR",
    "PK": "PKR", "LK": "LKR", "BD": "BDT", "NP": "NPR", "MY": "MYR", "PH": "PHP", "TH": "THB", "JP": "JPY",
    "CN": "CNY", "KR": "KRW", "BR": "BRL", "MX": "MXN", "KE": "KES", "NG": "NGN", "SE": "SEK", "NO": "NOK",
    "DK": "DKK", "CH": "CHF", "ID": "IDR", "VN": "VND", "TR": "TRY", "EG": "EGP", "PL": "PLN",
}

TWO_PLACES = Decimal("0.01")


def to_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"Not a valid amount: {value!r}") from exc


def money(amount: Any, currency: str | None = "INR") -> str:
    if amount is None:
        return "—"
    value = to_decimal(amount)
    symbol = CURRENCY_SYMBOLS.get((currency or "").upper(), (currency or "") + " ")
    sign = "-" if value < 0 else ""
    value = abs(value)
    if (currency or "").upper() == "INR":
        whole, frac = f"{value:.2f}".split(".")
        if len(whole) > 3:  # Indian digit grouping 12,34,567
            head, tail = whole[:-3], whole[-3:]
            head = re.sub(r"(\d)(?=(\d{2})+$)", r"\1,", head)
            whole = f"{head},{tail}"
        return f"{sign}{symbol}{whole}.{frac}"
    return f"{sign}{symbol}{value:,.2f}"


# ------------------------------------------------------------------ misc
def bmi(height_cm: float, weight_kg: float) -> float:
    h = height_cm / 100.0
    return round(weight_kg / (h * h), 1) if h > 0 else 0.0


def chunked(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]
