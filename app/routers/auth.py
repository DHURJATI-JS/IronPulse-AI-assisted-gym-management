"""WP-A2 authentication: login, logout, signup (customer / gym owner), e-mail verification, password reset,
customer invite, staff invite.

Security notes
* Sign-in errors are generic; accounts lock for ``login_lockout_minutes`` after ``login_max_failures``
  failures, counted by one atomic UPDATE each (``auth_flows.count_failed_sign_in``: guesses sent at once to several
  workers are all counted); unknown identifiers get the same treatment via ``auth_flows.ghost_throttle`` (shared by
  every worker through Redis) and the lockout is recorded as a BRUTEFORCE security event.
* Sign-in holds no database transaction while Argon2 runs: it reads the account, commits, verifies the password in a
  worker thread, then writes in a new transaction - a pooled connection is never idle in a transaction meanwhile. The
  sign-up, password reset and invitation forms likewise end their read transaction before hashing.
* A correct password signs in only through one conditional write that the lock must not be on
  (``auth_flows.clear_failed_sign_ins``): guesses verified at the same moment can never bypass a lockout they caused.
* Every successful sign-in rotates the session (old cookie revoked, new one issued) and - one session per account,
  SINGLE_SESSION_PER_ACCOUNT - ends the account's sessions on other devices (``auth_flows.start_session``); a device
  signed out that way lands here with "you were signed out because your account signed in on another device".
* Sign-up and invitations refuse identifiers of deleted or banned accounts (``auth_flows.blocked_identity``).
* A staff invitation is accepted (link or create) only while the owner may still add the person: under the person's and
  the record's locks, ``trust_safety.rehire_refusal`` is asked again - an invitation sent before the person was removed
  for a violation at another of the owner's gyms answers "can't be accepted" (neutral, never why).
* Remembered accounts (the account chooser, app/services/remembered_accounts.py): opaque per-device references only;
  ``POST /auth/remembered`` returns current details or "gone", rate-limited per address.
* One-time links store only ``sha256(token)``; tokens are single use and expire.
* Platform admins (super-admins) sign in ONLY at /superadmin/login (app/routers/superadmin_login.py,
  app/security/admin_login.py): on every path here their identifier is an unknown one - the same neutral error and work
  even with the right password, never a hint that the account is an admin - and the attempt counts towards the
  address's admin-sign-in ban. Password reset links and invitations never apply to them (their password is
  SUPERADMIN_PASSWORD in .env).
"""

from __future__ import annotations

import logging
import math
from datetime import date
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from app.config import get_settings
from app.core.deps import DB, AuthUser, CurrentUser, NotFound
from app.core.templating import add_flash, redirect, render
from app.core.utils import mask_middle, utcnow
from app.models import AuthToken, CustomerProfile, Employee, User
from app.models.enums import (
    EmploymentStatus,
    IdDocType,
    NotificationCategory,
    Role,
    Severity,
    ThreatCategory,
    TokenPurpose,
)
from app.security import admin_login
from app.security.bans import security_events
from app.security.crypto import token_hash
from app.security.csrf import signed_out_csrf_token
from app.security.net import client_ip
from app.security.passwords import hash_password, needs_rehash, verify_password
from app.security.ratelimit import limiter
from app.security.sessions import clear_session_cookie, get_session_token, revoke_all_sessions, revoke_session
from app.services import auth_flows as af
from app.services import remembered_accounts as remembered
from app.services import storage
from app.services import trust_safety as TS
from app.services.accounts import identifier_conflict
from app.services.audit import audit
from app.services.id_verification import submit_identity_document
from app.services.identity_lookup import find_user_by_identifier, generate_username
from app.services.jobs import enqueue
from app.services.notify import email_address, notify

log = logging.getLogger("ironpulse.auth")
router = APIRouter(prefix="/auth")

GENERIC_LOGIN_ERROR = "Those sign-in details didn't work. Check your e-mail, username, phone or member ID and your password."
SIGNUP_ROLES = {"customer": Role.CUSTOMER, "owner": Role.OWNER}
SIGNUP_STEPS = [
    ("account", "Account type"),
    ("about", "About you"),
    ("guardian", "Guardian consent"),
    ("photo", "Photo"),
    ("security", "Password"),
    ("identity", "ID card"),
    ("terms", "Review"),
]
WELCOME_BODY = {
    Role.CUSTOMER: (
        "Your member account is ready. Next steps: confirm your e-mail address (we sent you a link), let us verify "
        "your ID card - usually about a minute - then find a gym near you or start training online."
    ),
    Role.OWNER: (
        "Your gym-owner account is ready. Next steps: confirm your e-mail address, wait for your ID check, then add "
        "your first gym. Branches are published once they're verified, open and have at least three photos."
    ),
    Role.STAFF: (
        "Your staff account is ready and linked to your gym. The owner activates your profile once your ID card "
        "(and any required documents) are verified."
    ),
}


# ------------------------------------------------------------------ helpers
def _refdata() -> dict[str, Any]:
    return {
        "countries": af.COUNTRIES,
        "phone_countries": af.PHONE_COUNTRIES,
        "guardian_relations": af.GUARDIAN_RELATIONS,
        "timezone_groups": af.timezone_groups(),
    }


def _put(errors: dict[str, str], key: str, err: str | None) -> None:
    if err and key not in errors:
        errors[key] = err


def _date_bounds() -> tuple[str, str]:
    today = date.today()
    try:
        oldest = today.replace(year=today.year - 110)
    except ValueError:  # 29 February
        oldest = today.replace(year=today.year - 110, day=28)
    return today.isoformat(), oldest.isoformat()


def _is_minor_dob(raw: str | None) -> bool:
    try:
        return af.age_on(date.fromisoformat((raw or "").strip())) < 18
    except ValueError:
        return False


async def _discard(keys: list[str | None]) -> None:
    for key in keys:
        await storage.delete_key(key)


def _minutes_left(until: Any) -> int:
    now = utcnow()
    seconds = until - now.timestamp() if isinstance(until, (int, float)) else (until - now).total_seconds()
    return max(1, math.ceil(seconds / 60))


async def _claim_token(db: Any, token: AuthToken) -> bool:
    """Lock a one-time link's row for the rest of the transaction and check it is still unused - re-read from the
    database, not the identity map: of two submissions of one link at the same moment (two tabs, a resent form) only
    the first changes anything; the second waits for its COMMIT and then sees the link used."""
    row = await db.scalar(select(AuthToken).where(AuthToken.id == token.id).with_for_update()
                          .execution_options(populate_existing=True))
    if row is None or row.used_at is not None or row.expires_at <= utcnow():
        await db.commit()  # nothing written: release the row lock (a rollback would expire the page's objects)
        return False
    return True


