"""WP-A1 public site: home, gym directory, gym + branch pages, plans explainer, jobs board + enquiry,
terms, privacy and about pages.

Rules followed here (see ARCHITECTURE.md):
* every list page issues a constant number of queries (batched loads, correlated subqueries - no N+1);
* only PUBLISHED branches are ever shown (``app.services.visibility``) - unverified, closed, photo-less
  branches and branches of gyms with physical locations disabled stay invisible (404 on direct links);
* offline / combo plans are gated by the 30 km rule (``offline_eligibility``) using the visitor's
  ``?lat=&lon=`` (from the "Use my location" button), the signed-in user's saved coordinates, or their city;
* job enquiries are validated server-side and protected against spam without AI (honeypot field, signed form token
  with a minimum fill time, per address + posting cooldown, same message to a posting refused for 24 h, link and
  length caps - ``job_enquire``), notify the gym owner and send the applicant a confirmation e-mail;
* the jobs board has the same "near me" option as the directory (same visitor location): a posting tied to a branch
  is measured to that branch only while it is PUBLISHED - an unpublished own branch makes the posting's location
  unknown (no distance, never measured to a sister branch, left out of "Near me", still in "All jobs") - and a posting
  for the whole gym to the gym's nearest published branch; no distance is shown beyond ``offline_max_distance_km``
  (30 km). Everything - bounding-box pre-filter, haversine, radius filter, nearest-first order, paging and the total -
  happens in ONE SQL statement per page (``query_jobs``); the box scan and the haversine run once per request (CTE
  ``near_branches``), never per posting, and the browser only supplies coordinates. Remote postings never get a
  distance; an unpublished branch is never named.

Bounded work per request (none of it grows with the platform or with a chain's size):
* home: the location-less "Featured gyms" block, the trainer teaser and the site figures are cached in-process
  (``app.core.ttl_cache``: 120 s / 120 s / 300 s; cleared when this process commits a change to the models they are
  built from); a visitor with a location gets the gyms with a published branch within ``NEAR_SORT_KM`` (one range
  scan of ``ix_branches_lat_lon``), topped up from the cached block;
* gym cards carry per-gym aggregates computed in SQL (branch count, 3 cities + how many more, the nearest branch's
  distance, one cover, the "from" price) - one row per gym, never the chain's branch rows;
* /gyms near me: gyms with a published branch inside the ``NEAR_SORT_KM`` box come first (nearest first, the box scan
  runs once), the rest follow A-Z; the city filter lists the ``DIRECTORY_CITIES`` busiest cities (cached 300 s);
* gym page: the ``BRANCH_PAGE_SIZE`` nearest (or first) branch cards + "View all N locations" ->
  ``/gyms/{slug}/branches?page=`` (paginated); a branch page loads its own branch by id and the
  ``OTHER_BRANCHES`` nearest others.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import string
import time
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from email_validator import EmailNotValidError, validate_email
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import and_, case, func, inspect, null, or_, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, contains_eager, selectinload
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.config import get_settings
from app.core import caps
from app.core.deps import DB, CurrentUser, NotFound, wants_json
from app.core.geo import EARTH_RADIUS_KM, haversine_km, valid_coords
from app.core.pagination import paginate, parse_page
from app.core.templating import redirect, render
from app.core.ttl_cache import TTLCache, clear_on_commit
from app.core.utils import DIAL_CODES, clamp, local_now, money, normalize_phone
from app.models import (
    Branch,
    BranchFeature,
    BranchHours,
    BranchPhoto,
    Employee,
    Gym,
    JobEnquiry,
    JobPosting,
    Plan,
    TrainerShift,
    User,
)
from app.models.enums import (
    EmploymentStatus,
    EmploymentType,
    EmployeeRole,
    NotificationCategory,
    PlanMode,
    Role,
    Severity,
)
from app.security.crypto import equals, hmac_hex
from app.security.net import client_ip
from app.services import admin_insights as insights  # refusal counters of the super-admin's site overview
from app.services import gym_limits, storage
from app.services.notify import email_address, notify
from app.services.visibility import OfflineEligibility, offline_eligibility, published_branch_clauses

log = logging.getLogger("ironpulse.public")
router = APIRouter()

PAGE_SIZE = 12
JOBS_PAGE_SIZE = 10
JOB_RADII_KM = (5, 10, 20)  # smaller /jobs?km= radius chips; the last chip is always jobs_max_km() (30 km)
Q_MAX = 100
HOME_FEATURED = 6  # gym cards in the home page's "Featured gyms" block
HOME_TRAINERS = 8  # trainer teaser on the home page
BRANCH_PAGE_SIZE = 12  # branch cards on a gym page and per page of /gyms/{slug}/branches
OTHER_BRANCHES = 6  # "Other locations" on a branch page
CARD_CITIES = 3  # city chips on a gym card (then "+N more")
CARD_FEATURES = 5  # feature chips on a branch card (then "+N")
GALLERY_PHOTOS = 5  # photos in a gym page's hero gallery (from its first branch cards)
HERO_CITIES = 12  # city chips in a gym page's hero (then "+N more")
CITY_GROUPS_MAX = 500  # distinct cities read for a gym page (hero chips + the city fallback of the 30 km rule)
DIRECTORY_CITIES = 60  # /gyms city filter: the cities with the most published branches (A-Z)
NEAR_SORT_KM = 100.0  # "nearest first" (home, /gyms): gyms with a published branch within this radius; the rest A-Z
HOME_TTL = 120.0  # seconds the location-less featured block and the trainer teaser are cached
STATS_TTL = 300.0  # seconds the site figures (home, /about) are cached
CITIES_TTL = 300.0  # seconds the /gyms city filter is cached
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
WEEKDAYS_SHORT = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# ------------------------------------------------------------------ plan catalogue (12 plan types)
MODE_INFO: dict[str, dict[str, str]] = {
    PlanMode.OFFLINE.value: {
        "label": "In-person",
        "long": "In-person (offline)",
        "icon": "building",
        "blurb": "Full access to a verified branch near you. Bought at the branch desk with an owner OTP.",
    },
    PlanMode.ONLINE.value: {
        "label": "Online",
        "long": "Online",
        "icon": "video",
        "blurb": "Live online sessions and remote coaching from anywhere - no branch needed.",
    },
    PlanMode.COMBO.value: {
        "label": "Combo",
        "long": "Combo (online + offline)",
        "icon": "layers",
        "blurb": "The best of both: branch access plus online sessions on the days you can't make it.",
    },
}

PLAN_TYPES: list[dict[str, Any]] = [
    {"code": f"{mode}{'_trainer' if trainer else ''}{'_ai' if ai_ else ''}", "mode": mode, "trainer": trainer, "ai": ai_}
    for mode in (PlanMode.OFFLINE.value, PlanMode.ONLINE.value, PlanMode.COMBO.value)
    for trainer, ai_ in ((False, False), (True, False), (False, True), (True, True))
]
for _pt in PLAN_TYPES:
    _extras = [x for x, on in (("Trainer", _pt["trainer"]), ("AI coach", _pt["ai"])) if on]
    _pt["title"] = MODE_INFO[_pt["mode"]]["label"] + ("".join(f" + {x}" for x in _extras))
    _pt["gym_access"] = _pt["mode"] in (PlanMode.OFFLINE.value, PlanMode.COMBO.value)
    _pt["online_sessions"] = _pt["mode"] in (PlanMode.ONLINE.value, PlanMode.COMBO.value)
    _pt["needs_branch"] = _pt["gym_access"]

JOB_PERIODS = {"hour": "hour", "hr": "hour", "day": "day", "week": "week", "month": "month", "year": "year",
               "annum": "year", "annual": "year"}


# ------------------------------------------------------------------ small helpers
def clean_text(value: Any, max_len: int) -> str:
    """Strip control characters + surrounding whitespace; non-strings become ''."""
    if not isinstance(value, str):
        return ""
    return _CONTROL_CHARS.sub("", value).strip()[:max_len]


def like_pattern(value: str) -> str:
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def parse_float(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 32:
        return None
    try:
        f = float(value)
    except ValueError:
        return None
    return f if math.isfinite(f) else None


def page_param(request: Request) -> int:
    try:
        n = int(request.query_params.get("page", "1"))
    except (TypeError, ValueError):
        return 1
    return min(max(n, 1), 10_000)


def km_label(d: float | None) -> str | None:
    if d is None:
        return None
    if d < 1:
        return f"{max(10, int(round(d * 100)) * 10)} m"
    if d < 10:
        return f"{d:.1f} km"
    return f"{d:.0f} km"


def money_compact(amount: Any, currency: str | None) -> str:
    s = money(amount, currency)
    return s[:-3] if s.endswith(".00") else s


def hhmm(t: Any) -> str:
    return t.strftime("%H:%M") if t is not None else ""


def compress_weekdays(days: Iterable[int]) -> str:
    """[0, 1, 2, 4] -> 'Mon–Wed, Fri'."""
    ds = sorted(set(d for d in days if 0 <= d <= 6))
    if len(ds) == 7:
        return "Every day"
    parts: list[str] = []
    i = 0
    while i < len(ds):
        j = i
        while j + 1 < len(ds) and ds[j + 1] == ds[j] + 1:
            j += 1
        if j - i >= 2:
            parts.append(f"{WEEKDAYS_SHORT[ds[i]]}–{WEEKDAYS_SHORT[ds[j]]}")
        else:
            parts.extend(WEEKDAYS_SHORT[d] for d in ds[i : j + 1])
        i = j + 1
    return ", ".join(parts)


def url_with(path: str, params: dict[str, Any], **changes: Any) -> str:
    """Build ``path?query`` from params (+ changes), dropping empty values and ``page=1``."""
    merged = {**params, **changes}
    clean = {k: v for k, v in merged.items() if v is not None and v != "" and not (k == "page" and v == 1)}
    return path + ("?" + urlencode(clean) if clean else "")


def int_id(value: str) -> int:
    """Path ids arrive as strings so junk URLs give a friendly 404 instead of a 422."""
    if not value.isdigit() or len(value) > 18:
        raise NotFound()
    return int(value)


# ------------------------------------------------------------------ visitor location (30 km rule input)
@dataclass(slots=True)
class VisitorLocation:
    lat: float | None = None
    lon: float | None = None
    city: str | None = None
    source: str = "none"  # query | saved | city | none

    @property
    def has_coords(self) -> bool:
        return self.lat is not None and self.lon is not None

    def distance_to(self, branch: Branch) -> float | None:
        if not self.has_coords or not valid_coords(branch.latitude, branch.longitude):
            return None
        return haversine_km(float(self.lat), float(self.lon), float(branch.latitude), float(branch.longitude))  # type: ignore[arg-type]

    @property
    def query(self) -> dict[str, Any]:
        """Params to keep in links (only coordinates that came from the URL)."""
        return {"lat": self.lat, "lon": self.lon} if self.source == "query" else {}

    @property
    def qs(self) -> str:
        """``?lat=..&lon=..`` suffix for links, so a shared location follows the visitor to gym/branch pages."""
        return "?" + urlencode(self.query) if self.query else ""


def visitor_location(request: Request, user: User | None) -> VisitorLocation:
    lat = parse_float(request.query_params.get("lat"))
    lon = parse_float(request.query_params.get("lon"))
    city = (user.city or "").strip() or None if user is not None else None
    if valid_coords(lat, lon):
        return VisitorLocation(round(float(lat), 6), round(float(lon), 6), city, "query")  # type: ignore[arg-type]
    if user is not None and valid_coords(user.latitude, user.longitude):
        return VisitorLocation(float(user.latitude), float(user.longitude), city, "saved")  # type: ignore[arg-type]
    if city:
        return VisitorLocation(None, None, city, "city")
    return VisitorLocation()


def sql_distance_km(lat: float, lon: float) -> Any:
    """Haversine distance (km) from a point to ``Branch`` as a SQL expression."""
    dlat = func.radians(Branch.latitude - lat)
    dlon = func.radians(Branch.longitude - lon)
    a = func.power(func.sin(dlat * 0.5), 2) + math.cos(math.radians(lat)) * func.cos(
        func.radians(Branch.latitude)
    ) * func.power(func.sin(dlon * 0.5), 2)
    return (2 * EARTH_RADIUS_KM) * func.asin(func.least(1.0, func.sqrt(a)))


# ------------------------------------------------------------------ visibility building blocks
def published_exists(*extra: Any) -> Any:
    """EXISTS (published branch of the outer ``Gym`` [matching ``extra``])."""
    return select(Branch.id).where(Branch.gym_id == Gym.id, *published_branch_clauses(), *extra).correlate(Gym).exists()


def online_open_clause() -> Any:
    return and_(Gym.online_enabled.is_(True), Gym.online_open.is_(True))


def gym_listed_clauses(owner: Any = User) -> list[Any]:
    """A gym is listed when it is active, its owner account exists and it has a published branch or open online mode.
    The query must JOIN ``owner`` (User or an alias) on ``Gym.owner_id``."""
    return [Gym.is_active.is_(True), owner.deleted_at.is_(None), or_(published_exists(), online_open_clause())]


# ------------------------------------------------------------------ job postings: remote rule + distance (SQL)
REMOTE_JOB_LOCATIONS = ("remote", "online", "anywhere", "work from home", "wfh")
REMOTE_JOB_PREFIX = "remote"  # "Remote", "Remote (India)", "remote - IST hours", ...
# Both twins below trim exactly what ``str.strip()`` trims (U+3000 is the last code point ``str.isspace()``
# accepts) and lower-case ASCII only, so Python and PostgreSQL classify every location text the same way.
_TRIM_CHARS = "".join(c for c in map(chr, range(0x3001)) if c.isspace())
_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


def is_remote_location(location: str | None) -> bool:
    """A posting is remote (no physical place: Remote badge, never a distance) when its location text says so."""
    key = (location or "").strip(_TRIM_CHARS).translate(_ASCII_LOWER)
    return key in REMOTE_JOB_LOCATIONS or key.startswith(REMOTE_JOB_PREFIX)


def remote_job_clause() -> Any:
    """SQL twin of :func:`is_remote_location` for ``JobPosting.location``."""
    key = func.translate(func.btrim(JobPosting.location, _TRIM_CHARS), string.ascii_uppercase, string.ascii_lowercase)
    return or_(key.in_(REMOTE_JOB_LOCATIONS), key.startswith(REMOTE_JOB_PREFIX, autoescape=True))


KM_PER_DEGREE = 111.045  # km per degree of latitude: a bit under the true ~111.195, so boxes err on the large side


def jobs_max_km() -> float:
    """The jobs board's hard distance limit: the platform's "near" rule (``offline_max_distance_km``, 30 km).
    No distance beyond it is ever shown, and the near-me radius never exceeds it."""
    return float(get_settings().offline_max_distance_km)


def job_radii(max_km: float) -> tuple[float, ...]:
    """Near-me radius chips: the JOB_RADII_KM below the limit, then the limit itself (the default)."""
    return (*(float(r) for r in JOB_RADII_KM if r < max_km), max_km)


def km_param(km: float) -> str:
    """A radius as written in URLs and copy: 5.0 -> '5', 7.5 -> '7.5'."""
    return f"{km:g}"


@dataclass(frozen=True, slots=True)
class GeoBox:
    """Latitude / longitude bounds around a point (``lon_min`` / ``lon_max`` None: every longitude)."""

    lat_min: float
    lat_max: float
    lon_min: float | None = None
    lon_max: float | None = None


def radius_box(lat: float, lon: float, km: float) -> GeoBox:
    """A box containing every point within ``km`` of (lat, lon): the cheap pre-filter (index ``ix_branches_lat_lon``)
    applied before the exact haversine. It errs on the large side: longitudes are scaled at the box's most polar
    latitude, and the longitude bounds are dropped when the box reaches a pole or crosses the antimeridian."""
    dlat = km / KM_PER_DEGREE
    lat_min, lat_max = lat - dlat, lat + dlat
    if lat_min <= -90.0 or lat_max >= 90.0:
        return GeoBox(max(lat_min, -90.0), min(lat_max, 90.0))
    dlon = km / (KM_PER_DEGREE * math.cos(math.radians(max(abs(lat_min), abs(lat_max)))))
    if lon - dlon < -180.0 or lon + dlon > 180.0:
        return GeoBox(lat_min, lat_max)
    return GeoBox(lat_min, lat_max, lon - dlon, lon + dlon)


def job_km_label(d: float | None) -> str | None:
    """A posting's distance as the jobs board shows it: rounded UP to the precision shown (10 m; 0.1 km below 10 km;
    1 km above) - the directory's ``km_label`` rounds to the nearest. So a label never reads as inside a radius chip
    that leaves the job out (5.04 km is "5.1 km", not "5.0 km"), never as outside one that lists it, and never above
    the limit (a limit that is not a whole number of the shown unit caps the label at the limit itself)."""
    if d is None:
        return None
    metres = math.ceil(d * 100) * 10
    if metres < 1000:
        return f"{max(10, metres)} m"
    tenths = math.ceil(d * 10)
    if tenths < 100:
        shown, text = tenths / 10, f"{tenths / 10:.1f}"
    else:
        shown = float(math.ceil(d))
        text = f"{shown:.0f}"
    limit = jobs_max_km()
    return f"{km_param(limit) if shown > limit else text} km"


def own_branch_clauses() -> list[Any]:
    """JOIN ``Branch`` ON these: the posting's own branch - only when it is PUBLISHED (and of the posting's gym). The
    query must already JOIN ``Gym`` on ``JobPosting.gym_id``. Public pages load a posting's branch this way only, so
    an unpublished branch's name and city never reach them (the posting's own location text is shown instead)."""
    return [Branch.id == JobPosting.branch_id, Branch.gym_id == Gym.id, *published_branch_clauses()]


def near_branches_cte(loc: VisitorLocation, radius_km: float, gyms: Any = None) -> Any:
    """CTE ``near_branches``: every PUBLISHED branch inside the radius' bounding box (``radius_box``) and its haversine
    distance ``km`` from the visitor. MATERIALIZED, so it runs ONCE per statement whatever the number of postings: one
    range scan of the box (index ``ix_branches_lat_lon`` when the box is selective), the published check and the
    haversine of each candidate branch - never per posting. ``km`` may exceed ``radius_km`` in the box's corners:
    callers compare it. ``gyms``: a subquery of gym ids to restrict it to (one page of postings, one posting)."""
    lat, lon = float(loc.lat), float(loc.lon)  # type: ignore[arg-type]
    box = radius_box(lat, lon, radius_km)
    where = [Branch.latitude.between(box.lat_min, box.lat_max)]
    if box.lon_min is not None:
        where.append(Branch.longitude.between(box.lon_min, box.lon_max))
    if gyms is not None:
        where.append(Branch.gym_id.in_(gyms))
    return (
        select(Branch.id, Branch.gym_id, Branch.name, sql_distance_km(lat, lon).label("km"))
        .join(Gym, Gym.id == Branch.gym_id)
        .where(*where, *published_branch_clauses())
        .cte("near_branches")
        .prefix_with("MATERIALIZED")
    )


@dataclass(frozen=True, slots=True)
class JobPlace:
    """SQL expressions of a posting's place (``join_job_place``). Remote postings are the caller's business."""

    km: Any  # visitor -> place; NULL when unknown or outside the radius' box
    branch_name: Any  # the branch measured to
    own: Any  # True when that is the posting's own branch, False when it is the gym's nearest


def join_job_place(stmt: Any, loc: VisitorLocation, radius_km: float, gyms: Any = None, *,
                   near: Any = None) -> tuple[Any, JobPlace]:
    """LEFT JOIN the place of each posting onto ``stmt`` (a SELECT over ``JobPosting`` JOIN ``Gym``), measured from
    ``near_branches_cte`` (or ``near``, one the caller built for the same radius) - the haversine runs once per
    candidate branch, never per posting.

    * A posting tied to a branch is measured to THAT branch only, and only while it is PUBLISHED: a far own branch stays
      far (no distance - it never borrows a sister branch's), and an UNPUBLISHED own branch (draft, closed, photo-less
      ...) makes the posting's location unknown - no distance at all, so it is never "near" (product decision: its real
      site is not public, and a sister branch could be anywhere). It is still listed under "All jobs".
    * A posting for the whole gym (no branch) is measured to the gym's NEAREST published branch (CTE ``gym_nearest``:
      DISTINCT ON gym over ``near_branches``).
    Unpublished branches never count, and a distance never reveals where a non-public branch is."""
    near = near if near is not None else near_branches_cte(loc, radius_km, gyms)
    nearest = (
        select(near.c.gym_id, near.c.name, near.c.km)
        .ext(distinct_on(near.c.gym_id))
        .order_by(near.c.gym_id, near.c.km, near.c.id)
        .cte("gym_nearest")
    )
    own = near.alias("own_branch")
    stmt = stmt.outerjoin(own, and_(own.c.id == JobPosting.branch_id, own.c.gym_id == Gym.id)).outerjoin(
        nearest, nearest.c.gym_id == Gym.id
    )
    whole_gym = JobPosting.branch_id.is_(None)
    km = case(
        (own.c.id.is_not(None), own.c.km),  # own branch: published, in the box
        (whole_gym, nearest.c.km),  # a posting for the whole gym: its nearest published branch (NULL: none in the box)
        else_=null(),  # own branch outside the box (far) or not published (location unknown): no distance
    )
    name = case((own.c.id.is_not(None), own.c.name), (whole_gym, nearest.c.name), else_=null())
    return stmt, JobPlace(km=km, branch_name=name, own=own.c.id.is_not(None))


def with_job_distance(stmt: Any, loc: VisitorLocation, max_km: float, gyms: Any) -> Any:
    """``stmt`` (a few postings: one page, one job) + their place columns ``km`` / ``branch_name`` / ``own``. ``km`` is
    NULL for remote postings and beyond ``max_km``: a farther distance never even leaves the database."""
    stmt, place = join_job_place(stmt, loc, max_km, gyms)
    km = case((remote_job_clause(), null()), (place.km <= max_km, place.km)).label("km")
    return stmt.add_columns(km, place.branch_name.label("branch_name"), place.own.label("own"))


# ------------------------------------------------------------------ view models
# Cards of cached blocks hold plain data, never ORM objects: an ORM object belongs to the session that loaded it (a
# rollback there would expire it under every other request reading the cache).
@dataclass(frozen=True, slots=True)
class PersonView:
    display_name: str
    photo_key: str | None


@dataclass(frozen=True, slots=True)
class GymView:
    """What a gym card shows of a gym (plain data)."""

    id: int
    slug: str
    name: str
    tagline: str | None
    logo_key: str | None
    online_enabled: bool
    online_open: bool
    currency: str
    owner: PersonView

    @classmethod
    def of(cls, gym: Gym) -> GymView:
        """``gym.owner`` must be loaded (contains_eager)."""
        return cls(gym.id, gym.slug, gym.name, gym.tagline, gym.logo_key, bool(gym.online_enabled), bool(gym.online_open),
                   gym.currency or "", PersonView(gym.owner.display_name, gym.owner.photo_key))


@dataclass(frozen=True, slots=True)
class GymCard:
    """A gym in a list: per-gym aggregates (``load_gym_cards``), never the chain's branch rows."""

    gym: GymView
    branch_count: int = 0  # published branches
    cities: tuple[str, ...] = ()  # the first CARD_CITIES distinct cities (A-Z)
    city_count: int = 0  # distinct cities in all
    cover_key: str | None = None  # one photo of the nearest (or first) published branch
    nearest_km: float | None = None
    from_price: str | None = None

    @property
    def more_cities(self) -> int:
        return max(0, self.city_count - len(self.cities))

    @property
    def online_open(self) -> bool:
        return bool(self.gym.online_enabled and self.gym.online_open)

    @property
    def distance_label(self) -> str | None:
        return km_label(self.nearest_km)


