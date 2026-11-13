"""Server-side sessions: random token in an HttpOnly cookie, SHA-256 of it stored server-side.

Storage (both are kept in sync):
* **Redis** (when REDIS_URL is set): ``ip:<env>:<db>:sess:<hash> -> user_id`` with a TTL, plus a per-user set
  ``ip:<env>:<db>:usess:<id>`` of session hashes for "log out everywhere" - namespaced like the shared limits
  (app/security/ratelimit.py ``namespace``), so the dev servers, the load test and each test database never share
  entries. Every request resolves the cookie through Redis and then
  reads the user's *latest* name / e-mail / photo from PostgreSQL by primary key - so the navbar always
  shows current data (Google-style account chip). The same query checks that the session row is still live, so a
  Redis entry that outlived a revocation (deleted before the revoking transaction committed, or while Redis was
  unreachable) can never sign anyone in: PostgreSQL decides, Redis only saves the session row's joins.
* **PostgreSQL** ``user_sessions`` (always): the durable record used for auditing, revocation and as
  the automatic fallback when Redis is not configured or temporarily unreachable.

One session per account (SINGLE_SESSION_PER_ACCOUNT, default on): a successful sign-in ends every other session of the
account (``end_other_sessions``, serialised per account with ``lock_account_sessions`` so two sign-ins at the same
moment leave exactly one live session - the later one). An ended session is marked by ``revoked_at`` = the replacing
session's ``created_at``: the old device's next request learns from that (``lookup_session(...).ended``) that it was
signed out by a sign-in elsewhere, on which browser and when - without a schema change and in every worker. Its live
sockets close with code 4409 and its AI work stops once the sign-in commits.

A password change and "sign out every other device" renew THIS browser's session too (``renew_session``, hunt F14): a
copy of its cookie - a shared computer, malware, a leaked log - used to stay signed in although the page said every
other device was signed out.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from fastapi import Request, Response
from sqlalchemy import delete, exists, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.config import get_settings
from app.core.utils import utcnow
from app.models import RememberedAccount, User, UserSession
from app.security.crypto import new_token, token_hash
from app.security.net import client_ip

log = logging.getLogger("ironpulse.sessions")

# WebSocket close codes a revoked session's sockets get (app/static/src/js/00-core.js never reconnects after either)
CLOSE_SIGNED_OUT = 4401
CLOSE_SIGNED_IN_ELSEWHERE = 4409
ELSEWHERE = "elsewhere"  # EndedSession.reason: replaced by a sign-in on another device
ELSEWHERE_CLOSE_REASON = "signed in elsewhere"
# pg_advisory_xact_lock key of one account's sign-ins: 'IPSS' in the high half, the user id in the low half (the
# bigint key space; other locks of the app use small constants or the two-int form, which never collide with it)
SESSION_LOCK_BASE = 0x49505353 << 32


def single_session_enabled() -> bool:
    """SINGLE_SESSION_PER_ACCOUNT (default true): a sign-in ends the account's other sessions."""
    return bool(getattr(get_settings(), "single_session_per_account", True))


def cookie_name(base: str) -> str:
    # __Host- prefix forbids Domain/Path tampering, but browsers only accept it on secure cookies
    return f"__Host-{base}" if get_settings().cookie_secure else base


SESSION_COOKIE_BASE = "ip_session"


def session_cookie_name() -> str:
    return cookie_name(SESSION_COOKIE_BASE)


def get_session_token(request: Request) -> str | None:
    return request.cookies.get(session_cookie_name())


