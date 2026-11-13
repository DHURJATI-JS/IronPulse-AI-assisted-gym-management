"""WP-B owner overview + gyms, branches, opening hours, features, documents, AI verification, photos, close/reopen.

Routes (all owner-only, every query scoped by ``Gym.owner_id``; foreign IDs -> 404):
  GET  /owner                                   overview: KPIs, onboarding checklist, gyms, activity
  GET  /owner/gyms                              gym cards
  GET/POST /owner/gyms/new                      create a gym brand
  GET/POST /owner/gyms/{id}                     edit (+ logo) ; branches list ; mode toggles
  POST /owner/gyms/{id}/online                  action=enable|disable|close|reopen
  POST /owner/gyms/{id}/offline                 action=enable|disable  ("this gym has physical locations")
  GET/POST /owner/gyms/{id}/branches/new        create a branch (details, hours, features, map pin)
  GET/POST /owner/branches/{id}                 manage a branch (stepper, details form, docs, verification, photos)
  POST /owner/branches/{id}/documents           upload a compliance document (encrypted)
  POST /owner/branches/{id}/documents/{doc}/delete
  GET  /owner/branches/{id}/documents/{doc}/file   authorised inline download (no-store)
  POST /owner/branches/{id}/submit              queue AI verification
  POST /owner/branches/{id}/photos              upload photos (only when VERIFIED, <= 12)
  POST /owner/branches/{id}/photos/{photo}/delete
  POST /owner/branches/{id}/close | /reopen     (reopening never needs re-verification)
  GET  /api/owner/branches/{id}/status          live verification status (JSON, polled by the page)
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Annotated, Any
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, HTTPException, Path, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload
from starlette.datastructures import UploadFile

from app.config import get_settings
from app.core import caps
from app.core.caps import CAPS
from app.core.deps import DB, NotFound, OwnerUser, get_owned_branch, get_owned_gym
from app.core.geo import haversine_km, valid_coords
from app.core.templating import redirect, render
from app.core.utils import (
    COUNTRY_CURRENCY,
    CURRENCY_SYMBOLS,
    is_valid_timezone,
    local_now,
    minutes,
    money,
    normalize_phone,
    parse_hhmm,
    slugify,
    utcnow,
)
from app.models import (
    Branch,
    BranchDocument,
    BranchFeature,
    BranchHours,
    BranchPhoto,
    CustomerProfile,
    Employee,
    Gym,
    MisconductReport,
    Notification,
    OtpChallenge,
    Plan,
    SalaryStatement,
    Subscription,
    Transaction,
    User,
)
from app.models.enums import (
    BranchDocType,
    EmploymentStatus,
    IdStatus,
    NotificationCategory,
    PhotoKind,
    PlanMode,
    ReportStatus,
    Severity,
    StatementStatus,
    SubscriptionStatus,
    TxDirection,
    TxStatus,
    VerificationStatus,
)
from app.services import branch_verification  # noqa: F401 - registers the "verify_branch" job handler
from app.services import compliance, member_notices, quotas, storage  # member_notices registers "notify_members"
from app.services.audit import audit
from app.services.notify import notify
from app.services.visibility import branch_photo_count, is_branch_published, published_branch_clauses

log = logging.getLogger("ironpulse.owner")
router = APIRouter()
MAX_ID = 2**63 - 1  # BIGINT ids: a larger path id is a 422 page, never a driver error (a 500)
IdParam = Annotated[int, Path(ge=1, le=MAX_ID)]

VS = VerificationStatus
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
STANDARD_FEATURES: tuple[tuple[str, str], ...] = (
    ("Air conditioning", "wind"), ("Cardio zone", "activity"), ("Free weights", "dumbbell"), ("Machines", "settings"),
    ("Functional area", "layers"), ("Showers", "sparkles"), ("Lockers", "lock"), ("Parking", "map-pin"),
    ("Sauna", "thermometer"), ("Steam room", "wind"), ("Pool", "activity"), ("Women-only hours", "users"),
    ("Wheelchair access", "user"), ("Personal training", "bolt"), ("Group classes", "user-group"),
    ("Nutrition desk", "heart"),
)
STANDARD_FEATURE_NAMES = {name.lower(): name for name, _ in STANDARD_FEATURES}
MAX_CUSTOM_FEATURES = 10
# photos / documents per branch: the defaults of caps "branch_photos" / "branch_documents" (app/core/caps.py), which
# enforce them race-safely - pages read caps.limit() so a CAPS_OVERRIDES value shows everywhere
MAX_PHOTOS = CAPS["branch_photos"].default
MAX_DOCUMENTS = CAPS["branch_documents"].default
IN_PROGRESS = (VS.QUEUED, VS.PROCESSING)
SUBMITTABLE = (VS.DRAFT, VS.REJECTED, VS.NEEDS_REVIEW, VS.AI_UNAVAILABLE)
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[A-Za-z]{2,}$")
POSTAL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 \-]{1,18}[A-Za-z0-9]$")
DOC_MAGIC = (b"%PDF-", b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n")
PHOTO_KIND_LABELS = {PhotoKind.MACHINES: "Machines & equipment", PhotoKind.OVERALL: "The whole gym", PhotoKind.OTHER: "Other"}


# =====================================================================================================
# small helpers
# =====================================================================================================
def owner_missing_steps(user: User) -> list[str]:
    """What still blocks this owner from verification / online mode (photo + VERIFIED ID)."""
    missing = []
    if not user.photo_key:
        missing.append("Upload a profile photo")
    if user.id_status != IdStatus.VERIFIED:
        missing.append("Verify your government ID")
    return missing


def owner_ready(user: User) -> bool:
    return not owner_missing_steps(user)


def _txt(form: Any, key: str) -> str:
    value = form.get(key)
    return value.strip() if isinstance(value, str) else ""


def _file(form: Any, key: str) -> UploadFile | None:
    value = form.get(key)
    return value if isinstance(value, UploadFile) and value.filename else None


def _files(form: Any, key: str) -> list[UploadFile]:
    return [v for v in form.getlist(key) if isinstance(v, UploadFile) and v.filename]


def _checked(form: Any, key: str) -> bool:
    return _txt(form, key).lower() in ("1", "on", "true", "yes")


def _fmt_minutes(total: int) -> str:
    h, m = divmod(total, 60)
    return f"{h} h {m:02d} min" if m else f"{h} h"


def _currency_options() -> list[tuple[str, str]]:
    codes = sorted(set(COUNTRY_CURRENCY.values()) | set(CURRENCY_SYMBOLS))
    return [(c, f"{c} ({CURRENCY_SYMBOLS.get(c, c).strip()})") for c in codes]


def _human_size(n: int | None) -> str:
    n = int(n or 0)
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


def _parse_date(value: str) -> date | None:
    return date.fromisoformat(value) if value else None


def _as_utc(d: date | None) -> datetime | None:
    return datetime(d.year, d.month, d.day, tzinfo=UTC) if d else None


def _greeting(tz: str | None) -> str:
    hour = local_now(tz).hour
    return "Good morning" if 5 <= hour < 12 else "Good afternoon" if 12 <= hour < 17 else "Good evening"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


async def _unique_slug(db: AsyncSession, name: str) -> str:
    base = slugify(name, max_len=120)
    taken = set(
        (await db.execute(select(Gym.slug).where(or_(Gym.slug == base, Gym.slug.like(f"{base}-%"))))).scalars().all()
    )
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


async def _notify_members(
    db: AsyncSession, *, gym_id: int, title: str, body: str, link: str | None, severity: Severity,
    branch_id: int | None = None, modes: tuple[PlanMode, ...] | None = None,
) -> int:
    """Tell every active subscriber of a gym (optionally of one branch / plan modes). The request only counts them
    and queues one ``notify_members`` job (sent in batches once the caller commits - app/services/member_notices.py),
    so it never grows with the member count. Returns how many members will be notified."""
    return await member_notices.queue_member_notice(db, gym_id=gym_id, title=title, body=body, link=link,
                                                    severity=severity, branch_id=branch_id, modes=modes)


async def _lock_gym_state(db: AsyncSession, gym: Gym) -> None:
    """Lock the gym row (until COMMIT) and re-read its mode flags: the same switch sent from several tabs, or by two
    admins, at once runs one request after the other, each on the state the previous one saved - one change, one
    member notice, one audit row; the others answer 'already ...'."""
    await db.refresh(gym, ["online_enabled", "online_open", "offline_enabled"], with_for_update=True)


async def _lock_branch_state(db: AsyncSession, branch: Branch) -> None:
    """The same for a branch's open / closed state (close and reopen)."""
    await db.refresh(branch, ["is_open", "closed_at", "closed_reason"], with_for_update=True)


def _notice_text(n: int) -> str:
    return f"{_plural(n, 'member')} will be notified." if n else "No active members to notify."


# =====================================================================================================
# gym form
# =====================================================================================================
GYM_FIELDS = ("name", "tagline", "description", "country_code", "currency", "contact_email", "contact_phone", "website")