@dataclass(frozen=True, slots=True)
class PhotoView:
    image_key: str
    caption: str | None = None


@dataclass(slots=True)
class BranchCard:
    """A branch in a list (gym page, "other locations", /gyms/{slug}/branches): bounded data (``load_branch_cards``)."""

    branch: Branch
    distance_km: float | None = None
    open_now: bool | None = None
    hours_text: str | None = None
    photos: list[PhotoView] = field(default_factory=list)  # the first few (cover first)
    features: list[str] = field(default_factory=list)  # the first CARD_FEATURES (A-Z)
    feature_count: int = 0

    @property
    def distance_label(self) -> str | None:
        return km_label(self.distance_km)

    @property
    def cover_key(self) -> str | None:
        return self.photos[0].image_key if self.photos else None

    @property
    def more_features(self) -> int:
        return max(0, self.feature_count - len(self.features))


@dataclass(frozen=True, slots=True)
class BranchPoint:
    """Where a branch is - what ``offline_eligibility`` reads (id, coordinates, city) - without loading the branch."""

    id: int
    latitude: float | None
    longitude: float | None
    city: str | None


@dataclass(slots=True)
class PlanOffer:
    plan: Plan
    available: bool
    href: str | None = None
    note: str | None = None
    needs_location: bool = False
    limited: bool = False  # the signed-in member's gyms-per-person limit locks it (the card links "My memberships")

    @property
    def perks(self) -> list[tuple[str, str]]:
        p = self.plan
        perks: list[tuple[str, str]] = []
        if p.mode in (PlanMode.OFFLINE, PlanMode.COMBO):
            perks.append(("building", "Branch access"))
        if p.mode in (PlanMode.ONLINE, PlanMode.COMBO):
            perks.append(("video", "Live online sessions"))
        if p.includes_trainer:
            perks.append(("user", "Personal trainer"))
        if p.includes_ai:
            perks.append(("sparkles", "AI coach"))
        perks.append(("play", "Pre-recorded videos"))
        return perks


