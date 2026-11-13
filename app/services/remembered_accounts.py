"""Remembered accounts of the sign-in page - the account chooser ("like Google").

What the browser keeps (localStorage, app/static/src/js/02-accounts.js): a random device key and, per remembered
account, ONLY an opaque random reference (``new_token(32)``, 256 bits) - never the account's database id (sequential ids
would let anyone enumerate members' names and photos), a name, an e-mail, a photo or a password.

What the server keeps (``remembered_accounts``, app/models/remembered.py): the SHA-256 of each reference with the
account, the SHA-256 of the device key, the session it was issued with, a coarse "Chrome on macOS" label and
created / last-used times.

Rules
* Issued only after a successful password sign-in with "Remember this account on this device" ticked (the default) and
  a valid device key (the page's script sends it; without JavaScript nothing could keep the reference anyway). One
  reference per account per device (a new sign-in replaces it), at most MAX_PER_DEVICE per device and MAX_PER_USER per
  account (the least recently used go first), unused for UNUSED_DAYS = gone. Race-safe: the account's sign-in lock
  (sessions.lock_account_sessions) and a per-device advisory lock are held while the caps are applied.
* The chooser asks ``POST /auth/remembered`` (at most LOOKUP_MAX_REFS references, LOOKUPS_PER_MINUTE per address) and
  gets, per reference, the CURRENT name, a masked e-mail and the current photo through a signed URL that expires after
  PHOTO_URL_SECONDS - or ``{"state": "gone"}``, identical for an unknown, revoked, expired, banned or deleted account
  (one query for the whole batch, so the timing does not tell them apart either). The page removes every gone entry.
* Choosing an account signs in with the reference instead of the e-mail (``resolve``) - the password is always asked
  and checked, the sign-in lockout and one-session-per-account apply as for any sign-in.
* Revoked: "Remove from this device" (``forget``), sign out everywhere, a password change or reset, account deletion and
  a ban (sessions.revoke_all_sessions ``forget_devices``); the account's purge deletes the rows (ON DELETE CASCADE).
* Never a platform admin: super-admins sign in only at /superadmin/login, which never offers or stores a reference;
  a reference of one (stored before that rule) is "gone" like any other refused account.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from fastapi import Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.utils import initials, sha256_hex, utcnow
from app.models import RememberedAccount, User
from app.models.enums import Role
from app.security.crypto import new_token, token_hash
from app.security.sessions import cookie_name, device_label, lock_account_sessions

MAX_PER_DEVICE = 5  # remembered accounts one browser may hold (the chooser shows them all)
MAX_PER_USER = 10  # devices one account may be remembered on
UNUSED_DAYS = 180  # a reference not used to sign in for this long is gone
LOOKUP_MAX_REFS = 5  # references per chooser lookup
LOOKUPS_PER_MINUTE = 30  # chooser lookups + removals per network address
PHOTO_URL_SECONDS = 600  # lifetime of a signed photo URL

REF_RE = re.compile(r"^[A-Za-z0-9_-]{40,64}$")
DEVICE_RE = re.compile(r"^[A-Za-z0-9_-]{22,64}$")
HANDOFF_COOKIE_BASE = "ip_rmb"  # hands a new reference to the page's script after the sign-in redirect (5 minutes)
HANDOFF_SECONDS = 300
_DEVICE_LOCK_SALT = "ironpulse:remembered-device:"


def valid_ref(raw: Any) -> bool:
    return isinstance(raw, str) and bool(REF_RE.fullmatch(raw))


def valid_device(raw: Any) -> bool:
    return isinstance(raw, str) and bool(DEVICE_RE.fullmatch(raw))


def handoff_cookie_name() -> str:
    return cookie_name(HANDOFF_COOKIE_BASE)


def set_handoff_cookie(response: Response, raw_ref: str) -> None:
    """The new reference for the page's script (not HttpOnly on purpose: the script moves it into localStorage and
    deletes the cookie). SameSite=Strict, 5 minutes, Secure + __Host- with COOKIE_SECURE."""
    settings = get_settings()
    response.set_cookie(handoff_cookie_name(), raw_ref, max_age=HANDOFF_SECONDS, httponly=False,
                        secure=settings.cookie_secure, samesite="strict", path="/")


def mask_email(email: str | None) -> str | None:
    """``ab***@gmail.com`` - enough to recognise one's own address, never the whole local part."""
    local, at, domain = (email or "").partition("@")
    if not at or not local or not domain:
        return None
    keep = 2 if len(local) > 3 else 1
    return f"{local[:keep]}***@{domain}"


def _photo_signer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt="ironpulse-remembered-photo")


def photo_url(photo_key: str | None) -> str | None:
    if not photo_key or not photo_key.startswith("public/"):
        return None
    return f"/auth/remembered/photo/{_photo_signer().dumps(photo_key)}"