def _guardian_from(values: dict[str, Any], *, own_email: str | None, own_phone: str | None) -> tuple[dict[str, Any], dict[str, str]]:
    """Validate the guardian block of the signup form (customers under 18)."""
    errors: dict[str, str] = {}
    name, err = af.validate_name(values["guardian_name"], what="your guardian's full name")
    _put(errors, "guardian_name", err)
    relations = dict(af.GUARDIAN_RELATIONS)
    if values["guardian_relation"] not in relations:
        errors["guardian_relation"] = "Choose how this person is related to you."
    g_email, err = af.validate_email(values["guardian_email"])
    if err:
        errors["guardian_email"] = "Enter your guardian's e-mail address." if not values["guardian_email"] else err
    elif own_email and g_email == own_email:
        errors["guardian_email"] = "Your guardian needs their own e-mail address (different from yours)."
    _typed, g_norm, err = af.validate_phone(values["guardian_phone"], values["guardian_phone_country"] or values["phone_country"])
    if err:
        errors["guardian_phone"] = "Enter your guardian's phone number." if not values["guardian_phone"] else err
    elif own_phone and g_norm == own_phone:
        errors["guardian_phone"] = "Your guardian's phone number must be different from yours."
    if not values["guardian_consent"]:
        errors["guardian_consent"] = "Your parent or guardian must agree before you can join."
    data = {
        "guardian_name": name,
        "guardian_relation": relations.get(values["guardian_relation"]),
        "guardian_email": g_email,
        "guardian_phone": g_norm,
        "emergency_contact_name": name,
        "emergency_contact_phone": g_norm,
    }
    return data, errors


# ------------------------------------------------------------------ login / logout
ELSEWHERE_GENERIC = ("You were signed out because your account signed in on another device. "
                     "If this was not you, reset your password.")


def _login_ctx(identifier: str = "", nxt: str | None = None, **extra: Any) -> dict[str, Any]:
    ctx = {"form": {"identifier": identifier}, "next": nxt or "", "error": None, "locked": False, "signed_out": None,
           "account_ref": "", "remember": True}
    ctx.update(extra)
    return ctx


@router.get("/login")
async def login_page(request: Request, user: CurrentUser, next_: str | None = Query(None, alias="next"),
                     elsewhere: str | None = None):
    nxt = af.safe_next(next_)
    if user is not None:
        return RedirectResponse(nxt or af.role_home(user), status_code=303)
    if (nxt and (nxt == "/superadmin" or nxt.startswith(("/superadmin/", "/superadmin?")))
            and getattr(request.state, "session_ended", None) is None):
        # an admin page asked for a sign-in: platform admins sign in on their own page (decided by the page asked
        # for, never by an account - this sign-in page never says whether an identifier belongs to an admin)
        return RedirectResponse(f"/superadmin/login?next={quote(nxt, safe='')}", status_code=303)
    ended = getattr(request.state, "session_ended", None)
    signed_out = ended.message() if ended is not None else (ELSEWHERE_GENERIC if elsewhere == "1" else None)
    ctx = _login_ctx(nxt=nxt, signed_out=signed_out)
    if ended is not None:  # this response clears the stale cookie: the page's forms must match the signed-out state
        ctx["csrf_token"] = signed_out_csrf_token(request)
    response = render(request, "auth/login.html", ctx)
    if ended is not None:
        clear_session_cookie(response)  # said once; from now on this browser is simply signed out
    return response