@dataclass(slots=True)
class PlanGroup:
    mode: str
    label: str
    icon: str
    blurb: str
    offers: list[PlanOffer]
    notice: str | None = None


@dataclass(slots=True)
class HoursRow:
    weekday: int
    name: str
    short: str
    opens: str | None
    closes: str | None
    today: bool


@dataclass(slots=True)
class OpenState:
    open: bool
    text: str


@dataclass(frozen=True, slots=True)
class GymRef:
    slug: str
    name: str


@dataclass(frozen=True, slots=True)
class StaffView:
    """What ``member_card`` reads of an Employee (plain data, for the cached trainer teaser)."""

    id: int
    display_name: str
    photo_key: str | None
    bio: str | None
    years_experience: int | None
    specialties: list[Any]
    role: EmployeeRole
    role_title: str | None
    is_trainer: bool
    gym: GymRef | None

    @classmethod
    def of(cls, e: Employee) -> StaffView:
        """``e.gym`` must be loaded (contains_eager)."""
        return cls(e.id, e.display_name, e.photo_key, e.bio, e.years_experience,
                   list(e.specialties) if isinstance(e.specialties, list) else [], e.role, e.role_title,
                   bool(e.is_trainer), GymRef(e.gym.slug, e.gym.name) if e.gym is not None else None)


@dataclass(slots=True)
class TeamMember:
    employee: Employee | StaffView
    schedule: list[str] = field(default_factory=list)

    @property
    def title(self) -> str:
        e = self.employee
        return e.role_title or e.role.value.replace("_", " ").capitalize()

    @property
    def specialties(self) -> list[str]:
        raw = self.employee.specialties if isinstance(self.employee.specialties, list) else []
        return [str(s)[:40] for s in raw if isinstance(s, (str, int, float)) and str(s).strip()][:4]


@dataclass(slots=True)
class JobView:
    job: JobPosting
    distance_km: float | None = None  # visitor -> place (``join_job_place``); only ever set within jobs_max_km()
    nearest_branch: str | None = None  # set when measured to the gym's nearest branch, not the posting's own
    enquiry_block: str | None = None  # the job page: why this visitor may not enquire (``seeker_block``)

    @property
    def remote(self) -> bool:
        return is_remote_location(self.job.location)

    @property
    def distance_label(self) -> str | None:
        return None if self.remote else job_km_label(self.distance_km)

    @property
    def location(self) -> str:
        """"<branch>, <city>" of the posting's own branch - public pages load it only when it is PUBLISHED
        (``own_branch_clauses``) - else the posting's own location text."""
        if self.job.branch is not None and not self.remote:
            return f"{self.job.branch.name}, {self.job.branch.city}"
        return self.job.location

    @property
    def salary(self) -> str | None:
        job = self.job
        per = JOB_PERIODS.get((job.salary_period or "month").strip().lower(), (job.salary_period or "month").strip())
        lo, hi = job.salary_min, job.salary_max
        if lo is None and hi is None:
            return None
        if lo is not None and hi is not None:
            if Decimal(lo) == Decimal(hi):
                return f"{money_compact(lo, job.currency)} / {per}"
            return f"{money_compact(lo, job.currency)} – {money_compact(hi, job.currency)} / {per}"
        if lo is not None:
            return f"From {money_compact(lo, job.currency)} / {per}"
        return f"Up to {money_compact(hi, job.currency)} / {per}"

    @property
    def excerpt(self) -> str:
        return clamp(re.sub(r"\s+", " ", self.job.requirements or ""), 180)

    @property
    def requirement_lines(self) -> list[str]:
        lines = [re.sub(r"^[\s\-*•·]+", "", ln).strip() for ln in (self.job.requirements or "").splitlines()]
        return [ln for ln in lines if ln]

    @property
    def type_label(self) -> str:
        return self.job.employment_type.value.replace("_", " ").capitalize()

    @property
    def role_label(self) -> str:
        return self.job.role.value.replace("_", " ").capitalize()


# ------------------------------------------------------------------ cached blocks (app.core.ttl_cache)
featured_cache = TTLCache("public.featured_gyms", HOME_TTL, maxsize=4)
trainers_cache = TTLCache("public.featured_trainers", HOME_TTL, maxsize=4)
stats_cache = TTLCache("public.site_stats", STATS_TTL, maxsize=4)
cities_cache = TTLCache("public.directory_cities", CITIES_TTL, maxsize=4)

# UPDATEs of these models only matter to the cached blocks through these columns (inserts and deletes always count;
# every column of Gym, Branch and BranchPhoto counts).
_CACHED_COLUMNS: dict[type, tuple[str, ...]] = {
    User: ("deleted_at", "full_name", "photo_key"),
    Employee: ("gym_id", "user_id", "status", "is_trainer", "is_public", "deleted_at", "full_name", "photo_key", "bio",
               "specialties", "years_experience", "role", "role_title"),
    Plan: ("gym_id", "mode", "price", "currency", "is_enabled"),
}


def _changes_cached_blocks(obj: Any) -> bool:
    columns = _CACHED_COLUMNS.get(type(obj))
    if columns is None:
        return True
    attrs = inspect(obj).attrs
    return any(attrs[c].history.has_changes() for c in columns)


clear_on_commit((featured_cache, trainers_cache, stats_cache, cities_cache),
                (Gym, Branch, BranchPhoto, Plan, Employee, User), changed=_changes_cached_blocks)


# ------------------------------------------------------------------ queries: gyms
def near_gyms_cte(loc: VisitorLocation, radius_km: float = NEAR_SORT_KM) -> Any:
    """CTE ``near_gyms``: the gyms with a PUBLISHED branch inside the radius' bounding box and the distance of their
    nearest one - built on ``near_branches_cte``, so it is one range scan of ``ix_branches_lat_lon`` per request and
    only the branches inside the box are checked and measured, never every branch of the platform."""
    near = near_branches_cte(loc, radius_km)
    return select(near.c.gym_id.label("gym_id"), func.min(near.c.km).label("km")).group_by(near.c.gym_id).cte("near_gyms")


async def load_gym_cards(db: AsyncSession, gyms: Sequence[Gym], loc: VisitorLocation) -> list[GymCard]:
    """Cards for a page of gyms (owners loaded): per-gym aggregates computed by PostgreSQL, so the rows read are one
    per gym (+ <= CARD_CITIES cities and the plan modes of each), however many branches a chain has:

    1. per gym - its published branches (count), the nearest one to the visitor (else the first by city and name), that
       one's distance and ONE cover photo;
    2. per gym - the first CARD_CITIES distinct cities (A-Z) and how many there are;
    3. per gym, plan mode and currency - the cheapest enabled plan (the "from" price).
    3 statements, any page size."""
    if not gyms:
        return []
    ids = [g.id for g in gyms]
    km = sql_distance_km(float(loc.lat), float(loc.lon)) if loc.has_coords else null()  # type: ignore[arg-type]
    order = ([km.asc().nulls_last()] if loc.has_coords else []) + [Branch.city, Branch.name, Branch.id]
    pub = (
        select(
            Branch.id.label("id"),
            Branch.gym_id.label("gym_id"),
            km.label("km"),
            func.row_number().over(partition_by=Branch.gym_id, order_by=order).label("rn"),
            func.count().over(partition_by=Branch.gym_id).label("n"),
        )
        .join(Gym, Gym.id == Branch.gym_id)
        .where(Branch.gym_id.in_(ids), *published_branch_clauses())
        .subquery("published")
    )
    cover = (
        select(BranchPhoto.image_key)
        .where(BranchPhoto.branch_id == pub.c.id)
        .order_by(BranchPhoto.sort_order, BranchPhoto.id)
        .limit(1)
        .correlate(pub)
        .scalar_subquery()
    )
    firsts = {
        gid: (int(n), nearest, key)
        for gid, n, nearest, key in await db.execute(select(pub.c.gym_id, pub.c.n, pub.c.km, cover).where(pub.c.rn == 1))
    }
    city = func.trim(Branch.city)
    grouped = (
        select(Branch.gym_id.label("gym_id"), func.min(city).label("city"))
        .join(Gym, Gym.id == Branch.gym_id)
        .where(Branch.gym_id.in_(ids), *published_branch_clauses(), city != "")
        .group_by(Branch.gym_id, func.lower(city))
        .subquery("cities")
    )
    ranked = select(
        grouped.c.gym_id,
        grouped.c.city,
        func.row_number().over(partition_by=grouped.c.gym_id, order_by=(func.lower(grouped.c.city), grouped.c.city)).label("rn"),
        func.count().over(partition_by=grouped.c.gym_id).label("n"),
    ).subquery("ranked_cities")
    cities: dict[int, list[str]] = defaultdict(list)
    city_counts: dict[int, int] = {}
    for gid, name, n in await db.execute(
        select(ranked.c.gym_id, ranked.c.city, ranked.c.n).where(ranked.c.rn <= CARD_CITIES).order_by(ranked.c.gym_id, ranked.c.rn)
    ):
        cities[gid].append(name)
        city_counts[gid] = int(n)
    price_rows = await db.execute(
        select(Plan.gym_id, Plan.mode, Plan.currency, func.min(Plan.price))
        .where(Plan.gym_id.in_(ids), Plan.is_enabled.is_(True))
        .group_by(Plan.gym_id, Plan.mode, Plan.currency)
    )
    prices: dict[int, list[tuple[PlanMode, str, Decimal]]] = defaultdict(list)
    for gid, mode, cur, price in price_rows:
        prices[gid].append((mode, cur, price))

    cards: list[GymCard] = []
    for g in gyms:
        n, nearest, cover_key = firsts.get(g.id, (0, None, None))
        online_open = bool(g.online_enabled and g.online_open)
        modes_ok = set()
        if n:
            modes_ok.add(PlanMode.OFFLINE)
        if online_open:
            modes_ok.add(PlanMode.ONLINE)
        if n and online_open:
            modes_ok.add(PlanMode.COMBO)
        from_price = None
        offers = [(price, cur) for mode, cur, price in prices.get(g.id, []) if mode in modes_ok]
        if offers:
            same = [o for o in offers if (o[1] or "").upper() == (g.currency or "").upper()] or offers
            price, cur = min(same, key=lambda o: o[0])
            from_price = money_compact(price, cur)
        cards.append(GymCard(
            gym=GymView.of(g), branch_count=n, cities=tuple(cities.get(g.id, ())), city_count=city_counts.get(g.id, 0),
            cover_key=cover_key, nearest_km=float(nearest) if nearest is not None else None, from_price=from_price,
        ))
    return cards


@dataclass(slots=True)
class DirectoryPage:
    cards: list[GymCard]
    total: int  # -1 when not counted (``with_total=False``)
    page: int
    pages: int


async def query_directory(
    db: AsyncSession,
    *,
    loc: VisitorLocation,
    q: str = "",
    city: str = "",
    mode: str = "",
    page: int = 1,
    per_page: int = PAGE_SIZE,
    featured: bool = False,
    near_only: bool = False,
    with_total: bool = True,
) -> DirectoryPage:
    """Listed gyms (filtered). With a location: the gyms with a published branch within NEAR_SORT_KM first, nearest
    first (``near_gyms_cte`` - one box scan per request; ``near_only``: only those), then the rest A-Z; ``featured``:
    the largest chains first; else A-Z. Constant statements: [COUNT +] page + ``load_gym_cards`` (3).
    ``with_total=False`` skips the COUNT (the home page never shows a total; not with ``near_only``)."""
    clauses = [Gym.is_active.is_(True), User.deleted_at.is_(None)]
    if mode == "offline":
        clauses.append(published_exists())
    elif mode == "online":
        clauses.append(online_open_clause())
    else:
        clauses.append(or_(published_exists(), online_open_clause()))
    if city:
        clauses.append(published_exists(func.lower(func.trim(Branch.city)) == city.strip().lower()))
    if q:
        pat = like_pattern(q)
        clauses.append(
            or_(
                Gym.name.ilike(pat, escape="\\"),
                Gym.tagline.ilike(pat, escape="\\"),
                published_exists(Branch.city.ilike(pat, escape="\\")),
            )
        )
    total, pages, page = -1, 1, max(page, 1)
    if with_total and not near_only:
        total = int(
            await db.scalar(select(func.count(Gym.id)).select_from(Gym).join(User, User.id == Gym.owner_id).where(*clauses))
            or 0
        )
        pages = max(1, math.ceil(total / per_page))
        page = min(page, pages)

    stmt = select(Gym).join(User, User.id == Gym.owner_id).options(contains_eager(Gym.owner)).where(*clauses)
    if loc.has_coords:
        near = near_gyms_cte(loc)
        stmt = stmt.join(near, near.c.gym_id == Gym.id) if near_only else stmt.outerjoin(near, near.c.gym_id == Gym.id)
        stmt = stmt.order_by(near.c.km.asc().nulls_last(), func.lower(Gym.name), Gym.id)
    elif featured:
        pub_count = (
            select(func.count(Branch.id))
            .where(Branch.gym_id == Gym.id, *published_branch_clauses())
            .correlate(Gym)
            .scalar_subquery()
        )
        stmt = stmt.order_by(pub_count.desc(), Gym.online_open.desc(), Gym.created_at.desc(), Gym.id)
    else:
        stmt = stmt.order_by(func.lower(Gym.name), Gym.id)
    stmt = stmt.offset((page - 1) * per_page).limit(per_page)
    gyms = list((await db.execute(stmt)).unique().scalars().all())
    cards = await load_gym_cards(db, gyms, loc)
    return DirectoryPage(cards=cards, total=total, page=page, pages=pages)