def _gym_values(gym: Gym | None, user: User) -> dict[str, str]:
    if gym is None:
        cc = (user.country_code or "IN").upper()
        cc = cc if cc in compliance.COUNTRY_NAMES else "IN"
        # the owner's personal e-mail/phone are NOT pre-filled: gym contacts are shown publicly
        return {"name": "", "tagline": "", "description": "", "country_code": cc,
                "currency": COUNTRY_CURRENCY.get(cc, "INR"), "contact_email": "", "contact_phone": "", "website": ""}
    return {
        "name": gym.name, "tagline": gym.tagline or "", "description": gym.description or "",
        "country_code": gym.country_code, "currency": gym.currency, "contact_email": gym.contact_email or "",
        "contact_phone": gym.contact_phone or "", "website": gym.website or "",
    }


def _parse_gym_form(form: Any) -> tuple[dict[str, str], dict[str, str]]:
    values = {k: _txt(form, k) for k in GYM_FIELDS}
    errors: dict[str, str] = {}
    values["country_code"] = values["country_code"].upper()
    values["currency"] = values["currency"].upper()
    if not 2 <= len(values["name"]) <= 120:
        errors["name"] = "Give your gym a name (2-120 characters)."
    if len(values["tagline"]) > 200:
        errors["tagline"] = "Keep the tagline under 200 characters."
    if len(values["description"]) > 4000:
        errors["description"] = "Keep the description under 4,000 characters."
    if values["country_code"] not in compliance.COUNTRY_NAMES:
        errors["country_code"] = "Choose the country where this gym operates."
    if not values["currency"]:
        values["currency"] = COUNTRY_CURRENCY.get(values["country_code"], "")
    if not re.fullmatch(r"[A-Z]{3}", values["currency"] or ""):
        errors["currency"] = "Choose a 3-letter currency code (e.g. INR, USD)."
    if values["contact_email"] and (len(values["contact_email"]) > 254 or not EMAIL_RE.match(values["contact_email"])):
        errors["contact_email"] = "Enter a valid e-mail address."
    if values["contact_phone"]:
        try:
            normalize_phone(values["contact_phone"], values["country_code"] or None)
        except ValueError as exc:
            errors["contact_phone"] = str(exc)
        if len(values["contact_phone"]) > 32:
            errors["contact_phone"] = "Phone number is too long."
    if values["website"]:
        site = values["website"]
        if not re.match(r"^https?://", site, re.I):
            site = "https://" + site
        parts = urlsplit(site)
        if len(site) > 255 or not parts.netloc or "." not in parts.netloc or " " in site:
            errors["website"] = "Enter a valid website address (e.g. https://mygym.com)."
        else:
            values["website"] = site
    return values, errors


def _gym_form_ctx(gym: Gym | None, values: dict[str, str], errors: dict[str, str], **extra: Any) -> dict[str, Any]:
    ctx: dict[str, Any] = {
        "gym": gym, "values": values, "errors": errors,
        "country_options": compliance.country_options(),
        "currency_options": _currency_options(),
        "currency_map": COUNTRY_CURRENCY,
        "branches": [], "online_members": 0, "owner_missing": [],
    }
    ctx.update(extra)
    return ctx


async def _gym_side_ctx(db: AsyncSession, gym: Gym) -> dict[str, Any]:
    """Branch list (+ photo counts, live state) and online member count for the gym page - 2 queries."""
    photo_count = branch_photo_count().label("photo_count")
    rows = (await db.execute(select(Branch, photo_count).where(Branch.gym_id == gym.id).order_by(Branch.name))).all()
    branches = [
        {"branch": b, "photos": int(n or 0), "live": is_branch_published(b, gym, int(n or 0))} for b, n in rows
    ]
    today = date.today()
    online_members = await db.scalar(
        select(func.count(func.distinct(Subscription.customer_id)))
        .join(Plan, Plan.id == Subscription.plan_id)
        .where(Subscription.gym_id == gym.id, Subscription.status == SubscriptionStatus.ACTIVE,
               or_(Subscription.ends_on.is_(None), Subscription.ends_on >= today),
               Plan.mode.in_([PlanMode.ONLINE, PlanMode.COMBO]))
    )
    return {"branches": branches, "online_members": int(online_members or 0)}


# =====================================================================================================
# branch form
# =====================================================================================================
@dataclass(slots=True)
class BranchData:
    name: str = ""
    address_line1: str = ""
    address_line2: str = ""
    city: str = ""
    state: str = ""
    postal_code: str = ""
    country_code: str = ""
    timezone: str = ""
    phone: str = ""
    email: str = ""
    registration_number: str = ""
    latitude: float | None = None
    longitude: float | None = None
    hours: list[tuple[int, time, time]] = field(default_factory=list)
    features: list[str] = field(default_factory=list)


BRANCH_TEXT_FIELDS = ("name", "address_line1", "address_line2", "city", "state", "postal_code", "country_code",
                      "timezone", "phone", "email", "registration_number", "latitude", "longitude", "custom_features")
MATERIAL_FIELDS = ("address_line1", "address_line2", "city", "state", "postal_code", "country_code", "registration_number")
MATERIAL_LABELS = {"address_line1": "address", "address_line2": "address", "city": "city", "state": "state",
                   "postal_code": "postal code", "country_code": "country", "registration_number": "registration number"}


def _branch_values(branch: Branch | None, gym: Gym) -> dict[str, Any]:
    """Form values (strings) for a new or existing branch (``branch.hours`` / ``features`` must be loaded)."""
    if branch is None:
        values: dict[str, Any] = {k: "" for k in BRANCH_TEXT_FIELDS}
        values["country_code"] = gym.country_code
        values["timezone"] = compliance.guess_timezone(gym.country_code) or "UTC"
        values["phone"] = gym.contact_phone or ""
        values["email"] = gym.contact_email or ""
        values["features"] = []
        for wd in range(7):
            values[f"open_{wd}"], values[f"close_{wd}"], values[f"closed_{wd}"] = "06:00", "20:00", False
        return values
    values = {
        "name": branch.name, "address_line1": branch.address_line1, "address_line2": branch.address_line2 or "",
        "city": branch.city, "state": branch.state or "", "postal_code": branch.postal_code or "",
        "country_code": branch.country_code, "timezone": branch.timezone, "phone": branch.phone or "",
        "email": branch.email or "", "registration_number": branch.registration_number or "",
        "latitude": f"{branch.latitude:.6f}" if branch.latitude is not None else "",
        "longitude": f"{branch.longitude:.6f}" if branch.longitude is not None else "",
    }
    names = [f.name for f in branch.features]
    values["features"] = [n for n in names if n.lower() in STANDARD_FEATURE_NAMES]
    values["custom_features"] = ", ".join(n for n in names if n.lower() not in STANDARD_FEATURE_NAMES)
    by_day = {h.weekday: h for h in branch.hours}
    for wd in range(7):
        h = by_day.get(wd)
        values[f"open_{wd}"] = h.opens_at.strftime("%H:%M") if h else ""
        values[f"close_{wd}"] = h.closes_at.strftime("%H:%M") if h else ""
        values[f"closed_{wd}"] = h is None
    return values