# ------------------------------------------------------------------------- device labels
# Order matters: Edge, Opera and Samsung Internet also say "Chrome/"; Chrome on iOS and every Chromium also say
# "Safari/"; iPhones say "like Mac OS X" and Android says "Linux".
_BROWSERS = (
    ("Edg/", "Edge"), ("EdgA/", "Edge"), ("EdgiOS/", "Edge"), ("OPR/", "Opera"), ("OPiOS/", "Opera"),
    ("SamsungBrowser/", "Samsung Internet"), ("Firefox/", "Firefox"), ("FxiOS/", "Firefox"), ("CriOS/", "Chrome"),
    ("Chrome/", "Chrome"), ("Chromium/", "Chromium"), ("Safari/", "Safari"),
)
_SYSTEMS = (
    ("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("CrOS", "ChromeOS"), ("Windows", "Windows"),
    ("Macintosh", "macOS"), ("Mac OS X", "macOS"), ("Linux", "Linux"),
)


def device_label(user_agent: str | None) -> str:
    """A coarse "Chrome on macOS" from a User-Agent - a fixed vocabulary (safe to show anywhere), never the raw header."""
    ua = (user_agent or "")[:500]
    browser = next((name for token, name in _BROWSERS if token in ua), None)
    system = next((name for token, name in _SYSTEMS if token in ua), None)
    if browser and system:
        return f"{browser} on {system}"
    return browser or system or "an unknown browser"


# ------------------------------------------------------------------------- Redis helpers
_redis: Any = None
_redis_url: str | None = None
_redis_loop: Any = None
_redis_down_until = 0.0


def _prefix() -> str:
    """``ip:<env>:<db>:`` - the namespace of the shared limits without their ``sec:`` part."""
    from app.security.ratelimit import namespace

    return namespace().removesuffix("sec:")


def _key(h: str) -> str:
    return f"{_prefix()}sess:{h}"


def _ukey(uid: int) -> str:
    return f"{_prefix()}usess:{uid}"


async def get_redis() -> Any | None:
    """Shared asyncio Redis client, or None when not configured / recently unreachable."""
    global _redis, _redis_url, _redis_loop
    url = get_settings().redis_url
    if not url or time.monotonic() < _redis_down_until:
        return None
    import asyncio

    loop = asyncio.get_running_loop()
    if _redis is None or _redis_url != url or _redis_loop is not loop:  # clients are bound to one event loop
        import redis.asyncio as aioredis

        _redis = aioredis.from_url(url, decode_responses=True, socket_timeout=0.5, socket_connect_timeout=0.5,
                                   health_check_interval=30)
        _redis_url, _redis_loop = url, loop
    return _redis


def _mark_down(exc: Exception) -> None:
    global _redis_down_until
    _redis_down_until = time.monotonic() + 30
    log.warning("Redis unavailable (%s) - falling back to PostgreSQL sessions for 30 s", exc.__class__.__name__)


async def redis_status() -> dict[str, Any]:
    r = await get_redis()
    if r is None:
        return {"configured": bool(get_settings().redis_url), "ok": False}
    try:
        return {"configured": True, "ok": bool(await r.ping())}
    except Exception as exc:  # noqa: BLE001
        _mark_down(exc)
        return {"configured": True, "ok": False, "error": exc.__class__.__name__}


async def _r_set(h: str, user_id: int, ttl_seconds: int) -> None:
    r = await get_redis()
    if r is None or ttl_seconds <= 0:
        return
    try:
        async with r.pipeline(transaction=True) as p:
            p.set(_key(h), str(user_id), ex=ttl_seconds)
            p.sadd(_ukey(user_id), h)
            p.expire(_ukey(user_id), max(ttl_seconds, 60))
            await p.execute()
    except Exception as exc:  # noqa: BLE001
        _mark_down(exc)


async def _r_get(h: str) -> int | None:
    r = await get_redis()
    if r is None:
        return None
    try:
        value = await r.get(_key(h))
    except Exception as exc:  # noqa: BLE001
        _mark_down(exc)
        return None
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


async def _r_del(h: str, user_id: int | None = None) -> None:
    await _r_del_hashes(user_id, [h])


async def _r_del_hashes(user_id: int | None, hashes: list[str]) -> None:
    r = await get_redis()
    if r is None or not hashes:
        return
    try:
        async with r.pipeline(transaction=True) as p:
            p.delete(*[_key(h) for h in hashes])
            if user_id is not None:
                p.srem(_ukey(user_id), *hashes)
            await p.execute()
    except Exception as exc:  # noqa: BLE001
        _mark_down(exc)


async def _r_del_all(user_id: int, except_hash: str | None = None) -> None:
    r = await get_redis()
    if r is None:
        return
    try:
        hashes = await r.smembers(_ukey(user_id))
        doomed = [h for h in hashes if h != except_hash]
        if doomed:
            async with r.pipeline(transaction=True) as p:
                p.delete(*[_key(h) for h in doomed])
                p.srem(_ukey(user_id), *doomed)
                await p.execute()
    except Exception as exc:  # noqa: BLE001
        _mark_down(exc)


# ------------------------------------------------------------------------- public API
async def create_session(db: AsyncSession, user: User, request: Request, *, now: datetime | None = None) -> str:
    """Create a new session (DB row + Redis entry; the caller commits) and return the raw cookie token."""
    settings = get_settings()
    token = new_token(32)
    h = token_hash(token)
    now = now or utcnow()
    db.add(
        UserSession(
            id=h,
            user_id=user.id,
            created_at=now,
            last_seen_at=now,
            expires_at=now + timedelta(hours=settings.session_ttl_hours),
            ip=client_ip(request),
            user_agent=(request.headers.get("user-agent") or "")[:300],
        )
    )
    # the token only reaches the browser if the caller's commit succeeds, so a Redis entry for a
    # failed transaction is unreachable and simply expires
    await _r_set(h, user.id, settings.session_ttl_hours * 3600)
    return token


def set_session_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        session_cookie_name(),
        token,
        max_age=settings.session_ttl_hours * 3600,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(session_cookie_name(), path="/")


@dataclass(frozen=True, slots=True)
class EndedSession:
    """Why a session cookie stopped signing anyone in - only when its browser should be told: ``reason`` ELSEWHERE (a
    sign-in on another device replaced it at ``at``, from ``label`` - "Chrome on Android"). ``timezone`` is the
    account's, for showing ``at``."""

    reason: str
    user_id: int
    at: datetime
    label: str
    timezone: str = "UTC"

    def message(self) -> str:
        from app.core.utils import to_local

        local = to_local(self.at, self.timezone or "UTC") or self.at
        return (f"You were signed out because your account signed in on another device ({self.label}, "
                f"{local:%d %b %Y at %H:%M %Z}). If this was not you, reset your password.")


@dataclass(slots=True)
class SessionLookup:
    session: UserSession | None = None  # a live session whose ``.user`` is fresh from the database
    ended: EndedSession | None = None  # set when the cookie's session was replaced by a sign-in elsewhere


async def _live_user(db: AsyncSession, user_id: int, h: str, now: datetime) -> User | None:
    """The account behind a Redis hit - only while its session row is still live and the account usable (one query)."""
    live = exists().where(UserSession.id == h, UserSession.user_id == user_id, UserSession.revoked_at.is_(None),
                          UserSession.expires_at > now)
    stmt = (select(User).options(joinedload(User.customer_profile))
            .where(User.id == user_id, User.is_active.is_(True), User.deleted_at.is_(None), live))
    return (await db.execute(stmt)).unique().scalar_one_or_none()


async def _ended_elsewhere(db: AsyncSession, sess: UserSession) -> EndedSession | None:
    """A revoked session whose revocation time is exactly the creation time of a newer session of the same account was
    replaced by that sign-in (``end_other_sessions``) - "signed in on another device". Any other revocation (sign-out,
    sign out everywhere, a password change, a deletion or a ban) says nothing."""
    user = sess.user
    if sess.revoked_at is None or user is None or not user.is_active or user.deleted_at is not None:
        return None
    newer = (await db.execute(
        select(UserSession.user_agent, UserSession.created_at)
        .where(UserSession.user_id == sess.user_id, UserSession.created_at == sess.revoked_at, UserSession.id != sess.id)
        .limit(1)
    )).first()
    if newer is None:
        return None
    return EndedSession(ELSEWHERE, sess.user_id, newer.created_at, device_label(newer.user_agent), user.timezone or "UTC")


async def lookup_session(db: AsyncSession, token: str | None) -> SessionLookup:
    """Resolve a cookie token to a live session whose ``.user`` (with customer profile) is fresh from the DB - or say why
    it ended when its browser should be told (``.ended``: replaced by a sign-in on another device)."""
    if not token or len(token) > 200:
        return SessionLookup()
    h = token_hash(token)
    now = utcnow()

    user_id = await _r_get(h)
    if user_id is not None:  # fast path: Redis -> user id -> latest user row (+ the session row still live) from PG
        user = await _live_user(db, user_id, h, now)
        if user is not None:
            view = UserSession(id=h, user_id=user.id, expires_at=now, last_seen_at=now)
            view.user = user  # transient view object (never added to the session)
            return SessionLookup(view)
        await _r_del(h, user_id)  # a stale cache entry: the database below says why

    stmt = (
        select(UserSession)
        .options(joinedload(UserSession.user).joinedload(User.customer_profile))
        .where(UserSession.id == h)
    )
    sess = (await db.execute(stmt)).unique().scalar_one_or_none()
    if sess is None:
        return SessionLookup()
    if sess.revoked_at is not None:
        return SessionLookup(ended=await _ended_elsewhere(db, sess))
    if sess.expires_at <= now:
        return SessionLookup()
    user = sess.user
    if not user.is_active or user.deleted_at is not None:
        return SessionLookup()
    if (now - sess.last_seen_at) > timedelta(minutes=5):  # throttle writes
        await db.execute(update(UserSession).where(UserSession.id == sess.id).values(last_seen_at=now))
    # end the read transaction BEFORE talking to Redis (expire_on_commit=False keeps ``sess`` and its user): a stalled
    # Redis must never leave a pooled connection idle in transaction for the whole request
    await db.commit()
    await _r_set(h, user.id, int((sess.expires_at - now).total_seconds()))  # warm Redis for next time
    return SessionLookup(sess)


async def load_session(db: AsyncSession, token: str | None) -> UserSession | None:
    """Resolve a cookie token to a live session whose ``.user`` (with customer profile) is fresh from the DB."""
    return (await lookup_session(db, token)).session


async def lock_account_sessions(db: AsyncSession, user_id: int) -> None:
    """Serialise the sign-ins of one account until the caller's transaction ends (COMMIT / ROLLBACK): the second of two
    simultaneous sign-ins waits here, then sees - and ends - the first one's committed session."""
    await db.execute(text("SELECT pg_advisory_xact_lock(CAST(:k AS bigint))"), {"k": SESSION_LOCK_BASE + int(user_id)})


@dataclass(frozen=True, slots=True)
class EndedRow:
    id: str
    label: str
    ip: str | None


async def end_other_sessions(db: AsyncSession, user_id: int, *, keep_hash: str, now: datetime) -> list[EndedRow]:
    """One session per account: revoke every other live session of ``user_id`` (the caller holds
    ``lock_account_sessions`` and commits). ``now`` must be the new session's ``created_at`` - it marks these sessions as
    replaced by a sign-in elsewhere (``lookup_session(...).ended``). Their Redis entries go at once (every worker reads
    the same Redis, and a stale entry is refused by the database check anyway); after COMMIT their sockets close with
    4409 and their AI work stops, here and in the other processes."""
    rows = (await db.execute(
        update(UserSession)
        .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None), UserSession.expires_at > now,
               UserSession.id != keep_hash)
        .values(revoked_at=now)
        .returning(UserSession.id, UserSession.user_agent, UserSession.ip)
        .execution_options(synchronize_session=False)
    )).all()
    ended = [EndedRow(r.id, device_label(r.user_agent), r.ip) for r in rows]
    if ended:
        hashes = [r.id for r in ended]
        await _r_del_hashes(user_id, hashes)
        _end_revoked_sessions_after_commit(db, user_id, hashes, code=CLOSE_SIGNED_IN_ELSEWHERE,
                                           reason=ELSEWHERE_CLOSE_REASON)
    return ended