async def featured_gyms(db: AsyncSession) -> list[GymCard]:
    """The location-less "Featured gyms" block of the home page (the largest chains first, then gyms open online), cached
    HOME_TTL seconds: ordering every listed gym by its number of published branches scans them all."""

    async def build() -> tuple[GymCard, ...]:
        page = await query_directory(db, loc=VisitorLocation(), per_page=HOME_FEATURED, featured=True, with_total=False)
        return tuple(page.cards)

    return list(await featured_cache.get_or_compute("featured", build))


async def home_featured(db: AsyncSession, loc: VisitorLocation) -> list[GymCard]:
    """Home "Featured gyms": with a location, the nearest gyms with a published branch within NEAR_SORT_KM (one box
    scan; not cached - the distances are the visitor's), topped up from the cached block; without one, the cached block."""
    if not loc.has_coords:
        return await featured_gyms(db)
    cards = (await query_directory(db, loc=loc, per_page=HOME_FEATURED, near_only=True, with_total=False)).cards
    if len(cards) < HOME_FEATURED:
        shown = {c.gym.id for c in cards}
        cards += [c for c in await featured_gyms(db) if c.gym.id not in shown][: HOME_FEATURED - len(cards)]
    return cards


async def directory_cities(db: AsyncSession) -> list[str]:
    """The /gyms city filter: the DIRECTORY_CITIES cities with the most published branches, A-Z, cached CITIES_TTL
    seconds (it scans every published branch). Any other city still works: ``?q=`` matches branch cities and the
    filter shows the selected ``?city=`` as an extra option."""

    async def build() -> tuple[str, ...]:
        norm = func.lower(func.trim(Branch.city))
        rows = await db.execute(
            select(func.min(func.trim(Branch.city)))
            .select_from(Branch)
            .join(Gym, Gym.id == Branch.gym_id)
            .join(User, User.id == Gym.owner_id)
            .where(*published_branch_clauses(), User.deleted_at.is_(None), func.trim(Branch.city) != "")
            .group_by(norm)
            .order_by(func.count(Branch.id).desc(), norm)
            .limit(DIRECTORY_CITIES)
        )
        return tuple(sorted((r[0] for r in rows if r[0]), key=lambda c: (c.lower(), c)))

    return list(await cities_cache.get_or_compute("cities", build))


async def site_stats(db: AsyncSession) -> dict[str, int]:
    """The home and /about figures (published branches, gyms online, public trainers), cached STATS_TTL seconds: each
    figure counts a whole table."""
    return dict(await stats_cache.get_or_compute("stats", lambda: _count_site_stats(db)))


async def _count_site_stats(db: AsyncSession) -> tuple[tuple[str, int], ...]:
    owner = aliased(User)
    branches = (
        select(func.count(Branch.id))
        .select_from(Branch)
        .join(Gym, Gym.id == Branch.gym_id)
        .join(owner, owner.id == Gym.owner_id)
        .where(*published_branch_clauses(), owner.deleted_at.is_(None))
        .scalar_subquery()
    )
    online_owner = aliased(User)
    online = (
        select(func.count(Gym.id))
        .select_from(Gym)
        .join(online_owner, online_owner.id == Gym.owner_id)
        .where(Gym.is_active.is_(True), online_open_clause(), online_owner.deleted_at.is_(None))
        .scalar_subquery()
    )
    trainer_owner = aliased(User)
    trainer_user = aliased(User)
    trainers = (
        select(func.count(Employee.id))
        .select_from(Employee)
        .join(Gym, Gym.id == Employee.gym_id)
        .join(trainer_owner, trainer_owner.id == Gym.owner_id)
        .join(trainer_user, trainer_user.id == Employee.user_id)
        .where(
            Employee.status == EmploymentStatus.ACTIVE,
            Employee.is_trainer.is_(True),
            Employee.is_public.is_(True),
            Employee.deleted_at.is_(None),
            trainer_user.deleted_at.is_(None),
            trainer_user.is_active.is_(True),  # a disabled (banned) account is never counted or listed
            *gym_listed_clauses(trainer_owner),
        )
        .scalar_subquery()
    )
    row = (await db.execute(select(branches, online, trainers))).one()
    return (("branches", int(row[0] or 0)), ("online", int(row[1] or 0)), ("trainers", int(row[2] or 0)))


async def featured_trainers(db: AsyncSession, limit: int = HOME_TRAINERS) -> list[TeamMember]:
    """The home page's trainer teaser (the most experienced public trainers of listed gyms), cached HOME_TTL seconds."""

    async def build() -> tuple[TeamMember, ...]:
        staff_user = aliased(User)
        owner = aliased(User)
        rows = (
            await db.execute(
                select(Employee)
                .join(Gym, Gym.id == Employee.gym_id)
                .join(owner, owner.id == Gym.owner_id)
                .join(staff_user, staff_user.id == Employee.user_id)
                .options(contains_eager(Employee.gym))
                .where(
                    Employee.status == EmploymentStatus.ACTIVE,
                    Employee.is_trainer.is_(True),
                    Employee.is_public.is_(True),
                    Employee.deleted_at.is_(None),
                    staff_user.deleted_at.is_(None),
                    staff_user.is_active.is_(True),  # a disabled (banned) account is never listed
                    *gym_listed_clauses(owner),
                )
                .order_by(Employee.years_experience.desc().nulls_last(), Employee.id)
                .limit(limit)
            )
        ).unique().scalars().all()
        return tuple(TeamMember(StaffView.of(e)) for e in rows)

    return list(await trainers_cache.get_or_compute(limit, build))


async def load_public_gym(db: AsyncSession, slug: str) -> Gym:
    """Active gym (owner account not deleted) by slug, with its owner. 404 otherwise."""
    if not slug or len(slug) > 140:
        raise NotFound("Gym not found.")
    gym = (
        await db.execute(
            select(Gym)
            .join(User, User.id == Gym.owner_id)
            .options(contains_eager(Gym.owner))
            .where(Gym.slug == slug, Gym.is_active.is_(True), User.deleted_at.is_(None))
        )
    ).unique().scalar_one_or_none()
    if gym is None:
        raise NotFound("Gym not found.")
    return gym


def published_branches_stmt(gym_id: int) -> Any:
    """SELECT the PUBLISHED branches of one gym (unordered, unbounded: add ORDER BY + LIMIT / ``paginate``)."""
    return select(Branch).join(Gym, Gym.id == Branch.gym_id).where(Branch.gym_id == gym_id, *published_branch_clauses())


def branch_order(loc: VisitorLocation, origin: Branch | None = None) -> list[Any]:
    """ORDER BY of a branch list: nearest to the visitor first (else to ``origin``, a branch with a location), then by
    city and name."""
    keys: list[Any] = [Branch.city, Branch.name, Branch.id]
    if loc.has_coords:
        point = (float(loc.lat), float(loc.lon))  # type: ignore[arg-type]
    elif origin is not None and valid_coords(origin.latitude, origin.longitude):
        point = (float(origin.latitude), float(origin.longitude))  # type: ignore[arg-type]
    else:
        return keys
    return [sql_distance_km(*point).asc().nulls_last(), *keys]


async def load_branch_cards(db: AsyncSession, branches: Sequence[Branch], loc: VisitorLocation, *,
                            photos: int = 1) -> list[BranchCard]:
    """Cards for a page of branches (already ordered and bounded): the open state (opening hours), the first ``photos``
    photos (cover first) and the first CARD_FEATURES features (+ how many) of each - 3 statements with bounded rows,
    whatever the size of the chain."""
    if not branches:
        return []
    ids = [b.id for b in branches]
    hours: dict[int, list[BranchHours]] = defaultdict(list)
    for h in (await db.execute(select(BranchHours).where(BranchHours.branch_id.in_(ids)))).scalars():
        hours[h.branch_id].append(h)
    photo_rank = func.row_number().over(partition_by=BranchPhoto.branch_id, order_by=(BranchPhoto.sort_order, BranchPhoto.id))
    ranked_photos = (
        select(BranchPhoto.branch_id, BranchPhoto.image_key, BranchPhoto.caption, photo_rank.label("rn"))
        .where(BranchPhoto.branch_id.in_(ids))
        .subquery("photos")
    )
    pictures: dict[int, list[PhotoView]] = defaultdict(list)
    for bid, key, caption in await db.execute(
        select(ranked_photos.c.branch_id, ranked_photos.c.image_key, ranked_photos.c.caption)
        .where(ranked_photos.c.rn <= max(1, photos))
        .order_by(ranked_photos.c.branch_id, ranked_photos.c.rn)
    ):
        pictures[bid].append(PhotoView(key, caption))
    feature_rank = func.row_number().over(partition_by=BranchFeature.branch_id, order_by=(BranchFeature.name, BranchFeature.id))
    ranked_features = (
        select(BranchFeature.branch_id, BranchFeature.name, feature_rank.label("rn"),
               func.count().over(partition_by=BranchFeature.branch_id).label("n"))
        .where(BranchFeature.branch_id.in_(ids))
        .subquery("features")
    )
    features: dict[int, list[str]] = defaultdict(list)
    feature_counts: dict[int, int] = {}
    for bid, name, n in await db.execute(
        select(ranked_features.c.branch_id, ranked_features.c.name, ranked_features.c.n)
        .where(ranked_features.c.rn <= CARD_FEATURES)
        .order_by(ranked_features.c.branch_id, ranked_features.c.rn)
    ):
        features[bid].append(name)
        feature_counts[bid] = int(n)
    cards = []
    for b in branches:
        _, state = hours_view(b, hours.get(b.id, []))
        cards.append(BranchCard(branch=b, distance_km=loc.distance_to(b), open_now=state.open, hours_text=state.text,
                                photos=pictures.get(b.id, []), features=features.get(b.id, []),
                                feature_count=feature_counts.get(b.id, 0)))
    return cards


@dataclass(slots=True)
class BranchSummary:
    total: int  # published branches
    cities: list[str]  # distinct cities, A-Z (at most CITY_GROUPS_MAX)
    city_count: int  # distinct cities in all
    city_points: list[BranchPoint]  # one branch per city: the city fallback of the 30 km rule


async def branch_summary(db: AsyncSession, gym_id: int) -> BranchSummary:
    """A gym's published branches in ONE aggregate statement - how many, in which cities and one branch per city (one
    row per city, at most CITY_GROUPS_MAX) - never the branch rows themselves."""
    city = func.trim(Branch.city)
    key = func.lower(city)
    rows = (
        await db.execute(
            select(func.min(city), func.min(Branch.id), func.sum(func.count(Branch.id)).over(), func.count().over())
            .select_from(Branch)
            .join(Gym, Gym.id == Branch.gym_id)
            .where(Branch.gym_id == gym_id, *published_branch_clauses())
            .group_by(key)
            .order_by(key)
            .limit(CITY_GROUPS_MAX)
        )
    ).all()
    if not rows:
        return BranchSummary(0, [], 0, [])
    named = [(name, int(bid)) for name, bid, _, _ in rows if name]
    groups = int(rows[0][3]) - (0 if rows[0][0] else 1)  # an empty city (sorted first) is not a city
    return BranchSummary(total=int(rows[0][2]), cities=[name for name, _ in named], city_count=groups,
                         city_points=[BranchPoint(bid, None, None, name) for name, bid in named])


async def enabled_plans(db: AsyncSession, gym_id: int) -> list[Plan]:
    return list(
        (
            await db.execute(
                select(Plan).where(Plan.gym_id == gym_id, Plan.is_enabled.is_(True)).order_by(Plan.sort_order, Plan.price, Plan.id)
            )
        ).scalars().all()
    )


async def public_staff(db: AsyncSession, gym_id: int) -> list[TeamMember]:
    rows = (
        await db.execute(
            select(Employee)
            .join(User, User.id == Employee.user_id)
            .where(
                Employee.gym_id == gym_id,
                Employee.status == EmploymentStatus.ACTIVE,
                Employee.is_public.is_(True),
                Employee.deleted_at.is_(None),
                User.deleted_at.is_(None),
                User.is_active.is_(True),  # a disabled (banned) account is never listed
            )
            .order_by(Employee.is_trainer.desc(), Employee.full_name, Employee.id)
            .limit(48)
        )
    ).scalars().all()
    return [TeamMember(e) for e in rows]