@router.post("/login")
async def login_submit(request: Request, db: DB):
    settings = get_settings()
    f = af.FormReader(await request.form())
    # one box for e-mail, @username, phone number or member ID ("email" kept for older forms/clients) - or an account
    # picked in the sign-in page's account chooser (its opaque reference, never an id)
    identifier = f.text("identifier", 300) or f.text("email", 300)
    account_ref = "" if identifier else f.text("account_ref", 128)
    password = f.secret("password")
    nxt = af.safe_next(f.text("next", 1000))
    remember = f.flag("remember")
    device_id = f.text("device_id", 80)

    def fail(message: str, *, locked: bool = False) -> Response:
        ctx = _login_ctx(identifier, nxt, error=message, locked=locked, account_ref=account_ref, remember=remember)
        return render(request, "auth/login.html", ctx, status_code=400)

    if (not identifier and not account_ref) or len(identifier) > 254 or not password:
        return fail("Enter your e-mail, username, phone or member ID - and your password.")

    now = utcnow()
    if account_ref:
        user = await remembered.resolve(db, account_ref)
        ghost_key = "account-ref:" + token_hash(account_ref)
    else:
        user = await find_user_by_identifier(db, identifier)
        ghost_key = identifier  # ghost_throttle normalises it (af.identifier_key): every spelling shares a counter
    # Platform admins sign in ONLY at /superadmin/login (app/security/admin_login.py): here a super-admin's identifier
    # is an unknown one - the same answer, the same Argon2 work and lockout, even with the right password - and the
    # attempt counts towards the address's admin-sign-in ban
    admin_id = user.id if user is not None and user.role == Role.SUPERADMIN else None
    if admin_id is not None:
        user = None
    # Argon2 runs in a worker thread for ~50 ms (longer while the threads are busy): end the read transaction first, so
    # no pooled connection sits idle in it meanwhile. expire_on_commit=False keeps ``user`` loaded; the writes below
    # open a new transaction.
    await db.commit()
    if user is None:
        # unknown account: same work, same messages, same lockout behaviour as a real account
        await verify_password(None, password)
        until = await af.ghost_throttle.fail(ghost_key)  # no count while locked; shared by every worker
        if admin_id is not None:
            await admin_login.note_failure(db, request, reason="admin_on_member_signin", identifier=identifier,
                                           user_id=admin_id, path="/auth/login")
        if until is None:
            return fail(GENERIC_LOGIN_ERROR)
        return fail(af.lock_message(_minutes_left(until)), locked=True)

    if user.locked_until is not None and user.locked_until > now:
        await verify_password(None, password)
        return fail(af.lock_message(_minutes_left(user.locked_until)), locked=True)

    ok = await verify_password(user.password_hash, password)
    if not ok:
        if user.deleted_at is None and user.is_active:
            if await af.count_failed_sign_in(db, user, now):  # atomic: concurrent guesses are all counted
                security_events.record_from_request(
                    request, ThreatCategory.BRUTEFORCE, 2,
                    f"account {user.id} locked after {settings.login_max_failures} failed sign-ins",
                )
                await notify(
                    db, user, "Sign-in paused after failed attempts",
                    f"Someone entered the wrong password for your account {settings.login_max_failures} times, so "
                    f"sign-in is paused for {settings.login_lockout_minutes} minutes (last attempt from {client_ip(request)}). "
                    "If this wasn't you, reset your password.",
                    category=NotificationCategory.SECURITY, severity=Severity.WARNING, link="/auth/forgot",
                )
                await db.commit()
                return fail(af.lock_message(settings.login_lockout_minutes), locked=True)
            await db.commit()
            if user.locked_until is not None and user.locked_until > now:  # locked meanwhile by guesses in flight
                return fail(af.lock_message(_minutes_left(user.locked_until)), locked=True)
        return fail(GENERIC_LOGIN_ERROR)

    # correct password from here on - only now may we reveal the account's state
    if user.deleted_at is not None:
        when = f"{user.purge_after:%d %b %Y}" if user.purge_after else "soon"
        return fail(f"This account was deleted. Its data is kept until {when} and then erased permanently.")
    if not user.is_active:  # disabled by an admin or banned (trust & safety): the same neutral answer
        return fail("This account has been disabled. Please contact the IronPulse team for help.")

    # an outdated hash is re-hashed (Argon2 again) BEFORE the first statement of the write transaction below
    fresh_hash = await hash_password(password) if user.password_hash and needs_rehash(user.password_hash) else None
    # The lock may have been switched on by wrong guesses verified at the same time as this one (they all read the
    # account before Argon2): one conditional write decides, and a locked account is never signed in.
    if not await af.clear_failed_sign_ins(db, user, now):
        until = user.locked_until
        await db.commit()  # nothing was written; ends the transaction without expiring the loaded objects
        minutes = _minutes_left(until) if until is not None and until > now else settings.login_lockout_minutes
        return fail(af.lock_message(minutes), locked=True)
    ip = client_ip(request)
    previous_ip = user.last_login_ip
    user.last_login_at = now
    user.last_login_ip = ip
    if fresh_hash:
        user.password_hash = fresh_hash
    if previous_ip and previous_ip != ip:
        agent = (request.headers.get("user-agent") or "an unknown browser")[:120]
        await notify(
            db, user, "New sign-in to your account",
            f"Your account was signed in from {ip} ({agent}) on {now:%d %b %Y at %H:%M} UTC. "
            "If this wasn't you, change your password straight away.",
            category=NotificationCategory.SECURITY, severity=Severity.WARNING, link="/account#security",
        )
    response = RedirectResponse(nxt or af.role_home(user), status_code=303)
    raw = await af.start_session(db, request, response, user)
    new_ref = None
    if remember and remembered.valid_device(device_id):
        new_ref = await remembered.remember(db, user, device_id=device_id, session_hash=token_hash(raw),
                                            user_agent=request.headers.get("user-agent"), now=now)
    elif account_ref:
        await remembered.touch(db, account_ref, session_hash=token_hash(raw))
    await db.commit()
    if new_ref:
        remembered.set_handoff_cookie(response, new_ref)
    add_flash(response, f"Welcome back, {af.first_name(user)}.", "success")
    return response


# ------------------------------------------------------------------ remembered accounts (the sign-in page's chooser)
async def _remembered_throttled(request: Request) -> JSONResponse | None:
    """LOOKUPS_PER_MINUTE chooser lookups + removals per network address (shared by every worker through Redis)."""
    ip = client_ip(request)
    if ip in get_settings().rate_limit_whitelist:
        return None
    if await limiter.note(ip, "remembered", window=60.0) <= remembered.LOOKUPS_PER_MINUTE:
        return None
    return JSONResponse({"error": "Slow down", "detail": "Too many requests - try again in a minute.", "retry_after": 60},
                        status_code=429, headers={"Retry-After": "60", "Cache-Control": "no-store"})


async def _json_object(request: Request) -> dict[str, Any] | None:
    if not request.headers.get("content-type", "").lower().startswith("application/json"):
        return None  # a form body (already read by the CSRF check) is not what these endpoints take
    try:
        data = await request.json()
    except (ValueError, RuntimeError):
        return None
    return data if isinstance(data, dict) else None


@router.post("/remembered")
async def remembered_lookup(request: Request, db: DB):
    """``{"refs": [...]}`` (at most LOOKUP_MAX_REFS opaque references from this browser) -> per reference the account's
    CURRENT name, masked e-mail and a short-lived photo URL, or ``{"state": "gone"}`` - the same for an unknown, revoked,
    expired, banned or deleted account. Same-origin + CSRF token (the app-wide check), rate-limited per address."""
    if (limited := await _remembered_throttled(request)) is not None:
        return limited
    data = await _json_object(request)
    refs = data.get("refs") if data is not None else None
    if (not isinstance(refs, list) or len(refs) > remembered.LOOKUP_MAX_REFS
            or not all(isinstance(r, str) and 0 < len(r) <= 128 for r in refs)):
        return JSONResponse({"error": "Invalid input",
                             "detail": f"Send at most {remembered.LOOKUP_MAX_REFS} account references."},
                            status_code=422, headers={"Cache-Control": "no-store"})
    accounts = await remembered.lookup(db, refs)
    return JSONResponse({"accounts": accounts}, headers={"Cache-Control": "no-store"})


@router.post("/remembered/forget")
async def remembered_forget(request: Request, db: DB):
    """"Remove from this device": the reference stops working at once. Always ``{"ok": true}`` (no oracle)."""
    if (limited := await _remembered_throttled(request)) is not None:
        return limited
    data = await _json_object(request)
    ref = data.get("ref") if data is not None else None
    if isinstance(ref, str) and len(ref) <= 128:
        await remembered.forget(db, ref)
        await db.commit()
    return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})


@router.get("/remembered/photo/{token}")
async def remembered_photo(token: str):
    """A remembered account's current photo through a signed URL that expires (PHOTO_URL_SECONDS)."""
    key = remembered.photo_key_from(token)
    if key is None:
        raise NotFound()
    try:
        path = storage.path_for(key)
    except storage.UploadError:
        raise NotFound() from None
    if not path.is_file():
        raise NotFound()
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})


