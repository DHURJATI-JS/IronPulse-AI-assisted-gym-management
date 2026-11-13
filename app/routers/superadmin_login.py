"""``/superadmin/login`` - the platform admin's own sign-in page (the rules: app/security/admin_login.py).

* Only super-admin accounts sign in here; anyone else - an unknown e-mail, a wrong password, a member's, owner's or staff
  account even with its right password, a disabled admin - gets the same neutral error after the same Argon2 work, so
  the page never tells whether an identifier exists or what role it has.
* Stricter than the normal sign-in: the "auth" rate-limit bucket (app/security/ratelimit.py), the per-account lockout
  (``auth_flows.count_failed_sign_in``; unknown identifiers: ``ghost_throttle``) AND an IP ban after
  SUPERADMIN_LOGIN_MAX_FAILURES failed attempts from one address within SUPERADMIN_LOGIN_WINDOW_MINUTES (shared by every
  worker; loopback is never banned). A banned address is refused here at once - the database decides, on every worker.
* SUPERADMIN_ALLOWED_IPS: outside the list this page is a 404.
* The same session machinery as every sign-in (``auth_flows.start_session``: rotation, one session per account); a
  platform admin is never remembered by the account chooser. Every attempt is audited (the identifier masked, never the
  password). Success lands on /superadmin (or a /superadmin page asked for).
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import RedirectResponse, Response

from app.core.deps import DB, CurrentUser, NotFound
from app.core.templating import add_flash, render
from app.core.utils import utcnow
from app.models.enums import NotificationCategory, Role, Severity, ThreatCategory
from app.security import admin_login
from app.security.bans import security_events
from app.security.csrf import signed_out_csrf_token
from app.security.net import client_ip
from app.security.passwords import hash_password, needs_rehash, verify_password
from app.security.sessions import clear_session_cookie
from app.services import auth_flows as af
from app.services.identity_lookup import find_user_by_identifier
from app.services.notify import notify

router = APIRouter()

TEMPLATE = "superadmin/login.html"
ADMIN_LOGIN_ERROR = "Those sign-in details didn't work. Check the e-mail address and the password."


def admin_next(raw: str | None) -> str | None:
    """Where to go after signing in: a /superadmin page only (never the sign-in page itself)."""
    nxt = af.safe_next(raw)
    if not nxt or not (nxt == "/superadmin" or nxt.startswith(("/superadmin/", "/superadmin?"))):
        return None
    return None if nxt.startswith("/superadmin/login") else nxt


def lock_message(minutes: int) -> str:
    minutes = max(1, minutes)
    return (f"Too many failed sign-in attempts. Sign-in for this account is paused for {minutes} "
            f"minute{'s' if minutes != 1 else ''} - try again later.")


def _minutes_left(until: Any) -> int:
    now = utcnow()
    seconds = until - now.timestamp() if isinstance(until, (int, float)) else (until - now).total_seconds()
    return max(1, math.ceil(seconds / 60))


def _ctx(identifier: str = "", nxt: str | None = None, **extra: Any) -> dict[str, Any]:
    ctx = {"form": {"identifier": identifier}, "next": nxt or "", "error": None, "locked": False, "warning": None,
           "banned_until": None, "banned": False, "signed_out": None}
    ctx.update(extra)
    return ctx


def _banned_page(request: Request, until: datetime | None, identifier: str = "") -> Response:
    return render(request, TEMPLATE, _ctx(identifier, banned=True, banned_until=until), status_code=403)


def _refused(request: Request, outcome: admin_login.FailureOutcome, identifier: str, nxt: str | None, *,
             lock_minutes: int | None = None) -> Response:
    """The answer to a refused attempt: the ban page once the address is banned, else the neutral error (or the lock
    message) - with a warning as the address nears the ban (every failure counts the same, so it reveals nothing)."""
    if outcome.banned:
        return _banned_page(request, outcome.until, identifier)
    warning = None
    left = outcome.limit - outcome.failures
    if 0 < left <= 2 and admin_login.never_banned(client_ip(request)) is None:
        warning = (f"{left} more failed attempt{'s' if left != 1 else ''} from this network address and it is blocked "
                   "for every IronPulse page.")
    if lock_minutes is not None:
        return render(request, TEMPLATE, _ctx(identifier, nxt, error=lock_message(lock_minutes), locked=True,
                                              warning=warning), status_code=400)
    return render(request, TEMPLATE, _ctx(identifier, nxt, error=ADMIN_LOGIN_ERROR, warning=warning), status_code=400)


@router.get("/superadmin/login")
async def admin_login_page(request: Request, db: DB, user: CurrentUser,
                           next_: str | None = Query(None, alias="next")) -> Response:
    if not admin_login.address_allowed(client_ip(request)):
        raise NotFound()
    banned, until = await admin_login.active_ban(db, client_ip(request))
    if banned:
        return _banned_page(request, until)
    nxt = admin_next(next_)
    if user is not None and user.role == Role.SUPERADMIN:
        return RedirectResponse(nxt or "/superadmin", status_code=303)
    ended = getattr(request.state, "session_ended", None)  # signed out by a sign-in on another device: say so once
    ctx = _ctx(nxt=nxt, signed_out=ended.message() if ended is not None else None)
    if ended is not None:  # this response clears the stale cookie: the form's token must match the signed-out state
        ctx["csrf_token"] = signed_out_csrf_token(request)
    response = render(request, TEMPLATE, ctx)
    if ended is not None:
        clear_session_cookie(response)
    return response


@router.post("/superadmin/login")
async def admin_login_submit(request: Request, db: DB) -> Response:
    ip = client_ip(request)
    if not admin_login.address_allowed(ip):
        raise NotFound()
    f = af.FormReader(await request.form())
    identifier = f.text("identifier", 300) or f.text("email", 300)
    password = f.secret("password")
    nxt = admin_next(f.text("next", 1000))
    if not identifier or len(identifier) > 254 or not password:
        return render(request, TEMPLATE, _ctx(identifier, nxt, error="Enter your e-mail address and your password."),
                      status_code=400)
    banned, until = await admin_login.active_ban(db, ip)
    if banned:  # the database decides - on every worker, before its next ban sync
        await db.commit()
        return _banned_page(request, until, identifier)

    found = await find_user_by_identifier(db, identifier)
    admin = found if found is not None and found.role == Role.SUPERADMIN else None
    # end the read transaction before Argon2 (expire_on_commit=False keeps the account loaded)
    await db.commit()
    now = utcnow()
    if admin is None:
        # unknown, or not a platform admin (even with the right password): the work and the answer of a wrong password
        await verify_password(None, password)
        ghost_until = await af.ghost_throttle.fail(identifier)
        outcome = await admin_login.note_failure(db, request, reason="not_admin" if found is not None else "unknown",
                                                 identifier=identifier, user_id=found.id if found else None)
        return _refused(request, outcome, identifier, nxt,
                        lock_minutes=_minutes_left(ghost_until) if ghost_until is not None else None)

    if admin.locked_until is not None and admin.locked_until > now:
        await verify_password(None, password)
        outcome = await admin_login.note_failure(db, request, reason="locked", identifier=identifier, user_id=admin.id)
        return _refused(request, outcome, identifier, nxt, lock_minutes=_minutes_left(admin.locked_until))

    if not await verify_password(admin.password_hash, password):
        locked_now = False
        if admin.deleted_at is None and admin.is_active:
            locked_now = await af.count_failed_sign_in(db, admin, now)  # atomic: concurrent guesses all count
            if locked_now:
                security_events.record_from_request(request, ThreatCategory.BRUTEFORCE, 2,
                                                    f"platform admin account {admin.id} locked after failed sign-ins")
            await db.commit()
        outcome = await admin_login.note_failure(db, request, reason="wrong_password", identifier=identifier,
                                                 user_id=admin.id)
        locked = admin.locked_until is not None and admin.locked_until > now
        return _refused(request, outcome, identifier, nxt,
                        lock_minutes=_minutes_left(admin.locked_until) if (locked_now or locked) else None)

    if admin.deleted_at is not None or not admin.is_active:  # the neutral answer, counted like any refusal
        outcome = await admin_login.note_failure(db, request, reason="disabled", identifier=identifier,
                                                 user_id=admin.id)
        return _refused(request, outcome, identifier, nxt)

    fresh_hash = await hash_password(password) if admin.password_hash and needs_rehash(admin.password_hash) else None
    if not await af.clear_failed_sign_ins(db, admin, now):  # locked meanwhile by guesses in flight: never signed in
        until_lock = admin.locked_until
        await db.commit()
        outcome = await admin_login.note_failure(db, request, reason="locked", identifier=identifier, user_id=admin.id)
        minutes = _minutes_left(until_lock) if until_lock is not None and until_lock > now else 15
        return _refused(request, outcome, identifier, nxt, lock_minutes=minutes)
    previous_ip = admin.last_login_ip
    admin.last_login_at = now
    admin.last_login_ip = ip
    if fresh_hash:
        admin.password_hash = fresh_hash
    if previous_ip and previous_ip != ip:
        agent = (request.headers.get("user-agent") or "an unknown browser")[:120]
        await notify(db, admin, "New platform admin sign-in",
                     f"Your platform admin account was signed in from {ip} ({agent}) on {now:%d %b %Y at %H:%M} UTC. If "
                     "this wasn't you, change SUPERADMIN_PASSWORD in the server's .env file and restart IronPulse.",
                     category=NotificationCategory.SECURITY, severity=Severity.WARNING, link="/superadmin/audit")
    response = RedirectResponse(nxt or "/superadmin", status_code=303)
    await af.start_session(db, request, response, admin)  # rotation + one session per account
    await admin_login.note_success(db, request, admin)
    await db.commit()
    add_flash(response, f"Welcome back, {af.first_name(admin)}.", "success")
    return response