async def branch_team(db: AsyncSession, branch: Branch) -> list[TeamMember]:
    """Distinct ACTIVE, public employees with shifts at this branch (+ their compact weekly schedule)."""
    shifts = (
        await db.execute(
            select(TrainerShift)
            .join(Employee, Employee.id == TrainerShift.employee_id)
            .join(User, User.id == Employee.user_id)
            .options(contains_eager(TrainerShift.employee))
            .where(
                TrainerShift.branch_id == branch.id,
                Employee.gym_id == branch.gym_id,
                Employee.status == EmploymentStatus.ACTIVE,
                Employee.is_public.is_(True),
                Employee.deleted_at.is_(None),
                User.deleted_at.is_(None),
                User.is_active.is_(True),  # a disabled (banned) account is never listed
            )
            .order_by(Employee.is_trainer.desc(), Employee.full_name, Employee.id, TrainerShift.weekday, TrainerShift.start_time)
        )
    ).unique().scalars().all()
    members: dict[int, TeamMember] = {}
    slots: dict[int, dict[tuple[str, str], list[int]]] = defaultdict(lambda: defaultdict(list))
    for s in shifts:
        members.setdefault(s.employee_id, TeamMember(s.employee))
        slots[s.employee_id][(hhmm(s.start_time), hhmm(s.end_time))].append(s.weekday)
    for emp_id, member in members.items():
        member.schedule = [
            f"{compress_weekdays(days)} · {start}–{end}" for (start, end), days in sorted(slots[emp_id].items(), key=lambda kv: (min(kv[1]), kv[0]))
        ]
    return list(members.values())


# ------------------------------------------------------------------ branch hours / open state
def hours_view(branch: Branch, hours: Iterable[BranchHours] | None = None) -> tuple[list[HoursRow], OpenState]:
    """The week's opening hours and whether the branch is open now (local time). ``hours``: the branch's rows when
    ``branch.hours`` is not loaded."""
    now = local_now(branch.timezone)
    today = now.weekday()
    t = now.time()
    by_day = {h.weekday: h for h in (branch.hours if hours is None else hours)}
    rows = [
        HoursRow(
            weekday=i,
            name=WEEKDAYS[i],
            short=WEEKDAYS_SHORT[i],
            opens=hhmm(by_day[i].opens_at) if i in by_day else None,
            closes=hhmm(by_day[i].closes_at) if i in by_day else None,
            today=i == today,
        )
        for i in range(7)
    ]
    h = by_day.get(today)
    if h is not None and h.opens_at <= t < h.closes_at:
        return rows, OpenState(True, f"Open now · closes {hhmm(h.closes_at)}")
    if h is not None and t < h.opens_at:
        return rows, OpenState(False, f"Closed · opens today at {hhmm(h.opens_at)}")
    for step in range(1, 8):
        wd = (today + step) % 7
        nxt = by_day.get(wd)
        if nxt is not None:
            when = "tomorrow" if step == 1 else WEEKDAYS[wd]
            return rows, OpenState(False, f"Closed · opens {when} at {hhmm(nxt.opens_at)}")
    return rows, OpenState(False, "Closed · opening hours not published")


# ------------------------------------------------------------------ plans + 30 km gating
def subscribe_href(gym: Gym, plan: Plan, branch_id: int | None) -> str:
    params: dict[str, Any] = {"plan": plan.code}
    if plan.mode in (PlanMode.OFFLINE, PlanMode.COMBO) and branch_id is not None:
        params["branch"] = branch_id
    return f"/subscribe/{gym.slug}?{urlencode(params)}"


def plan_groups(
    gym: Gym, plans: Sequence[Plan], has_branches: bool, elig: OfflineEligibility | None, branch_id: int | None
) -> list[PlanGroup]:
    online_on = bool(gym.online_enabled)
    online_open = bool(gym.online_enabled and gym.online_open)
    groups: list[PlanGroup] = []
    for mode in (PlanMode.OFFLINE, PlanMode.ONLINE, PlanMode.COMBO):
        mode_plans = [p for p in plans if p.mode == mode]
        if not mode_plans:
            continue
        needs_branch = mode in (PlanMode.OFFLINE, PlanMode.COMBO)
        needs_online = mode in (PlanMode.ONLINE, PlanMode.COMBO)
        if (needs_branch and not has_branches) or (needs_online and not online_on):
            continue
        notice = None
        if needs_online and not online_open:
            notice = "Online coaching is temporarily closed by the gym. Check back soon."
        offers: list[PlanOffer] = []
        for p in mode_plans:
            offer = PlanOffer(plan=p, available=True)
            if needs_online and not online_open:
                offer.available, offer.note = False, "Online is temporarily closed"
            elif needs_branch:
                if elig is None or (not elig.eligible and elig.method == "unknown"):
                    offer.available, offer.needs_location = False, True
                    offer.note = "Share your location to check the 30 km rule"
                elif not elig.eligible:
                    offer.available, offer.note = False, "Not available in your area"
            if offer.available:
                target_branch = branch_id if branch_id is not None else (elig.nearest_branch_id if elig else None)
                offer.href = subscribe_href(gym, p, target_branch)
            offers.append(offer)
        info = MODE_INFO[mode.value]
        groups.append(PlanGroup(mode=mode.value, label=info["long"], icon=info["icon"], blurb=info["blurb"],
                                offers=offers, notice=notice))
    return groups


async def apply_gym_limits(db: AsyncSession, user: User | None, gym: Gym, groups: list[PlanGroup]) -> None:
    """A signed-in member at the gyms-per-person limit (3 online / 1 in person, app/services/gym_limits.py) sees why on
    the plan cards instead of a Subscribe button that would only be refused at checkout. One statement, members only."""
    if user is None or user.role != Role.CUSTOMER or not any(o.available for g in groups for o in g.offers):
        return
    slots = await gym_limits.gym_slots(db, user.id)
    for group in groups:
        for offer in group.offers:
            note = slots.short_reason(gym.id, offer.plan.mode) if offer.available else None
            if note:
                offer.available, offer.href, offer.note, offer.limited = False, None, note, True


# ------------------------------------------------------------------ queries: jobs
def open_job_clauses(owner: Any) -> list[Any]:
    """Open postings of active gyms whose owner account exists (the query must JOIN Gym and ``owner``)."""
    return [JobPosting.is_open.is_(True), Gym.is_active.is_(True), owner.deleted_at.is_(None)]


def open_job_stmt(owner: Any) -> Any:
    """Open postings with their gym + owner and their own branch - loaded only when PUBLISHED (``own_branch_clauses``;
    ``job.branch`` is None otherwise), so an unpublished branch's name and city never reach a public page."""
    return (
        select(JobPosting)
        .join(Gym, Gym.id == JobPosting.gym_id)
        .join(owner, owner.id == Gym.owner_id)
        .outerjoin(Branch, and_(*own_branch_clauses()))
        .options(contains_eager(JobPosting.gym).contains_eager(Gym.owner.of_type(owner)), contains_eager(JobPosting.branch))
        .where(*open_job_clauses(owner))
    )


def job_view(row: Any, max_km: float) -> JobView:
    """JobView of a result row whose first column is the posting, optionally followed by the place columns
    ``km`` / ``branch_name`` / ``own``. A distance is attached only within ``max_km`` (and never to remote jobs)."""
    job, m = row[0], row._mapping
    km = m.get("km")
    if km is None or not 0.0 <= float(km) <= max_km or is_remote_location(job.location):
        return JobView(job)
    return JobView(job, distance_km=float(km), nearest_branch=None if m.get("own") else m.get("branch_name"))


async def load_open_job(db: AsyncSession, job_id: int, loc: VisitorLocation, user: User | None = None) -> JobView:
    """An open posting (404 otherwise) with its gym, owner and published branch - and, when the visitor's location is
    known, its distance (within the 30 km limit, else none) computed by the same single query (``near_branches``
    restricted to this posting's gym). For a signed-in visitor, whether they may enquire (``seeker_block``): a staff
    account's current employer rides along in that same statement, so the page's statement count never changes."""
    owner = aliased(User)
    max_km = jobs_max_km()
    stmt = open_job_stmt(owner).where(JobPosting.id == job_id)
    if loc.has_coords:
        stmt = with_job_distance(stmt, loc, max_km, select(JobPosting.gym_id).where(JobPosting.id == job_id))
    employer = employer_column(user)
    if employer is not None:
        stmt = stmt.add_columns(employer)
    row = (await db.execute(stmt)).one_or_none()
    if row is None:
        raise NotFound("This job posting is closed or does not exist.")
    jv = job_view(row, max_km)
    jv.enquiry_block = seeker_block(user, row._mapping.get("employer") if employer is not None else None)
    return jv


# ------------------------------------------------------------------ job enquiries are for job seekers
# The owner: "enquire for jobs must be disabled for people in job or gym owners". Signed-in gym owners, super-admins and
# staff accounts with a CURRENT employment (active or suspended, at any gym) see this explanation instead of the
# enquiry form, and the enquiry route refuses them before any spam / cooldown accounting. Guests and members enquire
# as before; staff whose employment ended (terminated, or a deleted record) are job seekers again. Only staff accounts
# can hold an employment (app/routers/auth.py links invitations to staff accounts only).
EMPLOYED_STATUSES = (EmploymentStatus.ACTIVE, EmploymentStatus.SUSPENDED)
SEEKERS_ONLY = "Enquiries are for job seekers"


def employer_column(user: User | None) -> Any | None:
    """For a signed-in staff account: a scalar subquery naming a gym that currently employs them (NULL = none), to
    add to a statement the page runs anyway. None for everyone else (decided without SQL)."""
    if user is None or user.role != Role.STAFF:
        return None
    employer, emp = aliased(Gym), aliased(Employee)
    return (
        select(employer.name)
        .join(emp, emp.gym_id == employer.id)
        .where(emp.user_id == user.id, emp.status.in_(EMPLOYED_STATUSES), emp.deleted_at.is_(None))
        .order_by(emp.id)
        .limit(1)
        .correlate(None)
        .scalar_subquery()
        .label("employer")
    )


def seeker_block(user: User | None, employer: str | None) -> str | None:
    """Why this visitor may not enquire about job postings (None = they may) - one sentence, safe to show."""
    if user is None:
        return None
    if user.role == Role.SUPERADMIN:
        return f"{SEEKERS_ONLY} - you are signed in as the platform super-admin."
    if user.role == Role.OWNER:
        return f"{SEEKERS_ONLY} - you are signed in as a gym owner."
    if user.role == Role.STAFF and employer:
        return (f"{SEEKERS_ONLY} - you are signed in as a staff member of {employer}. Once that employment ends, you "
                "can enquire again.")
    return None


async def enquiry_block(db: AsyncSession, user: User | None) -> str | None:
    """``seeker_block`` on its own: no statement for guests, members, owners and super-admins; one for staff."""
    employer = employer_column(user)
    return seeker_block(user, await db.scalar(select(employer)) if employer is not None else None)


@dataclass(slots=True)
class JobSearch:
    """The /jobs query: text / role / type filters plus - only when the visitor's location is known - the near-me
    options: view ("near": within ``km`` only, nearest first, remote jobs in their own block | "all": every job,
    newest first), radius ``km`` (a ``job_radii`` chip; default and maximum ``max_km``) and ``remote``."""

    q: str = ""
    role: str = ""
    etype: str = ""
    max_km: float = 30.0
    located: bool = False
    view: str = "near"
    km: float = 30.0
    remote: bool = True

    @classmethod
    def from_request(cls, request: Request, loc: VisitorLocation) -> JobSearch:
        qp = request.query_params
        role, etype = qp.get("role", ""), qp.get("type", "")
        max_km = jobs_max_km()
        s = cls(
            q=clean_text(qp.get("q"), Q_MAX),
            role=role if role in {r.value for r in EmployeeRole} else "",
            etype=etype if etype in {t.value for t in EmploymentType} else "",
            max_km=max_km,
            km=max_km,
            located=loc.has_coords,
        )
        if s.located:
            s.view = "all" if qp.get("view") == "all" else "near"
            s.km = next((r for r in job_radii(max_km) if km_param(r) == qp.get("km")), max_km)  # junk -> the limit
            # "Include remote jobs" posts a hidden remote=0 plus remote=1 while ticked; links carry remote=0 alone
            flags = qp.getlist("remote")
            s.remote = "1" in flags or "0" not in flags
        return s

    @property
    def near(self) -> bool:
        return self.located and self.view == "near"

    @property
    def filtered(self) -> bool:
        return bool(self.q or self.role or self.etype)

    def text_clauses(self) -> list[Any]:
        clauses: list[Any] = []
        if self.q:
            pat = like_pattern(self.q)
            clauses.append(or_(JobPosting.title.ilike(pat, escape="\\"), JobPosting.location.ilike(pat, escape="\\"),
                               Gym.name.ilike(pat, escape="\\")))
        if self.role:
            clauses.append(JobPosting.role == EmployeeRole(self.role))
        if self.etype:
            clauses.append(JobPosting.employment_type == EmploymentType(self.etype))
        return clauses

    def params(self, loc: VisitorLocation) -> dict[str, Any]:
        """Query params reproducing this search for links (defaults left out - ``url_with`` drops empty values)."""
        located = self.located
        return {
            "q": self.q,
            "role": self.role,
            "type": self.etype,
            **loc.query,
            "view": "all" if located and self.view == "all" else None,
            "km": km_param(self.km) if located and self.km != self.max_km else None,
            "remote": 0 if located and not self.remote else None,
        }

    def location_line(self, loc: VisitorLocation) -> str:
        where = "your location" if loc.source == "query" else "your saved location"
        if self.view == "all":
            return f"Showing all jobs · distances shown up to {km_param(self.max_km)} km from {where}"
        return f"Showing jobs within {km_param(self.km)} km of {where}"


