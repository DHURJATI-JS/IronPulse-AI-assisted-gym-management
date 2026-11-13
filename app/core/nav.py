"""Navigation per role + cached global nav state ("Enquire for jobs" appears while any job is open).

Every menu - anonymous visitors and each signed-in role - ends with "Load test results" (``LOAD_TEST_ITEM``, the public
page app/routers/public_loadtest.py) while PUBLIC_LOADTEST_PAGE is on (the default)."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Gym, JobPosting, User
from app.models.enums import Role
from app.services.jobs import periodic


@dataclass(frozen=True, slots=True)
class NavItem:
    label: str
    href: str
    icon: str
    # a long label (partials/navbar.html): never wrapped, and the one item of a crowded desktop bar that shrinks (with an
    # ellipsis; tooltip + accessible name keep the label); beside a signed-in role's full menu the bar shows only its
    # icon below xl (1280 px). The mobile menu always shows icon + label
    compact: bool = False


LOAD_TEST_ITEM = NavItem("Load test results", "/load-test", "chart", compact=True)


NAV: dict[str, tuple[NavItem, ...]] = {
    "guest": (
        NavItem("Gyms", "/gyms", "map-pin"),
        NavItem("Plans", "/plans", "sparkles"),
        NavItem("Shop", "/shop", "credit-card"),
        NavItem("About", "/about", "info"),
    ),
    Role.CUSTOMER.value: (
        NavItem("Today", "/me", "home"),
        NavItem("Coach", "/me/coach", "bolt"),
        NavItem("Live", "/me/live", "activity"),
        NavItem("Health", "/me/health", "heart"),
        NavItem("Videos", "/me/videos", "play"),
        NavItem("Calendar", "/calendar", "calendar"),
        NavItem("Chat", "/chat", "chat"),
        NavItem("Shop", "/shop", "credit-card"),
    ),
    Role.STAFF.value: (
        NavItem("Portal", "/staff", "home"),
        NavItem("Schedule", "/staff/schedule", "calendar"),
        NavItem("Clients", "/staff/clients", "users"),
        NavItem("Chat", "/chat", "chat"),
        NavItem("Tasks", "/calendar", "check"),
    ),
    Role.OWNER.value: (
        NavItem("Overview", "/owner", "home"),
        NavItem("Gyms", "/owner/gyms", "building"),
        NavItem("Staff", "/owner/staff", "users"),
        NavItem("Members", "/owner/customers", "user-group"),
        NavItem("Finance", "/owner/finance", "banknotes"),
        NavItem("Shop", "/owner/shop", "credit-card"),
        NavItem("Chat", "/chat", "chat"),
        NavItem("Reports", "/owner/reports", "flag"),
    ),
    Role.SUPERADMIN.value: (
        NavItem("Overview", "/superadmin", "home"),
        NavItem("Security", "/superadmin/security", "shield"),
        NavItem("Reviews", "/superadmin/reviews", "check-badge"),
        NavItem("AI", "/superadmin/ai", "cpu"),
        NavItem("Users", "/superadmin/users", "users"),
        NavItem("Outbox", "/superadmin/outbox", "envelope"),
    ),
}


def nav_for(user: User | None) -> tuple[NavItem, ...]:
    items = NAV["guest"] if user is None else NAV.get(user.role.value, NAV["guest"])
    if get_settings().public_loadtest_page:  # a settings attribute: no query, no I/O
        items = (*items, LOAD_TEST_ITEM)
    return items


@dataclass
class NavState:
    open_jobs: int = 0


nav_state = NavState()


async def refresh_nav_state(db: AsyncSession) -> None:
    """Call after creating/closing job postings (and it runs every minute)."""
    nav_state.open_jobs = int(
        await db.scalar(
            select(func.count(JobPosting.id)).join(Gym, Gym.id == JobPosting.gym_id).where(
                JobPosting.is_open.is_(True), Gym.is_active.is_(True)
            )
        )
        or 0
    )


@periodic("refresh_nav_state", every_seconds=60, initial_delay=1)
async def _refresh(db: AsyncSession) -> None:
    await refresh_nav_state(db)