def _parse_branch_form(form: Any) -> tuple[dict[str, Any], dict[str, str], BranchData]:
    values: dict[str, Any] = {k: _txt(form, k) for k in BRANCH_TEXT_FIELDS}
    values["country_code"] = values["country_code"].upper()
    errors: dict[str, str] = {}
    data = BranchData()

    def need(key: str, lo: int, hi: int, msg: str) -> None:
        if not lo <= len(values[key]) <= hi:
            errors[key] = msg

    need("name", 2, 120, "Name the branch (2-120 characters), e.g. after the neighbourhood.")
    need("address_line1", 3, 200, "Enter the street address (3-200 characters).")
    need("city", 2, 120, "Enter the city.")
    if len(values["address_line2"]) > 200:
        errors["address_line2"] = "Keep the second address line under 200 characters."
    if len(values["state"]) > 120:
        errors["state"] = "State / region is too long."
    if values["postal_code"] and not POSTAL_RE.match(values["postal_code"]):
        errors["postal_code"] = "Enter a valid postal code (letters, digits, spaces or dashes)."
    cc = values["country_code"]
    if cc not in compliance.COUNTRY_NAMES:
        errors["country_code"] = "Choose the branch's country."
    if not values["timezone"]:
        values["timezone"] = compliance.guess_timezone(cc) or "UTC"
    if len(values["timezone"]) > 64 or not is_valid_timezone(values["timezone"]):
        errors["timezone"] = "Choose a valid time zone, e.g. Asia/Kolkata."
    if values["phone"]:
        try:
            normalize_phone(values["phone"], cc or None)
        except ValueError as exc:
            errors["phone"] = str(exc)
        if len(values["phone"]) > 32:
            errors["phone"] = "Phone number is too long."
    if values["email"] and (len(values["email"]) > 254 or not EMAIL_RE.match(values["email"])):
        errors["email"] = "Enter a valid e-mail address."
    if values["registration_number"]:
        ok, msg = compliance.validate_registration_number(cc, values["registration_number"])
        if not ok:
            errors["registration_number"] = msg
        else:
            values["registration_number"] = compliance.canonical_registration(cc, values["registration_number"])

    # map pin
    lat_s, lon_s = values["latitude"], values["longitude"]
    if lat_s or lon_s:
        try:
            lat, lon = float(lat_s), float(lon_s)
            if not valid_coords(lat, lon):
                raise ValueError
            data.latitude, data.longitude = round(lat, 6), round(lon, 6)
        except ValueError:
            errors["location"] = "Drop the pin on the map (or enter a valid latitude and longitude)."

    # opening hours (form validation mirrors the DB CHECK constraints)
    max_minutes = int(get_settings().max_open_hours_per_day * 60)
    for wd, day in enumerate(WEEKDAYS):
        o, c = _txt(form, f"open_{wd}"), _txt(form, f"close_{wd}")
        closed = _checked(form, f"closed_{wd}")
        values[f"open_{wd}"], values[f"close_{wd}"], values[f"closed_{wd}"] = o, c, closed
        if closed or (not o and not c):
            values[f"closed_{wd}"] = True
            continue
        if not o or not c:
            errors[f"hours_{wd}"] = f"{day}: enter both the opening and the closing time, or mark the day as closed."
            continue
        try:
            opens, closes = parse_hhmm(o), parse_hhmm(c)
        except ValueError:
            errors[f"hours_{wd}"] = f"{day}: use the 24-hour HH:MM format."
            continue
        assert opens is not None and closes is not None
        span = minutes(closes) - minutes(opens)
        if span <= 0:
            errors[f"hours_{wd}"] = f"{day}: the closing time must be later than the opening time (no overnight hours)."
        elif span > max_minutes:
            errors[f"hours_{wd}"] = (f"{day}: a branch may be open at most {_fmt_minutes(max_minutes)} a day "
                                     f"- you entered {_fmt_minutes(span)}.")
        else:
            data.hours.append((wd, opens, closes))

    # features
    chosen: list[str] = []
    seen: set[str] = set()
    for raw in form.getlist("features"):
        name = STANDARD_FEATURE_NAMES.get(str(raw).strip().lower())
        if name and name.lower() not in seen:
            seen.add(name.lower())
            chosen.append(name)
    values["features"] = list(chosen)
    custom_unique: list[str] = []
    for c in (c.strip() for c in re.split(r"[,\n]", values["custom_features"])):
        if c and c.lower() not in seen:
            seen.add(c.lower())
            custom_unique.append(c)
    if len(custom_unique) > MAX_CUSTOM_FEATURES:
        errors["custom_features"] = f"Add at most {MAX_CUSTOM_FEATURES} custom features."
    elif any(len(c) < 2 or len(c) > 60 for c in custom_unique):
        errors["custom_features"] = "Each custom feature must be 2-60 characters long."
    data.features = chosen + custom_unique

    for key in ("name", "address_line1", "address_line2", "city", "state", "postal_code", "country_code", "timezone",
                "phone", "email", "registration_number"):
        setattr(data, key, values[key])
    return values, errors, data


def _material_changes(branch: Branch, data: BranchData) -> list[str]:
    changed: list[str] = []
    for f in MATERIAL_FIELDS:
        if (getattr(branch, f) or "").strip().lower() != (getattr(data, f) or "").strip().lower():
            label = MATERIAL_LABELS[f]
            if label not in changed:
                changed.append(label)
    old_has, new_has = valid_coords(branch.latitude, branch.longitude), data.latitude is not None
    if old_has != new_has or (old_has and new_has and haversine_km(
            float(branch.latitude), float(branch.longitude),  # type: ignore[arg-type]
            float(data.latitude), float(data.longitude)) > 0.01):  # type: ignore[arg-type]
        changed.append("map pin")
    return changed


def _apply_branch_data(branch: Branch, data: BranchData) -> None:
    for key in ("name", "address_line1", "city", "country_code", "timezone"):
        setattr(branch, key, getattr(data, key))
    for key in ("address_line2", "state", "postal_code", "phone", "email", "registration_number"):
        setattr(branch, key, getattr(data, key) or None)
    branch.latitude, branch.longitude = data.latitude, data.longitude


async def _replace_hours_and_features(db: AsyncSession, branch_id: int, data: BranchData) -> None:
    await db.execute(delete(BranchHours).where(BranchHours.branch_id == branch_id))
    await db.execute(delete(BranchFeature).where(BranchFeature.branch_id == branch_id))
    db.add_all([BranchHours(branch_id=branch_id, weekday=wd, opens_at=o, closes_at=c) for wd, o, c in data.hours])
    db.add_all([BranchFeature(branch_id=branch_id, name=name[:60]) for name in data.features])


def _hours_rows(values: dict[str, Any], errors: dict[str, str]) -> list[dict[str, Any]]:
    return [
        {"wd": wd, "day": day, "short": day[:3], "open": values.get(f"open_{wd}", ""), "close": values.get(f"close_{wd}", ""),
         "closed": bool(values.get(f"closed_{wd}")), "error": errors.get(f"hours_{wd}")}
        for wd, day in enumerate(WEEKDAYS)
    ]


def _branch_form_ctx(gym: Gym, branch: Branch | None, values: dict[str, Any], errors: dict[str, str]) -> dict[str, Any]:
    cc = values.get("country_code") or gym.country_code
    return {
        "values": values,
        "errors": errors,
        "hours_rows": _hours_rows(values, errors),
        "hours_errors": [errors[k] for k in sorted(errors) if k.startswith("hours_")],
        "standard_features": STANDARD_FEATURES,
        "country_options": compliance.country_options(),
        "timezone_suggestions": sorted(set(compliance.COUNTRY_TIMEZONES.values())),
        "form_rules": compliance.requirements_for(cc),
        "registration_labels": compliance.registration_labels(),
        "map_center": compliance.map_center(cc),
        "max_open_hours": get_settings().max_open_hours_per_day,
        "is_verified_branch": branch is not None and branch.verification_status == VS.VERIFIED,
        "locked_for_review": branch is not None and branch.verification_status in IN_PROGRESS,
        "geoapify": get_settings().geoapify_enabled,
    }


# =====================================================================================================
# branch page context
# =====================================================================================================
def _doc_state(doc: BranchDocument, branch: Branch) -> str:
    if branch.verification_status in IN_PROGRESS:
        return "processing"
    if doc.accepted is True:
        return "verified"
    if doc.accepted is False:
        return "rejected"
    return "uploaded"


def _submit_blockers(owner: User, branch: Branch, doc_types: list[BranchDocType]) -> list[dict[str, str]]:
    """Everything that prevents submitting for verification (each with a link to fix it)."""
    blockers: list[dict[str, str]] = []
    for step in owner_missing_steps(owner):
        href = "/account" if "photo" in step else "/account/identity"
        blockers.append({"text": f"{step} - owners need a photo and a verified ID first.", "href": href})
    ok, msg = compliance.validate_registration_number(branch.country_code, branch.registration_number)
    if not ok:
        blockers.append({"text": f"Registration number: {msg}", "href": "#details"})
    if not valid_coords(branch.latitude, branch.longitude):
        blockers.append({"text": "Drop the map pin on the branch's exact location.", "href": "#details"})
    if not branch.hours:
        blockers.append({"text": "Add the opening hours (at least one day).", "href": "#details"})
    for req in compliance.missing_requirements(branch.country_code, doc_types):
        blockers.append({"text": f"Upload: {req.label}.", "href": "#documents"})
    return blockers


def _steps(branch: Branch, details_ok: bool, docs_ok: bool, photo_count: int, live: bool) -> list[dict[str, Any]]:
    status = branch.verification_status
    min_photos = get_settings().min_branch_photos
    verified = status == VS.VERIFIED
    if verified:
        ver_state, ver_hint = "done", "Verified"
    elif status in IN_PROGRESS:
        ver_state, ver_hint = "active", "Checking now…"
    elif status == VS.REJECTED:
        ver_state, ver_hint = "error", "Fix & resubmit"
    elif status in (VS.NEEDS_REVIEW, VS.AI_UNAVAILABLE):
        ver_state, ver_hint = "warning", "Manual review" if status == VS.NEEDS_REVIEW else "Retrying soon"
    else:
        ver_state, ver_hint = ("current" if details_ok and docs_ok else "todo"), "Submit when ready"
    photos_state = "done" if verified and photo_count >= min_photos else ("current" if verified else "locked")
    if live:
        live_state, live_hint = "done", "On the website"
    elif verified and photo_count >= min_photos:
        live_state, live_hint = "warning", "Hidden"
    else:
        live_state, live_hint = "locked", "Not yet"
    return [
        {"key": "details", "label": "Details", "short": "Details", "state": "done" if details_ok else "current",
         "hint": "Complete" if details_ok else "Address, pin & hours", "href": "#details"},
        {"key": "documents", "label": "Documents", "short": "Docs", "state": "done" if docs_ok else ("current" if details_ok else "todo"),
         "hint": "All uploaded" if docs_ok else "Country checklist", "href": "#documents"},
        {"key": "verification", "label": "AI verification", "short": "AI check", "state": ver_state, "hint": ver_hint, "href": "#verification"},
        {"key": "photos", "label": "Photos", "short": "Photos", "state": photos_state,
         "hint": f"{min(photo_count, min_photos)}/{min_photos} photos", "href": "#photos"},
        {"key": "live", "label": "Live", "short": "Live", "state": live_state, "hint": live_hint, "href": "#status"},
    ]