@dataclass(slots=True)
class JobsPage:
    jobs: list[JobView]
    total: int  # postings listed over all pages (near-me view: the nearby ones + the remote block)
    nearby: int  # near-me view: postings within the radius (the rest of ``total`` is the remote block)
    page: int
    pages: int

    @property
    def remote_count(self) -> int:
        return self.total - self.nearby

    @property
    def onsite_jobs(self) -> list[JobView]:
        return [jv for jv in self.jobs if not jv.remote]

    @property
    def remote_jobs(self) -> list[JobView]:
        return [jv for jv in self.jobs if jv.remote]


async def query_jobs(db: AsyncSession, s: JobSearch, loc: VisitorLocation, page: int) -> JobsPage:
    """One page of the open postings matching ``s`` in ONE SQL statement, whatever the view or the number of postings:

    * near-me view only - CTE ``near_branches`` (MATERIALIZED, ``join_job_place``): the published branches inside the
      radius' bounding box with their haversine distance, computed once per request; CTE ``placed_jobs``
      (MATERIALIZED): the matching postings that can be listed - remote ones (unless hidden) and those of a gym with a
      published branch in the box (a posting of any other gym has no distance within the radius: pre-filtered with
      ``gym_id IN near_branches``, so the far postings of the platform are never placed or sorted) - with their remote
      flag and place (distance, branch, own), computed once per posting and reused by the radius filter, the order,
      the counts and the result;
    * CTE ``ranked_jobs``: the listed postings (near-me: within the radius + the remote ones unless hidden; nearby
      first, nearest then newest; the remote block newest first) with the total (``count(*) OVER ()`` - no COUNT
      query), the nearby count (a FILTERed window) and their position (``row_number() OVER (...)``);
    * CTE ``jobs_page``: the requested page, clamped to the last page in SQL (an out-of-range ?page= costs no
      extra round trip);
    * outer SELECT: those <= JOBS_PAGE_SIZE postings with their gym and published own branch (no lazy loads) and, in
      the "all jobs" view, their distance - only these rows are measured (``near_branches`` of their gyms only),
      capped at the limit."""
    owner = aliased(User)
    base = (
        select()
        .select_from(JobPosting)
        .join(Gym, Gym.id == JobPosting.gym_id)
        .join(owner, owner.id == Gym.owner_id)
        .where(*open_job_clauses(owner), *s.text_clauses())
    )
    if s.near:
        near = near_branches_cte(loc, s.km)
        local = JobPosting.gym_id.in_(select(near.c.gym_id))  # necessary for a distance within the radius
        base = base.where(or_(remote_job_clause(), local) if s.remote else local)
        base, place = join_job_place(base, loc, s.km, near=near)
        placed = (
            base.add_columns(
                JobPosting.id.label("job_id"),
                JobPosting.gym_id.label("gym_id"),
                JobPosting.created_at.label("created_at"),
                remote_job_clause().label("remote"),
                place.km.label("km"),
                place.branch_name.label("branch_name"),
                place.own.label("own"),
            )
            .cte("placed_jobs")
            .prefix_with("MATERIALIZED")
        )
        p = placed.c
        km = case((p.remote, null()), else_=p.km)  # remote postings never get a distance
        nearby = and_(~p.remote, p.km <= s.km)
        ranked_q = select(
            p.job_id,
            p.gym_id,
            km.label("km"),
            p.branch_name,
            p.own,
            func.count().filter(~p.remote).over().label("nearby"),
            func.count().over().label("total"),
            func.row_number().over(order_by=(p.remote, km, p.created_at.desc(), p.job_id.desc())).label("rn"),
        ).where(or_(nearby, p.remote) if s.remote else nearby)
    else:
        ranked_q = base.add_columns(
            JobPosting.id.label("job_id"),
            JobPosting.gym_id.label("gym_id"),
            func.count().over().label("total"),
            func.row_number().over(order_by=(JobPosting.created_at.desc(), JobPosting.id.desc())).label("rn"),
        )
    ranked = ranked_q.cte("ranked_jobs")
    last_page_start = (ranked.c.total - 1) // JOBS_PAGE_SIZE * JOBS_PAGE_SIZE
    paged = (
        select(ranked)
        .where(ranked.c.rn > func.least((page - 1) * JOBS_PAGE_SIZE, last_page_start))
        .order_by(ranked.c.rn)
        .limit(JOBS_PAGE_SIZE)
        .cte("jobs_page")
    )
    stmt = (
        select(JobPosting, paged.c.total, paged.c.rn)
        .join(paged, paged.c.job_id == JobPosting.id)
        .join(Gym, Gym.id == JobPosting.gym_id)
        .outerjoin(Branch, and_(*own_branch_clauses()))
        .options(contains_eager(JobPosting.gym), contains_eager(JobPosting.branch))
        .order_by(paged.c.rn)
    )
    if s.near:
        stmt = stmt.add_columns(paged.c.km, paged.c.branch_name, paged.c.own, paged.c.nearby)
    elif s.located:
        stmt = with_job_distance(stmt, loc, s.max_km, select(paged.c.gym_id))
    rows = (await db.execute(stmt)).all()
    if not rows:
        return JobsPage(jobs=[], total=0, nearby=0, page=1, pages=1)
    first = rows[0]._mapping
    total = int(first["total"])
    return JobsPage(
        jobs=[job_view(r, s.max_km) for r in rows],
        total=total,
        nearby=int(first["nearby"]) if s.near else total,
        page=(int(first["rn"]) - 1) // JOBS_PAGE_SIZE + 1,
        pages=max(1, math.ceil(total / JOBS_PAGE_SIZE)),
    )


# ------------------------------------------------------------------ routes: home
@router.get("/")
async def home(request: Request, db: DB, user: CurrentUser):
    loc = visitor_location(request, user)
    featured: list[GymCard] = []
    trainers: list[TeamMember] = []
    stats = {"branches": 0, "online": 0, "trainers": 0}
    try:
        featured = await home_featured(db, loc)
        trainers = await featured_trainers(db)
        stats = await site_stats(db)
    except Exception as exc:  # the landing page must always render
        from app.core.overload import warn_db_unavailable  # noqa: PLC0415
        from app.db import db_unavailable  # noqa: PLC0415

        kind = db_unavailable(exc)
        if kind:  # an overloaded / unreachable database: one throttled warning line, never a traceback
            warn_db_unavailable(log, "GET / (home page data)", kind, exc, "rendered without featured gyms and figures")
        else:  # a real bug still gets its traceback
            log.exception("home page data failed to load")
        await db.rollback()
    return render(
        request,
        "public/home.html",
        {"featured": featured, "trainers": trainers, "stats": stats, "modes": MODE_INFO, "loc": loc},
    )


# ------------------------------------------------------------------ routes: gym directory
@router.get("/gyms")
async def gyms_directory(request: Request, db: DB, user: CurrentUser):
    qp = request.query_params
    q = clean_text(qp.get("q"), Q_MAX)
    city = clean_text(qp.get("city"), 120)
    mode = qp.get("mode", "")
    if mode not in ("offline", "online"):
        mode = ""
    loc = visitor_location(request, user)
    result = await query_directory(db, loc=loc, q=q, city=city, mode=mode, page=page_param(request))
    cities = await directory_cities(db)
    params: dict[str, Any] = {"q": q, "city": city, "mode": mode, **loc.query}
    pager = {
        "page": result.page,
        "pages": result.pages,
        "total": result.total,
        "prev": url_with("/gyms", params, page=result.page - 1) if result.page > 1 else None,
        "next": url_with("/gyms", params, page=result.page + 1) if result.page < result.pages else None,
    }
    mode_links = {m: url_with("/gyms", params, mode=m, page=1) for m in ("", "offline", "online")}
    clear_location = url_with("/gyms", {"q": q, "city": city, "mode": mode})
    return render(
        request,
        "public/gyms.html",
        {
            "result": result,
            "cards": result.cards,
            "cities": cities,
            "q": q,
            "city": city,
            "mode": mode,
            "loc": loc,
            "pager": pager,
            "mode_links": mode_links,
            "clear_location": clear_location,
            "filtered": bool(q or city or mode),
            "near_km": km_param(NEAR_SORT_KM),
        },
    )


# ------------------------------------------------------------------ routes: gym brand page
@router.get("/gyms/{slug}")
async def gym_page(request: Request, slug: str, db: DB, user: CurrentUser):
    """The BRANCH_PAGE_SIZE nearest (or first) branch cards - never the whole chain - plus "View all N locations"."""
    gym = await load_public_gym(db, slug)
    summary = await branch_summary(db, gym.id)
    if not summary.total and not gym.online_enabled:
        raise NotFound("Gym not found.")
    loc = visitor_location(request, user)
    first: Sequence[Branch] = []
    if summary.total:
        first = (await db.execute(published_branches_stmt(gym.id).order_by(*branch_order(loc)).limit(BRANCH_PAGE_SIZE))).scalars().all()
    cards = await load_branch_cards(db, first, loc, photos=GALLERY_PHOTOS)
    plans = await enabled_plans(db, gym.id)
    team = await public_staff(db, gym.id)
    elig = None
    if summary.total:
        # the 30 km rule: the first card is the nearest branch (nearest-first order) and one branch per city covers the
        # city fallback - the whole chain is never loaded
        elig = offline_eligibility([c.branch for c in cards[:1]] + summary.city_points, lat=loc.lat, lon=loc.lon, city=loc.city)
    groups = plan_groups(gym, plans, bool(summary.total), elig, None)
    await apply_gym_limits(db, user, gym, groups)
    gallery = [p for c in cards for p in c.photos][:GALLERY_PHOTOS]
    return render(
        request,
        "public/gym.html",
        {
            "gym": gym,
            "owner": gym.owner,
            "branch_cards": cards,
            "branch_total": summary.total,
            "all_branches_href": f"/gyms/{gym.slug}/branches{loc.qs}" if summary.total > len(cards) else None,
            "groups": groups,
            "elig": elig,
            "loc": loc,
            "team": team,
            "trainers": [m for m in team if m.employee.is_trainer],
            "staff": [m for m in team if not m.employee.is_trainer],
            "hero_photos": gallery,
            "cities": summary.cities[:HERO_CITIES],
            "more_cities": max(0, summary.city_count - HERO_CITIES),
            "online_open": bool(gym.online_enabled and gym.online_open),
            "has_offline_groups": any(g.mode != PlanMode.ONLINE.value for g in groups),
        },
    )


# ------------------------------------------------------------------ routes: all locations of a gym (paginated)
@router.get("/gyms/{slug}/branches")
async def gym_branches(request: Request, slug: str, db: DB, user: CurrentUser):
    """Every published branch of a gym, BRANCH_PAGE_SIZE per page, nearest first when the location is known (the
    pager keeps ``?lat=&lon=``). Constant statements: gym + page (window count) + ``load_branch_cards`` (3)."""
    gym = await load_public_gym(db, slug)
    loc = visitor_location(request, user)
    wanted = parse_page(request, default_per_page=BRANCH_PAGE_SIZE, max_per_page=BRANCH_PAGE_SIZE)
    page = await paginate(db, published_branches_stmt(gym.id).order_by(*branch_order(loc)), page=wanted.page,
                          per_page=BRANCH_PAGE_SIZE, max_per_page=BRANCH_PAGE_SIZE, window_count=True)
    if not page.total:
        raise NotFound("This gym has no public locations yet.")
    cards = await load_branch_cards(db, page.items, loc)
    return render(request, "public/branches.html", {"gym": gym, "owner": gym.owner, "cards": cards, "page": page,
                                                    "loc": loc})