@router.post("/logout")
async def logout(request: Request, db: DB):
    form = await request.form()
    nxt = af.safe_next(af.clean_text(form.get("next"), 1000))
    token = get_session_token(request)
    if token:
        await revoke_session(db, token, end_live=False)  # ended right below, awaited (an immediate exit)
        await db.commit()
        await _logout_presence(request, token)
    response = redirect(nxt or "/", "You've been signed out. See you soon!", "success")
    clear_session_cookie(response)
    return response


async def _logout_presence(request: Request, token: str) -> None:
    """After COMMIT: close this session's live sockets and stop the AI work it started - here and, through the PG
    bridge, in the other processes. With no other socket or live session left the user counts as gone at once (their
    background jobs follow their exit policies)."""
    from app.security.crypto import token_hash
    from app.services.presence import presence
    from app.services.realtime import hub

    session_hash = token_hash(token)
    try:
        user = getattr(request.state, "user", None)
        hub.close_sessions([session_hash], user_id=user.id if user is not None else None, exit_reason="logout")
        if user is not None:
            await presence.logout(user.id, session_hash=session_hash)
    except Exception:  # noqa: BLE001 - signing out must never fail because of presence bookkeeping
        log.exception("presence cleanup after logout failed")


# ------------------------------------------------------------------ signup
def _signup_ctx(form: dict[str, Any], errors: dict[str, str]) -> dict[str, Any]:
    today, oldest = _date_bounds()
    show_guardian = form.get("role", "customer") == "customer" and _is_minor_dob(form.get("date_of_birth"))
    return {**_refdata(), "form": form, "errors": errors, "steps": SIGNUP_STEPS, "today": today, "min_dob": oldest,
            "show_guardian": show_guardian}


@router.get("/signup")
async def signup_page(request: Request, user: CurrentUser, role: str = "customer"):
    if user is not None:
        return redirect(af.role_home(user), "You're already signed in.", "info")
    form = {"role": role if role in SIGNUP_ROLES else "customer", "country": "IN", "phone_country": "IN",
            "guardian_phone_country": "IN", "id_country": "IN"}
    return render(request, "auth/signup.html", _signup_ctx(form, {}))


@router.post("/signup")
async def signup_submit(request: Request, db: DB, user: CurrentUser):
    if user is not None:
        return redirect(af.role_home(user), "You're already signed in.", "info")
    f = af.FormReader(await request.form())
    values: dict[str, Any] = {
        "role": f.text("role", 20),
        "full_name": f.text("full_name", 300),
        "email": f.text("email", 300),
        "username": f.text("username", 64),
        "phone": f.text("phone", 64),
        "phone_country": f.text("phone_country", 4).upper(),
        "date_of_birth": f.text("date_of_birth", 20),
        "country": f.text("country", 4).upper(),
        "city": f.text("city", 300),
        "timezone": f.text("timezone", 64),
        "guardian_name": f.text("guardian_name", 300),
        "guardian_relation": f.text("guardian_relation", 40),
        "guardian_email": f.text("guardian_email", 300),
        "guardian_phone": f.text("guardian_phone", 64),
        "guardian_phone_country": f.text("guardian_phone_country", 4).upper(),
        "guardian_consent": f.flag("guardian_consent"),
        "accept_terms": f.flag("accept_terms"),
    }
    id_values, id_errors = af.validate_id_fields(f, "id")
    values.update(id_values)
    password, confirm = f.secret("password"), f.secret("password_confirm")

    errors: dict[str, str] = {}
    role = SIGNUP_ROLES.get(values["role"])
    if role is None:
        errors["role"] = "Choose whether you're joining as a member or as a gym owner."
    name, err = af.validate_name(values["full_name"])
    _put(errors, "full_name", err)
    email, err = af.validate_email(values["email"])
    _put(errors, "email", err)
    username, err = await af.validate_username(db, values["username"])
    _put(errors, "username", err)
    phone, phone_norm, err = af.validate_phone(values["phone"], values["phone_country"])
    _put(errors, "phone", err)
    dob, err = af.validate_dob(values["date_of_birth"])
    _put(errors, "date_of_birth", err)
    if dob is not None and not err and role == Role.OWNER and af.age_on(dob) < 18:
        errors["date_of_birth"] = "Gym owners must be at least 18 years old."
    country, err = af.validate_country(values["country"])
    _put(errors, "country", err)
    city, err = af.validate_city(values["city"])
    _put(errors, "city", err)
    tz, _ = af.validate_timezone(values["timezone"], fallback="UTC")
    minor = role == Role.CUSTOMER and dob is not None and af.age_on(dob) < 18
    guardian: dict[str, Any] = {}
    if minor:
        guardian, g_errors = _guardian_from(values, own_email=email, own_phone=phone_norm)
        errors.update(g_errors)
    photo = f.upload("photo")
    if role == Role.OWNER and photo is None:
        errors["photo"] = "A profile photo is required for gym owners."
    for key, msg in af.password_errors(password, confirm, email=email, name=name).items():
        _put(errors, key, msg)
    for key, msg in id_errors.items():
        _put(errors, key, msg)
    if not values["accept_terms"]:
        errors["accept_terms"] = "Please accept the Terms & disclaimer to create an account."
    if "email" not in errors:  # one person per e-mail, forever: deleted / banned accounts' identifiers stay blocked
        blocked = await af.blocked_identity(db, email=email)
        if blocked:
            errors["email"] = blocked
    if "phone" not in errors and phone_norm:
        blocked = await af.blocked_identity(db, phone=phone_norm, country_code=values["phone_country"])
        if blocked:
            errors["phone"] = blocked
    if "email" not in errors:
        conflict = await identifier_conflict(db, email=email)
        if conflict:
            errors["email"] = conflict.message
    if "phone" not in errors:
        conflict = await identifier_conflict(db, phone=phone, country_code=values["phone_country"])
        if conflict:
            errors["phone"] = conflict.message
    if errors or role is None or dob is None:
        return render(request, "auth/signup.html", _signup_ctx(values, errors), status_code=400)

    await db.commit()  # end the read transaction before Argon2: no pooled connection idles in it while hashing
    password_hash = await hash_password(password)
    now = utcnow()
    new_user = User(
        email=email, username=username or await generate_username(db, name, email), full_name=name, role=role,
        password_hash=password_hash, phone=phone, phone_normalized=phone_norm, date_of_birth=dob,
        country_code=country, city=city, timezone=tz, is_active=True, last_login_at=now, last_login_ip=client_ip(request),
    )
    db.add(new_user)
    saved: list[str | None] = []
    stage = "photo"
    try:
        await db.flush()
        if photo is not None:
            stored = await storage.save_public_image(photo, kind="avatars", max_side=800)
            saved.append(stored.key)
            new_user.photo_key = stored.key
        if role == Role.CUSTOMER:
            db.add(CustomerProfile(user_id=new_user.id, intake_source="self", **guardian))
            await db.flush()
        stage = "id_front"
        doc = await submit_identity_document(
            db, user=new_user, employee=None, doc_type=IdDocType(values["id_doc_type"]), country_code=values["id_country"],
            front=f.upload("id_front"), back=f.upload("id_back"), uploaded_by=new_user,
        )
        saved += [doc.front_key, doc.back_key]
        await af.send_email_verification(db, new_user)
        await notify(db, new_user, f"Welcome to {get_settings().app_name}", WELCOME_BODY[role],
                     category=NotificationCategory.ACCOUNT, severity=Severity.SUCCESS, link=af.role_home(new_user))
        await audit(db, new_user, "account.signup", "user", new_user.id, detail={"role": role.value, "minor": minor}, request=request)
        response = RedirectResponse(af.role_home(new_user), status_code=303)
        await af.start_session(db, request, response, new_user)
        await db.commit()
    except storage.UploadError as exc:
        await db.rollback()
        await _discard(saved)
        errors[stage] = str(exc)
        return render(request, "auth/signup.html", _signup_ctx(values, errors), status_code=400)
    except IntegrityError:
        await db.rollback()
        await _discard(saved)
        errors["email"] = ("An account with this e-mail address, username or phone number was just created. "
                           "Try signing in, or choose another username.")
        return render(request, "auth/signup.html", _signup_ctx(values, errors), status_code=400)
    add_flash(response, f"Welcome aboard, {af.first_name(new_user)}! We've e-mailed you a link to confirm your address, "
                        "and your ID card is being verified - this usually takes about a minute.", "success")
    return response


