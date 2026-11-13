"""Caps: the most of anything one person, gym, branch, product or posting may create (approved load-safety limits).

Every value lives in ``CAPS`` below - ONE table - so production can tune them without touching code:

* a cap with a ``setting`` (e.g. ``MAX_ONLINE_GYMS``) is read from that setting;
* every other cap can be overridden with the ``CAPS_OVERRIDES`` setting, e.g.
  ``CAPS_OVERRIDES=branches_per_gym=300,products_per_gym=5000`` (unknown names and bad values are ignored, logged once).

``enforce`` is the one race-safe check every create path uses::

    await caps.enforce(db, "branches_per_gym", gym.id, select(Branch.id).where(Branch.gym_id == gym.id))

It takes a transaction-scoped advisory lock on (cap name, scope id) - held until the caller's COMMIT / ROLLBACK, so the
caller must insert in the SAME transaction - and only then counts (in its own statement, whose READ COMMITTED snapshot
therefore sees every row a previous lock holder committed): N parallel requests can never pass the cap together. The
count reads at most ``limit + adding`` rows of the scope (never a whole table). A refusal is ``CapReached``: an
``HTTPException`` (409, or 429 for a per-day cap) whose ``detail`` is a friendly sentence, so a route that does not
catch it still answers with the friendly error page / ``{"error", "detail"}`` JSON of app/core/errors.py - never a 500.
Routes with a form catch it and show ``str(exc)`` next to the field; JSON routes return ``exc.payload()``.

Lock order: always take a narrower cap (a product's photos) before a broader one (the gym's storage), never the
reverse, so two caps can never deadlock.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import timedelta
from functools import lru_cache
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select

log = logging.getLogger("ironpulse.caps")

DAY = 24  # hours of a per-day window (rolling: the last 24 hours, the same for every time zone)
MB = 1024 * 1024


@dataclass(frozen=True, slots=True)
class Cap:
    name: str
    default: int
    noun: str  # what is counted, plural ("branches")
    per: str  # its scope in words ("per gym")
    message: str  # the friendly refusal; ``{limit}`` is replaced by the value (with thousands separators)
    window_hours: int | None = None  # None = what exists now; DAY = created in the last 24 hours
    setting: str | None = None  # a dedicated setting holds the value (else CAPS_OVERRIDES may)
    unit: int = 1  # MB for the storage quotas: the value is in MB, enforce() compares bytes

    @property
    def status(self) -> int:
        return 429 if self.window_hours else 409

    @property
    def limit(self) -> int:
        return limit(self.name)

    @property
    def phrase(self) -> str:
        """How the README Limits table states it, e.g. "200 branches per gym"."""
        return f"{self.limit:,} {self.noun} {self.per}"


_CAPS: tuple[Cap, ...] = (
    # ---------------------------------------------------------------- gym memberships (app/services/checkout.py)
    Cap("online_gyms", 3, "gyms with an online or combo membership", "per person",
        "You can be an online member of at most {limit} gyms at a time.", setting="max_online_gyms"),
    Cap("offline_gyms", 1, "gym with an in-person (gym or combo) membership", "per person",
        "You can be an in-person member of at most {limit} gym at a time.", setting="max_offline_gyms"),
    # ---------------------------------------------------------------- gyms and branches (app/routers/owner_gyms.py)
    Cap("gyms_per_owner", 20, "gyms", "per owner account",
        "You can run at most {limit} gyms from one account. Contact support if you need more."),
    Cap("branches_per_gym", 200, "branches (open or closed)", "per gym",
        "A gym can have at most {limit} branches (closed ones included). Please contact support to raise the limit."),
    Cap("branch_photos", 12, "photos", "per branch", "A branch can have at most {limit} photos - remove one first."),
    Cap("branch_documents", 20, "documents", "per branch",
        "A branch can hold at most {limit} documents - delete an old one first."),
    # ---------------------------------------------------------------- staff and jobs (app/routers/owner_staff.py)
    Cap("staff_per_gym", 500, "current staff records (invited, in verification, active or suspended)", "per gym",
        "A gym can have at most {limit} current staff members. Remove someone who has left before adding more."),
    Cap("staff_invites_per_gym", 50, "unanswered staff invitations", "per gym",
        "This gym already has {limit} staff invitations waiting for an answer. Wait for some to be accepted, or remove "
        "old ones, before inviting more."),
    Cap("employee_documents", 40, "documents", "per staff member",
        "A staff member can have at most {limit} documents - delete an old one first."),
    Cap("shifts_per_employee", 50, "weekly shifts", "per staff member",
        "A staff member can have at most {limit} weekly shifts - remove one first."),
    Cap("open_jobs_per_gym", 50, "open job postings", "per gym",
        "A gym can have at most {limit} open job postings - close one before opening another."),
    Cap("jobs_per_gym_day", 20, "new job postings", "per gym per day",
        "You've posted {limit} jobs for this gym in the last 24 hours. Please try again tomorrow.", window_hours=DAY),
    Cap("enquiries_per_job_day", 200, "enquiries", "per job posting per day",
        "This job has received a lot of enquiries today ({limit}). Please try again tomorrow.", window_hours=DAY),
    # ---------------------------------------------------------------- members registered by owners
    Cap("members_added_per_owner_day", 200, "members registered at the desk", "per owner per day",
        "You've registered {limit} members in the last 24 hours. Please try again tomorrow.", window_hours=DAY),
    # ---------------------------------------------------------------- tasks (app/services/tasks.py; open ones: TASKS_MAX_OPEN)
    Cap("tasks_per_person_day", 300, "new tasks and reminders", "per person per day",
        "You've added {limit} tasks in the last 24 hours - the most for one day. Please add more tomorrow.",
        window_hours=DAY),
    # ---------------------------------------------------------------- gym shop (app/routers/owner_shop.py, app/services/shop.py)
    Cap("products_per_gym", 2000, "products", "per gym shop",
        "A gym shop can list at most {limit} products. Delete products you no longer sell first."),
    Cap("product_images", 8, "photos", "per product", "A product can have at most {limit} photos - remove one first."),
    Cap("coupons_per_gym", 200, "coupons", "per gym shop",
        "A gym shop can have at most {limit} coupons. Switch off and reuse an old code, or contact support."),
    Cap("stock_changes_per_product_day", 200, "manual stock changes", "per product per day",
        "This product's stock was changed {limit} times in the last 24 hours. Please try again tomorrow.",
        window_hours=DAY),
    Cap("open_orders_per_member", 10, "unpaid orders", "per member",
        "You have {limit} unpaid orders. Pay for or cancel one under My orders before placing another."),
    Cap("orders_per_member_day", 30, "orders", "per member per day",
        "You've placed {limit} orders in the last 24 hours. Please try again tomorrow.", window_hours=DAY),
    # ---------------------------------------------------------------- accounts
    Cap("id_documents_per_user", 30, "ID card uploads", "per account",
        "This account has reached {limit} ID card uploads. Please contact support to verify your ID."),
    Cap("notifications_kept", 1000, "notifications kept (the newest, read or not; the nightly clean-up removes older ones)",
        "per person", "Older notifications are removed automatically."),
    # ---------------------------------------------------------------- health (app/services/health.py)
    Cap("wellness_checks_per_member_day", 10, "wellness check-ins", "per member per day",
        "You've saved {limit} wellness check-ins in the last 24 hours - please check in again tomorrow. If you feel "
        "unwell, stop exercising and call your local emergency number or a doctor.", window_hours=DAY),
    # never refused: past these the newest alert of the same kind is updated / the rules decide alone (health.py)
    Cap("health_alerts_per_member_day", 20, "new health alerts", "per member per day",
        "Health alerts beyond {limit} a day update the newest alert of the same kind.", window_hours=DAY),
    Cap("health_reviews_per_member_day", 10, "AI safety reviews of abnormal health readings", "per member per day",
        "The AI safety review is not repeated after {limit} reviews a day - the safety rules still check every reading.",
        window_hours=DAY),
    # ---------------------------------------------------------------- storage quotas (bytes; the value is in MB)
    Cap("storage_mb_per_gym", 2048, "MB of uploaded files", "per gym",
        "This gym's uploaded files have reached its {limit} MB storage quota. Delete photos or documents you no longer "
        "need, then try again.", unit=MB),
    Cap("storage_mb_per_member", 100, "MB of uploaded files", "per member",
        "Your uploads have reached the {limit} MB storage quota of an account. Please contact support.", unit=MB),
)
CAPS: dict[str, Cap] = {c.name: c for c in _CAPS}


# ============================================================================ values
@lru_cache(maxsize=8)
def _parse_overrides(raw: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for part in (raw or "").replace(";", ",").split(","):
        name, sep, value = part.strip().partition("=")
        name = name.strip().lower()
        if not part.strip():
            continue
        cap = CAPS.get(name)
        try:
            number = int(value.strip().replace("_", ""))
        except ValueError:
            number = -1
        if not sep or cap is None or cap.setting is not None or number < 1:
            log.warning("CAPS_OVERRIDES: ignoring %r (use name=number with a cap name from app/core/caps.py)", part.strip())
            continue
        out[name] = number
    return out


def limit(name: str) -> int:
    """The cap's value: its own setting, else a ``CAPS_OVERRIDES`` entry, else the default above."""
    from app.config import get_settings

    cap = CAPS[name]
    s = get_settings()
    if cap.setting:
        value = getattr(s, cap.setting, None)
        try:
            return max(1, int(value)) if value is not None else cap.default
        except (TypeError, ValueError):
            return cap.default
    return _parse_overrides(str(getattr(s, "caps_overrides", "") or "")).get(name, cap.default)