# ------------------------------------------------------------------ routes: branch page
@router.get("/gyms/{slug}/branches/{branch_id}")
async def branch_page(request: Request, slug: str, branch_id: str, db: DB, user: CurrentUser):
    """One published branch (loaded by id with its photos, features and hours) and its OTHER_BRANCHES nearest sister
    branches (to the visitor, else to this branch) with "View all N locations"."""
    bid = int_id(branch_id)
    gym = await load_public_gym(db, slug)
    branch = (
        await db.execute(
            published_branches_stmt(gym.id)
            .options(selectinload(Branch.photos), selectinload(Branch.features), selectinload(Branch.hours))
            .where(Branch.id == bid)
        )
    ).scalar_one_or_none()
    if branch is None:
        raise NotFound("Branch not found.")
    plans = await enabled_plans(db, gym.id)
    team = await branch_team(db, branch)
    team_ids = {m.employee.id for m in team}
    others = [m for m in await public_staff(db, gym.id) if m.employee.id not in team_ids]
    loc = visitor_location(request, user)
    elig = offline_eligibility([branch], lat=loc.lat, lon=loc.lon, city=loc.city)
    rows, state = hours_view(branch)
    photos = [
        {"src": storage.media_url(p.image_key), "caption": p.caption or f"{branch.name} - photo {i + 1}", "kind": p.kind.value}
        for i, p in enumerate(branch.photos)
        if storage.media_url(p.image_key)
    ]
    has_coords = valid_coords(branch.latitude, branch.longitude)
    sisters = await paginate(db, published_branches_stmt(gym.id).where(Branch.id != branch.id).order_by(*branch_order(loc, branch)),
                             per_page=OTHER_BRANCHES, max_per_page=OTHER_BRANCHES, window_count=True)
    groups = plan_groups(gym, plans, True, elig, branch.id)
    await apply_gym_limits(db, user, gym, groups)
    return render(
        request,
        "public/branch.html",
        {
            "gym": gym,
            "owner": gym.owner,
            "branch": branch,
            "photos": photos,
            "hours": rows,
            "open_state": state,
            "team": team,
            "others": others,
            "groups": groups,
            "elig": elig,
            "loc": loc,
            "distance": km_label(loc.distance_to(branch)),
            "has_coords": has_coords,
            "other_branches": await load_branch_cards(db, sisters.items, loc),
            "other_total": sisters.total,
            "all_branches_href": f"/gyms/{gym.slug}/branches{loc.qs}" if sisters.total > len(sisters.items) else None,
            "online_open": bool(gym.online_enabled and gym.online_open),
            "has_offline_groups": True,
        },
    )


# ------------------------------------------------------------------ routes: plans explainer
@router.get("/plans")
async def plans_page(request: Request):
    return render(request, "public/plans.html", {"plan_types": PLAN_TYPES, "modes": MODE_INFO})


# ------------------------------------------------------------------ routes: jobs board
@router.get("/jobs")
async def jobs_page(request: Request, db: DB, user: CurrentUser):
    loc = visitor_location(request, user)
    search = JobSearch.from_request(request, loc)
    result = await query_jobs(db, search, loc, page_param(request))
    params = search.params(loc)
    pager = {
        "page": result.page,
        "pages": result.pages,
        "total": result.total,
        "prev": url_with("/jobs", params, page=result.page - 1) if result.page > 1 else None,
        "next": url_with("/jobs", params, page=result.page + 1) if result.page < result.pages else None,
    }

    def link(**changes: Any) -> str:  # this search with some params changed, back on page 1
        return url_with("/jobs", params, page=1, **changes)

    def keep(*names: str) -> dict[str, Any]:  # hidden inputs that carry the rest of the search through a form
        return {k: params[k] for k in names if params.get(k) is not None and params[k] != ""}

    return render(
        request,
        "public/jobs.html",
        {
            "jobs": result.jobs,
            "result": result,
            "search": search,
            "loc": loc,
            # owners, super-admins and employed staff browse, but can't enquire: say so up front (no SQL but for staff)
            "enquiry_block": await enquiry_block(db, user),
            "q": search.q,
            "role": search.role,
            "etype": search.etype,
            "pager": pager,
            "roles": [(r.value, r.value.replace("_", " ").capitalize()) for r in EmployeeRole],
            "types": [(t.value, t.value.replace("_", " ").capitalize()) for t in EmploymentType],
            "filtered": search.filtered,
            "max_km": km_param(search.max_km),
            "radius": km_param(search.km),
            "location_line": search.location_line(loc) if search.located else "",
            "view_links": [("near", "Near me", link(view=None)), ("all", "All jobs", link(view="all"))],
            "radius_links": [
                {"label": km_param(r), "href": link(km=None if r == search.max_km else km_param(r)), "active": r == search.km}
                for r in job_radii(search.max_km)
            ],
            "widen_link": link(km=None) if search.km < search.max_km else None,
            "all_link": link(view="all"),
            "clear_filters": url_with("/jobs", {**params, "q": None, "role": None, "type": None}),
            "clear_location": url_with("/jobs", {"q": search.q, "role": search.role, "type": search.etype}),
            "search_keep": keep("view", "km", "remote"),
            "remote_keep": keep("q", "role", "type", "lat", "lon", "view", "km"),
            # a card opens its job with the whole search + page, so the job page links back to exactly this list
            "card_qs": url_with("", params, page=result.page),
        },
    )


def job_board_params(request: Request, loc: VisitorLocation) -> dict[str, Any]:
    """The /jobs list a job page was opened from - filters, shared location, near-me view / radius / remote and page,
    re-validated from the job page's own query string - so its links lead back to exactly that list."""
    return {**JobSearch.from_request(request, loc).params(loc), "page": page_param(request)}