# ------------------------------------------------------------------ e-mail verification
@router.get("/verify-email")
async def verify_email(request: Request, db: DB, user: CurrentUser, token: str = ""):
    found = await af.lookup_token(db, token, TokenPurpose.EMAIL_VERIFY)
    target = await db.get(User, found.token.user_id) if found.ok and found.token and found.token.user_id else None
    state = found.state
    if found.ok and (
        target is None or target.deleted_at is not None
        or (found.token.email and (target.email or "").lower() != found.token.email.lower())
    ):
        state = "missing"  # account gone, or the address changed since this link was sent
    if state != "ok" or target is None or found.token is None:
        return render(request, "auth/verify_email.html", {"state": state, "signed_in": user is not None}, status_code=400)
    now = utcnow()
    found.token.used_at = now
    if target.email_verified_at is None:
        target.email_verified_at = now
        await notify(db, target, "E-mail address confirmed", f"Thanks - {target.email} is now verified for your account.",
                     category=NotificationCategory.ACCOUNT, severity=Severity.SUCCESS, link="/account")
    await db.commit()
    return render(request, "auth/verify_email.html", {"state": "ok", "email": target.email, "signed_in": user is not None,
                                                      "home": af.role_home(user) if user else "/auth/login"})


@router.post("/verify-email/resend")
async def verify_email_resend(request: Request, db: DB, user: AuthUser):
    if user.email_verified_at is not None:
        return redirect("/account", "Your e-mail address is already confirmed.", "info")
    if await af.recently_issued(db, TokenPurpose.EMAIL_VERIFY, user_id=user.id, within_seconds=60):
        return redirect("/account", "We just sent you a link - please wait a minute before asking for another.", "warning")
    await af.send_email_verification(db, user)
    await db.commit()
    return redirect("/account", f"We've sent a new confirmation link to {user.email}.", "success")


# ------------------------------------------------------------------ forgotten password
@router.get("/forgot")
async def forgot_page(request: Request, sent: int = 0):
    return render(request, "auth/forgot.html", {"sent": bool(sent), "form": {}, "errors": {}})


@router.post("/forgot")
async def forgot_submit(request: Request, db: DB):
    f = af.FormReader(await request.form())
    raw = f.text("identifier", 300) or f.text("email", 300)
    if len(raw) < 3 or len(raw) > 254:
        return render(request, "auth/forgot.html", {"sent": False, "form": {"identifier": raw},
                                                    "errors": {"identifier": "Enter your e-mail address, username or phone number."}},
                      status_code=400)
    user = await find_user_by_identifier(db, raw)
    # a platform admin's password is SUPERADMIN_PASSWORD in the server's .env: no reset link, the answer of an unknown
    # account (app/security/admin_login.py)
    if user is not None and (user.deleted_at is not None or not user.is_active or user.role == Role.SUPERADMIN):
        user = None
    if user is not None and user.email and not await af.recently_issued(db, TokenPurpose.PASSWORD_RESET, user_id=user.id, within_seconds=60):
        token = await af.issue_token(db, TokenPurpose.PASSWORD_RESET, user_id=user.id, email=user.email)
        await email_address(
            db, user.email, "Reset your password",
            "We received a request to reset the password for your IronPulse account. The link below works once and "
            "expires in 1 hour. If you didn't ask for this, you can ignore this e-mail - your password stays the same.",
            link=f"/auth/reset?token={token}", recipient=user.full_name,
        )
        await db.commit()
    # identical response whether or not the account exists (no user enumeration)
    return redirect("/auth/forgot?sent=1")


async def _reset_target(db: Any, raw: str) -> tuple[af.TokenLookup, User | None]:
    found = await af.lookup_token(db, raw, TokenPurpose.PASSWORD_RESET)
    if not found.ok or found.token is None or found.token.user_id is None:
        return found, None
    user = await db.get(User, found.token.user_id)
    if user is None or user.deleted_at is not None or not user.is_active or user.role == Role.SUPERADMIN:
        return af.TokenLookup("missing"), None  # (a platform admin's password lives in .env: never reset by a link)
    if found.token.email and (user.email or "").lower() != found.token.email.lower():
        return af.TokenLookup("missing"), None
    return found, user


@router.get("/reset")
async def reset_page(request: Request, db: DB, token: str = ""):
    found, target = await _reset_target(db, token)
    if target is None:
        return render(request, "auth/reset.html", {"state": found.state}, status_code=400)
    return render(request, "auth/reset.html", {"state": "ok", "token": token, "email": target.email, "name": target.full_name, "errors": {}})