def since(hours: int = DAY) -> Any:
    """``created_at >= since()``: the rows of a per-day cap's window (database clock)."""
    return func.now() - timedelta(hours=hours)


# ============================================================================ refusal
class CapReached(HTTPException):
    """A cap refused a create. ``str(exc)`` / ``exc.detail`` is the friendly sentence (safe to show)."""

    def __init__(self, cap: Cap | str, message: str | None = None, *, retry_after: int | None = None) -> None:
        self.cap = CAPS[cap] if isinstance(cap, str) else cap
        self.limit = self.cap.limit
        self.message = message or self.cap.message.format(limit=f"{self.limit:,}")
        headers = {"Retry-After": str(int(retry_after))} if retry_after else None
        if headers is None and self.cap.window_hours:
            headers = {"Retry-After": str(3600)}
        super().__init__(status_code=self.cap.status, detail=self.message, headers=headers)

    def __str__(self) -> str:
        return self.message

    def payload(self) -> dict[str, Any]:
        """The documented JSON body: ``{"error": "Limit reached", "detail", "code": "cap_reached", "cap", "limit"}``."""
        return {"error": "Limit reached", "detail": self.message, "code": "cap_reached", "cap": self.cap.name,
                "limit": self.limit}


# ============================================================================ the race-safe check
def lock_key(name: str, scope_id: Any) -> int:
    """64-bit advisory-lock key of (cap, scope) - a collision only serialises two unrelated scopes a little."""
    digest = hashlib.blake2b(f"cap:{name}:{scope_id}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


async def lock(db: Any, name: str, scope_id: Any) -> None:
    """Serialise every create of ``name`` in this scope until the caller's transaction ends (COMMIT / ROLLBACK)."""
    await db.execute(select(func.pg_advisory_xact_lock(lock_key(name, scope_id))))


async def count_upto(db: Any, rows: Any, upto: int) -> int:
    """How many rows ``rows`` (a SELECT of one column, e.g. ids) returns - reading at most ``upto`` of them."""
    return int(await db.scalar(select(func.count()).select_from(rows.limit(max(1, upto)).subquery("capped"))) or 0)


async def used(db: Any, name: str, rows: Any) -> int:
    """Current use for a display ("3 of 20"), bounded like the check; no lock (an estimate for the page only)."""
    return await count_upto(db, rows, limit(name) + 1)


def _counted(name: str, scope_id: Any) -> None:
    """Today's refusal counter of the cap (the super-admin's site overview; never raises, never waits)."""
    from app.services.admin_insights import count_cap

    count_cap(name, scope_id)


async def enforce(db: Any, name: str, scope_id: Any, rows: Any, *, adding: int = 1, message: str | None = None) -> int:
    """Refuse (``CapReached``) when ``adding`` more rows would take the scope past cap ``name``; else return how many
    more still fit after them. ``rows`` selects the scope's existing rows (one column; for a per-day cap, already
    filtered with ``created_at >= since()``). Takes the (cap, scope) advisory lock first - see the module docstring."""
    cap = CAPS[name]
    n = limit(name)
    await lock(db, name, scope_id)
    have = await count_upto(db, rows, n + max(1, adding))
    if have + adding > n:
        _counted(name, scope_id)
        raise CapReached(cap, message)
    return n - have - adding


async def room(db: Any, name: str, scope_id: Any, rows: Any) -> int:
    """How many more fit (0 = full) - under the lock, for a create of several rows at once (a batch of photos):
    take what fits, report the rest."""
    n = limit(name)
    await lock(db, name, scope_id)
    return max(0, n - await count_upto(db, rows, n + 1))


async def enforce_bytes(db: Any, name: str, scope_id: Any, total_bytes: Any, *, adding: int,
                        message: str | None = None) -> int:
    """Storage quota: ``total_bytes`` is a scalar SELECT of the scope's stored bytes; refuse when ``adding`` more bytes
    would pass the quota. Same lock discipline as ``enforce`` - take it AFTER any narrower cap of the same create."""
    cap = CAPS[name]
    quota = limit(name) * cap.unit
    await lock(db, name, scope_id)
    have = int(await db.scalar(total_bytes) or 0)
    if have + max(0, int(adding)) > quota:
        _counted(name, scope_id)
        raise CapReached(cap, message)
    return quota - have - int(adding)


def mb(nbytes: int | None) -> str:
    """"1.4 MB" for a quota line."""
    value = (nbytes or 0) / MB
    return f"{value:,.1f} MB" if value < 100 else f"{value:,.0f} MB"


__all__ = ["CAPS", "Cap", "CapReached", "DAY", "MB", "count_upto", "enforce", "enforce_bytes", "limit", "lock",
           "lock_key", "mb", "room", "since", "used"]