async def revoke_session(db: AsyncSession, token: str | None, *, end_live: bool = True) -> None:
    """Revoke one session (the caller commits) - e.g. the browser's previous session when someone signs in on it.
    Once that COMMITs its live sockets are closed and its AI work stops, like any revoked session's
    (``end_live``; the sign-out route passes False because it does that itself, right after its commit)."""
    if not token:
        return
    h = token_hash(token)
    user_id = await db.scalar(update(UserSession).where(UserSession.id == h, UserSession.revoked_at.is_(None))
                              .values(revoked_at=utcnow()).returning(UserSession.user_id))
    await _r_del(h)
    if end_live and user_id is not None:
        _end_revoked_sessions_after_commit(db, user_id, [h])


async def revoke_all_sessions(db: AsyncSession, user_id: int, *, except_token: str | None = None,
                              forget_devices: bool = True) -> None:
    """Sign the account out everywhere (except the session of ``except_token``; the caller commits): sign out
    everywhere, a password change or reset, account deletion, a ban. ``forget_devices`` (default) also revokes the
    account's remembered-account references of the sign-in page (app/services/remembered_accounts.py) - except the one
    issued with the kept session, the device the person is using."""
    stmt = update(UserSession).where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
    except_hash = token_hash(except_token) if except_token else None
    if except_hash:
        stmt = stmt.where(UserSession.id != except_hash)
    revoked = list((await db.execute(stmt.values(revoked_at=utcnow()).returning(UserSession.id))).scalars().all())
    await _r_del_all(user_id, except_hash)
    if forget_devices:
        doomed = delete(RememberedAccount).where(RememberedAccount.user_id == user_id)
        if except_hash:
            doomed = doomed.where(or_(RememberedAccount.session_id.is_(None), RememberedAccount.session_id != except_hash))
        await db.execute(doomed.execution_options(synchronize_session=False))
    _end_revoked_sessions_after_commit(db, user_id, revoked)