async def _load_branch(db: AsyncSession, branch_id: int, owner: User) -> Branch:
    return await get_owned_branch(
        db, branch_id, owner,
        joinedload(Branch.gym),
        selectinload(Branch.hours),
        selectinload(Branch.features),
        selectinload(Branch.photos),
        selectinload(Branch.documents),
        selectinload(Branch.verification_runs),
    )


def _live_reason(branch: Branch, gym: Gym, photo_count: int, live: bool) -> str:
    if live:
        return "Visible on the public website and selling offline plans."
    if branch.verification_status != VS.VERIFIED:
        return "Branches appear on the website after AI verification and photos."
    if not branch.is_open:
        return "This branch is closed - reopen it to show it again."
    if not gym.offline_enabled:
        return "Physical locations are switched off for this gym."
    if not gym.is_active:
        return "This gym is inactive."
    need = get_settings().min_branch_photos - photo_count
    return f"Upload {_plural(need, 'more photo')} (machines + the whole gym) to go live."


def _branch_page_ctx(owner: User, branch: Branch) -> dict[str, Any]:
    gym = branch.gym
    settings = get_settings()
    doc_types = [d.doc_type for d in branch.documents]
    blockers = _submit_blockers(owner, branch, doc_types)
    reg_ok, _ = compliance.validate_registration_number(branch.country_code, branch.registration_number)
    details_ok = reg_ok and valid_coords(branch.latitude, branch.longitude) and bool(branch.hours)
    docs_ok = not compliance.missing_requirements(branch.country_code, doc_types)
    photo_count = len(branch.photos)
    live = is_branch_published(branch, gym, photo_count)
    documents = sorted(branch.documents, key=lambda d: (d.created_at or utcnow(), d.id), reverse=True)
    runs = sorted(branch.verification_runs, key=lambda r: (r.created_at or utcnow(), r.id), reverse=True)
    geo_points: dict[str, Any] | None = None
    if valid_coords(branch.latitude, branch.longitude):
        geo_points = {"pin": [branch.latitude, branch.longitude], "label": branch.name}
        if valid_coords(branch.geocoded_latitude, branch.geocoded_longitude):
            geo_points["geocoded"] = [branch.geocoded_latitude, branch.geocoded_longitude]
            geo_points["distance_km"] = round(haversine_km(
                float(branch.latitude), float(branch.longitude),  # type: ignore[arg-type]
                float(branch.geocoded_latitude), float(branch.geocoded_longitude)), 2)  # type: ignore[arg-type]
    today = date.today()
    return {
        "gym": gym,
        "branch": branch,
        "rules": compliance.requirements_for(branch.country_code),
        "requirement_status": compliance.requirement_status(branch.country_code, doc_types),
        "documents": [
            {"doc": d, "state": _doc_state(d, branch), "label": compliance.doc_type_label(d.doc_type),
             "size": _human_size(d.size_bytes),
             "can_delete": branch.verification_status not in (*IN_PROGRESS, VS.VERIFIED),
             "expired": d.expires_on is not None and d.expires_on.date() < today}
            for d in documents
        ],
        "doc_type_options": compliance.doc_type_options(),
        "blockers": blockers,
        "can_submit": not blockers and branch.verification_status in SUBMITTABLE,
        "in_progress": branch.verification_status in IN_PROGRESS,
        "checks": branch.verification_checks or [],
        "runs": runs,
        "photos": branch.photos,
        "photo_count": photo_count,
        "max_photos": caps.limit("branch_photos"),
        "min_photos": settings.min_branch_photos,
        "photo_kinds": [(k.value, PHOTO_KIND_LABELS[k]) for k in PhotoKind],
        "photo_kind_labels": {k.value: v for k, v in PHOTO_KIND_LABELS.items()},
        "live": live,
        "live_reason": _live_reason(branch, gym, photo_count, live),
        "steps": _steps(branch, details_ok, docs_ok, photo_count, live),
        "owner_missing": owner_missing_steps(owner),
        "geo_points": geo_points,
        "max_upload_mb": settings.max_upload_mb,
        "country_label": compliance.country_name(branch.country_code),
    }


async def _render_branch_page(request: Request, db: AsyncSession, owner: User, branch_id: int, *,
                              status_code: int = 200, form_values: dict[str, Any] | None = None,
                              errors: dict[str, str] | None = None, doc_errors: dict[str, str] | None = None,
                              doc_values: dict[str, str] | None = None, photo_errors: dict[str, str] | None = None,
                              close_errors: dict[str, str] | None = None) -> Response:
    branch = await _load_branch(db, branch_id, owner)
    ctx = _branch_page_ctx(owner, branch)
    values = form_values if form_values is not None else _branch_values(branch, branch.gym)
    ctx.update(_branch_form_ctx(branch.gym, branch, values, errors or {}))
    ctx.update(
        doc_errors=doc_errors or {}, doc_values=doc_values or {}, photo_errors=photo_errors or {},
        close_errors=close_errors or {}, ai_feature=True,
        details_open=bool(errors) or branch.verification_status != VS.VERIFIED,
    )
    return render(request, "owner/branches/detail.html", ctx, status_code=status_code)