@router.post("/reset")
async def reset_submit(request: Request, db: DB):
    f = af.FormReader(await request.form())
    token = f.text("token", 200)
    found, target = await _reset_target(db, token)
    if target is None or found.token is None:
        return render(request, "auth/reset.html", {"state": found.state}, status_code=400)
    password, confirm = f.secret("password"), f.secret("password_confirm")
    errors = af.password_errors(password, confirm, email=target.email, name=target.full_name)
    if errors:
        return render(request, "auth/reset.html", {"state": "ok", "token": token, "email": target.email,
                                                   "name": target.full_name, "errors": errors}, status_code=400)
    await db.commit()  # end the read transaction before Argon2 (expire_on_commit=False keeps ``target`` loaded)
    password_hash = await hash_password(password)
    if not await _claim_token(db, found.token):  # the same link submitted twice at once: the second one stops here
        return render(request, "auth/reset.html", {"state": "used"}, status_code=400)
    now = utcnow()
    target.password_hash = password_hash
    target.failed_login_count = 0
    target.locked_until = None
    if target.email_verified_at is None:
        target.email_verified_at = now  # the reset link proved the address
    found.token.used_at = now
    await db.execute(
        update(AuthToken).where(AuthToken.user_id == target.id, AuthToken.purpose == TokenPurpose.PASSWORD_RESET,
                                AuthToken.used_at.is_(None)).values(used_at=now).execution_options(synchronize_session=False)
    )
    await revoke_all_sessions(db, target.id)
    await notify(
        db, target, "Your password was changed",
        f"The password for your account was reset on {now:%d %b %Y at %H:%M} UTC and every device was signed out. "
        "If you didn't do this, reset it again immediately and contact the gym administrator.",
        category=NotificationCategory.SECURITY, severity=Severity.WARNING, link="/account#security",
    )
    await audit(db, target, "account.password_reset", "user", target.id, request=request)
    await db.commit()
    response = redirect("/auth/login", "Your password has been updated. Sign in with your new password.", "success")
    clear_session_cookie(response)
    return response


# ------------------------------------------------------------------ customer invite (owner-registered members)
async def _invite_target(db: Any, raw: str) -> tuple[af.TokenLookup, User | None]:
    found = await af.lookup_token(db, raw, TokenPurpose.INVITE)
    if not found.ok or found.token is None or found.token.user_id is None:
        return found, None
    user = await db.scalar(select(User).options(joinedload(User.customer_profile)).where(User.id == found.token.user_id))
    if user is None or user.deleted_at is not None or not user.is_active or user.role == Role.SUPERADMIN:
        return af.TokenLookup("missing"), None  # (an invitation never signs a platform admin in)
    if found.token.email and (user.email or "").lower() != found.token.email.lower():
        return af.TokenLookup("missing"), None
    return found, user


async def _invite_ctx(db: Any, token: str, invitee: User, errors: dict[str, str] | None = None,
                      accept_terms: bool = False) -> dict[str, Any]:
    registrar = None
    profile = invitee.customer_profile
    if profile is not None and profile.registered_by_id:
        registrar = await db.scalar(select(User.full_name).where(User.id == profile.registered_by_id))
    return {"state": "ok", "token": token, "invitee": invitee, "registrar": registrar, "errors": errors or {},
            "form": {"accept_terms": accept_terms}}


@router.get("/invite")
async def invite_page(request: Request, db: DB, token: str = ""):
    found, invitee = await _invite_target(db, token)
    if invitee is None:
        return render(request, "auth/invite.html", {"state": found.state}, status_code=400)
    return render(request, "auth/invite.html", await _invite_ctx(db, token, invitee))


@router.post("/invite")
async def invite_submit(request: Request, db: DB):
    f = af.FormReader(await request.form())
    token = f.text("token", 200)
    found, invitee = await _invite_target(db, token)
    if invitee is None or found.token is None:
        return render(request, "auth/invite.html", {"state": found.state}, status_code=400)
    password, confirm = f.secret("password"), f.secret("password_confirm")
    errors = af.password_errors(password, confirm, email=invitee.email, name=invitee.full_name)
    accept = f.flag("accept_terms")
    if not accept:
        errors["accept_terms"] = "Please accept the Terms & disclaimer to activate your account."
    blocked = await af.blocked_identity(db, email=invitee.email, phone=invitee.phone_normalized or invitee.phone,
                                        country_code=invitee.country_code, exclude_user_id=invitee.id)
    if blocked:  # the gym registered an e-mail / phone of a deleted or banned account: never activated
        errors["account"] = blocked
    if errors:
        return render(request, "auth/invite.html", await _invite_ctx(db, token, invitee, errors, accept), status_code=400)
    await db.commit()  # end the read transaction before Argon2 (expire_on_commit=False keeps ``invitee`` loaded)
    password_hash = await hash_password(password)
    if not await _claim_token(db, found.token):  # the same invitation accepted twice at once: the second one stops here
        return render(request, "auth/invite.html", {"state": "used"}, status_code=400)
    now = utcnow()
    invitee.password_hash = password_hash
    invitee.failed_login_count = 0
    invitee.locked_until = None
    invitee.email_verified_at = invitee.email_verified_at or now  # the invite e-mail proved the address
    invitee.last_login_at = now
    invitee.last_login_ip = client_ip(request)
    tz = af.validate_timezone(f.text("timezone", 64), fallback="")[0]
    if tz and invitee.timezone in (None, "", "UTC"):
        invitee.timezone = tz
    if not invitee.username:
        invitee.username = await generate_username(db, invitee.full_name, invitee.email)
    found.token.used_at = now
    await notify(
        db, invitee, "Your account is ready",
        "You set your password and activated the account your gym created for you. You can now buy plans, follow your "
        "coaching and chat with your trainers.",
        category=NotificationCategory.ACCOUNT, severity=Severity.SUCCESS, link=af.role_home(invitee),
    )
    await audit(db, invitee, "account.invite_accepted", "user", invitee.id, request=request)
    response = RedirectResponse(af.role_home(invitee), status_code=303)
    await af.start_session(db, request, response, invitee)
    await db.commit()
    add_flash(response, f"Welcome, {af.first_name(invitee)}! Your account is active.", "success")
    return response


