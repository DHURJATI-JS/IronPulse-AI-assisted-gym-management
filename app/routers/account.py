"""WP-A2 account: profile, photo, password, sessions, account deletion, notification history and the
notifications / location JSON APIs used by the Dynamic Island and the "Use my location" button.

Every account change is confirmed with ``notify`` (in-app + e-mail, guardian CC for minors). E-mail
address changes also warn the OLD address and require the new one to be confirmed.

Notification lists are keyset windows (``app.core.pagination.keyset``, served by ix_notifications_user_created):
the history page pages with ``?before=`` / ``?after=`` ids and the bell feed with ``?before_id=``, so a window costs
the same however deep the reader goes. Counts are bounded index reads that stop at ``COUNT_CAP`` ("999+"); the island
badge asks ``GET /api/notifications/unread`` (one bounded count) instead of loading a feed.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.core.deps import DB, AuthUser, NotFound
from app.core.geo import valid_coords
from app.core.pagination import keyset
from app.core.templating import redirect, render
from app.core.utils import DIAL_CODES, is_valid_timezone, mask_middle, utcnow
from app.models import Notification, User, UserSession
from app.models.enums import IdStatus, NotificationCategory, Role, Severity, ThreatCategory
from app.security.bans import security_events
from app.security.passwords import hash_password, verify_password
from app.security.sessions import clear_session_cookie, renew_session
from app.services import auth_flows as af
from app.services import storage
from app.services.accounts import AccountDeletionBlocked, identifier_conflict, soft_delete_user
from app.services.audit import audit
from app.services.notify import email_address, notify

router = APIRouter()

PAGE_SIZE = 20
FEED_MAX = 50  # /api/notifications?limit= ceiling
COUNT_CAP = 999  # unread / total counts stop here and show "999+": every count reads at most COUNT_CAP + 1 index entries
MAX_ID = 2**63 - 1
# platform admins: their credentials live in the server's .env (app/services/bootstrap.py applies them at every start)
ADMIN_EMAIL_FIXED = ("The platform admin e-mail is set by SUPERADMIN_EMAIL in the server's .env file - change it there "
                     "and restart IronPulse.")
ADMIN_PASSWORD_FIXED = ("The platform admin password is set by SUPERADMIN_PASSWORD in the server's .env file - change it "
                        "there and restart IronPulse (every session is signed out then).")
CATEGORY_ICONS = {
    "account": "user", "security": "shield", "payment": "credit-card", "health": "heart", "reminder": "clock",
    "message": "chat", "verification": "check-badge", "staff": "briefcase", "system": "info",
}


# ------------------------------------------------------------------ helpers
def _count_upto(*where: Any) -> Any:
    """Scalar subquery: matching notifications, counted up to COUNT_CAP + 1 (a bounded index read, never a full count)."""
    return select(func.count()).select_from(
        select(Notification.id).where(*where).limit(COUNT_CAP + 1).subquery()
    ).scalar_subquery()


def count_label(n: int) -> int | str:
    """``n`` itself, or "999+" once the bounded count reached the cap."""
    return n if n <= COUNT_CAP else f"{COUNT_CAP}+"


async def _unread_count(db: Any, user_id: int) -> int:
    """Unread notifications of a user, counted up to COUNT_CAP + 1 (ix_notifications_user_unread)."""
    return int(await db.scalar(select(_count_upto(Notification.user_id == user_id, Notification.read_at.is_(None))))
               or 0)


def _cursor(raw: Any) -> int | None:
    """A notification id from a query string / form value (junk, 0 and ids beyond BIGINT -> None)."""
    text = str(raw or "").strip()
    if not text.isascii() or not text.isdigit() or len(text) > 19:
        return None
    value = int(text)
    return value if 0 < value <= MAX_ID else None


def _phone_country(user: User) -> str:
    """Best guess of the calling-code country for re-displaying the stored phone number."""
    norm = (user.phone_normalized or "").lstrip("+")
    preferred = (user.country_code or "").upper()
    if norm and preferred in DIAL_CODES and norm.startswith(DIAL_CODES[preferred]):
        return preferred
    if norm:
        for code, dial in sorted(DIAL_CODES.items(), key=lambda kv: -len(kv[1])):
            if norm.startswith(dial):
                return code
    return preferred if preferred in DIAL_CODES else "IN"


def _phone_display(user: User, country: str) -> str:
    """The national number when it matches the selected calling code (the code sits in its own selector)."""
    dial = DIAL_CODES.get(country)
    norm = user.phone_normalized or ""
    if dial and norm.startswith(f"+{dial}") and len(norm) > len(dial) + 1:
        return norm[len(dial) + 1:]
    return user.phone or ""


def _dob_locked(user: User) -> bool:
    return user.id_status == IdStatus.VERIFIED and user.date_of_birth is not None


def _mask_email(email: str) -> str:
    local, _, domain = (email or "").partition("@")
    return f"{mask_middle(local, keep_end=1, keep_start=2) if len(local) > 3 else local[:1] + '•••'}@{domain}"


async def _account_ctx(db: Any, user: User, *, form: dict[str, Any] | None = None, errors: dict[str, str] | None = None,
                       pw_errors: dict[str, str] | None = None, photo_error: str | None = None) -> dict[str, Any]:
    now = utcnow()
    session_count = int(await db.scalar(
        select(func.count()).select_from(UserSession).where(
            UserSession.user_id == user.id, UserSession.revoked_at.is_(None), UserSession.expires_at > now)
    ) or 0)
    today = date.today()
    try:
        oldest = today.replace(year=today.year - 110)
    except ValueError:
        oldest = today.replace(year=today.year - 110, day=28)
    phone_country = _phone_country(user)
    defaults = {
        "full_name": user.full_name, "email": user.email or "", "username": user.username or "",
        "phone": _phone_display(user, phone_country), "phone_country": phone_country,
        "date_of_birth": user.date_of_birth.isoformat() if user.date_of_birth else "",
        "country": user.country_code or "", "city": user.city or "", "timezone": user.timezone or "UTC",
    }
    return {
        "countries": af.COUNTRIES, "phone_countries": af.PHONE_COUNTRIES, "timezone_groups": af.timezone_groups(),
        "form": {**defaults, **(form or {})}, "errors": errors or {}, "pw_errors": pw_errors or {},
        "photo_error": photo_error, "session_count": session_count,
        "unread_count": count_label(await _unread_count(db, user.id)),
        "dob_locked": _dob_locked(user), "today": today.isoformat(), "min_dob": oldest.isoformat(),
        "retention_days": get_settings().account_retention_days, "member_id": f"IP-{user.id:06d}",
    }


async def _check_password(db: Any, request: Request, user: User, password: str) -> str | None:
    """Re-authentication for sensitive actions; wrong guesses count towards the sign-in lockout - like the sign-in:
    one atomic UPDATE per guess (``auth_flows.count_failed_sign_in``: guesses sent at once to several workers are all
    counted) and no database transaction open while Argon2 runs (the request's read transaction ends first)."""
    now = utcnow()

    def locked_message() -> str:
        minutes = max(1, math.ceil((user.locked_until - utcnow()).total_seconds() / 60)) if user.locked_until else 1
        return f"Too many wrong passwords. Try again in {minutes} minute{'s' if minutes != 1 else ''}."

    if user.locked_until is not None and user.locked_until > now:
        return locked_message()
    await db.commit()  # expire_on_commit=False keeps ``user``; the callers' writes open a new transaction
    if password and await verify_password(user.password_hash, password):
        # one conditional write: a right password verified while wrong guesses in flight locked the account must not
        # change the password or delete the account (review #2 - the sign-in's rule, auth_flows.clear_failed_sign_ins)
        if not await af.clear_failed_sign_ins(db, user, now):
            await db.commit()
            return locked_message()
        return None
    if await af.count_failed_sign_in(db, user, now):
        security_events.record_from_request(request, ThreatCategory.BRUTEFORCE, 2,
                                            f"account {user.id}: repeated wrong current password on {request.url.path}")
    await db.commit()
    return "That password isn't correct." if not password else "Your current password isn't correct."


# ------------------------------------------------------------------ profile
@router.get("/account")
async def account_page(request: Request, db: DB, user: AuthUser):
    return render(request, "account/index.html", await _account_ctx(db, user))


@router.post("/account")
async def account_update(request: Request, db: DB, user: AuthUser):
    f = af.FormReader(await request.form())
    values = {
        "full_name": f.text("full_name", 300),
        "email": f.text("email", 300),
        "username": f.text("username", 64),
        "phone": f.text("phone", 64),
        "phone_country": f.text("phone_country", 4).upper(),
        "date_of_birth": f.text("date_of_birth", 20),
        "country": f.text("country", 4).upper(),
        "city": f.text("city", 300),
        "timezone": f.text("timezone", 64),
    }
    optional = user.role == Role.SUPERADMIN
    errors: dict[str, str] = {}

    name, err = af.validate_name(values["full_name"])
    if err:
        errors["full_name"] = err
    email, err = af.validate_email(values["email"])
    if err:
        errors["email"] = err
    username, err = await af.validate_username(db, values["username"], exclude_user_id=user.id, required=bool(user.username))
    if err:
        errors["username"] = err
    phone, phone_norm, err = af.validate_phone(values["phone"], values["phone_country"], required=not optional)
    if err:
        errors["phone"] = err
    if _dob_locked(user):
        dob = user.date_of_birth
    else:
        dob, err = af.validate_dob(values["date_of_birth"], min_age=16 if user.role == Role.STAFF else 5, required=not optional)
        if err:
            errors["date_of_birth"] = err
        elif dob is not None and user.role == Role.OWNER and af.age_on(dob) < 18:
            errors["date_of_birth"] = "Gym owners must be at least 18 years old."
        elif dob is not None and user.role == Role.CUSTOMER and af.age_on(dob) < 18:
            profile = user.customer_profile
            if profile is None or not (profile.guardian_name and profile.guardian_email and profile.guardian_phone):
                errors["date_of_birth"] = ("Members under 18 need a parent or guardian on file. Add their details in your "
                                           "health profile first, then update your date of birth.")
    country, err = af.validate_country(values["country"], required=not optional)
    if err:
        errors["country"] = err
    city, err = af.validate_city(values["city"], required=not optional)
    if err:
        errors["city"] = err
    tz, err = af.validate_timezone(values["timezone"])
    if err:
        errors["timezone"] = err

    email_changed = bool(email) and email != (user.email or "").lower()
    if email_changed and optional:  # a platform admin's e-mail is SUPERADMIN_EMAIL in the server's .env
        errors["email"] = ADMIN_EMAIL_FIXED
    if email_changed and "email" not in errors:
        conflict = await identifier_conflict(db, email=email, exclude_user_id=user.id)
        if conflict:
            errors["email"] = conflict.message
    if phone_norm and phone_norm != user.phone_normalized and "phone" not in errors:
        conflict = await identifier_conflict(db, phone=phone, country_code=values["phone_country"], exclude_user_id=user.id)
        if conflict:
            errors["phone"] = conflict.message
    if errors:
        ctx = await _account_ctx(db, user, form=values, errors=errors)
        return render(request, "account/index.html", ctx, status_code=400)

    changes: list[str] = []
    if name != user.full_name:
        changes.append("full name")
        user.full_name = name
    if email_changed:
        changes.append("e-mail address")
    if username and username != (user.username or "").lower():
        changes.append("username")
        user.username = username
    if phone_norm != user.phone_normalized:
        changes.append("phone number")
        user.phone = phone or None
        user.phone_normalized = phone_norm
    if dob != user.date_of_birth:
        changes.append("date of birth")
        user.date_of_birth = dob
    if (country or None) != (user.country_code or None):
        changes.append("country")
        user.country_code = country or None
    if (city or None) != (user.city or None):
        changes.append("city")
        user.city = city or None
    if tz != user.timezone:
        changes.append("time zone")
        user.timezone = tz
    if not changes:
        await db.commit()
        return redirect("/account", "Nothing to update - your details are unchanged.", "info")

    if email_changed:
        old_email = user.email
        user.email = email
        user.email_verified_at = None
        if old_email:
            await email_address(
                db, old_email, "Your e-mail address was changed",
                f"The e-mail address on your IronPulse account was changed to {_mask_email(email)}. From now on we'll "
                "write to the new address. If you didn't make this change, reset your password straight away.",
                link="/auth/forgot", recipient=user.full_name,
            )
        await af.send_email_verification(db, user)
    await notify(
        db, user, "Your account details were changed",
        f"Your account details were changed: {', '.join(changes)}. If you didn't make this change, reset your password "
        "straight away.",
        category=NotificationCategory.ACCOUNT, severity=Severity.INFO, link="/account",
    )
    await audit(db, user, "account.update", "user", user.id, detail={"fields": changes}, request=request)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return redirect("/account", "That e-mail address, username or phone number was just taken by another account.", "error")
    message = "Your changes were saved."
    if email_changed:
        message += f" Check {email} for a link to confirm your new address."
    return redirect("/account", message, "success")


# ------------------------------------------------------------------ photo
@router.post("/account/photo")
async def account_photo(request: Request, db: DB, user: AuthUser):
    f = af.FormReader(await request.form())
    if f.text("action", 20) == "remove":
        if user.role in (Role.OWNER, Role.STAFF):
            ctx = await _account_ctx(db, user, photo_error="Gym owners and staff must keep a profile photo - you can replace it, but not remove it.")
            return render(request, "account/index.html", ctx, status_code=400)
        if not user.photo_key:
            return redirect("/account#photo", "You don't have a profile photo yet.", "info")
        old = user.photo_key
        user.photo_key = None
        await notify(db, user, "Your profile photo was removed", "Your profile photo was removed from your account.",
                     category=NotificationCategory.ACCOUNT, link="/account")
        await db.commit()
        await storage.delete_key(old)
        return redirect("/account#photo", "Your photo was removed.", "success")

    upload = f.upload("photo")
    if upload is None:
        ctx = await _account_ctx(db, user, photo_error="Choose a photo to upload.")
        return render(request, "account/index.html", ctx, status_code=400)
    try:
        stored = await storage.save_public_image(upload, kind="avatars", max_side=800)
    except storage.UploadError as exc:
        ctx = await _account_ctx(db, user, photo_error=str(exc))
        return render(request, "account/index.html", ctx, status_code=400)
    old = user.photo_key
    user.photo_key = stored.key
    await notify(db, user, "Your profile photo was updated", "A new profile photo was uploaded to your account.",
                 category=NotificationCategory.ACCOUNT, link="/account")
    await db.commit()
    if old and old != stored.key:
        await storage.delete_key(old)
    return redirect("/account#photo", "Looking good - your new photo is saved.", "success")


# ------------------------------------------------------------------ password + sessions
@router.post("/account/password")
async def account_password(request: Request, db: DB, user: AuthUser):
    f = af.FormReader(await request.form())
    if user.role == Role.SUPERADMIN:  # SUPERADMIN_PASSWORD in the server's .env (applied at the next start)
        ctx = await _account_ctx(db, user, pw_errors={"current_password": ADMIN_PASSWORD_FIXED})
        return render(request, "account/index.html", ctx, status_code=400)
    current, new, confirm = f.secret("current_password"), f.secret("new_password"), f.secret("new_password_confirm")
    pw_errors: dict[str, str] = {}
    err = await _check_password(db, request, user, current)
    if err:
        pw_errors["current_password"] = err if current else "Enter your current password."
    pw_errors.update(af.password_errors(new, confirm, email=user.email, name=user.full_name,
                                        field="new_password", confirm_field="new_password_confirm"))
    if new and current and new == current and "new_password" not in pw_errors:
        pw_errors["new_password"] = "Choose a password that's different from your current one."
    if pw_errors:
        ctx = await _account_ctx(db, user, pw_errors=pw_errors)
        return render(request, "account/index.html", ctx, status_code=400)
    user.password_hash = await hash_password(new)
    response = redirect("/account#security", "Password updated. Every other device has been signed out.", "success")
    # every session ends - this browser's too, so a copied cookie stops working - and this browser gets a fresh one
    await renew_session(db, request, response, user)
    await notify(
        db, user, "Your password was changed",
        f"Your password was changed on {utcnow():%d %b %Y at %H:%M} UTC and every other device was signed out. "
        "If you didn't do this, reset your password immediately.",
        category=NotificationCategory.SECURITY, severity=Severity.WARNING, link="/account#security",
    )
    await audit(db, user, "account.password_change", "user", user.id, request=request)
    await db.commit()
    return response


@router.post("/account/sessions/revoke-others")
async def account_revoke_sessions(request: Request, db: DB, user: AuthUser):
    response = redirect("/account#security", "Every other device has been signed out.", "success")
    # this browser's session is renewed too: a copy of its cookie elsewhere is "another device" as well
    await renew_session(db, request, response, user)
    await notify(db, user, "Other devices signed out", "You signed out every other device that was signed in to your account.",
                 category=NotificationCategory.SECURITY, link="/account#security")
    await db.commit()
    return response


# ------------------------------------------------------------------ deletion
async def _delete_ctx(db: Any, user: User, *, errors: dict[str, str] | None = None, blockers: list[str] | None = None,
                      form: dict[str, Any] | None = None) -> dict[str, Any]:
    days = get_settings().account_retention_days
    return {"retention_days": days, "purge_date": (utcnow() + timedelta(days=days)).date(), "errors": errors or {},
            "blockers": blockers or [],
            "form": form or {"reason": "not_using", "confirm_text": "", "confirm_identity": False},
            "reasons": af.DELETE_REASONS, "unread_count": count_label(await _unread_count(db, user.id))}


@router.get("/account/delete")
async def account_delete_page(request: Request, db: DB, user: AuthUser):
    return render(request, "account/delete.html", await _delete_ctx(db, user))


@router.post("/account/delete")
async def account_delete(request: Request, db: DB, user: AuthUser):
    f = af.FormReader(await request.form())
    reason_key = f.text("reason", 40)
    reasons = dict(af.DELETE_REASONS)
    if reason_key not in reasons:
        reason_key = "other"
    confirm_text = f.text("confirm_text", 20)
    # one person per ID and e-mail, forever (app/services/trust_safety.py): the holder confirms they read it
    confirm_identity = f.flag("confirm_identity")
    form = {"reason": reason_key, "confirm_text": confirm_text, "confirm_identity": confirm_identity}
    errors: dict[str, str] = {}
    err = await _check_password(db, request, user, f.secret("password"))
    if err:
        errors["password"] = err if f.secret("password") else "Enter your password to confirm it's you."
    if confirm_text != "DELETE":
        errors["confirm_text"] = "Type DELETE in capital letters to confirm."
    if not confirm_identity:
        errors["confirm_identity"] = ("Tick the box to confirm you understand that this e-mail, phone number and ID can "
                                      "never be used for a new IronPulse account.")
    if errors:
        return render(request, "account/delete.html", await _delete_ctx(db, user, errors=errors, form=form), status_code=400)
    try:
        await soft_delete_user(db, user, reason=f"Deleted by the account holder: {reasons[reason_key]}", actor=user, request=request)
    except AccountDeletionBlocked as exc:
        # raised before anything is modified; no rollback, which would expire the signed-in user the page renders
        return render(request, "account/delete.html", await _delete_ctx(db, user, blockers=exc.reasons, form=form), status_code=400)
    await db.commit()
    days = get_settings().account_retention_days
    response = redirect("/", f"Your account has been deleted. Your data is kept for {days} days and then erased permanently.", "success")
    clear_session_cookie(response)
    return response


# ------------------------------------------------------------------ notification history (HTML)
def _list_url(flt: str, **cursor: int | None) -> str:
    params: dict[str, Any] = {"filter": "unread"} if flt == "unread" else {}
    params.update({k: v for k, v in cursor.items() if v})
    return "/account/notifications" + ("?" + urlencode(params) if params else "")


@router.get("/account/notifications")
async def notifications_page(request: Request, db: DB, user: AuthUser, filter_: str = Query("all", alias="filter")):
    """One keyset window of 20 (``?before=<id>`` = older, ``?after=<id>`` = newer), newest first. Two statements
    whatever the history size: both bounded counts, then the window (index range read, no OFFSET)."""
    flt = "unread" if filter_ == "unread" else "all"
    before = _cursor(request.query_params.get("before"))
    after = None if before else _cursor(request.query_params.get("after"))
    counts = (await db.execute(select(
        _count_upto(Notification.user_id == user.id).label("total"),
        _count_upto(Notification.user_id == user.id, Notification.read_at.is_(None)).label("unread"),
    ))).one()
    stmt = select(Notification).where(Notification.user_id == user.id)
    if flt == "unread":
        stmt = stmt.where(Notification.read_at.is_(None))
    window = await keyset(db, stmt, Notification.id, before_id=before, after_id=after, limit=PAGE_SIZE,
                          max_limit=PAGE_SIZE, order_col=Notification.created_at)
    items = list(window.items) if window.newest_first else list(reversed(window.items))  # always newest first
    if window.newest_first:
        older = items[-1].id if items and window.has_more else None
        newer = items[0].id if items and before else None
    else:
        older = items[-1].id if items else None
        newer = items[0].id if items and window.has_more else None
    return render(request, "account/notifications.html", {
        "items": items, "filter": flt, "category_icons": CATEGORY_ICONS, "page_size": PAGE_SIZE,
        "total_all": count_label(int(counts.total)), "unread_count": count_label(int(counts.unread)),
        "cursor": {"before": before, "after": after}, "paged": bool(before or after),
        "older_url": _list_url(flt, before=older) if older else None,
        "newer_url": _list_url(flt, after=newer) if newer else None,
        "newest_url": _list_url(flt),
    })


def _back_to_list(f: af.FormReader) -> str:
    """Back to the window the form was posted from (``before`` / ``after`` cursor + filter)."""
    flt = "unread" if f.text("filter", 10) == "unread" else "all"
    before = _cursor(f.text("before", 20))
    return _list_url(flt, before=before, after=None if before else _cursor(f.text("after", 20)))


@router.post("/account/notifications/read-all")
async def notifications_read_all_form(request: Request, db: DB, user: AuthUser):
    await db.execute(update(Notification).where(Notification.user_id == user.id, Notification.read_at.is_(None))
                     .values(read_at=utcnow()).execution_options(synchronize_session=False))
    await db.commit()
    return redirect("/account/notifications", "All notifications are marked as read.", "success")


@router.post("/account/notifications/{nid}/read")
async def notification_read_form(request: Request, db: DB, user: AuthUser, nid: int = Path(ge=1, le=MAX_ID)):
    f = af.FormReader(await request.form())
    n = await db.scalar(select(Notification).where(Notification.id == nid, Notification.user_id == user.id))
    if n is None:
        raise NotFound("Notification not found.")
    if n.read_at is None:
        n.read_at = utcnow()
        await db.commit()
    return redirect(_back_to_list(f))


# ------------------------------------------------------------------ JSON APIs
def _notification_json(n: Notification) -> dict[str, Any]:
    return {
        "id": n.id, "title": n.title, "body": n.body, "severity": n.severity.value, "category": n.category.value,
        "link": n.link, "created_at": n.created_at.isoformat() if n.created_at else None, "read": n.read_at is not None,
    }


@router.get("/api/notifications")
async def api_notifications(db: DB, user: AuthUser, limit: int = Query(20, ge=1, le=FEED_MAX),
                            before_id: int | None = Query(None, ge=1, le=MAX_ID)):
    """The bell feed: newest first, ``limit`` at a time; ``?before_id=<next_before_id>`` loads the next (older)
    window. Two statements: the keyset window and the bounded unread count."""
    window = await keyset(db, select(Notification).where(Notification.user_id == user.id), Notification.id,
                          before_id=before_id, limit=limit, max_limit=FEED_MAX, order_col=Notification.created_at)
    unread = await _unread_count(db, user.id)
    return {"items": [_notification_json(n) for n in window.items], "unread": min(unread, COUNT_CAP),
            "unread_capped": unread > COUNT_CAP, "has_more": window.has_more,
            "next_before_id": window.next_cursor if window.has_more else None}


@router.get("/api/notifications/unread")
async def api_notifications_unread(db: DB, user: AuthUser):
    """The island badge on page load: one bounded count, no feed."""
    unread = await _unread_count(db, user.id)
    return {"unread": min(unread, COUNT_CAP), "capped": unread > COUNT_CAP}


@router.post("/api/notifications/read-all")
async def api_notifications_read_all(db: DB, user: AuthUser):
    result = await db.execute(update(Notification).where(Notification.user_id == user.id, Notification.read_at.is_(None))
                              .values(read_at=utcnow()).execution_options(synchronize_session=False))
    await db.commit()
    return {"ok": True, "updated": int(result.rowcount or 0), "unread": 0}


@router.post("/api/notifications/{nid}/read")
async def api_notification_read(db: DB, user: AuthUser, nid: int = Path(ge=1, le=MAX_ID)):
    n = await db.scalar(select(Notification).where(Notification.id == nid, Notification.user_id == user.id))
    if n is None:
        raise NotFound("Notification not found.")
    if n.read_at is None:
        n.read_at = utcnow()
        await db.commit()
    return {"ok": True, "id": n.id, "unread": min(await _unread_count(db, user.id), COUNT_CAP)}


class LocationIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    lat: float = Field(ge=-90, le=90, allow_inf_nan=False)
    lon: float = Field(ge=-180, le=180, allow_inf_nan=False)
    accuracy: float | None = Field(default=None, ge=0, le=1_000_000, allow_inf_nan=False)
    timezone: str | None = Field(default=None, max_length=64)
    city: str | None = Field(default=None, max_length=120)


@router.post("/api/me/location")
async def api_location(payload: LocationIn, db: DB, user: AuthUser):
    if not valid_coords(payload.lat, payload.lon):
        raise HTTPException(status_code=422, detail="Those coordinates aren't valid.")
    user.latitude = round(payload.lat, 6)
    user.longitude = round(payload.lon, 6)
    user.location_updated_at = utcnow()
    tz = (payload.timezone or "").strip()
    if tz and is_valid_timezone(tz):
        user.timezone = tz
    if payload.city:
        city, err = af.validate_city(payload.city, required=False)
        if city and not err:
            user.city = city
    await db.commit()
    return {"ok": True, "timezone": user.timezone, "city": user.city}
