"""FastAPI dependencies: DB session, current user, role guards and ownership-scoped loaders.

Ownership loaders return 404 (not 403) for objects that belong to someone else, so IDs cannot be
probed (IDOR protection). Pass loader options to eager-load what the page needs (no lazy loads!).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import HTTPConnection

from app.db import get_db
from app.models import Branch, Employee, Gym, User
from app.models.enums import Role
from app.security.sessions import get_session_token, lookup_session

DB = Annotated[AsyncSession, Depends(get_db)]

# Where a browser signed out by a sign-in on another device may still go: the sign-in page itself (it shows why and
# clears the stale cookie), its helpers (the account chooser, "forgot / reset password" - "if this was not you") and
# public files. Everything else lands on the sign-in page.
SIGNED_OUT_PASS_PREFIXES = ("/auth/", "/superadmin/login", "/media/", "/static/", "/healthz", "/robots.txt",
                            "/favicon.ico")
SIGNED_OUT_HEADER = "X-Signed-Out"


class LoginRequired(HTTPException):
    def __init__(self) -> None:
        super().__init__(status_code=401, detail="Please sign in to continue.")


class SignedOutElsewhere(HTTPException):
    """The browser's session was ended by a sign-in on another device (one session per account). Pages are redirected
    to the sign-in page, which says why (app/core/errors.py: every non-JSON 401); JSON callers get 401 with
    ``X-Signed-Out: elsewhere`` and the message, and app/static/src/js/00-core.js takes the tab to the sign-in page."""

    def __init__(self, message: str) -> None:
        super().__init__(status_code=401, detail=message, headers={SIGNED_OUT_HEADER: "elsewhere"})


class Forbidden(HTTPException):
    def __init__(self, detail: str = "You don't have access to this page.") -> None:
        super().__init__(status_code=403, detail=detail)


class NotFound(HTTPException):
    def __init__(self, detail: str = "Not found.") -> None:
        super().__init__(status_code=404, detail=detail)


async def load_user(request: HTTPConnection, db: DB) -> User | None:
    """App-wide dependency (runs once per request): resolves the session cookie to a user.

    WebSocket connections are skipped: /ws authenticates itself with a short-lived session so a
    long-lived socket never pins a pooled database connection."""
    if request.scope.get("type") != "http":
        return None
    if getattr(request.state, "user_loaded", False):
        return request.state.user
    request.state.user = None
    request.state.session = None
    request.state.session_ended = None
    token = get_session_token(request)
    if token:
        found = await lookup_session(db, token)
        sess = found.session
        if sess is not None:
            request.state.user = sess.user
            request.state.session = sess
            _set_ai_owner(request, sess)
            from app.services.presence import presence

            presence.touch(sess.user.id)  # back after leaving: resume their paused jobs (one dict lookup otherwise)
        elif found.ended is not None:
            # signed out by a sign-in on another device: its next request lands on the sign-in page, which says why
            request.state.session_ended = found.ended
            if not request.url.path.startswith(SIGNED_OUT_PASS_PREFIXES):
                request.state.user_loaded = True
                raise SignedOutElsewhere(found.ended.message())
    request.state.user_loaded = True
    return request.state.user


def _set_ai_owner(request: HTTPConnection, sess: Any) -> None:
    """Attribute any AI work done while handling this request to the signed-in user (app/ai/owner.py):
    per-user quotas, the fair AI queue and 'stop when they leave' all key off this."""
    from app.ai.owner import AIOwner, clean_page_id, set_owner
    from app.security.net import client_ip

    user = sess.user
    set_owner(AIOwner(
        user_id=user.id, role=user.role.value, session_hash=sess.id, ip=client_ip(request),
        page_id=clean_page_id(request.headers.get("x-page-id")),
        disconnected=getattr(request, "is_disconnected", None),
    ))


async def current_user(request: Request, _: Annotated[Any, Depends(load_user)]) -> User | None:
    return request.state.user


async def require_user(request: Request, _: Annotated[Any, Depends(load_user)]) -> User:
    user = request.state.user
    if user is None:
        raise LoginRequired()
    return user


def require_roles(*roles: Role):
    async def dep(user: Annotated[User, Depends(require_user)]) -> User:
        if user.role not in roles:
            raise Forbidden()
        return user

    return dep


CurrentUser = Annotated[User | None, Depends(current_user)]
AuthUser = Annotated[User, Depends(require_user)]
OwnerUser = Annotated[User, Depends(require_roles(Role.OWNER))]
StaffUser = Annotated[User, Depends(require_roles(Role.STAFF))]
CustomerUser = Annotated[User, Depends(require_roles(Role.CUSTOMER))]


async def require_superadmin(request: Request, user: Annotated[User, Depends(require_roles(Role.SUPERADMIN))]) -> User:
    """A super-admin - and, with SUPERADMIN_ALLOWED_IPS set, only from those addresses (app/security/admin_login.py):
    a super-admin session used from anywhere else is refused like any other forbidden page."""
    from app.security.admin_login import address_allowed
    from app.security.net import client_ip

    if not address_allowed(client_ip(request)):
        raise Forbidden("The platform admin console can't be opened from this network address.")
    return user


SuperAdminUser = Annotated[User, Depends(require_superadmin)]


async def get_owned_gym(db: AsyncSession, gym_id: int, owner: User, *options: Any) -> Gym:
    stmt = select(Gym).where(Gym.id == gym_id, Gym.owner_id == owner.id)
    if options:
        stmt = stmt.options(*options)
    gym = (await db.execute(stmt)).unique().scalar_one_or_none()
    if gym is None:
        raise NotFound("Gym not found.")
    return gym


async def get_owned_branch(db: AsyncSession, branch_id: int, owner: User, *options: Any) -> Branch:
    stmt = select(Branch).join(Gym, Gym.id == Branch.gym_id).where(Branch.id == branch_id, Gym.owner_id == owner.id)
    if options:
        stmt = stmt.options(*options)
    branch = (await db.execute(stmt)).unique().scalar_one_or_none()
    if branch is None:
        raise NotFound("Branch not found.")
    return branch


async def get_owned_employee(db: AsyncSession, employee_id: int, owner: User, *options: Any) -> Employee:
    stmt = select(Employee).join(Gym, Gym.id == Employee.gym_id).where(Employee.id == employee_id, Gym.owner_id == owner.id)
    if options:
        stmt = stmt.options(*options)
    emp = (await db.execute(stmt)).unique().scalar_one_or_none()
    if emp is None:
        raise NotFound("Staff member not found.")
    return emp


def wants_json(request: Request) -> bool:
    if request.url.path.startswith("/api/"):
        return True
    accept = request.headers.get("accept", "")
    return "application/json" in accept and "text/html" not in accept
