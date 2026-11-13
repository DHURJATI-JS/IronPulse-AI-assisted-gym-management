"""Startup bootstrap: super-admin account, storage folders, cached nav state, ban cache."""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.utils import utcnow
from app.models import User
from app.models.enums import IdStatus, NotificationCategory, Role, Severity
from app.security.passwords import hash_password, verify_password

log = logging.getLogger("ironpulse.bootstrap")


async def ensure_superadmin(db: AsyncSession) -> User | None:
    """The platform admin of SUPERADMIN_EMAIL / SUPERADMIN_PASSWORD - the credentials the owner keeps in .env.

    * No super-admin yet: the account is created.
    * The account of SUPERADMIN_EMAIL is a super-admin whose password is not SUPERADMIN_PASSWORD any more (the .env
      changed): the new password applies now - the failed-sign-in lock is cleared, every session of the account is
      signed out, the change is audited and the admin notified. The password is never logged, audited or shown.
    * SUPERADMIN_EMAIL changed to an address without an account: a super-admin with it is created; earlier super-admin
      accounts stay (the log names them - they keep their own passwords).
    * SUPERADMIN_EMAIL belongs to a member, owner or staff account: nothing changes (logged) - configuration never
      raises an account's role.
    Without SUPERADMIN_PASSWORD nothing is created or changed. The caller commits."""
    s = get_settings()
    email = (s.superadmin_email or "").strip().lower()
    password = s.superadmin_password or ""
    account = await db.scalar(select(User).where(User.email == email)) if email else None  # citext: any case
    if account is not None:
        if account.role != Role.SUPERADMIN:
            log.warning("SUPERADMIN_EMAIL belongs to a %s account - it was not made a platform admin; set another "
                        "address in .env", account.role.value)
            return await db.scalar(select(User).where(User.role == Role.SUPERADMIN).order_by(User.id).limit(1))
        if password and account.deleted_at is None and not await verify_password(account.password_hash, password):
            await _rotate_password(db, account, password)
        return account
    existing = await db.scalar(select(User).where(User.role == Role.SUPERADMIN).order_by(User.id).limit(1))
    if not password or not email:
        if existing is None:
            log.warning("No SUPERADMIN_PASSWORD set - the security centre has no admin account yet")
        return existing
    admin = User(
        email=email,
        full_name="Platform Admin",
        role=Role.SUPERADMIN,
        password_hash=await hash_password(password),
        email_verified_at=utcnow(),
        id_status=IdStatus.VERIFIED,
        timezone="UTC",
    )
    db.add(admin)
    await db.flush()
    if existing is not None:
        log.warning("SUPERADMIN_EMAIL changed: created the platform admin %s; the earlier super-admin account #%s "
                    "still exists and keeps its own password", admin.email, existing.id)
    else:
        log.info("created super-admin %s", admin.email)
    return admin


async def _rotate_password(db: AsyncSession, admin: User, password: str) -> None:
    """SUPERADMIN_PASSWORD changed in .env: apply it to the platform admin account (the value is never logged)."""
    from app.security.sessions import revoke_all_sessions
    from app.services.audit import audit
    from app.services.notify import notify

    admin.password_hash = await hash_password(password)
    admin.failed_login_count = 0
    admin.locked_until = None
    await revoke_all_sessions(db, admin.id)
    await notify(db, admin, "Your platform admin password was changed",
                 "The password set in the server's .env file (SUPERADMIN_PASSWORD) now applies to your platform admin "
                 "account, and every session was signed out. Sign in again at /superadmin/login. If you did not change "
                 "it, check who has access to the server.",
                 category=NotificationCategory.SECURITY, severity=Severity.WARNING, link="/superadmin/login")
    await audit(db, None, "superadmin.password_rotated", "user", admin.id, detail={"via": ".env", "by": "bootstrap"})
    log.info("platform admin #%s: the password from SUPERADMIN_PASSWORD was applied (sessions signed out)", admin.id)


async def bootstrap(db: AsyncSession) -> None:
    from app.core.nav import refresh_nav_state
    from app.security.bans import sync_bans_from_db
    from app.services.identity_lookup import backfill_usernames

    s = get_settings()
    for folder in (s.upload_dir, s.mail_dir, s.log_dir):
        folder.mkdir(parents=True, exist_ok=True)
    await ensure_superadmin(db)
    await backfill_usernames(db)
    await refresh_nav_state(db)
    await sync_bans_from_db(db)
    try:  # exercise library + pre-recorded videos (app content, idempotent; demo data is loaded separately)
        from app.services.content import ensure_content

        async with db.begin_nested():
            await ensure_content(db)
    except Exception as exc:  # noqa: BLE001 - never block startup on content
        log.warning("content sync skipped: %s", exc)