# =====================================================================================================
# overview
# =====================================================================================================
@router.get("/owner")
async def owner_dashboard(request: Request, db: DB, owner: OwnerUser):
    now = utcnow()
    today = date.today()
    local = local_now(owner.timezone)
    month_start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    gym_ids = select(Gym.id).where(Gym.owner_id == owner.id)
    active_sub = (
        Subscription.gym_id.in_(gym_ids),
        Subscription.status == SubscriptionStatus.ACTIVE,
        or_(Subscription.ends_on.is_(None), Subscription.ends_on >= today),
    )

    # 1) every scalar KPI in ONE round trip
    kpi = (await db.execute(select(
        select(func.count(Gym.id)).where(Gym.owner_id == owner.id).scalar_subquery().label("gyms"),
        select(func.count(func.distinct(Subscription.customer_id))).where(*active_sub).scalar_subquery().label("members"),
        select(func.count(OtpChallenge.id)).join(Branch, Branch.id == OtpChallenge.branch_id).where(
            Branch.gym_id.in_(gym_ids), OtpChallenge.consumed_at.is_(None), OtpChallenge.expires_at > now,
            OtpChallenge.attempts < OtpChallenge.max_attempts).scalar_subquery().label("otps"),
        select(func.count(MisconductReport.id)).where(
            MisconductReport.gym_id.in_(gym_ids), MisconductReport.status == ReportStatus.OPEN
        ).scalar_subquery().label("reports"),
        select(func.count(SalaryStatement.id)).where(
            SalaryStatement.gym_id.in_(gym_ids),
            SalaryStatement.status.in_([StatementStatus.DUE, StatementStatus.AWAITING_PROOF, StatementStatus.PROOF_REVIEW]),
        ).scalar_subquery().label("unpaid"),
        select(func.count(func.distinct(CustomerProfile.user_id)))
        .join(Subscription, Subscription.customer_id == CustomerProfile.user_id)
        .where(*active_sub, CustomerProfile.activity_suspended.is_(True)).scalar_subquery().label("paused"),
        select(func.count(Employee.id)).where(
            Employee.gym_id.in_(gym_ids), Employee.status == EmploymentStatus.ACTIVE, Employee.deleted_at.is_(None)
        ).scalar_subquery().label("staff"),
        select(func.count(Plan.id)).where(Plan.gym_id.in_(gym_ids), Plan.is_enabled.is_(True)).scalar_subquery().label("plans"),
        select(func.count(Branch.id)).join(Gym, Gym.id == Branch.gym_id).where(
            Gym.owner_id == owner.id, *published_branch_clauses()).scalar_subquery().label("live"),
    ))).one()

    # 2) revenue this month per currency (INCOMING + SUCCESS)
    revenue_rows = (await db.execute(
        select(Transaction.currency, func.coalesce(func.sum(Transaction.amount), 0))
        .where(Transaction.gym_id.in_(gym_ids), Transaction.direction == TxDirection.INCOMING,
               Transaction.status == TxStatus.SUCCESS,
               func.coalesce(Transaction.completed_at, Transaction.created_at) >= month_start)
        .group_by(Transaction.currency).order_by(func.sum(Transaction.amount).desc())
    )).all()
    revenue = [(cur, Decimal(amount)) for cur, amount in revenue_rows]

    # 3) branches by verification status
    status_rows = (await db.execute(
        select(Branch.verification_status, func.count(Branch.id)).join(Gym, Gym.id == Branch.gym_id)
        .where(Gym.owner_id == owner.id).group_by(Branch.verification_status)
    )).all()
    by_status = {VS(s): int(n) for s, n in status_rows}
    total_branches = sum(by_status.values())

    # 4) gyms with aggregated branch/member counts (correlated subqueries -> one query)
    gyms_rows = (await db.execute(
        select(
            Gym,
            select(func.count(Branch.id)).where(Branch.gym_id == Gym.id).correlate(Gym).scalar_subquery(),
            select(func.count(Branch.id)).where(Branch.gym_id == Gym.id, Branch.verification_status == VS.VERIFIED)
            .correlate(Gym).scalar_subquery(),
            select(func.count(Branch.id)).where(Branch.gym_id == Gym.id, *published_branch_clauses())
            .correlate(Gym).scalar_subquery(),
            select(func.count(func.distinct(Subscription.customer_id))).where(
                Subscription.gym_id == Gym.id, Subscription.status == SubscriptionStatus.ACTIVE,
                or_(Subscription.ends_on.is_(None), Subscription.ends_on >= today)).correlate(Gym).scalar_subquery(),
        ).where(Gym.owner_id == owner.id).order_by(Gym.created_at.desc()).limit(6)
    )).all()
    gyms = [{"gym": g, "branches": int(b or 0), "verified": int(v or 0), "live": int(lv or 0), "members": int(m or 0)}
            for g, b, v, lv, m in gyms_rows]

    # 5) branches needing attention
    attention_rows = (await db.execute(
        select(Branch, Gym.name, branch_photo_count()).join(Gym, Gym.id == Branch.gym_id)
        .where(Gym.owner_id == owner.id, or_(
            Branch.verification_status != VS.VERIFIED,
            branch_photo_count() < get_settings().min_branch_photos,
            Branch.is_open.is_(False),
        ))
        .order_by(Branch.updated_at.desc()).limit(6)
    )).all()

    # 6) recent activity = the owner's latest notifications
    activity = (await db.execute(
        select(Notification).where(Notification.user_id == owner.id).order_by(Notification.created_at.desc()).limit(6)
    )).scalars().all()

    first_gym = gyms[0]["gym"] if gyms else None
    checklist = [
        {"title": "Add your profile photo", "hint": "Members see who runs the gym.", "done": bool(owner.photo_key),
         "href": "/account", "cta": "Upload"},
        {"title": "Verify your government ID", "hint": "Needed for verification and online mode.",
         "done": owner.id_status == IdStatus.VERIFIED, "href": "/account/identity", "cta": "Verify"},
        {"title": "Create your gym", "hint": "Name, country, currency and contacts.", "done": kpi.gyms > 0,
         "href": "/owner/gyms/new", "cta": "Create"},
        {"title": "Add a branch", "hint": "Address, map pin, hours and features.", "done": total_branches > 0,
         "href": f"/owner/gyms/{first_gym.id}/branches/new" if first_gym else "/owner/gyms/new", "cta": "Add"},
        {"title": "Get a branch AI-verified", "hint": "Upload your country's documents and submit.",
         "done": by_status.get(VS.VERIFIED, 0) > 0, "href": "/owner/gyms", "cta": "Open"},
        {"title": "Go live with 3+ photos", "hint": "Machines and the whole gym.", "done": kpi.live > 0,
         "href": "/owner/gyms", "cta": "Photos"},
        {"title": "Publish your plans", "hint": "Offline, online and combo memberships.", "done": kpi.plans > 0,
         "href": f"/owner/gyms/{first_gym.id}/plans" if first_gym else "/owner/gyms/new", "cta": "Plans"},
    ]
    status_order = [VS.VERIFIED, VS.PROCESSING, VS.QUEUED, VS.NEEDS_REVIEW, VS.AI_UNAVAILABLE, VS.REJECTED, VS.DRAFT]
    waffle: list[str] = []
    for s in status_order:
        waffle += [s.value] * by_status.get(s, 0)
    return render(request, "owner/dashboard.html", {
        "greeting": _greeting(owner.timezone),
        "first_name": (owner.full_name or "").split(" ")[0] or "there",
        "kpi": kpi,
        "revenue": revenue,
        "revenue_labels": [money(amount, cur) for cur, amount in revenue],
        "by_status": [(s, by_status.get(s, 0)) for s in status_order if by_status.get(s, 0)],
        "total_branches": total_branches,
        "verified_branches": by_status.get(VS.VERIFIED, 0),
        "waffle": waffle[:84],
        "waffle_more": max(0, len(waffle) - 84),
        "gyms": gyms,
        "attention": [{"branch": b, "gym_name": gname, "photos": int(pc or 0)} for b, gname, pc in attention_rows],
        "activity": activity,
        "checklist": checklist,
        "checklist_done": sum(1 for c in checklist if c["done"]),
        "owner_missing": owner_missing_steps(owner),
        "first_gym": first_gym,
        "month_label": local.strftime("%B"),
        "min_photos": get_settings().min_branch_photos,
        "ai_feature": True,
    })


# =====================================================================================================
# gyms
# =====================================================================================================
@router.get("/owner/gyms")
async def gyms_index(request: Request, db: DB, owner: OwnerUser):
    today = date.today()
    rows = (await db.execute(
        select(
            Gym,
            select(func.count(Branch.id)).where(Branch.gym_id == Gym.id).correlate(Gym).scalar_subquery(),
            select(func.count(Branch.id)).where(Branch.gym_id == Gym.id, Branch.verification_status == VS.VERIFIED)
            .correlate(Gym).scalar_subquery(),
            select(func.count(Branch.id)).where(Branch.gym_id == Gym.id, *published_branch_clauses())
            .correlate(Gym).scalar_subquery(),
            select(func.count(Branch.id)).where(
                Branch.gym_id == Gym.id,
                Branch.verification_status.in_([VS.QUEUED, VS.PROCESSING, VS.NEEDS_REVIEW, VS.AI_UNAVAILABLE]),
            ).correlate(Gym).scalar_subquery(),
            select(func.count(func.distinct(Subscription.customer_id))).where(
                Subscription.gym_id == Gym.id, Subscription.status == SubscriptionStatus.ACTIVE,
                or_(Subscription.ends_on.is_(None), Subscription.ends_on >= today)).correlate(Gym).scalar_subquery(),
        ).where(Gym.owner_id == owner.id).order_by(Gym.created_at.desc())
    )).all()
    gyms = [
        {"gym": g, "branches": int(b or 0), "verified": int(v or 0), "live": int(lv or 0), "pending": int(p or 0),
         "members": int(m or 0), "country": compliance.country_name(g.country_code)}
        for g, b, v, lv, p, m in rows
    ]
    return render(request, "owner/gyms/index.html", {"gyms": gyms, "owner_missing": owner_missing_steps(owner)})


@router.get("/owner/gyms/new")
async def gym_new_form(request: Request, owner: OwnerUser):
    return render(request, "owner/gyms/form.html", _gym_form_ctx(None, _gym_values(None, owner), {}))


@router.post("/owner/gyms/new")
async def gym_create(request: Request, db: DB, owner: OwnerUser):
    form = await request.form()
    values, errors = _parse_gym_form(form)
    if not errors:  # gyms per owner account - held (advisory lock) until this request commits its gym
        try:
            await caps.enforce(db, "gyms_per_owner", owner.id, select(Gym.id).where(Gym.owner_id == owner.id))
        except caps.CapReached as exc:
            return render(request, "owner/gyms/form.html", _gym_form_ctx(None, values, {"name": str(exc)}),
                          status_code=exc.status_code)
    logo = _file(form, "logo")
    stored = None
    if not errors and logo is not None:
        try:
            stored = await storage.save_public_image(logo, kind="gym_logos", max_side=512)
        except storage.UploadError as exc:
            errors["logo"] = str(exc)
    if errors:
        return render(request, "owner/gyms/form.html", _gym_form_ctx(None, values, errors), status_code=400)
    gym: Gym | None = None
    for attempt in range(3):
        slug = await _unique_slug(db, values["name"]) if attempt == 0 else \
            f"{slugify(values['name'], max_len=110)}-{secrets.token_hex(3)}"
        candidate = Gym(
            owner_id=owner.id, name=values["name"], slug=slug,
            tagline=values["tagline"] or None, description=values["description"] or None,
            country_code=values["country_code"], currency=values["currency"],
            contact_email=values["contact_email"] or None, contact_phone=values["contact_phone"] or None,
            website=values["website"] or None, logo_key=stored.key if stored else None,
            online_enabled=False, offline_enabled=False,
        )
        try:
            async with db.begin_nested():  # SAVEPOINT: a slug race only rolls back this insert
                db.add(candidate)
                await db.flush()
        except IntegrityError:
            continue
        gym = candidate
        break
    if gym is None:
        raise HTTPException(status_code=409, detail="Could not reserve a web address for this gym - please try again.")
    await audit(db, owner, "gym.create", "gym", gym.id, gym_id=gym.id, detail={"name": gym.name}, request=request)
    await db.commit()
    return redirect(f"/owner/gyms/{gym.id}", f"{gym.name} is set up. Next: add your first branch or enable online classes.")