# ------------------------------------------------------------------ staff invite (create or link an account)
async def _staff_target(db: Any, raw: str) -> tuple[af.TokenLookup, Employee | None]:
    found = await af.lookup_token(db, raw, TokenPurpose.STAFF_INVITE)
    if not found.ok or found.token is None or found.token.employee_id is None:
        return found, None
    emp = await db.scalar(select(Employee).options(joinedload(Employee.gym)).where(Employee.id == found.token.employee_id))
    if (emp is None or emp.deleted_at is not None or emp.user_id is not None
            or emp.status != EmploymentStatus.INVITED or not emp.gym.is_active):
        return af.TokenLookup("used" if emp is not None and emp.user_id is not None else "missing"), None
    if found.token.email and found.token.email.lower() != (emp.invite_email or "").lower():
        return af.TokenLookup("missing"), None
    return found, emp


def _staff_eligibility(user: User | None, emp: Employee) -> tuple[str, str | None]:
    """('anonymous' | 'ok' | 'refused', message)."""
    if user is None:
        return "anonymous", None
    invited = (emp.invite_email or "").lower()
    same_email = (user.email or "").lower() == invited
    if user.role != Role.STAFF:
        kind = {Role.CUSTOMER: "member", Role.OWNER: "gym-owner", Role.SUPERADMIN: "administrator"}.get(user.role, "this")
        if same_email:
            return "refused", (f"You're signed in with a {kind} account. {kind.capitalize()} accounts can't be linked to a staff "
                               f"position - ask {emp.gym.name} to send the invitation to a different e-mail address for your staff account.")
        return "refused", (f"You're signed in with a {kind} account ({user.email}), which can't be linked to a staff position. "
                           f"Sign out, then create your staff account with {invited}.")
    if not same_email:
        local, _, domain = invited.partition("@")
        return "refused", (f"This invitation was sent to {mask_middle(local, keep_end=2)}@{domain}, "
                           f"but you're signed in as {user.email}. Sign out and use the invited address.")
    return "ok", None


async def _staff_ctx(db: Any, token: str, emp: Employee, user: User | None, *,
                     form: dict[str, Any] | None = None, errors: dict[str, str] | None = None) -> dict[str, Any]:
    eligibility, message = _staff_eligibility(user, emp)
    owner_name = await db.scalar(select(User.full_name).where(User.id == emp.gym.owner_id))
    today, oldest = _date_bounds()
    defaults = {"full_name": emp.full_name, "phone": emp.phone or "", "phone_country": emp.country_code or "IN",
                "country": emp.country_code if emp.country_code in af.COUNTRY_NAMES else "IN",
                "id_country": emp.country_code or "IN"}
    login_next = quote(f"/auth/staff-invite?token={token}", safe="")
    return {**_refdata(), "state": "ok", "token": token, "employee": emp, "gym": emp.gym, "owner_name": owner_name,
            "eligibility": eligibility, "refusal": message, "form": {**defaults, **(form or {})}, "errors": errors or {},
            "login_url": f"/auth/login?next={login_next}", "today": today, "min_dob": oldest}


async def _link_employee(db: Any, request: Request, token: AuthToken, emp: Employee, user: User) -> None:
    now = utcnow()
    emp.user_id = user.id
    emp.status = EmploymentStatus.PENDING_VERIFICATION
    token.used_at = now
    await enqueue(db, "staff_activation_check", {"employee_id": emp.id}, dedupe_key=f"staff_activation_check:{emp.id}",
                  user_id=user.id)
    role_label = (emp.role_title or emp.role.value.replace("_", " ")).strip()
    await notify(
        db, emp.gym.owner_id, f"{user.full_name} accepted your staff invitation",
        f"{user.full_name} linked their IronPulse account to the {role_label} position at {emp.gym.name}. "
        "They'll be activated automatically once their photo and ID card (and any required documents) are verified.",
        category=NotificationCategory.STAFF, severity=Severity.SUCCESS, link=f"/owner/staff/{emp.id}",
    )
    await notify(
        db, user, f"You joined {emp.gym.name}",
        f"Your account is now linked to the {role_label} position at {emp.gym.name}. The gym activates your profile "
        "once your ID card is verified - we'll let you know.",
        category=NotificationCategory.STAFF, severity=Severity.SUCCESS, link="/staff",
    )
    await audit(db, user, "staff.invite_accepted", "employee", emp.id, gym_id=emp.gym_id, request=request)


async def _staff_rehire_refusal(db: Any, emp: Employee) -> str | None:
    """Whether the gym's owner may still add the invited person (``TS.rehire_refusal``): not when the account is banned,
    the identity is in the blocked registry, or a gym of this owner removed the person for a VIOLATION / SEVERE reason
    - also AFTER this invitation was sent (hunt F45: an invitation pending before the violation was still accepted)."""
    return await TS.rehire_refusal(db, owner_id=emp.gym.owner_id, email=emp.invite_email or "")


async def _locked_staff_refusal(db: Any, request: Request, emp: Employee) -> Response | None:
    """Right before an invitation is accepted (link or create): take the person's lock, then the employment row's - the
    lock order of every staff path (app/services/trust_safety.py) - and check again that the record is still a pending
    invitation and that the owner may still add the person. A removal for a violation at another of the owner's gyms
    committed meanwhile is seen here; one that starts meanwhile waits for this acceptance and then ends it. The page to
    answer with, or None (the locks stay held until the caller's COMMIT)."""
    await TS.lock_person(db, await TS.person_of_email(db, emp.invite_email or ""))
    current = await TS.lock_employee(db, emp.id)
    if (current is None or current.deleted_at is not None or current.user_id is not None
            or current.status != EmploymentStatus.INVITED):
        await db.commit()  # nothing written: releases the locks
        state = ("missing" if current is None else "used" if current.user_id is not None
                 else "withdrawn")  # ended / removed by the gym meanwhile
        return render(request, "auth/staff_invite.html", {"state": state}, status_code=400)
    if await _staff_rehire_refusal(db, emp):
        await db.commit()
        return render(request, "auth/staff_invite.html", {"state": "withdrawn"}, status_code=403)
    return None


@router.get("/staff-invite")
async def staff_invite_page(request: Request, db: DB, user: CurrentUser, token: str = ""):
    found, emp = await _staff_target(db, token)
    if emp is None:
        return render(request, "auth/staff_invite.html", {"state": found.state}, status_code=400)
    if await _staff_rehire_refusal(db, emp):  # the neutral answer: never why
        return render(request, "auth/staff_invite.html", {"state": "withdrawn"}, status_code=403)
    return render(request, "auth/staff_invite.html", await _staff_ctx(db, token, emp, user))