# ------------------------------------------------------------------ job enquiries: deterministic spam protection (no AI)
ENQUIRY_MIN_FILL_SECONDS = 3  # a form sent sooner after its page was served is refused ("That was quick!" - send again)
ENQUIRY_FORM_MAX_AGE = 2 * 86400  # a form older than this (seconds) is stale: shown again with a fresh token
ENQUIRY_COOLDOWN_SECONDS = 300  # one enquiry per network address and posting in this many seconds
ENQUIRY_DUPLICATE_HOURS = 24  # the same message to the same posting is refused for this long
ENQUIRY_RECENT_SCAN = 1000  # a posting's newest enquiries (of the last 24 h) the two checks above look at
ENQUIRY_MAX_LINKS = 2  # links (http://, www., domain/path) in one message; none in the name
ENQUIRY_MIN_CHARS, ENQUIRY_MAX_CHARS = 20, 4000  # message length
ENQUIRY_TOKEN_FIELD = "form_token"
_ENQUIRY_TOKEN_PURPOSE = "job-enquiry-form"
# Linear time on any input (it runs on anonymous 4,000-character messages, on the event loop): a bare domain + path only
# starts where a word can start (never inside a run of dots / hyphens) and its labels are matched possessively - an
# unanchored "(label.)+tld/" would rescan the rest of a run like "a.a.a.a..." from every letter: quadratic, ~0.15 s.
_LINK_RE = re.compile(r"\b(?:https?://|ftp://|www\.)\S+|(?<![\w.-])(?:[a-z0-9-]++\.)++[a-z]{2,24}/\S*", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class EnquiryRefusal:
    """Why an enquiry was not sent - shown above the re-displayed form, the visitor's details kept."""

    title: str
    message: str
    status: int = 400
    retry_after: int | None = None


def _enquiry_signature(job_id: int, ts: int) -> str:
    return hmac_hex(f"{int(job_id)}.{int(ts)}", _ENQUIRY_TOKEN_PURPOSE)[:32]


def enquiry_form_token(job_id: int, issued: float | None = None) -> str:
    """The enquiry form's signed timestamp, issued when the job page is rendered: ``<unix seconds>.<HMAC-SHA256 of the
    posting id and that time>`` (keyed by SECRET_KEY) - it can't be forged, moved to another posting or backdated."""
    ts = int(time.time() if issued is None else issued)
    return f"{ts}.{_enquiry_signature(job_id, ts)}"


def enquiry_form_age(raw: Any, job_id: int, now: float | None = None) -> float | None:
    """Seconds since this posting's form was served, or None: no token, a forged one, another posting's, one from the
    future (beyond a few seconds of clock skew between workers)."""
    if not isinstance(raw, str) or len(raw) > 64:
        return None
    ts_text, _, signature = raw.partition(".")
    if not (ts_text.isascii() and ts_text.isdigit()) or len(ts_text) > 12:
        return None
    if not equals(signature, _enquiry_signature(job_id, int(ts_text))):
        return None
    age = (time.time() if now is None else now) - int(ts_text)
    return age if age >= -5 else None


def enquiry_token_refusal(age: float | None) -> EnquiryRefusal | None:
    if age is None:
        return EnquiryRefusal("Please send your enquiry again",
                              "We couldn't confirm this form came from this page (it may be from an older version of "
                              "it). Your details are kept below - just check them and press Send enquiry.")
    if age > ENQUIRY_FORM_MAX_AGE:
        return EnquiryRefusal("This form has expired",
                              "It was open for a long time. Your details are kept below - check them and press Send "
                              "enquiry again.")
    if age < ENQUIRY_MIN_FILL_SECONDS:
        return EnquiryRefusal("That was quick!", "Please check your details below and press Send enquiry again.")
    return None


def count_links(text: str) -> int:
    return len(_LINK_RE.findall(text or ""))


def _wait_text(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    minutes = math.ceil(seconds / 60)
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def _enquiry_lock_key(job_id: int) -> int:
    return int.from_bytes(hashlib.blake2b(f"job_enquiry:{int(job_id)}".encode(), digest_size=8).digest(), "big",
                          signed=True)


# Characters str.isprintable() accepts that still render as nothing or as a blank: the combining grapheme joiner, Hangul
# fillers (U+3164 / U+FFA0 become U+1160 under NFKC), Khmer inherent vowels, Mongolian variation selectors, variation
# selectors, the Braille blank - and every Unicode whitespace character (Python's \s: no-break, em, ideographic ...).
_ENQUIRY_INVISIBLE_RE = re.compile(
    "[\\s\u034f\u115f\u1160\u17b4\u17b5\u180b-\u180f\u2800\u3164\ufe00-\ufe0f\uffa0\U000e0100-\U000e01ef]+")


def _visible_text(text: str) -> str:
    """``text`` without whitespace and without characters that render as nothing: ``_ENQUIRY_INVISIBLE_RE`` and all
    that str.isprintable() refuses (category Cf - zero-width spaces and joiners, soft hyphens, BOMs, bidi and tag
    characters - and control, surrogate, private-use and unassigned code points)."""
    return "".join(filter(str.isprintable, _ENQUIRY_INVISIBLE_RE.sub("", text)))


def enquiry_message_key(message: str) -> str:
    """What the duplicate check compares, stored with each enquiry (``JobEnquiry.message_key``): the SHA-256 (hex) of
    the message as the gym owner reads it - compatibility forms folded (NFKC: full-width letters, ligatures, composed or
    decomposed accents), any case (casefold - every script, not only ASCII), spacing and invisible characters left out
    (``_visible_text``, before and after folding). Done in Python because PostgreSQL can't here: the cluster's C locale
    makes its ``\\s``, btrim() and lower() ASCII-only, so a no-break space or a zero-width character slipped through.
    Linear in the message (at most ENQUIRY_MAX_CHARS characters)."""
    folded = unicodedata.normalize("NFKC", _visible_text(message)).casefold()
    return hashlib.sha256(unicodedata.normalize("NFKC", _visible_text(folded)).encode()).hexdigest()


def _comparable(text: Any) -> Any:
    """SQL form an enquiry stored without a message_key (before it existed) is compared in, both sides alike: trimmed,
    whitespace runs collapsed, lower case - ASCII only (the C locale)."""
    return func.lower(func.regexp_replace(func.btrim(text), r"\s+", " ", "g"))


async def enquiry_rate_refusal(db: AsyncSession, job_id: int, ip: str | None, message: str,
                               message_key: str) -> EnquiryRefusal | None:
    """The cooldown (one enquiry per address and posting per ENQUIRY_COOLDOWN_SECONDS) and the duplicate check (the
    same message to this posting within ENQUIRY_DUPLICATE_HOURS: the same ``enquiry_message_key``, or for an enquiry
    stored before keys existed the same ``_comparable`` text), race-free across workers: the posting's enquiries
    are serialised by a transaction-scoped advisory lock (released by the COMMIT / ROLLBACK that follows), taken in its
    own statement BEFORE the check, whose snapshot therefore sees every enquiry committed meanwhile. The check reads the
    posting's newest ENQUIRY_RECENT_SCAN enquiries of the last 24 h, in index order (ix_job_enquiries_job_recent: never
    the posting's older history). 2 statements."""
    await db.execute(select(func.pg_advisory_xact_lock(_enquiry_lock_key(job_id))))
    same = func.coalesce(JobEnquiry.message_key == message_key,  # NULL only on an enquiry stored before keys existed
                         _comparable(JobEnquiry.message) == _comparable(message))
    recent = (
        select(JobEnquiry.ip.label("ip"), JobEnquiry.created_at.label("created_at"), same.label("same"))
        .where(JobEnquiry.job_id == job_id,
               JobEnquiry.created_at >= func.now() - timedelta(hours=ENQUIRY_DUPLICATE_HOURS))
        .order_by(JobEnquiry.created_at.desc())
        .limit(ENQUIRY_RECENT_SCAN)
        .subquery("recent_enquiries")
    )
    since_last, duplicate = (await db.execute(select(
        func.date_part("epoch", func.now() - func.max(recent.c.created_at).filter(recent.c.ip == ip)),
        func.coalesce(func.bool_or(recent.c.same), False),
    ))).one()
    if since_last is not None and float(since_last) < ENQUIRY_COOLDOWN_SECONDS:
        wait = max(1, math.ceil(ENQUIRY_COOLDOWN_SECONDS - float(since_last)))
        insights.count("enquiry.cooldown", who=f"ip:{ip}")
        return EnquiryRefusal("You've just sent an enquiry for this job",
                              f"To keep the gym's inbox free of spam, one enquiry per job every few minutes is enough - "
                              f"you can send another in {_wait_text(wait)}.", 429, wait)
    if duplicate:
        insights.count("enquiry.duplicate", who=f"ip:{ip}")
        return EnquiryRefusal("This message was already sent",
                              f"The gym received this exact message for this job in the last {ENQUIRY_DUPLICATE_HOURS} "
                              "hours. To add something, please write it in your own words.", 409)
    return None


async def render_job(
    request: Request,
    db: AsyncSession,
    jv: JobView,
    loc: VisitorLocation,
    *,
    values: dict[str, str] | None = None,
    errors: dict[str, str] | None = None,
    status_code: int = 200,
    form_token: str | None = None,
    refusal: EnquiryRefusal | None = None,
):
    """The job page; after a refused enquiry, the form again with the visitor's details and the reason. ``form_token``
    = the enquiry form's still-valid token (so the time already spent filling it in counts), else a fresh one."""
    job = jv.job
    owner = aliased(User)
    more = (
        await db.execute(  # "more roles at this gym": no distances needed
            open_job_stmt(owner).where(JobPosting.gym_id == job.gym_id, JobPosting.id != job.id)
            .order_by(JobPosting.created_at.desc(), JobPosting.id.desc()).limit(4)
        )
    ).scalars().all()
    board = job_board_params(request, loc)
    return render(
        request,
        "public/job.html",
        {
            "jv": jv,
            "job": job,
            "gym": job.gym,
            "loc": loc,
            "jobs_href": url_with("/jobs", board),  # back to the list this job was opened from
            "board_qs": url_with("", board),  # carried by the job links and the enquiry form
            "more": [JobView(j) for j in more],
            "values": values or {},
            "errors": errors or {},
            "refusal": refusal,
            "token_field": ENQUIRY_TOKEN_FIELD,
            "form_token": form_token or enquiry_form_token(job.id),
            "message_chars": (ENQUIRY_MIN_CHARS, ENQUIRY_MAX_CHARS),
            "max_links": ENQUIRY_MAX_LINKS,
            "sent": request.query_params.get("sent") == "1" and not errors and refusal is None,
            "max_upload_mb": get_settings().max_upload_mb,
            "dial_code": DIAL_CODES.get((job.gym.country_code or "").upper()),
        },
        status_code=status_code,
    )


@router.get("/jobs/{job_id}")
async def job_page(request: Request, job_id: str, db: DB, user: CurrentUser):
    loc = visitor_location(request, user)
    jv = await load_open_job(db, int_id(job_id), loc, user)
    return await render_job(request, db, jv, loc)


@router.post("/jobs/{job_id}/enquire")
async def job_enquire(request: Request, job_id: str, db: DB, user: CurrentUser):
    """An anonymous enquiry about an open posting, with deterministic spam protection (no AI). Every refusal shows the
    form again with the visitor's details and a friendly reason:

    * honeypot ``website`` filled -> a pretend success: nothing stored, nobody notified;
    * the signed form token (``enquiry_form_token``, issued with the page): missing, forged, another posting's or older
      than ENQUIRY_FORM_MAX_AGE, or sent within ENQUIRY_MIN_FILL_SECONDS of the page -> "send again" (400);
    * the fields: message ENQUIRY_MIN_CHARS-ENQUIRY_MAX_CHARS characters with at most ENQUIRY_MAX_LINKS links, a name
      without links (400);
    * one enquiry per network address and posting per ENQUIRY_COOLDOWN_SECONDS (429 + Retry-After), and the same message
      to the same posting within ENQUIRY_DUPLICATE_HOURS (409; ``enquiry_message_key``: case, spacing, invisible
      characters and compatibility forms aside) - checked under a per-posting advisory lock, so concurrent submissions
      (a double click, a burst spread over several workers) can't both pass.
    These limits only look at earlier enquiries: a first enquiry from a form that was filled in always goes through.
    Nothing is stored (not even the résumé) before every check has passed.

    Enquiries are for job seekers (``seeker_block``): a signed-in gym owner, super-admin or currently employed staff
    member is refused FIRST - before the form is read and before any spam / cooldown / cap accounting - with the
    page's explanation (403), or ``{"error", "detail", "code": "job_seekers_only"}`` for a JSON caller."""
    # the list's search (shared ?lat=&lon=, near-me options, filters, page) rides along on the form action: the
    # re-rendered form and the "sent" page keep it, so "Browse more jobs" returns to that list
    loc = visitor_location(request, user)
    jv = await load_open_job(db, int_id(job_id), loc, user)
    if jv.enquiry_block:
        log.info("job enquiry refused: not a job seeker (job %s, user %s)", jv.job.id, user.id if user else None)
        if wants_json(request):
            return JSONResponse({"error": "Not available", "detail": jv.enquiry_block, "code": "job_seekers_only"},
                                status_code=403)
        return await render_job(request, db, jv, loc, status_code=403)
    job = jv.job
    sent_url = url_with(f"/jobs/{job.id}", {"sent": 1, **job_board_params(request, loc)}) + "#enquire"
    form = await request.form()
    values = {
        "name": clean_text(form.get("name"), 400),
        "email": clean_text(form.get("email"), 400),
        "phone": clean_text(form.get("phone"), 64),
        "message": clean_text(form.get("message"), 8000),
    }
    honeypot = form.get("website")
    if isinstance(honeypot, str) and honeypot.strip():
        # Bots fill every field: pretend success, store nothing, tell nobody.
        log.info("job enquiry honeypot triggered (job %s, ip %s)", job.id, client_ip(request))
        insights.count("enquiry.honeypot", who=f"ip:{client_ip(request)}")
        return redirect(sent_url, "Thanks! Your enquiry has been sent.", "success")
    raw_token = form.get(ENQUIRY_TOKEN_FIELD)
    age = enquiry_form_age(raw_token, job.id)
    refusal = enquiry_token_refusal(age)
    insights.count("enquiry.form", int(refusal is not None), who=f"ip:{client_ip(request)}")
    # a token that is still valid goes back into the re-shown form: the time already spent filling it in counts
    keep_token = raw_token if isinstance(raw_token, str) and age is not None and age <= ENQUIRY_FORM_MAX_AGE else None

    errors: dict[str, str] = {}
    name = values["name"]
    if len(name) < 2:
        errors["name"] = "Please enter your full name."
    elif len(name) > 120:
        errors["name"] = "Your name is too long (120 characters max)."
    elif count_links(name):
        errors["name"] = "Please enter just your name (no links)."
        insights.count("enquiry.links", who=f"ip:{client_ip(request)}")
    email = ""
    if not values["email"]:
        errors["email"] = "Please enter your e-mail address."
    elif len(values["email"]) > 254:
        errors["email"] = "That e-mail address is too long."
    else:
        try:
            email = validate_email(values["email"], check_deliverability=False).normalized
        except EmailNotValidError:
            errors["email"] = "Please enter a valid e-mail address (like name@example.com)."
    phone = values["phone"]
    if phone:
        if len(phone) > 32:
            errors["phone"] = "That phone number is too long."
        else:
            try:
                normalize_phone(phone, job.gym.country_code)
            except ValueError as exc:
                errors["phone"] = str(exc)
    message = values["message"]
    if len(message) < ENQUIRY_MIN_CHARS:
        errors["message"] = f"Tell the gym a little about yourself (at least {ENQUIRY_MIN_CHARS} characters)."
    elif len(message) > ENQUIRY_MAX_CHARS:
        errors["message"] = f"Please keep your message under {ENQUIRY_MAX_CHARS:,} characters (it has {len(message):,})."
    elif count_links(message) > ENQUIRY_MAX_LINKS:
        errors["message"] = (f"Please include at most {ENQUIRY_MAX_LINKS} links (it has {count_links(message)}) - "
                             "you can attach your résumé as a PDF instead.")
        insights.count("enquiry.links", who=f"ip:{client_ip(request)}")

    resume = form.get("resume")
    resume_bytes = b""
    resume_name = None
    if isinstance(resume, StarletteUploadFile) and resume.filename:
        resume_name = clean_text(resume.filename, 200) or "resume.pdf"
        try:
            resume_bytes = await storage.read_upload(resume, required=False)  # type: ignore[arg-type]
        except storage.UploadError as exc:
            errors["resume"] = str(exc)
        else:
            if resume_bytes and storage.sniff_kind(resume_bytes) != "pdf":
                errors["resume"] = "Please upload your résumé as a PDF file."
    if errors or refusal is not None:
        return await render_job(request, db, jv, loc, values=values, errors=errors, status_code=400,
                                form_token=keep_token, refusal=refusal)
    message_key = enquiry_message_key(message)
    refusal = await enquiry_rate_refusal(db, job.id, client_ip(request), message, message_key)
    if refusal is None:  # enquiries per posting per day (approved cap): bounds a gym's inbox and résumé storage
        try:
            await caps.enforce(db, "enquiries_per_job_day", job.id, select(JobEnquiry.id).where(
                JobEnquiry.job_id == job.id, JobEnquiry.created_at >= caps.since()))
        except caps.CapReached as exc:
            refusal = EnquiryRefusal("This job's inbox is full for today", str(exc), exc.status_code, 3600)
    if refusal is not None:
        log.info("job enquiry refused (job %s, ip %s): %s", job.id, client_ip(request), refusal.title)
        response = await render_job(request, db, jv, loc, values=values, status_code=refusal.status,
                                    form_token=keep_token, refusal=refusal)
        if refusal.retry_after:
            response.headers["Retry-After"] = str(refusal.retry_after)
        return response

    resume_key = None
    if resume_bytes:
        try:
            stored, _plain = await storage.save_private_file(resume_bytes, kind="resumes", filename=resume_name)
            resume_key = stored.key
        except storage.UploadError as exc:
            return await render_job(request, db, jv, loc, values=values, errors={"resume": str(exc)}, status_code=400,
                                    form_token=keep_token)

    enquiry = JobEnquiry(job_id=job.id, name=name, email=email, phone=phone or None, message=message,
                         message_key=message_key, resume_key=resume_key, ip=client_ip(request))
    db.add(enquiry)
    await db.flush()
    gym = job.gym
    await notify(
        db,
        gym.owner,
        f"New job enquiry: {job.title}",
        f"{name} ({email}) enquired about the “{job.title}” role at {gym.name}"
        + (" and attached a résumé." if resume_key else ".")
        + " Reply to them directly from your jobs dashboard.",
        category=NotificationCategory.STAFF,
        severity=Severity.INFO,
        link=f"/owner/jobs/{job.id}",
    )
    await email_address(
        db,
        email,
        f"We received your enquiry: {job.title}",
        f"Thanks for your interest in the “{job.title}” role at {gym.name}. Your enquiry has been passed to the gym's "
        "owner, who will contact you directly if your profile is a good fit. IronPulse only forwards enquiries - hiring "
        "decisions are made by the gym.",
        link=f"/jobs/{job.id}",
        recipient=name,
    )
    await db.commit()
    return redirect(sent_url, f"Thanks, {name.split()[0]}! Your enquiry was sent to {gym.name}.", "success")


# ------------------------------------------------------------------ routes: legal + about
def _legal_context() -> dict[str, Any]:
    s = get_settings()
    return {
        "updated": "10 November 2026",
        "retention_days": s.account_retention_days,
        "offline_km": int(s.offline_max_distance_km),
        "presence_km": s.presence_radius_km,
        "min_photos": s.min_branch_photos,
        "otp_minutes": s.otp_ttl_minutes,
        "contact": s.superadmin_email,
    }


@router.get("/terms")
async def terms_page(request: Request):
    return render(request, "public/terms.html", _legal_context())


@router.get("/privacy")
async def privacy_page(request: Request):
    return render(request, "public/privacy.html", _legal_context())


@router.get("/about")
async def about_page(request: Request, db: DB):
    try:
        stats = await site_stats(db)
    except Exception:
        log.exception("about page stats failed")
        await db.rollback()
        stats = {"branches": 0, "online": 0, "trainers": 0}
    return render(request, "public/about.html", {**_legal_context(), "stats": stats})