@router.get("/owner/gyms/{gym_id}")
async def gym_edit_form(request: Request, gym_id: IdParam, db: DB, owner: OwnerUser):
    gym = await get_owned_gym(db, gym_id, owner)
    side = await _gym_side_ctx(db, gym)
    return render(request, "owner/gyms/form.html", _gym_form_ctx(
        gym, _gym_values(gym, owner), {}, owner_missing=owner_missing_steps(owner), **side))


@router.post("/owner/gyms/{gym_id}")
async def gym_update(request: Request, gym_id: IdParam, db: DB, owner: OwnerUser):
    gym = await get_owned_gym(db, gym_id, owner)
    form = await request.form()
    values, errors = _parse_gym_form(form)
    logo = _file(form, "logo")
    stored = None
    if not errors and logo is not None:
        try:
            stored = await storage.save_public_image(logo, kind="gym_logos", max_side=512)
        except storage.UploadError as exc:
            errors["logo"] = str(exc)
    if errors:
        side = await _gym_side_ctx(db, gym)
        return render(request, "owner/gyms/form.html", _gym_form_ctx(
            gym, values, errors, owner_missing=owner_missing_steps(owner), **side), status_code=400)
    changed = [k for k in GYM_FIELDS if (getattr(gym, k) or "") != (values[k] or "")]
    gym.name = values["name"]
    gym.tagline = values["tagline"] or None
    gym.description = values["description"] or None
    gym.country_code = values["country_code"]
    gym.currency = values["currency"]
    gym.contact_email = values["contact_email"] or None
    gym.contact_phone = values["contact_phone"] or None
    gym.website = values["website"] or None
    old_logo = None
    if stored is not None:
        old_logo, gym.logo_key = gym.logo_key, stored.key
        changed.append("logo")
    elif _checked(form, "remove_logo") and gym.logo_key:
        old_logo, gym.logo_key = gym.logo_key, None
        changed.append("logo")
    await audit(db, owner, "gym.update", "gym", gym.id, gym_id=gym.id, detail={"fields": changed}, request=request)
    await db.commit()
    if old_logo:
        await storage.delete_key(old_logo)
    return redirect(f"/owner/gyms/{gym.id}", "Gym details saved." if changed else "Nothing changed.",
                    "success" if changed else "info")


@router.post("/owner/gyms/{gym_id}/online")
async def gym_online(request: Request, gym_id: IdParam, db: DB, owner: OwnerUser):
    gym = await get_owned_gym(db, gym_id, owner)
    action = _txt(await request.form(), "action")
    back = f"/owner/gyms/{gym.id}#modes"
    public_link = f"/gyms/{gym.slug}"
    online_modes = (PlanMode.ONLINE, PlanMode.COMBO)
    await _lock_gym_state(db, gym)
    if action == "enable":
        missing = owner_missing_steps(owner)
        if missing:
            return redirect(back, "Online mode needs a verified owner: " + "; ".join(missing).lower() + ".", "error")
        if gym.online_enabled:
            return redirect(back, "Online classes are already enabled.", "info")
        gym.online_enabled, gym.online_open = True, True
        message = f"Online classes are enabled for {gym.name}."
    elif action == "disable":
        if not gym.online_enabled:
            return redirect(back, "Online classes are already off.", "info")
        gym.online_enabled = False
        n = await _notify_members(db, gym_id=gym.id, modes=online_modes, severity=Severity.WARNING, link=public_link,
                                  title=f"{gym.name}: online classes ended",
                                  body=f"{gym.name} has switched off online classes. Contact the gym about your membership.")
        message = f"Online classes are switched off. {_notice_text(n)}"
    elif action == "close":
        if not gym.online_enabled:
            return redirect(back, "Enable online classes first.", "error")
        if not gym.online_open:
            return redirect(back, "Online classes are already closed.", "info")
        gym.online_open = False
        n = await _notify_members(db, gym_id=gym.id, modes=online_modes, severity=Severity.WARNING, link=public_link,
                                  title=f"{gym.name}: online classes paused",
                                  body=f"{gym.name} has temporarily closed its online classes. We'll let you know as soon as they reopen.")
        message = f"Online classes are closed. {_notice_text(n)}"
    elif action == "reopen":
        if not gym.online_enabled:
            return redirect(back, "Enable online classes first.", "error")
        if gym.online_open:
            return redirect(back, "Online classes are already open.", "info")
        gym.online_open = True  # reopening never needs re-verification
        n = await _notify_members(db, gym_id=gym.id, modes=online_modes, severity=Severity.SUCCESS, link=public_link,
                                  title=f"{gym.name}: online classes are back",
                                  body=f"Good news - {gym.name} has reopened its online classes.")
        message = f"Online classes reopened. {_notice_text(n)}"
    else:
        return redirect(back, "Unknown action.", "error")
    await audit(db, owner, f"gym.online_{action}", "gym", gym.id, gym_id=gym.id, request=request)
    await db.commit()
    return redirect(back, message)


@router.post("/owner/gyms/{gym_id}/offline")
async def gym_offline(request: Request, gym_id: IdParam, db: DB, owner: OwnerUser):
    gym = await get_owned_gym(db, gym_id, owner)
    action = _txt(await request.form(), "action")
    back = f"/owner/gyms/{gym.id}#modes"
    offline_modes = (PlanMode.OFFLINE, PlanMode.COMBO)
    await _lock_gym_state(db, gym)
    if action == "enable":
        if gym.offline_enabled:
            return redirect(back, "Physical locations are already on.", "info")
        gym.offline_enabled = True
        await _notify_members(db, gym_id=gym.id, modes=offline_modes, severity=Severity.SUCCESS, link=f"/gyms/{gym.slug}",
                              title=f"{gym.name}: branches are open again",
                              body=f"{gym.name}'s physical branches are available again.")
        message = "Physical locations are on. Verified, open branches with 3+ photos are visible again."
    elif action == "disable":
        if not gym.offline_enabled:
            return redirect(back, "Physical locations are already off.", "info")
        gym.offline_enabled = False
        await _notify_members(db, gym_id=gym.id, modes=offline_modes, severity=Severity.WARNING, link=f"/gyms/{gym.slug}",
                              title=f"{gym.name}: branches unavailable",
                              body=f"{gym.name} has temporarily switched off its physical branches. Contact the gym about your membership.")
        message = "Physical locations are off - branches are hidden from the website (their verification is kept)."
    else:
        return redirect(back, "Unknown action.", "error")
    await audit(db, owner, f"gym.offline_{action}", "gym", gym.id, gym_id=gym.id, request=request)
    await db.commit()
    return redirect(back, message)


# =====================================================================================================
# branches: create / manage / update
# =====================================================================================================
@router.get("/owner/gyms/{gym_id}/branches/new")
async def branch_new_form(request: Request, gym_id: IdParam, db: DB, owner: OwnerUser):
    gym = await get_owned_gym(db, gym_id, owner)
    ctx = _branch_form_ctx(gym, None, _branch_values(None, gym), {})
    ctx.update(gym=gym, owner_missing=owner_missing_steps(owner))
    return render(request, "owner/branches/new.html", ctx)


@router.post("/owner/gyms/{gym_id}/branches/new")
async def branch_create(request: Request, gym_id: IdParam, db: DB, owner: OwnerUser):
    gym = await get_owned_gym(db, gym_id, owner)
    values, errors, data = _parse_branch_form(await request.form())
    status = 400
    if not errors:  # branches per gym (closed ones too: a branch is closed, never deleted)
        try:
            await caps.enforce(db, "branches_per_gym", gym.id, select(Branch.id).where(Branch.gym_id == gym.id))
        except caps.CapReached as exc:
            errors, status = {"name": str(exc)}, exc.status_code
    if errors:
        ctx = _branch_form_ctx(gym, None, values, errors)
        ctx.update(gym=gym, owner_missing=owner_missing_steps(owner))
        return render(request, "owner/branches/new.html", ctx, status_code=status)
    branch = Branch(gym_id=gym.id, verification_status=VS.DRAFT, is_open=True)
    _apply_branch_data(branch, data)
    db.add(branch)
    await db.flush()
    await _replace_hours_and_features(db, branch.id, data)
    switched_on = not gym.offline_enabled
    gym.offline_enabled = True
    await audit(db, owner, "branch.create", "branch", branch.id, gym_id=gym.id, detail={"name": branch.name}, request=request)
    await db.commit()
    msg = "Branch saved. Next: upload the documents your country requires, then submit it for AI verification."
    if switched_on:
        msg += " Physical locations were switched on for this gym."
    return redirect(f"/owner/branches/{branch.id}#documents", msg)