@router.post("/staff-invite")
async def staff_invite_submit(request: Request, db: DB, user: CurrentUser):
    f = af.FormReader(await request.form())
    token = f.text("token", 200)
    action = f.text("action", 20)
    found, emp = await _staff_target(db, token)
    if emp is None or found.token is None:
        return render(request, "auth/staff_invite.html", {"state": found.state}, status_code=400)

    if action == "link":
        if user is None:
            return RedirectResponse(f"/auth/login?next={quote(f'/auth/staff-invite?token={token}', safe='')}", status_code=303)
        eligibility, _message = _staff_eligibility(user, emp)
        if eligibility != "ok":
            return render(request, "auth/staff_invite.html", await _staff_ctx(db, token, emp, user), status_code=403)
        if (refused := await _locked_staff_refusal(db, request, emp)) is not None:
            return refused
        if not await _claim_token(db, found.token):  # linked twice at once (two tabs): only the first one counts
            return render(request, "auth/staff_invite.html", {"state": "used"}, status_code=400)
        await _link_employee(db, request, found.token, emp, user)
        await db.commit()
        return redirect("/staff", f"Your account is now linked to {emp.gym.name}. We'll let you know once you're activated.", "success")

    if action != "create":
        return render(request, "auth/staff_invite.html", await _staff_ctx(db, token, emp, user), status_code=400)
    if user is not None:
        return render(request, "auth/staff_invite.html", await _staff_ctx(db, token, emp, user), status_code=403)

    values: dict[str, Any] = {
        "full_name": f.text("full_name", 300),
        "phone": f.text("phone", 64),
        "phone_country": f.text("phone_country", 4).upper(),
        "date_of_birth": f.text("date_of_birth", 20),
        "country": f.text("country", 4).upper(),
        "city": f.text("city", 300),
        "timezone": f.text("timezone", 64),
        "accept_terms": f.flag("accept_terms"),
    }
    id_values, id_errors = af.validate_id_fields(f, "id")
    values.update(id_values)
    password, confirm = f.secret("password"), f.secret("password_confirm")
    email = (emp.invite_email or "").strip().lower()

    errors: dict[str, str] = {}
    name, err = af.validate_name(values["full_name"])
    _put(errors, "full_name", err)
    blocked = await af.blocked_identity(db, email=email)  # one person per e-mail, forever (deleted / banned accounts)
    if blocked:
        errors["email"] = blocked
    conflict = None if blocked else await identifier_conflict(db, email=email)
    if conflict:
        errors["email"] = conflict.message
    phone, phone_norm, err = af.validate_phone(values["phone"], values["phone_country"])
    _put(errors, "phone", err)
    if "phone" not in errors and phone_norm:
        blocked = await af.blocked_identity(db, phone=phone_norm, country_code=values["phone_country"])
        if blocked:
            errors["phone"] = blocked
    dob, err = af.validate_dob(values["date_of_birth"], min_age=16)
    _put(errors, "date_of_birth", err)
    country, err = af.validate_country(values["country"])
    _put(errors, "country", err)
    city, err = af.validate_city(values["city"])
    _put(errors, "city", err)
    tz, _ = af.validate_timezone(values["timezone"], fallback="UTC")
    photo = f.upload("photo")
    if photo is None:
        errors["photo"] = "A profile photo is required for staff accounts."
    for key, msg in af.password_errors(password, confirm, email=email, name=name).items():
        _put(errors, key, msg)
    for key, msg in id_errors.items():
        _put(errors, key, msg)
    if not values["accept_terms"]:
        errors["accept_terms"] = "Please accept the Terms & disclaimer to create your account."
    if "phone" not in errors:
        conflict = await identifier_conflict(db, phone=phone, country_code=values["phone_country"])
        if conflict:
            errors["phone"] = conflict.message
    if errors or dob is None:
        ctx = await _staff_ctx(db, token, emp, None, form=values, errors=errors)
        return render(request, "auth/staff_invite.html", ctx, status_code=400)

    await db.commit()  # end the read transaction before Argon2 (expire_on_commit=False keeps ``emp`` loaded)
    password_hash = await hash_password(password)
    if (refused := await _locked_staff_refusal(db, request, emp)) is not None:
        return refused
    if not await _claim_token(db, found.token):  # the same invitation used twice at once: the second one stops here
        return render(request, "auth/staff_invite.html", {"state": "used"}, status_code=400)
    now = utcnow()
    staff = User(
        email=email, username=await generate_username(db, name, email), full_name=name, role=Role.STAFF,
        password_hash=password_hash, phone=phone, phone_normalized=phone_norm, date_of_birth=dob,
        country_code=country, city=city, timezone=tz, is_active=True, email_verified_at=now, last_login_at=now,
        last_login_ip=client_ip(request),
    )
    db.add(staff)
    saved: list[str | None] = []
    stage = "photo"
    gym_name = emp.gym.name
    try:
        await db.flush()
        stored = await storage.save_public_image(photo, kind="avatars", max_side=800)
        saved.append(stored.key)
        staff.photo_key = stored.key
        stage = "id_front"
        doc = await submit_identity_document(
            db, user=staff, employee=emp, doc_type=IdDocType(values["id_doc_type"]), country_code=values["id_country"],
            front=f.upload("id_front"), back=f.upload("id_back"), uploaded_by=staff,
        )
        saved += [doc.front_key, doc.back_key]
        await _link_employee(db, request, found.token, emp, staff)
        await notify(db, staff, f"Welcome to {get_settings().app_name}", WELCOME_BODY[Role.STAFF],
                     category=NotificationCategory.ACCOUNT, severity=Severity.SUCCESS, link="/staff")
        await audit(db, staff, "account.signup", "user", staff.id, detail={"role": "staff", "employee_id": emp.id}, request=request)
        response = RedirectResponse("/staff", status_code=303)
        await af.start_session(db, request, response, staff)
        await db.commit()
    except storage.UploadError as exc:
        await db.rollback()
        await _discard(saved)
        errors[stage] = str(exc)
        found2, emp2 = await _staff_target(db, token)
        if emp2 is None:
            return render(request, "auth/staff_invite.html", {"state": found2.state}, status_code=400)
        ctx = await _staff_ctx(db, token, emp2, None, form=values, errors=errors)
        return render(request, "auth/staff_invite.html", ctx, status_code=400)
    except IntegrityError:
        await db.rollback()
        await _discard(saved)
        return redirect(f"/auth/staff-invite?token={quote(token, safe='')}",
                        "An account with this e-mail address or phone number already exists - sign in instead.", "error")
    add_flash(response, f"Welcome aboard, {af.first_name(staff)}! Your staff account is linked to {gym_name}.", "success")
    return response