async def renew_session(db: AsyncSession, request: Request, response: Response, user: User) -> str:
    """After a password change or "sign out every other device" (the caller commits): EVERY session of the account ends -
    this browser's too, so a copy of its cookie (a shared or public computer, malware, a leaked log) stops working at
    once - and this browser carries on with a fresh session set on ``response`` (OWASP: renew the session after a
    credential change). Returns the new raw token.

    The other devices' remembered-account references go (``revoke_all_sessions``); this device's follows the new
    session, so its account-chooser entry survives. Serialised with the account's sign-ins (``lock_account_sessions``).
    The ended sessions are marked revoked BEFORE the new one is created, so none of them reads as "signed in on another
    device" (``_ended_elsewhere`` matches revoked_at to a newer session's created_at)."""
    await lock_account_sessions(db, user.id)
    old = get_session_token(request)
    old_hash = token_hash(old) if old else None
    await revoke_all_sessions(db, user.id, except_token=old)  # the other devices (+ their remembered references)
    if old:
        await revoke_session(db, old)  # this browser's own: its live sockets close as for any revoked session
    raw = await create_session(db, user, request, now=utcnow())
    if old_hash:
        await db.execute(update(RememberedAccount)
                         .where(RememberedAccount.user_id == user.id, RememberedAccount.session_id == old_hash)
                         .values(session_id=token_hash(raw)).execution_options(synchronize_session=False))
    set_session_cookie(response, raw)
    return raw


def _end_revoked_sessions_after_commit(db: AsyncSession, user_id: int, session_hashes: list[str], *,
                                       code: int = CLOSE_SIGNED_OUT, reason: str = "signed out") -> None:
    """Once the revocation COMMITs: close the sockets still open on those sessions (here and, through the PG bridge, in
    other processes, which also stop the sessions' AI work there) with ``code`` - 4401 signed out, 4409 signed in on
    another device - and stop their AI work - with nothing of the user left that is an immediate exit
    (app/services/presence.py). That includes a browser's session replaced by someone else signing in on it
    (``revoke_session`` from the session rotation): the previous user's tabs must not keep receiving their live events
    on a browser that is no longer theirs."""
    if not session_hashes:
        return
    from app.services.realtime import call_after_commit

    hashes = list(session_hashes)

    def ended() -> None:
        from app.services.presence import presence
        from app.services.realtime import hub

        hub.close_sessions(hashes, code=code, reason=reason, user_id=user_id, exit_reason="signed_out")
        presence.sessions_revoked(user_id, hashes, reason="signed_out")

    call_after_commit(db, ended)