@router.get("/owner/branches/{branch_id}")
async def branch_page(request: Request, branch_id: IdParam, db: DB, owner: OwnerUser):
    return await _render_branch_page(request, db, owner, branch_id)


@router.post("/owner/branches/{branch_id}")
async def branch_update(request: Request, branch_id: IdParam, db: DB, owner: OwnerUser):
    branch = await get_owned_branch(db, branch_id, owner, joinedload(Branch.gym))
    form = await request.form()
    values, errors, data = _parse_branch_form(form)
    changes = _material_changes(branch, data) if not errors else []
    if changes and branch.verification_status in IN_PROGRESS:
        errors["form"] = ("Verification is running right now - wait for the result (about a minute) before changing "
                          "the address, registration number or map pin.")
    elif changes and branch.verification_status == VS.VERIFIED and not _checked(form, "confirm_reverify"):
        errors["confirm_reverify"] = ("This branch is verified. Changing the " + ", ".join(changes)
                                      + " resets it to draft and hides it until it is verified again - tick the box to confirm.")
    if errors:
        return await _render_branch_page(request, db, owner, branch_id, status_code=400, form_values=values, errors=errors)
    was = branch.verification_status
    _apply_branch_data(branch, data)
    await _replace_hours_and_features(db, branch.id, data)
    reset = bool(changes) and was != VS.DRAFT
    if reset:
        branch.verification_status = VS.DRAFT
        branch.verified_at = None
        branch.verification_summary = f"Reset to draft after changing the {', '.join(changes)}."
        await notify(
            db, owner, f"{branch.name} needs re-verification",
            f"You changed the {', '.join(changes)} of {branch.name}. "
            + ("Its verification was reset and the branch is hidden from the website until it is verified again. "
               if was == VS.VERIFIED else "")
            + "Submit it for AI verification again when you're ready.",
            category=NotificationCategory.VERIFICATION, severity=Severity.WARNING,
            link=f"/owner/branches/{branch.id}#verification",
        )
    await audit(db, owner, "branch.update", "branch", branch.id, gym_id=branch.gym_id,
                detail={"material_changes": changes, "reset_to_draft": reset, "previous_status": was.value}, request=request)
    await db.commit()
    if reset and was == VS.VERIFIED:
        return redirect(f"/owner/branches/{branch.id}#verification",
                        "Saved. Verification was reset because the location details changed - submit the branch again.",
                        "warning")
    return redirect(f"/owner/branches/{branch.id}#details", "Branch details saved.")


@router.get("/api/owner/branches/{branch_id}/status")
async def branch_status_api(branch_id: IdParam, db: DB, owner: OwnerUser):
    branch = await get_owned_branch(db, branch_id, owner)
    status = branch.verification_status
    return JSONResponse({
        "id": branch.id,
        "status": status.value,
        "label": status.value.replace("_", " ").capitalize(),
        "summary": branch.verification_summary,
        "in_progress": status in IN_PROGRESS,
        "verified": status == VS.VERIFIED,
        "updated_at": branch.updated_at.isoformat() if branch.updated_at else None,
    })


# =====================================================================================================
# documents
# =====================================================================================================
@router.post("/owner/branches/{branch_id}/documents")
async def document_upload(request: Request, branch_id: IdParam, db: DB, owner: OwnerUser):
    branch = await get_owned_branch(db, branch_id, owner)
    form = await request.form()
    values = {k: _txt(form, k) for k in ("doc_type", "document_number", "issued_on", "expires_on")}
    errors: dict[str, str] = {}
    doc_type: BranchDocType | None = None
    try:
        doc_type = BranchDocType(values["doc_type"])
    except ValueError:
        errors["doc_type"] = "Choose what kind of document this is."
    if branch.verification_status in IN_PROGRESS:
        errors["form"] = "Verification is running - you can upload more documents once it has finished."
    if len(values["document_number"]) > 80:
        errors["document_number"] = "Document number is too long."
    issued = expires = None
    try:
        issued = _parse_date(values["issued_on"])
    except ValueError:
        errors["issued_on"] = "Use a valid date."
    try:
        expires = _parse_date(values["expires_on"])
    except ValueError:
        errors["expires_on"] = "Use a valid date."
    if expires and expires < date.today():
        errors["expires_on"] = "This document has expired - upload the renewed one."
    if issued and issued > date.today():
        errors["issued_on"] = "The issue date cannot be in the future."
    try:  # documents per branch, race-safe (held until this request commits its document)
        await caps.enforce(db, "branch_documents", branch.id,
                           select(BranchDocument.id).where(BranchDocument.branch_id == branch.id))
    except caps.CapReached as exc:
        errors["form"] = str(exc)
    upload = _file(form, "file")
    raw = b""
    if upload is None:
        errors["file"] = "Choose a PDF, JPG or PNG file."
    elif not errors:
        try:
            raw = await storage.read_upload(upload)
            if not raw.startswith(DOC_MAGIC):
                raise storage.UploadError("Upload a PDF, JPG or PNG file.")
        except storage.UploadError as exc:
            errors["file"] = str(exc)
    sha = ""
    if not errors:
        sha = hashlib.sha256(raw).hexdigest()
        dup = await db.scalar(select(BranchDocument.id).where(BranchDocument.branch_id == branch.id,
                                                              BranchDocument.sha256 == sha).limit(1))
        if dup:
            errors["file"] = "This exact file is already uploaded for this branch."
    stored = None
    if not errors:
        try:
            stored, _plain = await storage.save_private_file(raw, kind="branch_docs", filename=upload.filename)  # type: ignore[union-attr]
        except storage.UploadError as exc:
            errors["file"] = str(exc)
    if stored is not None and not errors:
        try:  # the gym's storage quota (after the narrower per-branch cap)
            await quotas.reserve_gym(db, branch.gym_id, stored.size)
        except caps.CapReached as exc:
            await storage.delete_key(stored.key)
            stored, errors["file"] = None, str(exc)
    if errors or stored is None or doc_type is None:
        return await _render_branch_page(request, db, owner, branch_id, status_code=400, doc_errors=errors, doc_values=values)
    doc = BranchDocument(
        branch_id=branch.id, doc_type=doc_type, file_key=stored.key,
        original_filename=re.sub(r"[^\w.\- ()]", "_", stored.original_filename)[:255] or "document",
        content_type=stored.content_type, sha256=sha, size_bytes=stored.size,
        document_number=values["document_number"] or None, issued_on=_as_utc(issued), expires_on=_as_utc(expires),
    )
    db.add(doc)
    await db.flush()
    await audit(db, owner, "branch.document_upload", "branch_document", doc.id, gym_id=branch.gym_id,
                detail={"branch_id": branch.id, "doc_type": doc_type.value}, request=request)
    have = list((await db.execute(select(BranchDocument.doc_type).where(BranchDocument.branch_id == branch.id))).scalars())
    await db.commit()
    missing = compliance.missing_requirements(branch.country_code, have)
    tail = (" All required documents are in - submit the branch for AI verification." if not missing
            else f" Still needed: {'; '.join(r.label for r in missing)}.")
    return redirect(f"/owner/branches/{branch.id}#documents", f"{compliance.doc_type_label(doc_type)} uploaded.{tail}")


@router.post("/owner/branches/{branch_id}/documents/{doc_id}/delete")
async def document_delete(request: Request, branch_id: IdParam, doc_id: IdParam, db: DB, owner: OwnerUser):
    branch = await get_owned_branch(db, branch_id, owner)
    doc = await db.scalar(select(BranchDocument).where(BranchDocument.id == doc_id, BranchDocument.branch_id == branch.id))
    if doc is None:
        raise NotFound("Document not found.")
    if branch.verification_status in (*IN_PROGRESS, VS.VERIFIED):
        return redirect(f"/owner/branches/{branch.id}#documents",
                        "Documents can't be deleted while the branch is being verified or once it is verified.", "error")
    key, doc_type = doc.file_key, doc.doc_type
    await db.execute(delete(BranchDocument).where(BranchDocument.id == doc.id))
    await audit(db, owner, "branch.document_delete", "branch_document", doc_id, gym_id=branch.gym_id,
                detail={"branch_id": branch.id, "doc_type": doc_type.value}, request=request)
    await db.commit()
    await storage.delete_key(key)
    return redirect(f"/owner/branches/{branch.id}#documents", "Document deleted.")


@router.get("/owner/branches/{branch_id}/documents/{doc_id}/file")
async def document_file(branch_id: IdParam, doc_id: IdParam, db: DB, owner: OwnerUser):
    branch = await get_owned_branch(db, branch_id, owner)
    doc = await db.scalar(select(BranchDocument).where(BranchDocument.id == doc_id, BranchDocument.branch_id == branch.id))
    if doc is None:
        raise NotFound("Document not found.")
    try:
        data = await storage.load_private(doc.file_key)
    except (OSError, ValueError):
        raise NotFound("The file is no longer available.") from None
    safe = re.sub(r"[^\w.\- ]", "_", doc.original_filename)[:120] or "document"
    return Response(data, media_type=doc.content_type, headers={
        "Content-Disposition": f"inline; filename=\"{safe}\"; filename*=UTF-8''{quote(doc.original_filename[:120])}",
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    })