def photo_key_from(token: str) -> str | None:
    """The photo key of a signed URL that is still valid (PHOTO_URL_SECONDS), else None."""
    if not token or len(token) > 400:
        return None
    try:
        key = _photo_signer().loads(token, max_age=PHOTO_URL_SECONDS)
    except (SignatureExpired, BadSignature):
        return None
    return key if isinstance(key, str) and key.startswith("public/") else None


def _cutoff(now: datetime) -> datetime:
    return now - timedelta(days=UNUSED_DAYS)


async def remember(db: AsyncSession, user: User, *, device_id: str, session_hash: str | None, user_agent: str | None,
                   now: datetime | None = None) -> str:
    """Issue a reference for ``user`` on the device ``device_id`` (the caller commits) and return the raw value.
    Never for a platform admin (they sign in at /superadmin/login only): ValueError."""
    if user.role == Role.SUPERADMIN:
        raise ValueError("platform admin accounts are never remembered on a device")
    now = now or utcnow()
    device = sha256_hex(device_id)
    await lock_account_sessions(db, user.id)  # re-entrant: start_session already holds it with one session per account
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": _DEVICE_LOCK_SALT + device})
    ra = RememberedAccount
    # one reference per account per device (this sign-in replaces it) + this account's expired ones
    await db.execute(delete(ra).where(ra.user_id == user.id, (ra.device_hash == device) | (ra.last_used_at < _cutoff(now)))
                     .execution_options(synchronize_session=False))
    # room for the new one: at most MAX_PER_DEVICE on this device, MAX_PER_USER for this account (least recently used go)
    for column, value, cap in ((ra.device_hash, device, MAX_PER_DEVICE), (ra.user_id, user.id, MAX_PER_USER)):
        surplus = select(ra.id).where(column == value).order_by(ra.last_used_at.desc(), ra.created_at.desc()).offset(cap - 1)
        await db.execute(delete(ra).where(ra.id.in_(surplus)).execution_options(synchronize_session=False))
    raw = new_token(32)
    db.add(ra(id=token_hash(raw), user_id=user.id, device_hash=device, session_id=session_hash,
              label=device_label(user_agent)[:80], created_at=now, last_used_at=now))
    await db.flush()
    return raw


async def resolve(db: AsyncSession, raw_ref: str | None) -> User | None:
    """The account a still-valid reference points at (for signing in with a chosen account) - None for anything else,
    as in ``lookup``: a banned or deleted account's reference is gone; the caller treats None exactly like an unknown
    e-mail."""
    if not valid_ref(raw_ref):
        return None
    now = utcnow()
    return await db.scalar(
        select(User).join(RememberedAccount, RememberedAccount.user_id == User.id)
        .where(RememberedAccount.id == token_hash(raw_ref), RememberedAccount.last_used_at > _cutoff(now),
               User.is_active.is_(True), User.deleted_at.is_(None), User.role != Role.SUPERADMIN)
    )


async def touch(db: AsyncSession, raw_ref: str | None, *, session_hash: str | None) -> None:
    """A sign-in through this reference: it was used now, by this session (caller commits)."""
    if not valid_ref(raw_ref):
        return
    await db.execute(
        RememberedAccount.__table__.update().where(RememberedAccount.id == token_hash(raw_ref))
        .values(last_used_at=utcnow(), session_id=session_hash)
    )


async def lookup(db: AsyncSession, refs: list[str]) -> list[dict[str, Any]]:
    """Current details per reference (input order, duplicates dropped, at most LOOKUP_MAX_REFS) - or gone."""
    wanted: list[str] = []
    for ref in refs:
        if ref not in wanted:
            wanted.append(ref)
    wanted = wanted[:LOOKUP_MAX_REFS]
    hashes = {token_hash(r): r for r in wanted if valid_ref(r)}
    found: dict[str, Any] = {}
    if hashes:
        now = utcnow()
        rows = await db.execute(
            select(RememberedAccount.id, User.full_name, User.email, User.photo_key)
            .join(User, User.id == RememberedAccount.user_id)
            .where(RememberedAccount.id.in_(list(hashes)), RememberedAccount.last_used_at > _cutoff(now),
                   User.is_active.is_(True), User.deleted_at.is_(None), User.role != Role.SUPERADMIN)
        )
        found = {row.id: row for row in rows}
    out: list[dict[str, Any]] = []
    for ref in wanted:
        row = found.get(token_hash(ref)) if valid_ref(ref) else None
        if row is None:
            out.append({"ref": ref, "state": "gone"})
            continue
        out.append({"ref": ref, "state": "ok", "name": row.full_name, "initials": initials(row.full_name),
                    "email": mask_email(row.email), "photo": photo_url(row.photo_key)})
    return out


async def forget(db: AsyncSession, raw_ref: str | None) -> None:
    """"Remove from this device": the reference stops working at once (caller commits). Silent for unknown values."""
    if valid_ref(raw_ref):
        await db.execute(delete(RememberedAccount).where(RememberedAccount.id == token_hash(raw_ref))
                         .execution_options(synchronize_session=False))
