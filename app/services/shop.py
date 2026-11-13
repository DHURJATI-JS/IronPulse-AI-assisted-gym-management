"""WP-L gym shop: catalogue search, deterministic product safety, cart, coupons, checkout/orders, reviews,
inventory and owner analytics. Payments and refunds live in ``app/services/shop_payments.py``.

Money is ``Decimal`` (ROUND_HALF_UP via ``to_decimal``). Prices are tax-exclusive; an order's
``total = subtotal - discount_total + tax_total + shipping_fee``. The coupon discount is spread pro-rata over
the lines before tax, and every ``ShopOrderItem.line_total`` is what the customer paid for that line
(after its discount share, including tax) - so partial refunds by quantity are exact. Every price, discount
and total is computed on the server from the database; client-sent amounts are never trusted.

Service functions never commit - the caller owns the transaction.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from pgvector.sqlalchemy import Vector as VectorType
from sqlalchemy import (
    Index,
    String,
    and_,
    case,
    cast,
    event,
    exists,
    func,
    inspect,
    literal,
    or_,
    select,
    text,
    union,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, contains_eager, joinedload, selectinload
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.orm.util import identity_key

from app.ai.embeddings import embed
from app.core import caps
from app.core.pagination import KeysetPage, Page, keyset, paginate
from app.core.ttl_cache import TTLCache, clear_on_commit
from app.core.utils import clamp, money, normalize_phone, safe_zone, slugify, to_decimal, utcnow
from app.db import long_statement, session_scope
from app.models import (
    Branch,
    CartItem,
    Coupon,
    Gym,
    HealthCondition,
    InventoryMovement,
    Notification,
    Product,
    ProductImage,
    ProductReview,
    Refund,
    ShopOrder,
    ShopOrderItem,
    User,
    WishlistItem,
)
from app.models.base import EMBEDDING_DIM
from app.models.enums import ConditionKind, NotificationCategory, Role, Severity, VerificationStatus
from app.models.shop import OrderStatus, ProductCategory, RefundStatus
from app.services.batching import Chunks, remember, resume_after, slices
from app.services.conditions import TAG_LABELS
from app.services.jobs import periodic
from app.services.notify import Note, notify, notify_bulk

log = logging.getLogger("ironpulse.shop")

# ====================================================================================================== constants
PAGE_SIZE = 24
CATALOGUE_MAX_PAGE = 500  # ?page= is clamped to 1..500
COUNT_CAP = PAGE_SIZE * CATALOGUE_MAX_PAGE  # 12,000: the catalogue counts no further ("12,000+ products")
SEMANTIC_TOP_K = 200  # nearest products by embedding considered by a search (HNSW, pgvector iterative scan)
FACETS_TTL = 60.0  # seconds the category counts, the top stores and store searches are cached (per process)
STORE_OPTIONS = 20  # stores in the catalogue's store filter (the most products); the typeahead finds the others
STORE_SEARCH_LIMIT = 20  # results of the store typeahead
STORE_QUERY_MIN, STORE_QUERY_MAX = 2, 60  # typeahead input length
WISHLIST_PAGE_SIZE = 24
WISHLIST_MAX = 500  # products one member can save
MAX_CART_LINES = 50  # different products in one bag ...
MAX_STORE_LINES = 30  # ... and from one store (one order)
ORDERS_PAGE_SIZE = 20
REVIEWS_PAGE_SIZE = 20  # product page reviews, newest first, "Older reviews" for the rest
PICKUP_PREVIEW = 3  # pickup branches named on a product page (+ how many in all)
MAX_CONDITIONS = 50  # health conditions read for the safety panel (the safety-relevant ones first)
PROMPT_CONDITIONS = 10  # of those, named in the AI product prompt (the ones relevant to that product first)
RATINGS_CHUNK = 500  # products locked + recomputed per statement when their reviews change
# products compared with their reviews per transaction of the reconcile run - and so at most rewritten + locked in one:
# rewriting a product's summary is not a HOT update (indexed columns), so every index of products gets a new entry -
# the HNSW one ~5 ms at 100,000 products - and a checkout waits for a product the run holds: ~1 s at most
RATINGS_BATCH = 200
RATINGS_EVERY_S = 3600  # the reconcile run (also the one-off backfill of rating_avg / review_count): hourly
RATINGS_BUDGET_S = 50.0  # time budget of one reconcile run (the next run carries on where it stopped)
RATINGS_TASK = "shop_product_ratings"
LOW_STOCK_CHUNK = 200  # owners per transaction of the hourly low-stock digest
LOW_STOCK_LISTED = 6  # products named in one digest ("... and N more")
LOW_STOCK_REPEAT_HOURS = 20  # an owner gets at most one digest in this many hours
MAX_QTY = 20
MAX_IMAGES = 8
MAX_PRICE = Decimal("10000000")
MAX_STOCK = 100_000
REFUND_WINDOW_DAYS = 7
UNPAID_TTL_HOURS = 24
# a placed order holds its units for the buyer this long (then other buyers can take them; a later payment is still
# accepted while units are free, else refunded). Short on purpose: unpaid orders must not hoard stock
STOCK_HOLD_MINUTES = 15
SEMANTIC_MAX_DISTANCE = 0.8  # cosine distance of the local hashing embeddings (relevant matches < 0.8)
LOW_STOCK_TITLE = "Low stock in your shop"
SHOPPER_ROLES = (Role.CUSTOMER, Role.STAFF)
ZERO = Decimal("0.00")
HUNDRED = Decimal("100")

CATEGORY_INFO: dict[ProductCategory, tuple[str, str, str]] = {
    ProductCategory.SUPPLEMENTS: ("Supplements", "bolt", "Whey, creatine & pre-workout"),
    ProductCategory.NUTRITION: ("Nutrition", "heart", "Bars, shakes & snacks"),
    ProductCategory.APPAREL: ("Apparel", "user", "Tees, shorts & layers"),
    ProductCategory.ACCESSORIES: ("Accessories", "briefcase", "Bags, belts & shakers"),
    ProductCategory.WEARABLES: ("Wearables", "activity", "Watches & heart-rate straps"),
    ProductCategory.EQUIPMENT: ("Equipment", "dumbbell", "Bands, mats & weights"),
    ProductCategory.OTHER: ("Other", "sparkles", "Everything else"),
}

ALLERGENS: dict[str, str] = {
    "milk": "Milk / dairy", "soy": "Soy", "nuts": "Tree nuts", "peanuts": "Peanuts", "gluten": "Gluten",
    "egg": "Egg", "fish": "Fish", "shellfish": "Shellfish", "sesame": "Sesame",
}

# Safety tags an owner can attach to a product: every health-condition tag plus "minor" (under 18).
CAUTION_TAGS: dict[str, str] = {"minor": "Under 18", **TAG_LABELS}
COMMON_CAUTION_TAGS: tuple[str, ...] = (
    "kidney", "hypertension", "cardiac", "pregnancy", "diabetes", "minor", "epilepsy", "thyroid", "mental_health",
    "eating_disorder", "migraine", "food_allergy",
)
SERIOUS_TAGS = {"kidney", "cardiac", "hypertension", "pregnancy", "epilepsy", "eating_disorder"}

NUTRITION_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("calories", "Energy", "kcal"),
    ("protein_g", "Protein", "g"),
    ("carbs_g", "Carbohydrates", "g"),
    ("sugar_g", "of which sugars", "g"),
    ("fat_g", "Fat", "g"),
    ("caffeine_mg", "Caffeine", "mg"),
)

FULFILLMENT_LABELS = {"pickup": "Pickup at the gym", "delivery": "Home delivery", "both": "Pickup or delivery"}

# flat delivery fee per currency and the order value from which delivery is free
DELIVERY_RULES: dict[str, tuple[str, str]] = {
    "INR": ("49", "999"), "USD": ("4.99", "75"), "EUR": ("4.99", "60"), "GBP": ("3.99", "50"), "AED": ("15", "200"),
    "AUD": ("9.95", "99"), "CAD": ("7.99", "75"), "SGD": ("5", "60"), "NZD": ("8", "99"), "CHF": ("6", "75"),
    "SAR": ("15", "200"), "QAR": ("15", "200"), "KWD": ("1.5", "20"), "OMR": ("1.5", "20"), "BHD": ("1.5", "20"),
    "JPY": ("500", "8000"), "CNY": ("15", "199"), "KRW": ("3000", "50000"), "MYR": ("8", "150"), "PHP": ("99", "1999"),
    "THB": ("50", "1000"), "IDR": ("15000", "300000"), "VND": ("30000", "700000"), "PKR": ("199", "4999"),
    "LKR": ("299", "9999"), "BDT": ("80", "2000"), "NPR": ("100", "3000"), "ZAR": ("60", "900"), "KES": ("200", "5000"),
    "NGN": ("2000", "40000"), "EGP": ("50", "1500"), "BRL": ("15", "250"), "MXN": ("80", "1200"), "TRY": ("100", "1500"),
    "PLN": ("15", "250"), "SEK": ("49", "699"), "NOK": ("49", "699"), "DKK": ("39", "499"),
}
DEFAULT_DELIVERY = ("5", "75")

ORDER_STATUS_INFO: dict[OrderStatus, tuple[str, str, str]] = {
    OrderStatus.PENDING_PAYMENT: ("Awaiting payment", "amber", "clock"),
    OrderStatus.PAID: ("Paid", "green", "check-circle"),
    OrderStatus.PROCESSING: ("Processing", "blue", "refresh"),
    OrderStatus.READY_FOR_PICKUP: ("Ready for pickup", "violet", "map-pin"),
    OrderStatus.SHIPPED: ("Shipped", "blue", "send"),
    OrderStatus.COMPLETED: ("Completed", "green", "check-badge"),
    OrderStatus.CANCELLED: ("Cancelled", "zinc", "x-circle"),
    OrderStatus.PAYMENT_FAILED: ("Payment failed", "red", "alert"),
    OrderStatus.REFUND_REQUESTED: ("Refund requested", "amber", "receipt"),
    OrderStatus.PARTIALLY_REFUNDED: ("Partially refunded", "violet", "receipt"),
    OrderStatus.REFUNDED: ("Refunded", "zinc", "receipt"),
}
REFUND_STATUS_INFO: dict[RefundStatus, tuple[str, str]] = {
    RefundStatus.REQUESTED: ("Requested", "amber"),
    RefundStatus.APPROVED: ("Approved", "blue"),
    RefundStatus.PROCESSING: ("Processing", "blue"),
    RefundStatus.COMPLETED: ("Refunded", "green"),
    RefundStatus.REJECTED: ("Declined", "red"),
    RefundStatus.FAILED: ("Failed", "red"),
}
PAID_STATUSES = (
    OrderStatus.PAID, OrderStatus.PROCESSING, OrderStatus.READY_FOR_PICKUP, OrderStatus.SHIPPED, OrderStatus.COMPLETED,
    OrderStatus.REFUND_REQUESTED, OrderStatus.PARTIALLY_REFUNDED, OrderStatus.REFUNDED,
)
UNPAID_STATUSES = (OrderStatus.PENDING_PAYMENT, OrderStatus.PAYMENT_FAILED)
OWNER_CANCELLABLE = (OrderStatus.PENDING_PAYMENT, OrderStatus.PAYMENT_FAILED, OrderStatus.PAID, OrderStatus.PROCESSING,
                     OrderStatus.READY_FOR_PICKUP)


def order_status_label(status: OrderStatus | str) -> str:
    return ORDER_STATUS_INFO.get(OrderStatus(status), (str(status), "zinc", "info"))[0]


def order_status_tone(status: OrderStatus | str) -> str:
    return ORDER_STATUS_INFO.get(OrderStatus(status), (str(status), "zinc", "info"))[1]


# ====================================================================================================== errors
class ShopError(ValueError):
    """A user-facing validation problem (the message is safe to show)."""


class CheckoutError(ShopError):
    def __init__(self, errors: dict[str, str]) -> None:
        super().__init__("; ".join(errors.values()))
        self.errors = errors


# ====================================================================================================== money
def q2(value: Any) -> Decimal:
    return to_decimal(value)


def delivery_rule(currency: str | None) -> tuple[Decimal, Decimal]:
    fee, free_from = DELIVERY_RULES.get((currency or "").upper(), DEFAULT_DELIVERY)
    return Decimal(fee), Decimal(free_from)


def delivery_fee(currency: str | None, merchandise_total: Decimal) -> Decimal:
    fee, free_from = delivery_rule(currency)
    return ZERO if merchandise_total >= free_from else q2(fee)


def percent_off(product: Product) -> int | None:
    mrp, price = product.compare_at_price, product.price
    if mrp is None or price is None or mrp <= 0 or mrp <= price:
        return None
    pct = int(((mrp - price) / mrp * 100).to_integral_value())
    return pct if pct >= 1 else None


@dataclass(slots=True)
class QuoteLine:
    product_id: int | None
    name: str
    unit_price: Decimal
    tax_rate: Decimal
    quantity: int
    subtotal: Decimal
    discount: Decimal = ZERO
    tax: Decimal = ZERO

    @property
    def total(self) -> Decimal:
        return self.subtotal - self.discount + self.tax


@dataclass(slots=True)
class Quote:
    currency: str
    lines: list[QuoteLine]
    subtotal: Decimal
    discount_total: Decimal
    tax_total: Decimal
    shipping_fee: Decimal
    total: Decimal

    @property
    def item_count(self) -> int:
        return sum(line.quantity for line in self.lines)

    @property
    def merchandise_total(self) -> Decimal:
        return self.subtotal - self.discount_total

    def tax_by_rate(self) -> list[tuple[Decimal, Decimal]]:
        rates: dict[Decimal, Decimal] = defaultdict(lambda: ZERO)
        for line in self.lines:
            if line.tax:
                rates[q2(line.tax_rate)] += line.tax
        return sorted(rates.items())


def build_quote(
    lines: Sequence[tuple[int | None, str, Decimal, Decimal, int]],
    currency: str,
    *,
    discount: Decimal = ZERO,
    shipping: Decimal = ZERO,
) -> Quote:
    """Server-side price maths. ``lines`` = (product_id, name, unit_price, tax_rate %, quantity)."""
    qlines = [QuoteLine(pid, name, q2(price), q2(rate), int(qty), q2(q2(price) * int(qty))) for pid, name, price, rate, qty in lines]
    subtotal = sum((ql.subtotal for ql in qlines), ZERO)
    discount = max(ZERO, min(q2(discount), subtotal))
    if discount > 0 and subtotal > 0:
        remaining = discount
        for ql in qlines:
            share = min(q2(discount * ql.subtotal / subtotal), ql.subtotal, remaining)
            ql.discount, remaining = share, remaining - share
        for ql in sorted(qlines, key=lambda x: x.subtotal - x.discount, reverse=True):  # rounding residue
            if remaining <= 0:
                break
            extra = min(remaining, ql.subtotal - ql.discount)
            ql.discount += extra
            remaining -= extra
    for ql in qlines:
        ql.tax = q2((ql.subtotal - ql.discount) * ql.tax_rate / HUNDRED)
    tax_total = sum((ql.tax for ql in qlines), ZERO)
    shipping = q2(shipping)
    return Quote(currency, qlines, subtotal, discount, tax_total, shipping, subtotal - discount + tax_total + shipping)


def quote_for_order(order: ShopOrder, items: Sequence[ShopOrderItem]) -> Quote:
    """Re-derive the per-line tax/discount split of a stored order (display: receipts, tax breakdown)."""
    return build_quote(
        [(i.product_id, i.product_name, i.unit_price, i.tax_rate, i.quantity) for i in items],
        order.currency, discount=order.discount_total, shipping=order.shipping_fee,
    )


def unit_refund_amount(item: ShopOrderItem, quantity: int) -> Decimal:
    """Amount paid for ``quantity`` more units of a line (exact: refunding every unit returns ``line_total``)."""
    quantity = max(0, min(quantity, item.quantity - item.refunded_quantity))
    before = q2(item.line_total * item.refunded_quantity / item.quantity)
    after = q2(item.line_total * (item.refunded_quantity + quantity) / item.quantity)
    return after - before


# ====================================================================================================== coupons
CODE_RE = re.compile(r"^[A-Z0-9-]{3,32}$")


def normalize_code(raw: Any) -> str:
    return re.sub(r"\s+", "", str(raw or "")).upper()[:40]


def coupon_problem(coupon: Coupon | None, subtotal: Decimal, currency: str, now: datetime | None = None) -> str | None:
    now = now or utcnow()
    if coupon is None:
        return "That coupon code isn't valid for this store."
    if not coupon.is_active:
        return "This coupon is no longer active."
    if coupon.valid_until is not None and coupon.valid_until < now:
        return f"This coupon expired on {coupon.valid_until:%d %b %Y}."
    if coupon.max_uses is not None and coupon.used_count >= coupon.max_uses:
        return "This coupon has reached its usage limit."
    if coupon.min_order_total is not None and subtotal < coupon.min_order_total:
        short = coupon.min_order_total - subtotal
        return (f"This coupon needs a minimum order of {money(coupon.min_order_total, currency)} "
                f"- add {money(short, currency)} more.")
    return None


def coupon_discount(coupon: Coupon, subtotal: Decimal) -> Decimal:
    if coupon.percent_off:
        return min(subtotal, q2(subtotal * Decimal(coupon.percent_off) / HUNDRED))
    return min(subtotal, q2(coupon.amount_off or 0))


def coupon_label(coupon: Coupon, currency: str) -> str:
    return f"{coupon.percent_off}% off" if coupon.percent_off else f"{money(coupon.amount_off, currency)} off"


async def find_coupon(db: AsyncSession, gym_id: int, code: str, *, lock: bool = False) -> Coupon | None:
    code = normalize_code(code)
    if not CODE_RE.match(code):
        return None
    stmt = select(Coupon).where(Coupon.gym_id == gym_id, Coupon.code == code)
    if lock:
        stmt = stmt.with_for_update()
    return await db.scalar(stmt)


# ====================================================================================================== safety
@dataclass(slots=True)
class SafetyFlag:
    code: str
    level: str  # block | danger | warning | info
    title: str
    message: str

    @property
    def text(self) -> str:
        return f"{self.title}: {self.message}"


@dataclass(slots=True)
class SafetyReport:
    personalised: bool
    flags: list[SafetyFlag] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(f.level == "block" for f in self.flags)

    @property
    def block_reason(self) -> str | None:
        return next((f.message for f in self.flags if f.level == "block"), None)

    @property
    def warnings(self) -> list[SafetyFlag]:
        return [f for f in self.flags if f.level in ("block", "danger", "warning")]

    @property
    def notes(self) -> list[SafetyFlag]:
        return [f for f in self.flags if f.level == "info"]

    @property
    def tone(self) -> str:
        if self.blocked or any(f.level == "danger" for f in self.flags):
            return "danger"
        return "warning" if self.warnings else "ok"

    def texts(self) -> list[str]:
        return [f.text for f in self.flags if f.level != "info"] + [f.text for f in self.flags if f.level == "info"]


_ALLERGY_PATTERNS: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (re.compile(r"\b(milk|dairy|lactose|whey|casein|cheese|butter|ghee|curd|paneer)"), ("milk",)),
    (re.compile(r"\b(soy|soya|soybean)"), ("soy",)),
    (re.compile(r"\bpeanuts?\b|\bgroundnuts?\b"), ("peanuts",)),
    (re.compile(r"\b(tree[ -]?nuts?|almonds?|cashews?|walnuts?|hazelnuts?|pistachios?|pecans?|macadamias?)\b"), ("nuts",)),
    (re.compile(r"(?<!tree )(?<!tree-)\bnuts?\b"), ("nuts", "peanuts")),  # generic "nut allergy": be conservative
    (re.compile(r"\b(gluten|wheat|coeliac|celiac|barley|rye)"), ("gluten",)),
    (re.compile(r"\beggs?\b"), ("egg",)),
    (re.compile(r"\b(shellfish|shrimps?|prawns?|crabs?|lobsters?|crustaceans?|molluscs?|mollusks?)\b"), ("shellfish",)),
    (re.compile(r"\b(fish|salmon|tuna|cod|sardines?)\b"), ("fish",)),
    (re.compile(r"\b(sesame|til)\b"), ("sesame",)),
)

_CREATINE = re.compile(r"creatine")
_STIMULANT = re.compile(r"caffeine|pre[- ]?workout|stimulant|thermogenic|fat[- ]?burner|guarana|yohimb|synephrine|taurine")

SPECIFIC_CAUTIONS: dict[tuple[str, str], tuple[str, str]] = {
    ("creatine", "kidney"): (
        "Creatine and kidney disease",
        "Creatine is cleared by the kidneys. With kidney disease, don't take it unless your doctor or nephrologist approves.",
    ),
    ("stimulant", "hypertension"): (
        "Stimulants and high blood pressure",
        "Caffeine and pre-workout stimulants raise blood pressure and heart rate. Avoid them unless your doctor says it's safe.",
    ),
    ("stimulant", "cardiac"): (
        "Stimulants and your heart",
        "Stimulants can trigger palpitations or an irregular heartbeat. Not recommended with a heart condition without medical approval.",
    ),
    ("stimulant", "pregnancy"): (
        "Caffeine during pregnancy",
        "Caffeine should be limited during pregnancy (generally under 200 mg a day) and pre-workout blends are not recommended.",
    ),
}


def _product_text(product: Product) -> str:
    return " ".join(x for x in (product.name, product.description, product.ingredients) if x).lower()


def _caffeine_mg(product: Product) -> float:
    try:
        return float((product.nutrition_facts or {}).get("caffeine_mg") or 0)
    except (TypeError, ValueError):
        return 0.0


def product_traits(product: Product) -> set[str]:
    """Deterministic content traits inferred from the listing (independent of the owner's tags)."""
    traits: set[str] = set()
    consumable = product.is_supplement or product.category in (ProductCategory.SUPPLEMENTS, ProductCategory.NUTRITION)
    if not consumable:
        return traits
    text = _product_text(product)
    if _CREATINE.search(text):
        traits.add("creatine")
    if _STIMULANT.search(text) or _caffeine_mg(product) > 0:
        traits.add("stimulant")
    return traits


def effective_caution_tags(product: Product) -> dict[str, str]:
    """Owner caution tags + tags implied by the product's contents -> {tag: source trait or 'owner'}."""
    tags: dict[str, str] = {t: "owner" for t in (product.caution_tags or []) if t in CAUTION_TAGS}
    traits = product_traits(product)
    if "creatine" in traits:
        tags.setdefault("kidney", "creatine")
    if "stimulant" in traits:
        for t in ("hypertension", "cardiac", "pregnancy"):
            tags.setdefault(t, "stimulant")
    return tags


def is_allergy(c: HealthCondition) -> bool:
    """An allergy or intolerance (whatever kind it was saved as: 'Lactose intolerance' is often a condition)."""
    name = (c.name or "").lower()
    return c.kind == ConditionKind.ALLERGY or c.tag == "food_allergy" or "allerg" in name or "intoleran" in name


def condition_tag_of(c: HealthCondition) -> str | None:
    """A saved row's safety tag - or, for a row saved without one, what the shared normaliser makes of its name today
    (a cheap keyword match), so an item saved before the normaliser knew its words still gets its cautions."""
    if c.tag:
        return c.tag
    from app.services.conditions import normalize_condition

    return normalize_condition(c.name)


def allergy_keys(conditions: Iterable[HealthCondition]) -> dict[str, str]:
    """Map a member's allergies / intolerances (free text) to allergen keys -> {allergen: what they wrote}."""
    found: dict[str, str] = {}
    for c in conditions:
        if not is_allergy(c):
            continue
        name = (c.name or "").lower()
        for pattern, keys in _ALLERGY_PATTERNS:
            if pattern.search(name):
                for k in keys:
                    found.setdefault(k, c.name)
    return found


# words of a saved allergy that say nothing about the substance ("Severe allergy to sucralose" -> "sucralose")
_ALLERGY_FILLER = frozenset(
    "allergy allergies allergic intolerance intolerances intolerant sensitivity sensitive reaction reactions mild "
    "moderate severe serious food foods drug drugs to of and or the with from a an in on my any all some also no not "
    "very bad".split())
_WORDS = re.compile(r"[a-z]+")
MAX_ALLERGY_MENTIONS = 3  # "Check the ingredients" flags per product


def _saved_words(text: str) -> str:
    """The member's own words in a safety note (shown to them and quoted to the product AI): cut to 60 characters, and
    never text that reads like an instruction to the AI."""
    from app.ai.member_context import _injection

    return "an item" if _injection(text) else f"“{clamp(' '.join(str(text).split()), 60)}”"


def allergy_mentions(product: Product, conditions: Sequence[HealthCondition]) -> list[tuple[str, str]]:
    """[(word, what the member wrote)]: a saved allergy / intolerance whose own words the listing itself mentions (its
    name, ingredients or declared allergens) - for what no allergen key covers ('Sucralose', 'Stevia', 'Soya lecithin'
    vs 'soy lecithin'), or an allergen the store forgot to declare. One per saved item, at most MAX_ALLERGY_MENTIONS."""
    declared = set(product.allergens or [])
    text = " ".join(x for x in (product.name, product.ingredients,
                                " ".join(ALLERGENS.get(a, a) for a in declared)) if x).lower()
    if not text.strip():
        return []
    out: list[tuple[str, str]] = []
    for c in conditions:
        if len(out) >= MAX_ALLERGY_MENTIONS:
            break
        if not is_allergy(c) or set(allergy_keys([c])) & declared:
            continue  # not an allergy - or the "Contains ..." allergen flag already speaks for it
        for word in _WORDS.findall((c.name or "").lower()):
            if len(word) < 3 or word in _ALLERGY_FILLER:
                continue
            stem = word[:-1] if len(word) > 4 and word.endswith("s") else word
            if re.search(rf"\b{re.escape(stem)}(?:s|es)?\b", text):
                out.append((stem, c.name))
                break
    return out


def product_safety(product: Product, user: User | None, conditions: Sequence[HealthCondition] = ()) -> SafetyReport:
    """Deterministic safety panel. Always wins over AI text (merged into every AI answer)."""
    report = SafetyReport(personalised=user is not None)
    flags = report.flags
    minor = bool(user is not None and user.is_minor)
    if product.age_restricted:
        if minor:
            flags.append(SafetyFlag("age_restricted", "block", "Adults only (18+)",
                                    "This product is age-restricted and can't be sold to members under 18."))
        else:
            flags.append(SafetyFlag("age_restricted", "info", "18+ product",
                                    "Only members aged 18 or over can buy this product (checked from your date of birth)."))
    tags = effective_caution_tags(product)
    if minor and "minor" in tags and not product.age_restricted:
        flags.append(SafetyFlag("caution:minor", "warning", "Not recommended for under-18s",
                                "The store doesn't recommend this product for teenagers. Please ask your parent/guardian and doctor first."))
    member_tags = {t for t in (condition_tag_of(c) for c in conditions) if t}
    for tag in sorted(set(tags) & member_tags):
        trait = tags[tag]
        label = TAG_LABELS.get(tag, tag.replace("_", " "))
        specific = SPECIFIC_CAUTIONS.get((trait, tag))
        if specific:
            title, message = specific
        else:
            title = f"Caution: {label.lower()}"
            message = (f"The store flags this product for people with {label.lower()}, which is in your health profile. "
                       "Check with your doctor before using it.")
        flags.append(SafetyFlag(f"caution:{tag}", "danger" if tag in SERIOUS_TAGS else "warning", title, message))
    if "diabetes" in member_tags and "diabetes" not in tags:
        try:
            sugar = float((product.nutrition_facts or {}).get("sugar_g") or 0)
        except (TypeError, ValueError):
            sugar = 0.0
        if sugar >= 10:
            flags.append(SafetyFlag("caution:sugar", "warning", "High in sugar",
                                    f"{sugar:g} g of sugar per serving - account for it in your diabetes plan."))
    allergies = allergy_keys(conditions)
    for key in [a for a in (product.allergens or []) if a in allergies]:
        flags.append(SafetyFlag(f"allergen:{key}", "danger", f"Contains {ALLERGENS.get(key, key).lower()}",
                                f"You listed “{allergies[key]}” in your health profile. Don't use this product without "
                                "checking the full ingredient list with your doctor."))
    for word, saved in allergy_mentions(product, conditions):
        flags.append(SafetyFlag(f"allergy-text:{word}", "danger", "Check the ingredients",
                                f"The listing mentions “{word}”, and you listed {_saved_words(saved)} as an allergy or "
                                "intolerance in your health profile. Don't use this product without checking the full "
                                "label with your doctor."))
    if product.is_supplement:
        meds = [c.name for c in conditions if c.kind == ConditionKind.MEDICATION]
        if meds:
            flags.append(SafetyFlag("medication", "warning", "Check for interactions",
                                    f"You take {', '.join(meds[:3])}. Ask your doctor or pharmacist whether this supplement is OK with it."))
        if minor:
            flags.append(SafetyFlag("supplement:minor", "warning", "Supplements under 18",
                                    "Talk to your parent/guardian and a doctor before using supplements."))
        flags.append(SafetyFlag("supplement", "info", "Supplement",
                                "Supplements don't replace a balanced diet. Check with your doctor before starting one, "
                                "especially with a medical condition, medication or pregnancy."))
    return report


async def load_conditions(db: AsyncSession, user: User | None) -> list[HealthCondition]:
    """The member's health conditions for the safety panel, at most MAX_CONDITIONS in SQL (every shop page reads them,
    and rows from before the members' caps can be many), in the order the shop's safety rules need them: one row of
    every distinct tag (the cautions - as ``health.safety_first()``), then allergies (the allergens), then medications
    (the supplement warning), then the tag's other rows, then the rest - oldest first within each. A padded profile
    (untagged notes, or one tag over and over) can't make product pages heavier, nor push a caution, an allergy or a
    medication out of the rows read."""
    if user is None:
        return []
    first_of_tag = and_(HealthCondition.tag.is_not(None),
                        func.row_number().over(partition_by=HealthCondition.tag, order_by=HealthCondition.id) == 1)
    group = case((first_of_tag, 0), (HealthCondition.kind == ConditionKind.ALLERGY, 1),
                 (HealthCondition.kind == ConditionKind.MEDICATION, 2), (HealthCondition.tag.is_not(None), 3), else_=4)
    return list((await db.execute(
        select(HealthCondition).where(HealthCondition.customer_id == user.id)
        .order_by(group, HealthCondition.id).limit(MAX_CONDITIONS)
    )).scalars().all())


def prompt_conditions(product: Product, conditions: Sequence[HealthCondition], *,
                      limit: int = PROMPT_CONDITIONS) -> list[HealthCondition]:
    """The member's conditions an AI prompt about ``product`` may name: at most ``limit`` (PROMPT_CONDITIONS), the
    safety-relevant ones first - those behind one of this product's safety flags (a caution tag it carries, an allergen
    it contains, an allergy the listing mentions, a medication with a supplement, diabetes with a sugary product), then
    the other tagged conditions, allergies and medications, then allergies no allergen key covers, then the member's
    untagged notes ('CKD stage 3') - ``load_conditions`` order within each group. Nothing the member saved is left out
    for being untagged: only the cap cuts, and the prompt counts what it cut. The deterministic safety report still
    reads every condition ``load_conditions`` returned."""
    cautions = effective_caution_tags(product)
    allergens = set(product.allergens or [])
    mentioned = {id(c) for c in conditions for _, saved in allergy_mentions(product, [c])}
    try:
        sugary = float((product.nutrition_facts or {}).get("sugar_g") or 0) >= 10 and "diabetes" not in cautions
    except (TypeError, ValueError):
        sugary = False

    def rank(c: HealthCondition) -> int:
        allergy = set(allergy_keys([c]))
        medication = c.kind == ConditionKind.MEDICATION
        tag = condition_tag_of(c)
        if ((tag and tag in cautions) or allergy & allergens or id(c) in mentioned
                or (medication and product.is_supplement) or (tag == "diabetes" and sugary)):
            return 0
        if tag:
            return 1
        if allergy:
            return 2
        if medication:
            return 3
        return 4 if is_allergy(c) else 5

    ranked = sorted((rank(c), i, c) for i, c in enumerate(conditions))
    return [c for _, _, c in ranked[:max(0, limit)]]


# ====================================================================================================== catalogue
SORTS = {
    "relevance": "Featured",
    "newest": "Newest",
    "price_asc": "Price: low to high",
    "price_desc": "Price: high to low",
    "rating": "Top rated",
}


@dataclass(slots=True)
class ShopFilters:
    q: str = ""
    category: str = ""
    gym_id: int | None = None
    sort: str = "relevance"
    in_stock: bool = False
    page: int = 1

    @property
    def active(self) -> bool:
        return bool(self.q or self.category or self.gym_id or self.in_stock or self.sort != "relevance")

    def params(self, **override: Any) -> dict[str, Any]:
        data = {"q": self.q, "category": self.category, "gym": self.gym_id or "", "sort": self.sort,
                "in_stock": "1" if self.in_stock else "", "page": self.page}
        data.update(override)
        return {k: v for k, v in data.items() if v not in ("", None, "relevance") and not (k == "page" and v == 1)}


def parse_filters(params: Any) -> ShopFilters:
    q = clamp(str(params.get("q") or "").replace("\x00", ""), 80)
    category = str(params.get("category") or "")
    if category not in {c.value for c in ProductCategory}:
        category = ""
    try:
        gym_id = int(params.get("gym") or 0) or None
    except (TypeError, ValueError):
        gym_id = None
    if gym_id is not None and not 0 < gym_id < 2**63:  # a BIGINT id (a longer number would crash the query)
        gym_id = None
    sort = str(params.get("sort") or "relevance")
    if sort not in SORTS:
        sort = "relevance"
    in_stock = str(params.get("in_stock") or "") in ("1", "on", "true")
    try:
        page = max(1, min(CATALOGUE_MAX_PAGE, int(params.get("page") or 1)))
    except (TypeError, ValueError):
        page = 1
    return ShopFilters(q=q, category=category, gym_id=gym_id, sort=sort, in_stock=in_stock, page=page)


@dataclass(slots=True)
class ProductCard:
    product: Product
    cover_key: str | None
    rating: float | None
    review_count: int
    wished: bool = False

    @property
    def url(self) -> str:
        return product_url(self.product)

    @property
    def percent_off(self) -> int | None:
        return percent_off(self.product)

    @property
    def stock_state(self) -> str:
        return stock_state(self.product)


def product_url(product: Product) -> str:
    return f"/shop/p/{product.id}/{product.slug}"


def stock_state(product: Product) -> str:
    if product.stock <= 0:
        return "out"
    if product.stock <= max(product.low_stock_threshold, 0) or product.stock <= 5:
        return "low"
    return "in"


def vector_sql(vec: Sequence[float]) -> Any:
    """Bind an embedding as ``CAST('<text>' AS vector)`` - works with or without the binary pgvector codec on the
    asyncpg connection (the SQLAlchemy VECTOR type binds text, the codec expects a list). Queries and writes."""
    text_value = "[" + ",".join(format(float(x), ".7g") for x in vec) + "]"
    return cast(literal(text_value, String), VectorType(EMBEDDING_DIM))


def cover_key_col() -> Any:
    return (
        select(ProductImage.image_key)
        .where(ProductImage.product_id == Product.id)
        .order_by(ProductImage.sort_order, ProductImage.id)
        .limit(1)
        .correlate(Product)
        .scalar_subquery()
    )


def rating_cols() -> tuple[Any, Any]:
    """(average stars, number) of a product's VISIBLE reviews: the summary stored on the product (``rating_avg`` /
    ``review_count``, kept in step with its reviews - see "review summary" below), never an aggregate per row."""
    return Product.rating_avg, Product.review_count


def public_product_clauses() -> list[Any]:
    return [Product.is_active.is_(True), Gym.is_active.is_(True)]


def _like(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


async def search_products(
    db: AsyncSession, filters: ShopFilters, *, user: User | None = None, page_size: int = PAGE_SIZE
) -> Page[ProductCard]:
    """One catalogue page - constant statements, bounded work whatever the size of the catalogue:

    * text match: ``name / brand ILIKE '%q%'`` - the GIN trigram indexes ix_products_name_trgm / _brand_trgm;
    * semantic match: CTE ``semantic`` = the SEMANTIC_TOP_K products nearest to the query's embedding among those that
      pass the cheap filters (``ORDER BY embedding <=> :q LIMIT 200``: the HNSW index ix_products_embedding_hnsw,
      with pgvector's iterative scan so the filters don't starve it), kept when closer than SEMANTIC_MAX_DISTANCE -
      never a distance computed for every product of the platform; a match is a text match UNION a semantic one;
    * the COUNT stops at COUNT_CAP (the 12,000 products 500 pages can show): ``Page.total_capped`` -> "12,000+";
    * one LIMIT/OFFSET page (a page past the end shows the last one) + the wishlist marks;
    * the orders without a search read an index in order instead of sorting every matching product: "Featured" (in
      stock first - the stored ``in_stock`` -, then newest) ix_products_catalogue_featured, "Top rated" (the stored
      review summary ``rating_avg`` / ``review_count``, no aggregate per product) ix_products_catalogue_rating,
      "Newest" ix_products_catalogue_newest, both price orders ix_products_catalogue_price (forwards / backwards).
    Statements: [hnsw settings, with ``q``] + count + page [+ wishlist marks when signed in]."""
    cheap: list[Any] = []  # conditions on products alone: also applied inside the semantic CTE
    if filters.category:
        cheap.append(Product.category == ProductCategory(filters.category))
    if filters.gym_id:
        cheap.append(Product.gym_id == filters.gym_id)
    if filters.in_stock:
        cheap.append(Product.in_stock.is_(True))
    where: list[Any] = [*public_product_clauses(), *cheap]
    semantic = text_match = None
    if filters.q:
        pattern = _like(filters.q)
        text_match = or_(Product.name.ilike(pattern, escape="\\"), Product.brand.ilike(pattern, escape="\\"))
        distance = Product.embedding.cosine_distance(vector_sql(embed(filters.q)))
        semantic = (
            select(Product.id.label("id"), distance.label("distance"))
            .where(Product.embedding.is_not(None), Product.is_active.is_(True), *cheap)
            .order_by(distance)
            .limit(SEMANTIC_TOP_K)
            .cte("semantic")
            .prefix_with("MATERIALIZED")
        )
        matches = union(select(Product.id).where(text_match).correlate(None),  # its own scan: the trigram BitmapOr
                        select(semantic.c.id).where(semantic.c.distance < SEMANTIC_MAX_DISTANCE))
        where.append(Product.id.in_(matches))
        # pgvector >= 0.8 iterative scan: the HNSW scan goes on past hnsw.ef_search until SEMANTIC_TOP_K rows pass the
        # filters (bounded by hnsw.max_scan_tuples); the CTE re-sorts them by distance. This transaction only. (Raising
        # ef_search instead would make the planner prefer a full scan + sort.)
        await db.execute(text("SELECT set_config('hnsw.iterative_scan', 'relaxed_order', true)"))

    matched = select(Product.id).join(Gym, Gym.id == Product.gym_id).where(*where).limit(COUNT_CAP + 1).subquery("matched")
    total = int(await db.scalar(select(func.count()).select_from(matched)) or 0)
    capped = total > COUNT_CAP
    total = min(total, COUNT_CAP)
    page = min(max(filters.page, 1), max(1, -(-total // page_size)))
    avg, cnt = rating_cols()
    stmt = (
        select(Product, avg.label("rating"), cnt.label("reviews"), cover_key_col().label("cover"))
        .join(Gym, Gym.id == Product.gym_id)
        .options(contains_eager(Product.gym))
        .where(*where)
    )
    in_stock_first = Product.in_stock.desc()
    if filters.sort == "price_asc":  # = ix_products_catalogue_price
        order = [Product.price.asc(), Product.id]
    elif filters.sort == "price_desc":  # = ix_products_catalogue_price, scanned backwards
        order = [Product.price.desc(), Product.id.desc()]
    elif filters.sort == "newest":  # = ix_products_catalogue_newest
        order = [Product.created_at.desc(), Product.id.desc()]
    elif filters.sort == "rating":  # = ix_products_catalogue_rating
        order = [avg.desc().nulls_last(), cnt.desc(), Product.id]
    elif semantic is not None and text_match is not None:
        stmt = stmt.outerjoin(semantic, semantic.c.id == Product.id)
        order = [case((text_match, 0), else_=1), semantic.c.distance.asc().nulls_last(), in_stock_first, Product.id]
    else:  # "Featured" = ix_products_catalogue_featured
        order = [in_stock_first, Product.created_at.desc(), Product.id.desc()]
    stmt = stmt.order_by(*order).limit(page_size).offset((page - 1) * page_size)
    rows = (await db.execute(stmt)).all()
    cards = [ProductCard(p, cover, float(r) if r is not None else None, int(n or 0)) for p, r, n, cover in rows]
    await mark_wished(db, cards, user)
    return Page(items=cards, total=total, page=page, per_page=page_size, total_capped=capped)


async def mark_wished(db: AsyncSession, cards: Sequence[ProductCard], user: User | None) -> None:
    if user is None or not cards:
        return
    ids = [c.product.id for c in cards]
    wished = set((await db.execute(select(WishlistItem.product_id).where(
        WishlistItem.user_id == user.id, WishlistItem.product_id.in_(ids)))).scalars().all())
    for c in cards:
        c.wished = c.product.id in wished


async def product_cards(db: AsyncSession, where: Sequence[Any], *, order: Sequence[Any] = (), limit: int = 8,
                        user: User | None = None) -> list[ProductCard]:
    avg, cnt = rating_cols()
    stmt = (
        select(Product, avg, cnt, cover_key_col())
        .join(Gym, Gym.id == Product.gym_id)
        .options(contains_eager(Product.gym))
        .where(*where)
        .order_by(*(order or [Product.created_at.desc()]))
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()
    cards = [ProductCard(p, cover, float(r) if r is not None else None, int(n or 0)) for p, r, n, cover in rows]
    await mark_wished(db, cards, user)
    return cards


# ------------------------------------------------------------------ catalogue facets: cached in-process (app.core.ttl_cache)
category_cache = TTLCache("shop.category_counts", FACETS_TTL, maxsize=1024)  # key: store id (0 = every store)
stores_cache = TTLCache("shop.top_stores", FACETS_TTL, maxsize=4)
store_search_cache = TTLCache("shop.store_search", FACETS_TTL, maxsize=512)  # key: the lower-cased query


def _changes_catalogue(obj: Any) -> bool:
    """UPDATEs that change a facet: a product's store, category or visibility (not its stock or price)."""
    if not isinstance(obj, Product):
        return True  # any change of a gym (name, active)
    attrs = inspect(obj).attrs
    return any(attrs[c].history.has_changes() for c in ("gym_id", "category", "is_active"))


clear_on_commit((category_cache, stores_cache, store_search_cache), (Product, Gym), changed=_changes_catalogue)

# Store typeahead (``search_stores``): ``gyms.name ILIKE '%q%'`` through pg_trgm, like the product search. The index is
# declared on the model (Gym.__table_args__), so create_all, sync_schema and app.db.create_indexes_concurrently() know
# it even when this module is not imported (scripts/init_db.py, the pre-deploy concurrent build).
GYM_NAME_TRGM = "ix_gyms_name_trgm"
if not any(ix.name == GYM_NAME_TRGM for ix in Gym.__table__.indexes):  # pragma: no cover - declared on the model
    Index(GYM_NAME_TRGM, Gym.__table__.c.name, postgresql_using="gin", postgresql_ops={"name": "gin_trgm_ops"})


@dataclass(frozen=True, slots=True)
class StoreOption:
    """A store (a gym selling at least one active product) in the store filter / typeahead."""

    id: int
    name: str
    slug: str
    products: int | None = None  # active products (top stores only)


async def category_counts(db: AsyncSession, gym_id: int | None = None) -> dict[str, int]:
    """Active products per category, of every store or of one, cached FACETS_TTL seconds (the count reads the whole
    catalogue; it is cleared when this process commits a product or gym change)."""

    async def build() -> tuple[tuple[str, int], ...]:
        where = public_product_clauses() + ([Product.gym_id == gym_id] if gym_id else [])
        rows = (await db.execute(
            select(Product.category, func.count(Product.id)).join(Gym, Gym.id == Product.gym_id).where(*where)
            .group_by(Product.category))).all()
        return tuple((str(getattr(k, "value", k)), int(v)) for k, v in rows)

    return dict(await category_cache.get_or_compute(gym_id or 0, build))


async def top_stores(db: AsyncSession) -> tuple[list[StoreOption], bool]:
    """The catalogue's store filter: the STORE_OPTIONS stores with the most active products (A-Z) and whether there are
    more (the typeahead ``search_stores`` finds those) - never every gym. Cached FACETS_TTL seconds."""

    async def build() -> tuple[tuple[StoreOption, ...], bool]:
        n = func.count(Product.id)
        rows = (await db.execute(
            select(Gym.id, Gym.name, Gym.slug, n)
            .join(Product, Product.gym_id == Gym.id)
            .where(Gym.is_active.is_(True), Product.is_active.is_(True))
            .group_by(Gym.id)
            .order_by(n.desc(), func.lower(Gym.name), Gym.id)
            .limit(STORE_OPTIONS + 1)
        )).all()
        stores = sorted((StoreOption(gid, name, slug, int(count)) for gid, name, slug, count in rows[:STORE_OPTIONS]),
                        key=lambda s: (s.name.lower(), s.id))
        return tuple(stores), len(rows) > STORE_OPTIONS

    stores, more = await stores_cache.get_or_compute("top", build)
    return list(stores), more


async def store_option(db: AsyncSession, gym_id: int) -> StoreOption | None:
    """One active store by id (the selected ``?gym=`` when it is not among the top stores)."""
    row = (await db.execute(select(Gym.id, Gym.name, Gym.slug).where(Gym.id == gym_id, Gym.is_active.is_(True)))).first()
    return StoreOption(row[0], row[1], row[2]) if row else None


def store_query(raw: Any) -> str:
    """The typeahead's input: whitespace collapsed, at most STORE_QUERY_MAX characters ('' when too short)."""
    q = clamp(re.sub(r"\s+", " ", str(raw or "").replace("\x00", "")).strip(), STORE_QUERY_MAX)
    return q if len(q) >= STORE_QUERY_MIN else ""


async def search_stores(db: AsyncSession, raw: Any, *, limit: int = STORE_SEARCH_LIMIT) -> list[StoreOption]:
    """Store typeahead: active gyms selling at least one active product whose name contains the query
    (case-insensitive, ``ILIKE '%q%'`` - the GIN trigram index ix_gyms_name_trgm), names starting with it first, then
    A-Z, at most ``limit`` (20). One statement; cached FACETS_TTL seconds per query."""
    q = store_query(raw)
    if not q:
        return []
    limit = max(1, min(int(limit), STORE_SEARCH_LIMIT))

    async def build() -> tuple[StoreOption, ...]:
        name = func.lower(Gym.name)
        rows = (await db.execute(
            select(Gym.id, Gym.name, Gym.slug)
            .where(Gym.is_active.is_(True), Gym.name.ilike(_like(q), escape="\\"),
                   exists().where(Product.gym_id == Gym.id, Product.is_active.is_(True)))
            .order_by(name.startswith(q.lower(), autoescape=True).desc(), name, Gym.id)
            .limit(STORE_SEARCH_LIMIT)
        )).all()
        return tuple(StoreOption(gid, gname, slug) for gid, gname, slug in rows)

    return list(await store_search_cache.get_or_compute(q.lower(), build))[:limit]


def valid_id(value: Any) -> bool:
    """A BIGINT primary key (a longer number would make the query itself fail: asyncpg refuses it)."""
    return isinstance(value, int) and 0 < value < 2**63


async def load_product(db: AsyncSession, product_id: int) -> Product | None:
    if not valid_id(product_id):
        return None
    return (await db.execute(
        select(Product)
        .options(joinedload(Product.gym), selectinload(Product.images), joinedload(Product.pickup_branch))
        .where(Product.id == product_id)
    )).unique().scalar_one_or_none()


def is_public(product: Product) -> bool:
    return bool(product.is_active and product.gym is not None and product.gym.is_active)


@dataclass(slots=True)
class RatingSummary:
    average: float | None
    count: int
    distribution: dict[int, int]

    def percent(self, stars: int) -> int:
        return int(round(100 * self.distribution.get(stars, 0) / self.count)) if self.count else 0


async def rating_summary(db: AsyncSession, product_id: int) -> RatingSummary:
    rows = (await db.execute(
        select(ProductReview.rating, func.count(ProductReview.id))
        .where(ProductReview.product_id == product_id, ProductReview.is_hidden.is_(False))
        .group_by(ProductReview.rating)
    )).all()
    dist = {int(r): int(n) for r, n in rows}
    count = sum(dist.values())
    avg = round(sum(r * n for r, n in dist.items()) / count, 1) if count else None
    return RatingSummary(avg, count, dist)


async def product_reviews(db: AsyncSession, product_id: int, *, before_id: int | None = None,
                          limit: int | None = None) -> KeysetPage[ProductReview]:
    """Visible reviews of a product, newest first, ``limit`` (<= REVIEWS_PAGE_SIZE) at a time: a keyset window
    (``?before_id=`` = the last review shown) whose cost does not depend on how far the reader went - one statement."""
    stmt = (
        select(ProductReview).options(joinedload(ProductReview.user))
        .where(ProductReview.product_id == product_id, ProductReview.is_hidden.is_(False))
    )
    return await keyset(db, stmt, ProductReview.id, before_id=before_id, limit=limit or REVIEWS_PAGE_SIZE,
                        max_limit=REVIEWS_PAGE_SIZE, order_col=ProductReview.created_at)


async def has_purchased(db: AsyncSession, user_id: int, product_id: int) -> bool:
    """Verified purchaser = a completed order (completed_at set) containing the product."""
    return bool(await db.scalar(select(exists().where(
        ShopOrderItem.product_id == product_id,
        ShopOrderItem.order_id == ShopOrder.id,
        ShopOrder.customer_id == user_id,
        ShopOrder.completed_at.is_not(None),
    ))))


async def is_wished(db: AsyncSession, user: User | None, product_id: int) -> bool:
    if user is None:
        return False
    return bool(await db.scalar(select(exists().where(WishlistItem.user_id == user.id, WishlistItem.product_id == product_id))))


def nutrition_rows(product: Product) -> list[tuple[str, str]]:
    facts = product.nutrition_facts or {}
    rows: list[tuple[str, str]] = []
    for key, label, unit in NUTRITION_FIELDS:
        value = facts.get(key)
        if value in (None, ""):
            continue
        try:
            num = Decimal(str(value)).normalize()
            shown = f"{num:f}"
        except (InvalidOperation, ValueError):
            shown = str(value)
        rows.append((label, f"{shown} {unit}"))
    return rows


# ====================================================================================================== cart & wishlist
@dataclass(slots=True)
class CartLine:
    item: CartItem
    product: Product
    cover_key: str | None
    problem: str | None = None

    @property
    def quantity(self) -> int:
        return self.item.quantity

    @property
    def max_qty(self) -> int:
        return max(1, min(MAX_QTY, self.product.stock))

    @property
    def subtotal(self) -> Decimal:
        return q2(self.product.price * self.item.quantity)

    @property
    def ok(self) -> bool:
        return self.problem is None


@dataclass(slots=True)
class CartGroup:
    gym: Gym
    lines: list[CartLine]
    quote: Quote
    coupon: Coupon | None = None
    coupon_code: str = ""
    coupon_error: str | None = None

    @property
    def problems(self) -> list[str]:
        return [f"{line.product.name}: {line.problem}" for line in self.lines if line.problem]

    @property
    def can_checkout(self) -> bool:
        return bool(self.lines) and not self.problems

    @property
    def delivery_rule(self) -> tuple[Decimal, Decimal]:
        return delivery_rule(self.gym.currency)


def line_problem(product: Product, quantity: int, user: User | None) -> str | None:
    if not is_public(product):
        return "No longer available - please remove it."
    if product.age_restricted and user is not None and user.is_minor:
        return "Adults only (18+) - can't be bought on your account."
    if product.stock <= 0:
        return "Out of stock."
    if quantity > product.stock:
        return f"Only {product.stock} left - reduce the quantity."
    return None


async def load_cart_lines(db: AsyncSession, user: User, *, gym_id: int | None = None) -> list[CartLine]:
    """The bag's lines (``gym_id``: one store's), at most MAX_CART_LINES - ``add_to_cart`` never lets a bag grow past
    that; the LIMIT also bounds a bag filled before the cap existed."""
    stmt = (
        select(CartItem, cover_key_col())
        .join(Product, Product.id == CartItem.product_id)
        .join(Gym, Gym.id == Product.gym_id)
        .options(contains_eager(CartItem.product).contains_eager(Product.gym))
        .where(CartItem.user_id == user.id)
        .order_by(Gym.name, Gym.id, CartItem.created_at, CartItem.id)
        .limit(MAX_CART_LINES)
    )
    if gym_id is not None:
        stmt = stmt.where(Product.gym_id == gym_id)
    rows = (await db.execute(stmt)).all()
    return [CartLine(item, item.product, cover, line_problem(item.product, item.quantity, user)) for item, cover in rows]


def group_quote(lines: Sequence[CartLine], currency: str, *, discount: Decimal = ZERO, shipping: Decimal = ZERO) -> Quote:
    return build_quote([(ln.product.id, ln.product.name, ln.product.price, ln.product.tax_rate, ln.quantity)
                        for ln in lines if ln.ok], currency, discount=discount, shipping=shipping)


async def load_cart(db: AsyncSession, user: User, *, coupons: dict[int, str] | None = None) -> list[CartGroup]:
    lines = await load_cart_lines(db, user)
    by_gym: dict[int, list[CartLine]] = defaultdict(list)
    gyms: dict[int, Gym] = {}
    for line in lines:
        by_gym[line.product.gym_id].append(line)
        gyms[line.product.gym_id] = line.product.gym
    coupons = {gid: normalize_code(c) for gid, c in (coupons or {}).items() if gid in gyms and normalize_code(c)}
    found: dict[tuple[int, str], Coupon] = {}
    if coupons:
        rows = (await db.execute(select(Coupon).where(Coupon.gym_id.in_(list(coupons)),
                                                      Coupon.code.in_(list(coupons.values()))))).scalars().all()
        found = {(c.gym_id, c.code): c for c in rows}
    groups = []
    now = utcnow()
    for gid, glines in by_gym.items():
        gym = gyms[gid]
        base = group_quote(glines, gym.currency)
        group = CartGroup(gym=gym, lines=glines, quote=base)
        code = coupons.get(gid, "")
        if code:
            coupon = found.get((gid, code))
            group.coupon_code = code
            group.coupon_error = coupon_problem(coupon, base.subtotal, gym.currency, now)
            if group.coupon_error is None and coupon is not None:
                group.coupon = coupon
                group.quote = group_quote(glines, gym.currency, discount=coupon_discount(coupon, base.subtotal))
        groups.append(group)
    return groups


async def cart_count(db: AsyncSession, user: User | None) -> int:
    if user is None:
        return 0
    return int(await db.scalar(select(func.coalesce(func.sum(CartItem.quantity), 0)).where(CartItem.user_id == user.id)) or 0)


def can_shop(user: User | None) -> bool:
    return user is not None and user.role in SHOPPER_ROLES


def purchase_problem(product: Product, user: User) -> str | None:
    if not is_public(product):
        return "This product is no longer available."
    if user.role not in SHOPPER_ROLES:
        return "Shopping is available on member accounts. Owners manage their store in the owner shop."
    if product.age_restricted and user.is_minor:
        return "This product is for adults (18+) only, so it can't be added to your bag."
    if product.stock <= 0:
        return "Sorry - this product is out of stock."
    return None


async def add_to_cart(db: AsyncSession, user: User, product: Product, quantity: int) -> tuple[int, str | None]:
    """Upsert a cart line; quantities are clamped to 1..20 and to the stock, and a bag holds at most MAX_CART_LINES
    different products (MAX_STORE_LINES from one store) - so the bag, the checkout and the resulting order are bounded
    by design. Returns (quantity in bag, note); raises ShopError."""
    problem = purchase_problem(product, user)
    if problem:
        raise ShopError(problem)
    quantity = max(1, min(int(quantity or 1), MAX_QTY))
    cap = min(MAX_QTY, product.stock)
    current = int(await db.scalar(select(CartItem.quantity).where(CartItem.user_id == user.id,
                                                                  CartItem.product_id == product.id)) or 0)
    if current >= cap:
        return current, f"Your bag already has the maximum available quantity ({cap})."
    if not current:  # a new line: check the bag's size - under the person's bag lock, so parallel adds count each other
        await caps.lock(db, "bag_lines", user.id)
        lines, store_lines = (await db.execute(
            select(func.count(CartItem.id), func.count(CartItem.id).filter(Product.gym_id == product.gym_id))
            .join(Product, Product.id == CartItem.product_id)
            .where(CartItem.user_id == user.id)
        )).one()
        if int(lines) >= MAX_CART_LINES:
            raise ShopError(f"Your bag is full ({MAX_CART_LINES} different products). Check out or remove something "
                            "first.")
        if int(store_lines) >= MAX_STORE_LINES:
            raise ShopError(f"Your bag already has {MAX_STORE_LINES} different products from {product.gym.name} - "
                            "check them out first, then add more.")
    stmt = (
        pg_insert(CartItem)
        .values(user_id=user.id, product_id=product.id, quantity=min(quantity, cap))
        .on_conflict_do_update(
            index_elements=[CartItem.user_id, CartItem.product_id],
            set_={"quantity": func.least(CartItem.quantity + quantity, cap), "updated_at": func.now()},
        )
        .returning(CartItem.quantity)
    )
    new_qty = int(await db.scalar(stmt) or 0)
    note = None
    if current + quantity > cap:
        note = f"Only {cap} can be ordered at a time{' (that is all the stock)' if cap == product.stock else ''} - your bag has {new_qty}."
    return new_qty, note


async def update_cart_quantity(db: AsyncSession, user: User, product_id: int, quantity: int) -> tuple[int, str | None]:
    """Set a line's quantity (0 removes it). Returns (new quantity, note)."""
    item = await db.scalar(select(CartItem).options(joinedload(CartItem.product).joinedload(Product.gym))
                           .where(CartItem.user_id == user.id, CartItem.product_id == product_id))
    if item is None:
        raise ShopError("That item is not in your bag any more.")
    if quantity <= 0 or not is_public(item.product) or item.product.stock <= 0:
        await db.delete(item)
        if quantity > 0:
            return 0, f"{item.product.name} is no longer available and was removed from your bag."
        return 0, None
    cap = min(MAX_QTY, item.product.stock)
    new_qty = max(1, min(quantity, cap))
    item.quantity = new_qty
    note = f"Only {cap} available - quantity set to {new_qty}." if quantity > cap else None
    return new_qty, note


async def remove_from_cart(db: AsyncSession, user: User, product_id: int) -> bool:
    item = await db.scalar(select(CartItem).where(CartItem.user_id == user.id, CartItem.product_id == product_id))
    if item is None:
        return False
    await db.delete(item)
    return True


async def toggle_wishlist(db: AsyncSession, user: User, product: Product) -> bool:
    """Save / unsave a product. Raises ShopError when saving would exceed WISHLIST_MAX products."""
    existing = await db.scalar(select(WishlistItem).where(WishlistItem.user_id == user.id,
                                                          WishlistItem.product_id == product.id))
    if existing is not None:
        await db.delete(existing)
        return False
    await caps.lock(db, "wishlist", user.id)  # parallel saves count each other: the wishlist never passes its cap
    saved = int(await db.scalar(select(func.count(WishlistItem.id)).where(WishlistItem.user_id == user.id)) or 0)
    if saved >= WISHLIST_MAX:
        raise ShopError(f"Your wishlist is full ({WISHLIST_MAX} products). Remove a product you no longer want to save "
                        "this one.")
    await db.execute(pg_insert(WishlistItem).values(user_id=user.id, product_id=product.id)
                     .on_conflict_do_nothing(index_elements=[WishlistItem.user_id, WishlistItem.product_id]))
    return True


async def wishlist_page(db: AsyncSession, user: User, page: int = 1) -> Page[ProductCard]:
    """One page of a member's wishlist, newest first, WISHLIST_PAGE_SIZE per page (a page past the end shows the last):
    a COUNT of their saved items + one LIMIT/OFFSET page - 2 statements whatever the list's length."""
    avg, cnt = rating_cols()
    stmt = (
        select(Product, avg, cnt, cover_key_col())
        .join(WishlistItem, WishlistItem.product_id == Product.id)
        .join(Gym, Gym.id == Product.gym_id)
        .options(contains_eager(Product.gym))
        .where(WishlistItem.user_id == user.id)
        .order_by(WishlistItem.created_at.desc(), WishlistItem.id.desc())
    )
    result = await paginate(db, stmt, page=page, per_page=WISHLIST_PAGE_SIZE, max_per_page=WISHLIST_PAGE_SIZE,
                            count_stmt=select(func.count(WishlistItem.id)).where(WishlistItem.user_id == user.id))
    cards = [ProductCard(p, cover, float(r) if r is not None else None, int(n or 0), wished=True)
             for p, r, n, cover in result.items]
    return Page(items=cards, total=result.total, page=result.page, per_page=result.per_page)


# ====================================================================================================== checkout
POSTAL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 \-]{1,10}[A-Za-z0-9]$")


def _pickup_clauses(gym_id: int) -> list[Any]:
    return [Branch.gym_id == gym_id, Branch.verification_status == VerificationStatus.VERIFIED, Branch.is_open.is_(True)]


async def pickup_branches(db: AsyncSession, gym_id: int) -> list[Branch]:
    """VERIFIED, open branches of a gym (where orders can be collected) - every one: the checkout's branch choice and
    the owner's product form. A product page shows ``pickup_preview`` instead."""
    return list((await db.execute(
        select(Branch).where(*_pickup_clauses(gym_id)).order_by(Branch.name, Branch.id)
    )).scalars().all())


async def pickup_preview(db: AsyncSession, gym_id: int, *, limit: int = PICKUP_PREVIEW) -> tuple[list[Branch], int]:
    """The first ``limit`` pickup branches of a gym (A-Z) and how many there are - one statement (window count), never
    every branch of a chain (a product page names three)."""
    rows = (await db.execute(
        select(Branch, func.count().over()).where(*_pickup_clauses(gym_id)).order_by(Branch.name, Branch.id).limit(limit)
    )).all()
    return [r[0] for r in rows], int(rows[0][1]) if rows else 0


@dataclass(slots=True)
class CheckoutPlan:
    gym: Gym
    lines: list[CartLine]
    branches: list[Branch]
    methods: list[str]
    fulfillment: str
    coupon: Coupon | None
    coupon_code: str
    coupon_error: str | None
    pickup_quote: Quote
    delivery_quote: Quote
    safety: list[tuple[Product, SafetyReport]]
    problems: list[str] = field(default_factory=list)

    @property
    def quote(self) -> Quote:
        return self.delivery_quote if self.fulfillment == "delivery" else self.pickup_quote

    @property
    def needs_ack(self) -> bool:
        return any(r.warnings for _, r in self.safety)

    @property
    def ok(self) -> bool:
        return not self.problems and bool(self.methods)


async def build_checkout(
    db: AsyncSession, user: User, gym_id: int, *, coupon_code: str = "", fulfillment: str = "",
    conditions: Sequence[HealthCondition] | None = None,
) -> CheckoutPlan | None:
    """Everything the checkout page needs, computed from the database (None = nothing in the bag for that gym)."""
    lines = await load_cart_lines(db, user, gym_id=gym_id)
    if not lines:
        return None
    gym = lines[0].product.gym
    problems = [f"{ln.product.name}: {ln.problem}" for ln in lines if ln.problem]
    branches = await pickup_branches(db, gym.id)
    pinned = {ln.product.pickup_branch_id for ln in lines if ln.product.pickup_branch_id}
    if pinned:
        branches = [b for b in branches if b.id in pinned] if len(pinned) == 1 else []
    methods = []
    if branches and all(ln.product.fulfillment in ("pickup", "both") for ln in lines):
        methods.append("pickup")
    if all(ln.product.fulfillment in ("delivery", "both") for ln in lines):
        methods.append("delivery")
    if not methods:
        problems.append("These items can't be collected or delivered together (different pickup/delivery options or no "
                        "open, verified branch for pickup). Please check them out separately.")
    if fulfillment not in methods:
        fulfillment = methods[0] if methods else "pickup"
    base = group_quote(lines, gym.currency)
    code = normalize_code(coupon_code)
    coupon = None
    coupon_error = None
    discount = ZERO
    if code:
        found = await find_coupon(db, gym.id, code)
        coupon_error = coupon_problem(found, base.subtotal, gym.currency)
        if coupon_error is None and found is not None:
            coupon = found
            discount = coupon_discount(found, base.subtotal)
    pickup_quote = group_quote(lines, gym.currency, discount=discount)
    ship = delivery_fee(gym.currency, pickup_quote.merchandise_total)
    delivery_quote = group_quote(lines, gym.currency, discount=discount, shipping=ship)
    if conditions is None:
        conditions = await load_conditions(db, user)
    safety = [(ln.product, product_safety(ln.product, user, conditions)) for ln in lines]
    return CheckoutPlan(gym, lines, branches, methods, fulfillment, coupon, code, coupon_error, pickup_quote,
                        delivery_quote, safety, problems)


def quote_fingerprint(plan: CheckoutPlan) -> str:
    """What the checkout page showed the buyer: every line (product, unit price, tax rate, quantity), the coupon and
    both totals (pickup / delivery). The page sends it back with "Place order"; a different one means a price, the bag
    or the coupon changed in between - the order is not placed at a total the buyer never saw."""
    parts = [str(plan.gym.id), plan.gym.currency, plan.coupon.code if plan.coupon else ""]
    parts += [f"{ln.product.id}:{q2(ln.product.price)}:{q2(ln.product.tax_rate)}:{ln.quantity}" for ln in plan.lines]
    parts += [str(q2(plan.pickup_quote.total)), str(q2(plan.delivery_quote.total))]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


def parse_address(form: Any, country_code: str) -> tuple[dict[str, str], dict[str, str]]:
    def val(key: str, n: int) -> str:
        v = form.get(key)
        return re.sub(r"\s+", " ", v).strip()[:n] if isinstance(v, str) else ""

    addr = {"name": val("ship_name", 120), "phone": val("ship_phone", 32), "line1": val("ship_line1", 200),
            "city": val("ship_city", 120), "postal_code": val("ship_postal", 20).upper()}
    errors: dict[str, str] = {}
    if len(addr["name"]) < 2:
        errors["ship_name"] = "Enter the recipient's name."
    try:
        normalized = normalize_phone(addr["phone"], country_code)
        if not normalized:
            errors["ship_phone"] = "Enter a phone number for the courier."
    except ValueError as exc:
        errors["ship_phone"] = str(exc)
    if len(addr["line1"]) < 5:
        errors["ship_line1"] = "Enter the street address (house number, street, area)."
    if len(addr["city"]) < 2:
        errors["ship_city"] = "Enter the city."
    if not POSTAL_RE.match(addr["postal_code"]):
        errors["ship_postal"] = "Enter a valid postal / PIN code."
    addr["country_code"] = country_code
    return addr, errors


async def new_order_number(db: AsyncSession) -> str:
    year = utcnow().year
    for _ in range(20):
        candidate = f"IPO-{year}-{secrets.randbelow(10**6):06d}"
        if not await db.scalar(select(exists().where(ShopOrder.order_number == candidate))):
            return candidate
    raise RuntimeError("could not allocate a unique order number")


def customer_label(user: User) -> str:
    return clamp(f"{user.full_name} <{user.email}>" if user.email else user.full_name, 160)


async def reserve_coupon(db: AsyncSession, coupon: Coupon) -> None:
    """A coupon with ``max_uses`` counts its paid uses (``used_count``) AND the unpaid orders already carrying it: under
    the coupon's row lock, so buyers checking out at the same moment can never redeem it more often than allowed (the
    last use goes to the first order placed; an unpaid order that expires or is cancelled frees its use). Raises
    CheckoutError when it is used up."""
    locked = await db.scalar(select(Coupon).where(Coupon.id == coupon.id).with_for_update()
                             .execution_options(populate_existing=True))
    if locked is None or locked.max_uses is None:
        return
    left = max(0, int(locked.max_uses) - int(locked.used_count or 0))
    pending = await caps.count_upto(db, select(ShopOrder.id).where(
        ShopOrder.gym_id == locked.gym_id, ShopOrder.coupon_code == locked.code,
        ShopOrder.status.in_(UNPAID_STATUSES)), left + 1)
    if pending >= left:
        raise CheckoutError({"coupon": f"{locked.code} has been fully used (its last uses are held by orders waiting "
                                       "for payment) - remove it to check out."})


# ------------------------------------------------------------------ stock holds (the last units, many buyers at once)
# A placed order holds its units for STOCK_HOLD_MINUTES (``ShopOrder.stock_held_until``): checkout refuses what other
# buyers' open checkouts hold, so when N buyers race for k units only k reach the payment page - nobody is charged and
# then refunded. The hold is computed, never a counter that can drift: an order holds while it is unpaid and its
# ``stock_held_until`` is in the future. It ends exactly once, under the order's row lock: paid (converted into the
# sale), cancelled, superseded, failed, flagged or expired (the field is cleared; the status alone already ends it).
# Every hold decision runs under the products' row locks (FOR NO KEY UPDATE, in id order - the lock a payment's FOR
# UPDATE waits for), taken before any coupon row lock, like the payment's order -> products -> coupon.
def holding_clauses(now: datetime | None = None) -> list[Any]:
    """SQL conditions on ``ShopOrder``: the order still holds its units right now (the app's clock, the one that set
    ``stock_held_until`` - not the transaction's start time)."""
    return [ShopOrder.stock_held_until.is_not(None), ShopOrder.stock_held_until > (now or utcnow()),
            ShopOrder.status.in_(UNPAID_STATUSES)]


def holds_stock(order: ShopOrder, now: datetime | None = None) -> bool:
    return (order.stock_held_until is not None and order.stock_held_until > (now or utcnow())
            and order.status in UNPAID_STATUSES)


async def held_units(db: AsyncSession, product_ids: Iterable[int], *, exclude_order_id: int | None = None) -> dict[int, int]:
    """{product id: units held by open checkouts} (``exclude_order_id``: not counting that order's own hold)."""
    ids = sorted({int(i) for i in product_ids if i})
    if not ids:
        return {}
    stmt = (
        select(ShopOrderItem.product_id, func.sum(ShopOrderItem.quantity))
        .join(ShopOrder, ShopOrder.id == ShopOrderItem.order_id)
        .where(ShopOrderItem.product_id.in_(ids), *holding_clauses())
        .group_by(ShopOrderItem.product_id)
    )
    if exclude_order_id is not None:
        stmt = stmt.where(ShopOrder.id != exclude_order_id)
    return {int(pid): int(n or 0) for pid, n in (await db.execute(stmt)).all()}


@dataclass(frozen=True, slots=True)
class StockRow:
    """A product's stock as read under its row lock (columns only: the session's Product objects are left untouched)."""
    id: int
    stock: int
    listed: bool  # the product and its gym are active


async def lock_for_hold(db: AsyncSession, product_ids: Iterable[int]) -> dict[int, StockRow]:
    """The products' rows locked FOR NO KEY UPDATE, in id order, with their current stock - serialises every hold
    decision on these products (and waits for a payment's FOR UPDATE of them)."""
    ids = sorted({int(i) for i in product_ids if i})
    if not ids:
        return {}
    rows = (await db.execute(
        select(Product.id, Product.stock, Product.is_active, Gym.is_active)
        .join(Gym, Gym.id == Product.gym_id)
        .where(Product.id.in_(ids)).order_by(Product.id)
        .with_for_update(of=Product, key_share=True)
    )).all()
    return {int(pid): StockRow(int(pid), int(stock), bool(active and gym_active))
            for pid, stock, active, gym_active in rows}


def hold_problem(row: StockRow | None, name: str, quantity: int, held: int) -> str | None:
    """Why ``quantity`` units can't be held for a new checkout (None = they can)."""
    if row is None or not row.listed:
        return f"{name}: no longer available - please remove it from your bag."
    free = max(0, row.stock - held)
    if quantity <= free:
        return None
    if held and free <= 0:
        return (f"{name}: the last units are being bought by other members right now. They're held for "
                f"{STOCK_HOLD_MINUTES} minutes - try again later, or remove it from your bag.")
    if held:
        return (f"{name}: only {free} left right now (others are held by members checking out) - reduce the quantity "
                f"to {free} or try again in {STOCK_HOLD_MINUTES} minutes.")
    return f"{name}: " + ("out of stock - please remove it from your bag." if free <= 0 else
                          f"only {free} left - reduce the quantity.")


async def renew_stock_hold(db: AsyncSession, order: ShopOrder) -> list[str]:
    """Before a payment is (re)started for an unpaid order: make sure its units are held for it. A hold that lapsed is
    taken again when the units are still free (for another STOCK_HOLD_MINUTES; a live hold is never extended, so an
    order can't keep units by asking again and again). Returns the names of the items that went to other buyers
    meanwhile (the caller cancels the order before any money moves) - empty when the hold is in place. ``order`` must be
    locked by the caller."""
    if holds_stock(order):
        return []
    items = list((await db.execute(select(ShopOrderItem).where(ShopOrderItem.order_id == order.id)
                                   .order_by(ShopOrderItem.id))).scalars().all())
    products = await lock_for_hold(db, [i.product_id for i in items if i.product_id])
    held = await held_units(db, products, exclude_order_id=order.id)
    gone = [i.product_name for i in items
            if hold_problem(products.get(i.product_id) if i.product_id else None, i.product_name, i.quantity,
                            held.get(i.product_id or 0, 0))]
    if not gone:
        order.stock_held_until = utcnow() + timedelta(minutes=STOCK_HOLD_MINUTES)
    return gone


async def place_order(
    db: AsyncSession,
    user: User,
    plan: CheckoutPlan,
    *,
    fulfillment: str,
    branch_id: int | None,
    form: Any,
    safety_ack: bool,
    ip: str | None,
) -> ShopOrder:
    """Create a PENDING_PAYMENT order (+ item snapshots) from a freshly built plan, holding its units for
    STOCK_HOLD_MINUTES. Raises CheckoutError (the caller rolls back)."""
    errors: dict[str, str] = {}
    if plan.problems:
        errors["cart"] = plan.problems[0]
    if fulfillment not in plan.methods:
        errors["fulfillment"] = "Choose how you'd like to receive your order."
    if plan.coupon_code and plan.coupon is None:
        errors["coupon"] = plan.coupon_error or "That coupon can't be used."
    branch = None
    address = None
    if fulfillment == "pickup":
        branch = next((b for b in plan.branches if b.id == branch_id), None)
        if branch is None:
            errors["branch"] = "Choose the branch where you'll collect your order."
    elif fulfillment == "delivery":
        address, addr_errors = parse_address(form, plan.gym.country_code)
        errors.update(addr_errors)
    if plan.needs_ack and not safety_ack:
        errors["safety_ack"] = "Please confirm you've read the safety notes for your items."
    if errors:
        raise CheckoutError(errors)
    plan.fulfillment = fulfillment
    quote = plan.quote
    if quote.total <= 0:
        raise CheckoutError({"cart": "Your order total must be greater than zero."})
    now = utcnow()
    # orders per member (approved caps, app/core/caps.py): unpaid orders at once and orders per day - both under the
    # member's locks (taken before any order or coupon row lock), so a double submit or parallel tabs count each other
    try:
        await caps.lock(db, "open_orders_per_member", user.id)
        await caps.enforce(db, "orders_per_member_day", user.id, select(ShopOrder.id).where(
            ShopOrder.customer_id == user.id, ShopOrder.created_at >= caps.since()))
    except caps.CapReached as exc:
        raise CheckoutError({"cart": str(exc)}) from None
    # older unpaid orders for the same store are superseded by this one (a late payment is auto-refunded)
    stale = (await db.execute(select(ShopOrder).where(
        ShopOrder.customer_id == user.id, ShopOrder.gym_id == plan.gym.id,
        ShopOrder.status.in_(UNPAID_STATUSES)).with_for_update())).scalars().all()
    for old in stale:
        old.status, old.cancelled_at, old.stock_held_until = OrderStatus.CANCELLED, now, None
    await db.flush()
    try:
        await caps.enforce(db, "open_orders_per_member", user.id, select(ShopOrder.id).where(
            ShopOrder.customer_id == user.id, ShopOrder.status.in_(UNPAID_STATUSES)))
    except caps.CapReached as exc:
        raise CheckoutError({"cart": str(exc)}) from None
    # hold the units (see "stock holds" above): what other buyers' open checkouts hold can't be bought here
    products = await lock_for_hold(db, [ql.product_id for ql in quote.lines])
    held = await held_units(db, products)
    names = {ln.product.id: ln.product.name for ln in plan.lines}
    for ql in quote.lines:
        problem = hold_problem(products.get(ql.product_id), names.get(ql.product_id, ql.name), ql.quantity,
                               held.get(ql.product_id, 0))
        if problem:
            raise CheckoutError({"cart": problem})
    if plan.coupon is not None and plan.coupon.max_uses is not None:
        await reserve_coupon(db, plan.coupon)
    order = ShopOrder(
        order_number=await new_order_number(db),
        gym_id=plan.gym.id,
        customer_id=user.id,
        customer_label=customer_label(user),
        status=OrderStatus.PENDING_PAYMENT,
        fulfillment=fulfillment,
        pickup_branch_id=branch.id if branch else None,
        shipping_address=address,
        subtotal=quote.subtotal,
        tax_total=quote.tax_total,
        shipping_fee=quote.shipping_fee,
        discount_total=quote.discount_total,
        total=quote.total,
        currency=plan.gym.currency,
        coupon_code=plan.coupon.code if plan.coupon else None,
        created_ip=(ip or "")[:45] or None,
        stock_held_until=now + timedelta(minutes=STOCK_HOLD_MINUTES),
    )
    db.add(order)
    await db.flush()
    by_id = {ln.product.id: ln.product for ln in plan.lines}
    for ql in quote.lines:
        product = by_id[ql.product_id]
        name = f"{product.brand} {product.name}" if product.brand and not product.name.lower().startswith(product.brand.lower()) else product.name
        db.add(ShopOrderItem(order_id=order.id, product_id=product.id, product_name=clamp(name, 160),
                             unit_price=ql.unit_price, tax_rate=ql.tax_rate, quantity=ql.quantity, line_total=ql.total))
    await db.flush()
    return order


# ====================================================================================================== orders (read side)
async def load_order(db: AsyncSession, order_id: int, *, customer_id: int | None = None, owner_id: int | None = None,
                     lock: bool = False) -> ShopOrder | None:
    """Order + items (+ refunds, gym, branch, customer). Scoped to a customer or a gym owner (None if foreign)."""
    if not valid_id(order_id):
        return None
    stmt = select(ShopOrder).where(ShopOrder.id == order_id)
    if customer_id is not None:
        stmt = stmt.where(ShopOrder.customer_id == customer_id)
    if owner_id is not None:
        stmt = stmt.where(ShopOrder.gym_id.in_(select(Gym.id).where(Gym.owner_id == owner_id)))
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
        order = await db.scalar(stmt)
        if order is None:
            return None
        return await load_order(db, order.id)
    # no populate_existing here: the customer may be the signed-in user (same identity in this session) and a
    # refresh would reset their eagerly loaded customer_profile (lazy="raise") used by the page chrome.
    stmt = stmt.options(
        selectinload(ShopOrder.items).joinedload(ShopOrderItem.product),
        selectinload(ShopOrder.refunds),
        joinedload(ShopOrder.gym),
        joinedload(ShopOrder.pickup_branch),
        joinedload(ShopOrder.customer),
    )
    return (await db.execute(stmt)).unique().scalar_one_or_none()


async def order_covers(db: AsyncSession, product_ids: Iterable[int | None]) -> dict[int, str]:
    ids = sorted({p for p in product_ids if p})
    if not ids:
        return {}
    rows = (await db.execute(
        select(ProductImage.product_id, ProductImage.image_key)
        .where(ProductImage.product_id.in_(ids))
        .order_by(ProductImage.product_id, ProductImage.sort_order, ProductImage.id)
        .distinct(ProductImage.product_id)
    )).all()
    return {pid: key for pid, key in rows}


async def customer_orders(db: AsyncSession, user: User, page: int = 1) -> Page[ShopOrder]:
    """One page of a member's orders, newest first, ORDERS_PAGE_SIZE per page (older orders are one click away instead
    of cut off): COUNT + page + its items (selectin) - 3 statements. An order has at most MAX_STORE_LINES lines."""
    stmt = (
        select(ShopOrder)
        .options(selectinload(ShopOrder.items), joinedload(ShopOrder.gym))
        .where(ShopOrder.customer_id == user.id)
        .order_by(ShopOrder.created_at.desc(), ShopOrder.id.desc())
    )
    return await paginate(db, stmt, page=page, per_page=ORDERS_PAGE_SIZE, max_per_page=ORDERS_PAGE_SIZE, unique=True,
                          count_stmt=select(func.count(ShopOrder.id)).where(ShopOrder.customer_id == user.id))


def refund_window_ends(order: ShopOrder) -> datetime | None:
    return order.completed_at + timedelta(days=REFUND_WINDOW_DAYS) if order.completed_at else None


def refundable_items(order: ShopOrder, items: Sequence[ShopOrderItem] | None = None,
                     refunds: Sequence[Refund] | None = None) -> list[tuple[ShopOrderItem, int]]:
    """(item, units still refundable) excluding units held by an open request."""
    items = order.items if items is None else items
    refunds = order.refunds if refunds is None else refunds
    held: dict[int, int] = defaultdict(int)
    for r in refunds:
        if r.status in (RefundStatus.REQUESTED, RefundStatus.APPROVED, RefundStatus.PROCESSING):
            for entry in r.items or []:
                try:
                    held[int(entry.get("order_item_id"))] += int(entry.get("quantity") or 0)
                except (TypeError, ValueError, AttributeError):
                    continue
    out = []
    for item in items:
        left = item.quantity - item.refunded_quantity - held.get(item.id, 0)
        if left > 0:
            out.append((item, left))
    return out


def refund_request_problem(order: ShopOrder, now: datetime | None = None, *, items: Sequence[ShopOrderItem] | None = None,
                           refunds: Sequence[Refund] | None = None) -> str | None:
    now = now or utcnow()
    if order.status not in (OrderStatus.COMPLETED, OrderStatus.PARTIALLY_REFUNDED) or order.completed_at is None:
        if order.status == OrderStatus.REFUND_REQUESTED:
            return "A refund request for this order is already being reviewed."
        return "Refunds can be requested once your order is completed (picked up or delivered)."
    ends = refund_window_ends(order)
    if ends is not None and now > ends:
        return f"The {REFUND_WINDOW_DAYS}-day refund window for this order closed on {ends:%d %b %Y}."
    if not refundable_items(order, items, refunds):
        return "Every item in this order has already been refunded."
    return None


@dataclass(slots=True)
class TimelineStep:
    label: str
    at: datetime | None
    done: bool
    tone: str = "blue"
    note: str | None = None


def order_timeline(order: ShopOrder, events: Sequence[Any] = ()) -> list[TimelineStep]:
    """Progress steps from timestamps + status-change audit events (AuditLog rows of this order)."""
    seen: dict[str, Any] = {}
    for e in events:
        to = (e.detail or {}).get("to")
        if to and to not in seen:
            seen[to] = e
    s = order.status
    steps = [TimelineStep("Order placed", order.created_at, True, "blue")]
    if s in (OrderStatus.PENDING_PAYMENT, OrderStatus.PAYMENT_FAILED) or (s == OrderStatus.CANCELLED and order.paid_at is None):
        if s == OrderStatus.PAYMENT_FAILED:
            steps.append(TimelineStep("Payment failed", order.updated_at, True, "red"))
        elif s == OrderStatus.PENDING_PAYMENT:
            steps.append(TimelineStep("Awaiting payment", None, False, "amber"))
        if s == OrderStatus.CANCELLED:
            steps.append(TimelineStep("Cancelled before payment", order.cancelled_at, True, "zinc"))
        return steps
    steps.append(TimelineStep("Payment received", order.paid_at, order.paid_at is not None, "green"))
    rank = {OrderStatus.PAID: 1, OrderStatus.PROCESSING: 2, OrderStatus.READY_FOR_PICKUP: 3, OrderStatus.SHIPPED: 3,
            OrderStatus.COMPLETED: 4, OrderStatus.REFUND_REQUESTED: 4, OrderStatus.PARTIALLY_REFUNDED: 4,
            OrderStatus.REFUNDED: 4}
    current = rank.get(s, 0)
    if s == OrderStatus.CANCELLED:
        current = max((rank.get(OrderStatus(k), 0) for k in seen if k in OrderStatus._value2member_map_ and k != "cancelled"),
                      default=1)
    proc = seen.get("processing")
    steps.append(TimelineStep("Processing", proc.created_at if proc else None, current >= 2 or proc is not None, "blue"))
    if order.fulfillment == "pickup":
        ev = seen.get("ready_for_pickup")
        steps.append(TimelineStep("Ready for pickup", ev.created_at if ev else None, current >= 3 or ev is not None, "violet"))
    else:
        ev = seen.get("shipped")
        note = (ev.detail or {}).get("note") if ev else None
        steps.append(TimelineStep("Shipped", ev.created_at if ev else None, current >= 3 or ev is not None, "blue", note))
    if s == OrderStatus.CANCELLED:
        steps = [st for st in steps if st.done]
        steps.append(TimelineStep("Cancelled", order.cancelled_at, True, "zinc"))
    else:
        steps.append(TimelineStep("Picked up" if order.fulfillment == "pickup" else "Delivered", order.completed_at,
                                  order.completed_at is not None, "green"))
    return steps


# ====================================================================================================== reviews
async def save_review(db: AsyncSession, user: User, product: Product, *, rating: int, title: str, body: str) -> ProductReview:
    if not 1 <= rating <= 5:
        raise ShopError("Choose a rating from 1 to 5 stars.")
    if not await has_purchased(db, user.id, product.id):
        raise ShopError("Only verified purchasers (with a completed order for this product) can review it.")
    title = clamp(re.sub(r"\s+", " ", title or "").strip(), 120) or None
    body = clamp((body or "").strip(), 2000) or None
    # one review per (product, member): two tabs or a retry posting at the same moment are taken one after the other
    # (until COMMIT / ROLLBACK) - the later one edits the review the first wrote, never a duplicate-key error (a 500)
    await caps.lock(db, "product_review", f"{product.id}:{user.id}")
    review = await db.scalar(select(ProductReview).where(ProductReview.product_id == product.id,
                                                         ProductReview.user_id == user.id)
                             .execution_options(populate_existing=True))
    if review is None:
        review = ProductReview(product_id=product.id, user_id=user.id, rating=rating, title=title, body=body)
        db.add(review)
    else:
        review.rating, review.title, review.body = rating, title, body
    await db.flush()  # the flush hook below rewrites the product's rating_avg / review_count in this transaction
    return review


# ------------------------------------------------------------------ review summary on products (rating_avg / review_count)
# Product cards and the catalogue's "Top rated" order read the summary stored on the product. It is rewritten in the
# SAME transaction as every change of a product's reviews:
# * any ORM flush that inserts, deletes or changes (rating, visibility, product) a ProductReview - a member's review,
#   the owner's hide / unhide, the demo loader -: the ``after_flush`` hook below, whichever module made the change;
# * a change that bypasses the ORM unit of work (a Core DELETE / UPDATE of product_reviews, the reviews an ON DELETE
#   CASCADE removes with a purged account) calls ``refresh_product_ratings(db, product_ids)`` itself;
# * ``backfill_product_ratings`` (hourly @periodic, idempotent) brings any product whose summary disagrees with its
#   reviews back in line: the one-off backfill when the columns are added (0 / NULL), and the safety net for raw SQL.
# Concurrency: the product rows are locked (FOR NO KEY UPDATE, in id order) BEFORE the aggregate is computed, by a
# separate statement. Under READ COMMITTED that statement sees every review change committed by a transaction that held
# the lock before, and a writer still waiting for the lock recomputes after this one commits - concurrent reviews never
# lose one. NO KEY UPDATE (the lock an UPDATE of non-key columns takes anyway), never FOR UPDATE: inserting a review
# holds FOR KEY SHARE on its product (the foreign-key check) until COMMIT, and FOR UPDATE conflicts with that - two
# members reviewing one product at the same moment would each wait for the other's key-share lock: a deadlock.
_REVIEW_FIELDS = ("rating", "is_hidden", "product_id")


def _summary_values() -> dict[str, Any]:
    products = Product.__table__
    visible = and_(ProductReview.product_id == products.c.id, ProductReview.is_hidden.is_(False))
    return {
        "rating_avg": select(func.round(func.avg(ProductReview.rating), 4)).where(visible).scalar_subquery(),
        "review_count": select(func.count(ProductReview.id)).where(visible).scalar_subquery(),
        "updated_at": products.c.updated_at,  # a review is not an edit of the listing: no onupdate stamp
    }


def _refresh_ratings_sync(session: Session, product_ids: Iterable[int], *, skip_locked: bool = False) -> list[int]:
    """Lock, then recompute the summary of these products in ``session``'s transaction (sync: the flush hook and
    ``run_sync``), RATINGS_CHUNK per statement. ``skip_locked`` leaves out a product another transaction holds right now
    (the reconcile run). A Product loaded in the session gets the new values too (``set_committed_value``: no lazy load,
    nothing marked dirty). Returns the ids rewritten."""
    products = Product.__table__
    ids = sorted({int(i) for i in product_ids if i})
    if not ids:
        return []
    conn = session.connection()
    done: list[int] = []
    for chunk in slices(ids, RATINGS_CHUNK):
        locked = list(conn.execute(
            select(products.c.id).where(products.c.id.in_(chunk)).order_by(products.c.id)
            .with_for_update(key_share=True, skip_locked=skip_locked)  # FOR NO KEY UPDATE [SKIP LOCKED]
        ).scalars())
        if not locked:
            continue
        rows = conn.execute(
            update(products).where(products.c.id.in_(locked)).values(**_summary_values())
            .returning(products.c.id, products.c.rating_avg, products.c.review_count)
        ).all()
        for pid, avg, count in rows:
            loaded = session.identity_map.get(identity_key(Product, pid))
            if loaded is not None:
                set_committed_value(loaded, "rating_avg", avg)
                set_committed_value(loaded, "review_count", count)
        done.extend(locked)
    return done


async def refresh_product_ratings(db: AsyncSession, product_ids: Iterable[int]) -> int:
    """Rewrite rating_avg / review_count of these products from their visible reviews in the caller's transaction - for
    review changes the flush hook can't see (Core statements, ON DELETE CASCADE). Returns how many were rewritten."""
    ids = list(product_ids)
    return len(await db.run_sync(lambda s: _refresh_ratings_sync(s, ids)))


def _reviewed_products(session: Session) -> set[int]:
    """Products whose reviews this flush inserts, deletes or changes (after_flush still sees the pre-flush state)."""
    ids: set[Any] = set()
    for obj in (*session.new, *session.deleted):
        if isinstance(obj, ProductReview):
            ids.add(inspect(obj).dict.get("product_id"))  # never loads (an unloaded id is left to the reconcile run)
    for obj in session.dirty:
        if isinstance(obj, ProductReview):
            attrs = inspect(obj).attrs
            if any(attrs[name].history.has_changes() for name in _REVIEW_FIELDS):
                ids.add(inspect(obj).dict.get("product_id"))
                ids.update(attrs.product_id.history.deleted)  # moved to another product: the old one as well
    ids.discard(None)
    return ids


@event.listens_for(Session, "after_flush")
def _review_summary_after_flush(session: Session, _ctx: Any) -> None:
    # no try/except: a summary that can't be written fails the flush, so a review and its summary never disagree
    ids = _reviewed_products(session)
    if ids:
        _refresh_ratings_sync(session, ids)


async def reconcile_ratings_chunk(db: AsyncSession, *, after_id: int = 0,
                                  limit: int = RATINGS_BATCH) -> tuple[int, int | None, int]:
    """The next ``limit`` products (id > ``after_id``, keyset on the primary key) compared with their visible reviews in
    ONE statement (an aggregate over those products' reviews only); the ones whose summary disagrees are rewritten
    (locked with SKIP LOCKED: a product another transaction holds right now is left to it / the next run). Returns
    (products rewritten, the chunk's last id or None, products compared)."""
    chunk = (
        select(Product.id.label("id"), Product.rating_avg.label("avg"), Product.review_count.label("n"))
        .where(Product.id > after_id).order_by(Product.id).limit(limit)
        .cte("rating_chunk").prefix_with("MATERIALIZED")
    )
    stats = (
        select(ProductReview.product_id.label("product_id"),
               func.round(func.avg(ProductReview.rating), 4).label("avg"), func.count(ProductReview.id).label("n"))
        .where(ProductReview.product_id.in_(select(chunk.c.id)), ProductReview.is_hidden.is_(False))
        .group_by(ProductReview.product_id)
        .subquery("rating_stats")
    )
    drifted = or_(chunk.c.avg.is_distinct_from(stats.c.avg), chunk.c.n != func.coalesce(stats.c.n, 0))
    last, scanned, ids = (await db.execute(
        select(func.max(chunk.c.id), func.count(), func.array_agg(chunk.c.id).filter(drifted))
        .select_from(chunk.outerjoin(stats, stats.c.product_id == chunk.c.id))
    )).one()
    fixed = await db.run_sync(lambda s: _refresh_ratings_sync(s, ids, skip_locked=True)) if ids else []
    return len(fixed), last, int(scanned or 0)


async def backfill_product_ratings(holder: AsyncSession | None = None, *, batch: int = RATINGS_BATCH,
                                   budget_s: float | None = RATINGS_BUDGET_S) -> int:
    """Bring every product's rating_avg / review_count in line with its visible reviews, ``batch`` products per short
    transaction (``Chunks``; ``holder`` = a periodic's lock session). Idempotent - a product already in line is never
    written - so it is both the one-off backfill after the columns are added and the hourly safety net. A run that
    spends ``budget_s`` leaves a cursor and the next one carries on there. Returns how many products were rewritten::

        python -c "import asyncio; from app.services.shop import backfill_product_ratings as b; print(asyncio.run(b()))"
    """
    run = Chunks(holder=holder, budget_s=budget_s)
    last_id, fixed = resume_after(RATINGS_TASK), 0
    while await run.next():
        async with session_scope() as s:
            done, last, scanned = await reconcile_ratings_chunk(s, after_id=last_id, limit=batch)
        fixed += done
        if last is not None:
            last_id = int(last)
        run.done(scanned, batch)
    if run.stopped_early:
        remember(RATINGS_TASK, last_id)
    return fixed


@periodic(RATINGS_TASK, every_seconds=RATINGS_EVERY_S, initial_delay=60)
async def _product_ratings(db: AsyncSession) -> None:
    fixed = await backfill_product_ratings(db)
    if fixed:
        log.info("product ratings: %s product%s brought in line with their reviews", fixed, "" if fixed == 1 else "s")


# ====================================================================================================== owner: products
def owned_gym_ids(owner_id: int) -> Any:
    return select(Gym.id).where(Gym.owner_id == owner_id)


async def get_owned_product(db: AsyncSession, product_id: int, owner: User) -> Product | None:
    return (await db.execute(
        select(Product)
        .options(joinedload(Product.gym), selectinload(Product.images), joinedload(Product.pickup_branch))
        .join(Gym, Gym.id == Product.gym_id)
        .where(Product.id == product_id, Gym.owner_id == owner.id)
    )).unique().scalar_one_or_none()


def embedding_text(product: Product) -> str:
    category = product.category.value if isinstance(product.category, ProductCategory) else str(product.category)
    return " ".join(x for x in (product.name, product.brand, category, product.description, product.ingredients) if x)


def refresh_embedding(product: Product) -> None:
    """Set Product.embedding as a SQL expression (rendered into the INSERT/UPDATE; don't read it back afterwards)."""
    product.embedding = vector_sql(embed(embedding_text(product)))


async def unique_product_slug(db: AsyncSession, gym_id: int, name: str, *, exclude_id: int | None = None) -> str:
    base = slugify(name, max_len=160)
    stmt = select(Product.slug).where(Product.gym_id == gym_id, or_(Product.slug == base, Product.slug.like(f"{base}-%")))
    if exclude_id is not None:
        stmt = stmt.where(Product.id != exclude_id)
    taken = set((await db.execute(stmt)).scalars().all())
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


SKU_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,63}$")


def _txt(form: Any, key: str, n: int = 255) -> str:
    v = form.get(key)
    return v.strip()[:n] if isinstance(v, str) else ""


def _checked(form: Any, key: str) -> bool:
    return _txt(form, key).lower() in ("1", "on", "true", "yes")


def _decimal(raw: str, *, lo: Decimal, hi: Decimal) -> Decimal:
    raw = raw.replace(",", "").strip()
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Enter a number") from exc
    if not value.is_finite():
        raise ValueError("Enter a number")
    value = q2(value)
    if value < lo or value > hi:
        raise ValueError(f"Must be between {lo.normalize():f} and {hi.normalize():f}")
    return value


def _int(raw: str, *, lo: int, hi: int) -> int:
    try:
        value = int(raw.strip())
    except (ValueError, AttributeError) as exc:
        raise ValueError("Enter a whole number") from exc
    if not lo <= value <= hi:
        raise ValueError(f"Must be between {lo} and {hi}")
    return value


def parse_product_form(form: Any, *, branches: Sequence[Branch], creating: bool) -> tuple[dict[str, Any], dict[str, str]]:
    """Validate the owner product form. Returns (values, errors)."""
    v: dict[str, Any] = {}
    e: dict[str, str] = {}
    v["name"] = re.sub(r"\s+", " ", _txt(form, "name", 200))
    if not 2 <= len(v["name"]) <= 160:
        e["name"] = "Name must be 2-160 characters."
    cat = _txt(form, "category")
    try:
        v["category"] = ProductCategory(cat)
    except ValueError:
        v["category"] = ProductCategory.OTHER
        e["category"] = "Choose a category."
    v["brand"] = re.sub(r"\s+", " ", _txt(form, "brand", 120)) or None
    if v["brand"] and len(v["brand"]) > 80:
        e["brand"] = "Brand must be at most 80 characters."
    v["description"] = _txt(form, "description", 6000)
    if len(v["description"]) < 10:
        e["description"] = "Describe the product in at least 10 characters."
    elif len(v["description"]) > 5000:
        e["description"] = "Description must be at most 5000 characters."
    try:
        v["price"] = _decimal(_txt(form, "price", 30), lo=ZERO, hi=MAX_PRICE)
    except ValueError as exc:
        v["price"] = None
        e["price"] = f"Price: {exc}."
    mrp = _txt(form, "compare_at_price", 30)
    v["compare_at_price"] = None
    if mrp:
        try:
            v["compare_at_price"] = _decimal(mrp, lo=ZERO, hi=MAX_PRICE)
            if v["price"] is not None and v["compare_at_price"] < v["price"]:
                e["compare_at_price"] = "MRP (compare-at price) must be at least the selling price."
        except ValueError as exc:
            e["compare_at_price"] = f"MRP: {exc}."
    try:
        v["tax_rate"] = _decimal(_txt(form, "tax_rate", 10) or "0", lo=ZERO, hi=Decimal("50"))
    except ValueError as exc:
        v["tax_rate"] = ZERO
        e["tax_rate"] = f"Tax %: {exc}."
    v["sku"] = _txt(form, "sku", 80) or None
    if v["sku"] and not SKU_RE.match(v["sku"]):
        e["sku"] = "SKU: letters, digits, '.', '-', '_' or '/' (max 64)."
    if creating:
        try:
            v["stock"] = _int(_txt(form, "stock", 10) or "0", lo=0, hi=MAX_STOCK)
        except ValueError as exc:
            v["stock"] = 0
            e["stock"] = f"Stock: {exc}."
    try:
        v["low_stock_threshold"] = _int(_txt(form, "low_stock_threshold", 10) or "5", lo=0, hi=10_000)
    except ValueError as exc:
        v["low_stock_threshold"] = 5
        e["low_stock_threshold"] = f"Low-stock alert: {exc}."
    v["is_active"] = _checked(form, "is_active")
    v["is_supplement"] = _checked(form, "is_supplement")
    v["age_restricted"] = _checked(form, "age_restricted")
    allergens = form.getlist("allergens") if hasattr(form, "getlist") else []
    v["allergens"] = [a for a in ALLERGENS if a in allergens]
    tags = form.getlist("caution_tags") if hasattr(form, "getlist") else []
    v["caution_tags"] = [t for t in CAUTION_TAGS if t in tags]
    v["ingredients"] = _txt(form, "ingredients", 4100) or None
    if v["ingredients"] and len(v["ingredients"]) > 4000:
        e["ingredients"] = "Ingredients must be at most 4000 characters."
    facts: dict[str, Any] = {}
    serving = re.sub(r"\s+", " ", _txt(form, "serving_size", 80))
    if serving:
        if len(serving) > 60:
            e["serving_size"] = "Serving size must be at most 60 characters."
        facts["serving_size"] = serving[:60]
    for key, label, unit in NUTRITION_FIELDS:
        raw = _txt(form, key, 20)
        if not raw:
            continue
        try:
            hi = Decimal("1000") if key == "caffeine_mg" else Decimal("5000")
            num = _decimal(raw, lo=ZERO, hi=hi)
            facts[key] = float(num) if num != num.to_integral_value() else int(num)
        except ValueError as exc:
            e[key] = f"{label} ({unit}): {exc}."
    v["nutrition_facts"] = facts or None
    ful = _txt(form, "fulfillment") or "both"
    v["fulfillment"] = ful if ful in FULFILLMENT_LABELS else "both"
    bid = _txt(form, "pickup_branch_id")
    v["pickup_branch_id"] = None
    if bid:
        allowed = {b.id: b for b in branches}
        try:
            branch_id = int(bid)
        except ValueError:
            branch_id = -1
        if branch_id not in allowed:
            e["pickup_branch_id"] = "Choose one of your verified, open branches."
        else:
            v["pickup_branch_id"] = branch_id
    if v["fulfillment"] == "delivery":
        v["pickup_branch_id"] = None
    if v["fulfillment"] == "pickup" and not branches:
        e["fulfillment"] = "Pickup needs a verified, open branch. Choose delivery (or both) until a branch is verified."
    return v, e


async def adjust_stock(
    db: AsyncSession, product: Product, change: int, *, reason: str, actor: User | None = None,
    order_id: int | None = None, note: str | None = None,
) -> InventoryMovement:
    """Change stock atomically (row lock) and record an InventoryMovement. Never below zero."""
    locked = await db.scalar(select(Product).where(Product.id == product.id).with_for_update()
                             .execution_options(populate_existing=True))
    if locked is None:
        raise ShopError("Product not found.")
    new_stock = locked.stock + change
    if new_stock < 0:
        raise ShopError(f"Stock can't go below zero (currently {locked.stock}).")
    if new_stock > MAX_STOCK:
        raise ShopError(f"Stock can't exceed {MAX_STOCK}.")
    locked.stock = new_stock
    mv = InventoryMovement(product_id=locked.id, change=change, reason=reason[:30], order_id=order_id,
                           actor_id=actor.id if actor else None, note=clamp(note, 255) if note else None)
    db.add(mv)
    return mv


async def product_has_orders(db: AsyncSession, product_id: int) -> bool:
    return bool(await db.scalar(select(exists().where(ShopOrderItem.product_id == product_id))))


# ====================================================================================================== owner: analytics
@dataclass(slots=True)
class SalesFigure:
    currency: str
    today: Decimal
    month: Decimal
    orders_today: int
    orders_month: int


async def owner_dashboard(db: AsyncSession, owner: User) -> dict[str, Any]:
    """SQL aggregates only (constant number of queries)."""
    gyms = owned_gym_ids(owner.id)
    tz = safe_zone(owner.timezone)
    local = datetime.now(tz)
    today_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = today_start.replace(day=1)
    net = ShopOrder.total - ShopOrder.refunded_total
    sales_rows = (await db.execute(
        select(
            ShopOrder.currency,
            func.coalesce(func.sum(case((ShopOrder.paid_at >= today_start, net), else_=0)), 0),
            func.coalesce(func.sum(net), 0),
            func.count(case((ShopOrder.paid_at >= today_start, 1))),
            func.count(ShopOrder.id),
        )
        .where(ShopOrder.gym_id.in_(gyms), ShopOrder.paid_at.is_not(None), ShopOrder.paid_at >= month_start)
        .group_by(ShopOrder.currency)
        .order_by(func.sum(net).desc())
    )).all()
    sales = [SalesFigure(c, q2(t), q2(m), int(nt), int(nm)) for c, t, m, nt, nm in sales_rows]
    by_status = {OrderStatus(s): int(n) for s, n in (await db.execute(
        select(ShopOrder.status, func.count(ShopOrder.id)).where(ShopOrder.gym_id.in_(gyms)).group_by(ShopOrder.status)
    )).all()}
    low_stock = list((await db.execute(
        select(Product, cover_key_col()).join(Gym, Gym.id == Product.gym_id).options(contains_eager(Product.gym))
        .where(Gym.owner_id == owner.id, Product.is_active.is_(True), Product.stock <= Product.low_stock_threshold)
        .order_by(Product.stock, Product.name).limit(8)
    )).all())
    refunds = list((await db.execute(
        select(Refund).join(ShopOrder, ShopOrder.id == Refund.order_id).options(contains_eager(Refund.order))
        .where(ShopOrder.gym_id.in_(gyms), Refund.status == RefundStatus.REQUESTED)
        .order_by(Refund.created_at).limit(8)
    )).scalars().all())
    since = utcnow() - timedelta(days=30)
    top = (await db.execute(
        select(ShopOrderItem.product_id, ShopOrderItem.product_name,
               func.sum(ShopOrderItem.quantity - ShopOrderItem.refunded_quantity).label("units"),
               func.sum(ShopOrderItem.line_total).label("revenue"), ShopOrder.currency)
        .join(ShopOrder, ShopOrder.id == ShopOrderItem.order_id)
        .where(ShopOrder.gym_id.in_(gyms), ShopOrder.paid_at >= since,
               ShopOrder.status.notin_([OrderStatus.CANCELLED, OrderStatus.REFUNDED]))
        .group_by(ShopOrderItem.product_id, ShopOrderItem.product_name, ShopOrder.currency)
        .order_by(func.sum(ShopOrderItem.quantity - ShopOrderItem.refunded_quantity).desc())
        .limit(5)
    )).all()
    counts = (await db.execute(
        select(func.count(Product.id), func.count(case((Product.is_active.is_(True), 1))),
               func.count(case((and_(Product.is_active.is_(True), Product.stock <= 0), 1))))
        .where(Product.gym_id.in_(gyms))
    )).one()
    return {
        "sales": sales,
        "by_status": by_status,
        "low_stock": low_stock,
        "refunds": refunds,
        "top": top,
        "product_total": int(counts[0]),
        "product_active": int(counts[1]),
        "product_out": int(counts[2]),
        "to_fulfil": sum(by_status.get(s, 0) for s in (OrderStatus.PAID, OrderStatus.PROCESSING,
                                                       OrderStatus.READY_FOR_PICKUP, OrderStatus.SHIPPED)),
    }


# ====================================================================================================== periodic: low stock
def _low_stock_product() -> list[Any]:
    """An active product at or below its low-stock threshold, in an active gym (the query must JOIN Gym)."""
    return [Product.is_active.is_(True), Gym.is_active.is_(True), Product.stock <= Product.low_stock_threshold]


async def low_stock_digest_chunk(db: AsyncSession, *, after_owner_id: int = 0, limit: int | None = None,
                                 now: datetime | None = None) -> tuple[int, int | None, int]:
    """Digest the next ``limit`` (LOW_STOCK_CHUNK) due owners with an id above ``after_owner_id`` in the caller's
    transaction. Returns (digests sent, the chunk's last owner id, owners in the chunk).

    ONE statement finds them and aggregates in SQL: CTE ``due_owners`` = owners (account not deleted) with a low-stock
    product and no digest within LOW_STOCK_REPEAT_HOURS - NOT EXISTS on ix_notifications_user_created - keyset-paged by
    owner id; then per owner the number of low-stock products and the first LOW_STOCK_LISTED (lowest stock first) -
    at most ``limit`` x 6 rows. The chunk's owners are loaded in one more statement, then notified (a notification +
    an e-mail each)."""
    now = now or utcnow()
    limit = max(1, int(limit or LOW_STOCK_CHUNK))
    recent = select(Notification.id).where(
        Notification.user_id == Gym.owner_id, Notification.created_at >= now - timedelta(hours=LOW_STOCK_REPEAT_HOURS),
        Notification.title == LOW_STOCK_TITLE,
    ).correlate(Gym).exists()
    low = select(Product.id).where(Product.gym_id == Gym.id, Product.is_active.is_(True),
                                   Product.stock <= Product.low_stock_threshold).correlate(Gym).exists()
    due = (
        select(Gym.owner_id.label("owner_id"))
        .join(User, User.id == Gym.owner_id)
        .where(Gym.owner_id > after_owner_id, Gym.is_active.is_(True), User.deleted_at.is_(None), low, ~recent)
        .group_by(Gym.owner_id)
        .order_by(Gym.owner_id)
        .limit(limit)
        .cte("due_owners")
        .prefix_with("MATERIALIZED")
    )
    ranked = (
        select(
            Gym.owner_id.label("owner_id"), Product.name.label("name"), Product.stock.label("stock"),
            func.row_number().over(partition_by=Gym.owner_id, order_by=(Product.stock, Product.name, Product.id)).label("rn"),
            func.count().over(partition_by=Gym.owner_id).label("n"),
        )
        .join(Gym, Gym.id == Product.gym_id)
        .join(due, due.c.owner_id == Gym.owner_id)
        .where(*_low_stock_product())
        .subquery("ranked_low_stock")
    )
    rows = (await db.execute(
        select(ranked.c.owner_id, ranked.c.name, ranked.c.stock, ranked.c.n)
        .where(ranked.c.rn <= LOW_STOCK_LISTED).order_by(ranked.c.owner_id, ranked.c.rn)
    )).all()
    if not rows:
        return 0, None, 0
    digests: dict[int, tuple[int, list[tuple[str, int]]]] = {}
    for owner_id, name, stock, n in rows:
        digests.setdefault(owner_id, (int(n), []))[1].append((name, int(stock)))
    owners = {u.id: u for u in (await db.execute(select(User).where(User.id.in_(list(digests))))).scalars()}
    notes: list[Note] = []
    for owner_id, (total, items) in digests.items():
        listing = "; ".join(f"{name} ({'out of stock' if stock <= 0 else f'{stock} left'})" for name, stock in items)
        more = f" and {total - len(items)} more" if total > len(items) else ""
        body = (f"{total} product{'s are' if total != 1 else ' is'} running low: {listing}{more}. "
                "Restock them so members can keep ordering.")
        owner = owners.get(owner_id)
        if owner is not None:
            notes.append(Note(owner, LOW_STOCK_TITLE, body, link="/owner/shop/products?status=low",
                              category=NotificationCategory.SYSTEM, severity=Severity.WARNING))
    sent = len(await notify_bulk(db, notes)) if notes else 0  # one bulk send for the chunk (never one per owner)
    return sent, max(digests), len(digests)


async def send_low_stock_digests(db: AsyncSession, *, now: datetime | None = None) -> int:
    """Notify each owner (at most once per LOW_STOCK_REPEAT_HOURS) about their active products at/below the low-stock
    threshold - chunk after chunk of LOW_STOCK_CHUNK owners, all in the caller's transaction (the caller commits).
    The hourly periodic uses ``run_low_stock_digests`` (one transaction per chunk) instead."""
    sent, cursor = 0, 0
    while True:
        done, last, owners = await low_stock_digest_chunk(db, after_owner_id=cursor, now=now)
        sent += done
        if last is None or owners < LOW_STOCK_CHUNK:
            return sent
        cursor = last


async def run_low_stock_digests(*, now: datetime | None = None) -> int:
    """The hourly digest run: each chunk of LOW_STOCK_CHUNK owners in its own short transaction (``session_scope``,
    committed per chunk), so no transaction - and no list of owners - grows with the platform."""
    now = now or utcnow()
    sent, cursor = 0, 0
    while True:
        async with session_scope() as db:
            done, last, owners = await low_stock_digest_chunk(db, after_owner_id=cursor, now=now)
        sent += done
        if last is None or owners < LOW_STOCK_CHUNK:
            return sent
        cursor = last


@periodic("shop_low_stock_digest", every_seconds=3600, initial_delay=90)
async def _low_stock_digest(db: AsyncSession) -> None:
    # db's transaction stays idle while the chunks commit in their own transactions (run_periodic holds the task's lock
    # on a connection of its own): lift its idle limit only (as app/services/retention.py does)
    await long_statement(db, idle_ms=0)
    sent = await run_low_stock_digests()
    if sent:
        log.info("low-stock digest: %s owner%s notified", sent, "" if sent == 1 else "s")