# =====================================================================================================
# verification submit
# =====================================================================================================
@router.post("/owner/branches/{branch_id}/submit")
async def branch_submit(request: Request, branch_id: IdParam, db: DB, owner: OwnerUser):
    branch = await get_owned_branch(db, branch_id, owner, joinedload(Branch.gym), selectinload(Branch.hours),
                                    selectinload(Branch.documents))
    back = f"/owner/branches/{branch.id}#verification"
    if branch.verification_status in IN_PROGRESS:
        return redirect(back, "Verification is already running.", "info")
    if branch.verification_status == VS.VERIFIED:
        return redirect(back, "This branch is already verified.", "info")
    blockers = _submit_blockers(owner, branch, [d.doc_type for d in branch.documents])
    if blockers:
        return redirect(back, "Not ready yet: " + " ".join(b["text"] for b in blockers[:4]), "error")
    await branch_verification.queue_verification(db, branch, actor=owner, request=request)
    await db.commit()
    return redirect(back, "Submitted! Our OCR engines and AI are checking the documents - this usually takes a minute.")


# =====================================================================================================
# photos
# =====================================================================================================
@router.post("/owner/branches/{branch_id}/photos")
async def photos_upload(request: Request, branch_id: IdParam, db: DB, owner: OwnerUser):
    branch = await get_owned_branch(db, branch_id, owner, joinedload(Branch.gym))
    form = await request.form()
    errors: dict[str, str] = {}
    caption = _txt(form, "caption")
    try:
        kind = PhotoKind(_txt(form, "kind") or PhotoKind.OVERALL.value)
    except ValueError:
        kind = PhotoKind.OVERALL
        errors["kind"] = "Choose what the photos show."
    if len(caption) > 160:
        errors["caption"] = "Keep the caption under 160 characters."
    if branch.verification_status != VS.VERIFIED:
        errors["form"] = "Photos can be added once the branch is AI-verified."
    uploads = _files(form, "photos")
    # photos per branch, race-safe: the cap's lock is held until this request commits its photos
    room = await caps.room(db, "branch_photos", branch.id, select(BranchPhoto.id).where(BranchPhoto.branch_id == branch.id))
    max_photos = caps.limit("branch_photos")
    existing = max_photos - room if room else int(
        await db.scalar(select(func.count(BranchPhoto.id)).where(BranchPhoto.branch_id == branch.id)) or 0)
    if not uploads:
        errors["photos"] = "Choose at least one photo."
    elif len(uploads) > room:
        errors["photos"] = (f"A branch can show at most {max_photos} photos - you can add {room} more."
                            if room else str(caps.CapReached("branch_photos")))
    saved: list[storage.StoredFile] = []
    if not errors:
        for up in uploads:
            try:
                saved.append(await storage.save_public_image(up, kind="branch_photos"))
            except storage.UploadError as exc:
                errors["photos"] = f"{up.filename}: {exc}"
                break
    if saved and not errors:
        try:  # the gym's storage quota (after the narrower per-branch cap)
            await quotas.reserve_gym(db, branch.gym_id, sum(s.size for s in saved))
        except caps.CapReached as exc:
            errors["photos"] = str(exc)
    if errors:
        for s in saved:
            await storage.delete_key(s.key)
        return await _render_branch_page(request, db, owner, branch_id, status_code=400, photo_errors=errors)
    gym = branch.gym
    was_live = is_branch_published(branch, gym, existing)
    max_order = await db.scalar(select(func.max(BranchPhoto.sort_order)).where(BranchPhoto.branch_id == branch.id))
    for i, s in enumerate(saved):
        db.add(BranchPhoto(branch_id=branch.id, image_key=s.key, caption=caption or None, kind=kind,
                           width=s.width, height=s.height, sort_order=(max_order or 0) + i + 1, size_bytes=s.size))
    total = existing + len(saved)
    now_live = is_branch_published(branch, gym, total)
    if now_live and not was_live:
        await notify(db, owner, f"{branch.name} is live",
                     f"{branch.name} ({gym.name}) is now listed on the IronPulse website and can sell offline memberships "
                     f"to people within {get_settings().offline_max_distance_km:.0f} km.",
                     category=NotificationCategory.VERIFICATION, severity=Severity.SUCCESS,
                     link=f"/gyms/{gym.slug}/branches/{branch.id}")
    await audit(db, owner, "branch.photo_upload", "branch", branch.id, gym_id=branch.gym_id,
                detail={"count": len(saved), "kind": kind.value}, request=request)
    await db.commit()
    min_photos = get_settings().min_branch_photos
    added = _plural(len(saved), "photo")
    if now_live:
        msg = f"{added} added. Your branch is live on the website!"
    elif total < min_photos:
        msg = f"{added} added - {min_photos - total} more to go live."
    else:
        msg = f"{added} added."
    return redirect(f"/owner/branches/{branch.id}#photos", msg)


@router.post("/owner/branches/{branch_id}/photos/{photo_id}/delete")
async def photo_delete(request: Request, branch_id: IdParam, photo_id: IdParam, db: DB, owner: OwnerUser):
    branch = await get_owned_branch(db, branch_id, owner, joinedload(Branch.gym))
    photo = await db.scalar(select(BranchPhoto).where(BranchPhoto.id == photo_id, BranchPhoto.branch_id == branch.id))
    if photo is None:
        raise NotFound("Photo not found.")
    count = int(await db.scalar(select(func.count(BranchPhoto.id)).where(BranchPhoto.branch_id == branch.id)) or 0)
    was_live = is_branch_published(branch, branch.gym, count)
    key = photo.image_key
    await db.execute(delete(BranchPhoto).where(BranchPhoto.id == photo.id))
    await audit(db, owner, "branch.photo_delete", "branch_photo", photo_id, gym_id=branch.gym_id,
                detail={"branch_id": branch.id}, request=request)
    await db.commit()
    await storage.delete_key(key)
    if was_live and not is_branch_published(branch, branch.gym, count - 1):
        return redirect(f"/owner/branches/{branch.id}#photos",
                        f"Photo deleted. The branch is hidden until it has {get_settings().min_branch_photos} photos again.",
                        "warning")
    return redirect(f"/owner/branches/{branch.id}#photos", "Photo deleted.")


# =====================================================================================================
# close / reopen
# =====================================================================================================
@router.post("/owner/branches/{branch_id}/close")
async def branch_close(request: Request, branch_id: IdParam, db: DB, owner: OwnerUser):
    branch = await get_owned_branch(db, branch_id, owner, joinedload(Branch.gym))
    reason = _txt(await request.form(), "reason")
    await _lock_branch_state(db, branch)
    if not branch.is_open:
        return redirect(f"/owner/branches/{branch.id}#status", "This branch is already closed.", "info")
    if not 3 <= len(reason) <= 255:
        return await _render_branch_page(request, db, owner, branch_id, status_code=400,
                                         close_errors={"reason": "Tell your members why (3-255 characters)."})
    branch.is_open = False
    branch.closed_at = utcnow()
    branch.closed_reason = reason
    gym = branch.gym
    n = await _notify_members(
        db, gym_id=gym.id, branch_id=branch.id, severity=Severity.WARNING, link=f"/gyms/{gym.slug}",
        title=f"{gym.name} {branch.name} is closed",
        body=f"{branch.name} ({gym.name}) is temporarily closed. Reason: {reason}. We'll notify you when it reopens.",
    )
    await audit(db, owner, "branch.close", "branch", branch.id, gym_id=gym.id, detail={"reason": reason}, request=request)
    await db.commit()
    return redirect(f"/owner/branches/{branch.id}#status",
                    f"Branch closed and hidden from the website. {_notice_text(n)} "
                    "Its verification is kept - reopening needs no re-verification.")


@router.post("/owner/branches/{branch_id}/reopen")
async def branch_reopen(request: Request, branch_id: IdParam, db: DB, owner: OwnerUser):
    branch = await get_owned_branch(db, branch_id, owner, joinedload(Branch.gym))
    await _lock_branch_state(db, branch)
    if branch.is_open:
        return redirect(f"/owner/branches/{branch.id}#status", "This branch is already open.", "info")
    branch.is_open = True
    branch.closed_at = None
    branch.closed_reason = None
    gym = branch.gym
    n = await _notify_members(
        db, gym_id=gym.id, branch_id=branch.id, severity=Severity.SUCCESS, link=f"/gyms/{gym.slug}",
        title=f"{gym.name} {branch.name} has reopened", body=f"Good news - {branch.name} ({gym.name}) is open again.",
    )
    await audit(db, owner, "branch.reopen", "branch", branch.id, gym_id=gym.id, request=request)
    await db.commit()
    still = "" if branch.verification_status == VS.VERIFIED else " It still needs AI verification before it appears on the website."
    return redirect(f"/owner/branches/{branch.id}#status",
                    f"Branch reopened (no re-verification needed). {_notice_text(n)}{still}")
