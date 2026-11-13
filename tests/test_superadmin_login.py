"""Platform admin sign-in (/superadmin/login, app/security/admin_login.py, app/routers/superadmin_login.py).

Super-admins sign in ONLY at /superadmin/login: on every normal sign-in path (sign-in form, account chooser, password
reset, invitations, forgot-username) their identifier behaves exactly like an unknown one. Anyone else is refused on the
admin page with the neutral error of a wrong password. Failed attempts per client address - on the admin page, and any
attempt with a super-admin identifier on the normal page - are counted by every worker together; the 5th bans the address
through the ordinary ip_bans table (loopback never). A success resets the count; ``manage.py unban`` lifts a ban from the
server's terminal; SUPERADMIN_ALLOWED_IPS limits the page; SUPERADMIN_PASSWORD changes in .env apply at start-up.
"""

from __future__ import annotations

import html
import logging
import re
from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.core.utils import utcnow
from app.models import AuditLog, AuthToken, EmailOutbox, IpBan, Notification, RememberedAccount, SecurityEvent, User, UserSession
from app.models.enums import Role, TokenPurpose
from app.security import admin_login
from app.security.bans import ban_registry
from app.security.crypto import new_token, token_hash
from app.security.passwords import verify_password
from app.security.ratelimit import SharedStore
from tests.conftest import login_as, make_client, post_form
from tests.factories import DEFAULT_PASSWORD, make_gym, make_owner, make_user

ADMIN_EMAIL = "root.admin@example.com"
ATTACKER = "203.0.113.50"
GENERIC = "Those sign-in details didn't work"


# ====================================================================== helpers
async def make_admin(db, **kw: Any) -> User:
    return await make_user(db, role=Role.SUPERADMIN, email=kw.pop("email", ADMIN_EMAIL), full_name="Platform Admin", **kw)


async def admin_post(client: httpx.AsyncClient, identifier: str, password: str, **extra: str) -> httpx.Response:
    return await post_form(client, "/superadmin/login", {"identifier": identifier, "password": password, **extra})


async def member_post(client: httpx.AsyncClient, identifier: str, password: str) -> httpx.Response:
    return await post_form(client, "/auth/login", {"identifier": identifier, "password": password})


def alert(r: httpx.Response) -> str:
    """The page's error block (text only) - what the person is told."""
    m = re.search(r'<div class="alert alert-(?:danger|warning)[^"]*"[^>]*role="alert"[^>]*>(.*?)</div>', r.text, re.S)
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", m.group(1))).split()) if m else ""


def neutral_page(r: httpx.Response) -> str:
    """The sign-in page with what legitimately differs per request blanked: CSRF token, submit id, CSP nonce, the echoed
    identifier."""
    text = re.sub(r'name="(csrf_token|submit_id)" value="[^"]*"', r'name="\1" value=""', r.text)
    text = re.sub(r'nonce="[^"]*"', 'nonce=""', text)
    text = re.sub(r'(<meta name="csrf-token" content=")[^"]*', r"\1", text)
    text = re.sub(r'(id="f-identifier" name="identifier" type="[a-z]+" value=")[^"]*', r"\1", text)
    return text


async def count(db, model, *where) -> int:
    return int(await db.scalar(select(func.count()).select_from(model).where(*where)) or 0)


async def bans_of(db, ip: str) -> list[IpBan]:
    return list((await db.execute(select(IpBan).where(IpBan.ip == ip).order_by(IpBan.id))).scalars().all())


@pytest.fixture(autouse=True)
def _pinned(monkeypatch):
    s = get_settings()
    for name, value in (("superadmin_login_max_failures", 5), ("superadmin_login_window_minutes", 15),
                        ("superadmin_login_ban_hours", 24), ("superadmin_allowed_ips", []),
                        ("login_max_failures", 5), ("login_lockout_minutes", 15)):
        monkeypatch.setattr(s, name, value)


# ====================================================================== the admin page
async def test_admin_signs_in_on_the_admin_page(db):
    admin = await make_admin(db)
    async with make_client() as c:
        page = await c.get("/superadmin/login")
        assert page.status_code == 200 and "Platform admin sign-in" in page.text
        assert 'name="remember"' not in page.text  # never offered to the account chooser
        r = await admin_post(c, ADMIN_EMAIL, DEFAULT_PASSWORD)
        assert r.status_code == 303 and r.headers["location"] == "/superadmin"
        assert (await c.get("/superadmin")).status_code == 200
        assert (await c.get("/superadmin/login")).headers.get("location") == "/superadmin"  # signed in already
    assert await count(db, RememberedAccount, RememberedAccount.user_id == admin.id) == 0
    rows = (await db.execute(select(AuditLog).where(AuditLog.action == admin_login.AUDIT_OK))).scalars().all()
    assert len(rows) == 1 and rows[0].actor_id == admin.id


async def test_next_stays_inside_the_admin_area(db):
    await make_admin(db)
    async with make_client() as c:
        r = await c.get("/auth/login?next=%2Fsuperadmin%2Faudit")
        assert r.status_code == 303 and r.headers["location"] == "/superadmin/login?next=%2Fsuperadmin%2Faudit"
        r = await admin_post(c, ADMIN_EMAIL, DEFAULT_PASSWORD, next="/superadmin/audit")
        assert r.headers["location"] == "/superadmin/audit"
    async with make_client() as c:
        r = await admin_post(c, ADMIN_EMAIL, DEFAULT_PASSWORD, next="/owner")
        assert r.headers["location"] == "/superadmin"
    async with make_client() as c:  # an anonymous visit of an admin page ends on the admin sign-in page
        r = await c.get("/superadmin/audit")
        assert r.status_code == 303 and r.headers["location"].startswith("/auth/login?next=")
        r = await c.get(r.headers["location"])
        assert r.status_code == 303 and r.headers["location"].startswith("/superadmin/login?next=")


async def test_non_admin_accounts_get_the_neutral_error_even_with_the_right_password(db):
    owner = await make_owner(db, email="owner.one@example.com")
    member = await make_user(db, email="member.one@example.com")
    await make_admin(db)
    async with make_client() as c:
        unknown = await admin_post(c, "nobody@example.com", DEFAULT_PASSWORD)
        wrong = await admin_post(c, ADMIN_EMAIL, "Wrong-Passw0rd!")
        as_owner = await admin_post(c, owner.email, DEFAULT_PASSWORD)
        as_member = await admin_post(c, member.email, DEFAULT_PASSWORD)
    for r in (unknown, wrong, as_owner, as_member):
        assert r.status_code == 400 and "session" not in "".join(r.headers.get_list("set-cookie")).lower().split("=")[0]
        assert GENERIC in alert(r)
    assert neutral_page(unknown) == neutral_page(as_owner) == neutral_page(as_member)
    assert alert(unknown) == alert(wrong) == alert(as_owner)
    assert await count(db, UserSession, UserSession.user_id == owner.id) == 0
    reasons = sorted(r.detail["reason"] for r in (await db.execute(
        select(AuditLog).where(AuditLog.action == admin_login.AUDIT_FAILED))).scalars())
    assert reasons == ["not_admin", "not_admin", "unknown", "wrong_password"]  # audited (the true reason, admins only)


async def test_every_attempt_is_audited_without_the_password(db):
    await make_admin(db)
    secret = "Sup3r-Secret-Typed!"
    async with make_client() as c:
        await admin_post(c, ADMIN_EMAIL, secret)
        await member_post(c, ADMIN_EMAIL, secret)
    rows = (await db.execute(select(AuditLog).where(AuditLog.action.like("superadmin.login%")))).scalars().all()
    assert {r.action for r in rows} == {admin_login.AUDIT_FAILED, admin_login.AUDIT_REFUSED}
    for r in rows:
        assert secret not in str(r.detail) and ADMIN_EMAIL not in str(r.detail)
        assert r.detail["identifier"].startswith("ro***@")


# ====================================================================== the normal sign-in paths refuse admins
async def test_admin_credentials_fail_on_the_member_sign_in_like_an_unknown_account(db, monkeypatch):
    admin = await make_admin(db)
    calls: list[Any] = []

    async def counting(hash_: Any, password: str) -> bool:
        calls.append(hash_)
        return await verify_password(hash_, password)

    import app.routers.auth as auth_router

    monkeypatch.setattr(auth_router, "verify_password", counting)
    async with make_client() as c:
        as_admin = await member_post(c, ADMIN_EMAIL, DEFAULT_PASSWORD)
        as_unknown = await member_post(c, "ghost.person@example.com", DEFAULT_PASSWORD)
        by_username = await member_post(c, f"IP-{admin.id:06d}", DEFAULT_PASSWORD)
    assert as_admin.status_code == as_unknown.status_code == by_username.status_code == 400
    assert alert(as_admin) == alert(as_unknown) == alert(by_username) != ""
    assert neutral_page(as_admin) == neutral_page(as_unknown)  # nothing on the page tells them apart
    assert as_admin.text.count("/superadmin/login") == as_unknown.text.count("/superadmin/login") == 1  # the footer only
    assert calls == [None, None, None]  # the same Argon2 work: the dummy hash, never the admin's real one
    assert await count(db, UserSession, UserSession.user_id == admin.id) == 0
    refused = (await db.execute(select(AuditLog).where(AuditLog.action == admin_login.AUDIT_REFUSED))).scalars().all()
    assert len(refused) == 2 and {r.entity_id for r in refused} == {admin.id}


async def test_admins_are_never_offered_by_the_account_chooser(db):
    from app.services import remembered_accounts as remembered

    admin = await make_admin(db)
    raw = new_token(32)
    db.add(RememberedAccount(id=token_hash(raw), user_id=admin.id, device_hash="d" * 64, label="Chrome on macOS",
                             created_at=utcnow(), last_used_at=utcnow()))
    await db.commit()
    async with make_client() as c:
        from tests.conftest import api

        r = await api(c, "POST", "/auth/remembered", json={"refs": [raw]})
        assert r.json()["accounts"] == [{"ref": raw, "state": "gone"}]
        r = await post_form(c, "/auth/login", {"account_ref": raw, "password": DEFAULT_PASSWORD})
        assert r.status_code == 400 and GENERIC in alert(r)
    assert await remembered.resolve(db, raw) is None
    with pytest.raises(ValueError):
        await remembered.remember(db, admin, device_id="x" * 30, session_hash=None, user_agent="pytest")


async def test_password_reset_and_email_change_are_disabled_for_admins(db):
    admin = await make_admin(db)
    member = await make_user(db, email="reset.me@example.com")
    async with make_client() as c:
        r1 = await post_form(c, "/auth/forgot", {"identifier": ADMIN_EMAIL})
        r2 = await post_form(c, "/auth/forgot", {"identifier": "unknown.person@example.com"})
        r3 = await post_form(c, "/auth/forgot", {"identifier": member.email})
    assert r1.status_code == r2.status_code == r3.status_code == 303
    assert r1.headers["location"] == r2.headers["location"] == "/auth/forgot?sent=1"  # the neutral answer
    assert await count(db, AuthToken, AuthToken.user_id == admin.id) == 0
    assert await count(db, AuthToken, AuthToken.user_id == member.id) == 1
    assert await count(db, EmailOutbox, EmailOutbox.to_email == ADMIN_EMAIL) == 0
    # a reset link issued for an admin before this rule never works
    from app.services import auth_flows as af

    token = await af.issue_token(db, TokenPurpose.PASSWORD_RESET, user_id=admin.id, email=admin.email)
    await db.commit()
    async with make_client() as c:
        assert (await c.get(f"/auth/reset?token={token}")).status_code == 400
        r = await post_form(c, "/auth/reset", {"token": token, "password": "N3w-Strong-Pass!", "password_confirm": "N3w-Strong-Pass!"})
        assert r.status_code == 400
        await login_as(c, admin)
        r = await post_form(c, "/account/password", {"current_password": DEFAULT_PASSWORD, "new_password": "N3w-Strong-Pass!",
                                                     "new_password_confirm": "N3w-Strong-Pass!"})
        assert r.status_code == 400 and "SUPERADMIN_PASSWORD" in r.text
        r = await post_form(c, "/account", {"full_name": "Platform Admin", "email": "new.admin@example.com", "timezone": "UTC"})
        assert r.status_code == 400 and "SUPERADMIN_EMAIL" in r.text
    fresh = await db.get(User, admin.id, populate_existing=True)
    assert fresh.email == ADMIN_EMAIL and await verify_password(fresh.password_hash, DEFAULT_PASSWORD)
    async with make_client() as c:  # forgot-username e-mails nothing to an admin either
        await post_form(c, "/auth/forgot-username", {"contact": ADMIN_EMAIL})
    assert await count(db, Notification, Notification.user_id == admin.id, Notification.title == "Your sign-in details") == 0


async def test_invitations_never_sign_an_admin_in(db):
    from app.services import auth_flows as af

    admin = await make_admin(db)
    token = await af.issue_token(db, TokenPurpose.INVITE, user_id=admin.id, email=admin.email)
    await db.commit()
    async with make_client() as c:
        assert (await c.get(f"/auth/invite?token={token}")).status_code == 400
        r = await post_form(c, "/auth/invite", {"token": token, "password": "N3w-Strong-Pass!",
                                                "password_confirm": "N3w-Strong-Pass!", "accept_terms": "1"})
        assert r.status_code == 400
    assert await count(db, UserSession, UserSession.user_id == admin.id) == 0


async def test_the_member_sign_in_page_links_the_admin_page(db):
    async with make_client() as c:
        r = await c.get("/auth/login")
    assert 'href="/superadmin/login"' in r.text and "Platform admin sign-in" in r.text


# ====================================================================== lockout + IP ban
async def test_admin_account_locks_after_failed_attempts(db):
    admin = await make_admin(db)
    async with make_client() as c:  # loopback: counted, never banned - the account lock still applies
        for _ in range(5):
            r = await admin_post(c, ADMIN_EMAIL, "Wrong-Passw0rd!")
        assert r.status_code == 400 and "paused" in alert(r)
        r = await admin_post(c, ADMIN_EMAIL, DEFAULT_PASSWORD)  # the right password while locked: refused
        assert r.status_code == 400 and "paused" in alert(r)
    fresh = await db.get(User, admin.id, populate_existing=True)
    assert fresh.locked_until is not None and fresh.locked_until > utcnow()
    assert await count(db, IpBan) == 0


async def test_the_fifth_failure_bans_the_address(db):
    admin = await make_admin(db)
    async with make_client(ATTACKER) as c:
        for n in range(1, 5):
            r = await admin_post(c, f"guess{n}@example.com", "Wrong-Passw0rd!")
            assert r.status_code == 400, n
            assert await count(db, IpBan) == 0, n
        assert "1 more failed attempt" in r.text
        r = await admin_post(c, ADMIN_EMAIL, "Wrong-Passw0rd!")  # the 5th
        assert r.status_code == 403 and "Access blocked" in r.text
        bans = await bans_of(db, ATTACKER)
        assert len(bans) == 1 and bans[0].created_by == admin_login.BAN_CREATED_BY and bans[0].lifted_at is None
        hours = (bans[0].banned_until - utcnow()).total_seconds() / 3600
        assert 23.9 < hours <= 24.0
        # refused everywhere by the gate - every page, not just the sign-in
        assert (await c.get("/")).status_code == 403
        assert (await c.get("/gyms")).status_code == 403
    assert ban_registry.get(ATTACKER) is not None
    assert await count(db, SecurityEvent, SecurityEvent.ip == ATTACKER, SecurityEvent.action == "banned") == 1
    assert await count(db, AuditLog, AuditLog.action == admin_login.AUDIT_BAN) == 1
    assert await count(db, Notification, Notification.user_id == admin.id,
                       Notification.link == f"/superadmin/security/ip/{ATTACKER}") == 1


async def test_a_banned_address_is_refused_by_the_admin_page_on_every_worker(db):
    """Another worker has not synced the ban yet (empty registry): the admin page asks the database itself."""
    await make_admin(db)
    db.add(IpBan(ip=ATTACKER, reason="earlier", severity=4, banned_until=utcnow() + timedelta(hours=2),
                 created_by="admin_login"))
    await db.commit()
    assert ban_registry.get(ATTACKER) is None
    async with make_client(ATTACKER) as c:
        r = await admin_post(c, ADMIN_EMAIL, DEFAULT_PASSWORD)
        assert r.status_code == 403 and "Access blocked" in r.text  # the right password changes nothing
        assert (await c.get("/")).status_code == 403  # ... and this worker's gate now refuses it too
    assert await count(db, UserSession) == 0


async def test_admin_identifiers_on_the_member_sign_in_count_towards_the_ban(db):
    await make_admin(db)
    async with make_client(ATTACKER) as c:
        for _ in range(2):
            await admin_post(c, "someone@example.com", "Wrong-Passw0rd!")
        for n in range(2):
            r = await member_post(c, ADMIN_EMAIL, DEFAULT_PASSWORD)
            assert r.status_code == 400 and GENERIC in alert(r)
        assert await count(db, IpBan) == 0
        r = await member_post(c, ADMIN_EMAIL, "anything-at-all")  # the 5th failure
        assert r.status_code == 400  # the member page's answer never changes (no oracle) ...
    assert len(await bans_of(db, ATTACKER)) == 1  # ... but the address is banned
    async with make_client(ATTACKER) as c:
        assert (await c.get("/auth/login")).status_code == 403


async def test_loopback_is_counted_but_never_banned(db):
    await make_admin(db)
    for ip in ("127.0.0.1", "::1"):
        async with make_client(ip) as c:
            for _ in range(7):
                r = await admin_post(c, "nobody@example.com", "Wrong-Passw0rd!")
            assert r.status_code == 400 and "blocked" not in alert(r)
            assert (await c.get("/superadmin/login")).status_code == 200
    assert await count(db, IpBan) == 0
    skipped = (await db.execute(select(AuditLog).where(AuditLog.action == admin_login.AUDIT_BAN_SKIPPED))).scalars().all()
    assert skipped and {r.detail["why"] for r in skipped} == {"loopback"}
    assert await count(db, AuditLog, AuditLog.action == admin_login.AUDIT_FAILED) == 14  # every attempt counted / audited


async def test_a_successful_sign_in_resets_the_count(db):
    await make_admin(db)
    async with make_client(ATTACKER) as c:
        for _ in range(4):
            await admin_post(c, "nobody@example.com", "Wrong-Passw0rd!")
        assert await admin_login.failures.count(ATTACKER) == 4
        r = await admin_post(c, ADMIN_EMAIL, DEFAULT_PASSWORD)
        assert r.status_code == 303
    assert await admin_login.failures.count(ATTACKER) == 0
    async with make_client(ATTACKER) as c:
        for _ in range(4):
            r = await admin_post(c, "nobody@example.com", "Wrong-Passw0rd!")
            assert r.status_code == 400
    assert await count(db, IpBan) == 0


async def test_the_cli_unban_lifts_the_ban(db):
    from scripts import manage

    await make_admin(db)
    async with make_client(ATTACKER) as c:
        for _ in range(5):
            await admin_post(c, "nobody@example.com", "Wrong-Passw0rd!")
        assert (await c.get("/")).status_code == 403
    lifted = await manage.unban_address(db, ATTACKER)
    await db.commit()
    assert len(lifted) == 1
    assert (await bans_of(db, ATTACKER))[0].lifted_at is not None
    assert ban_registry.get(ATTACKER) is None
    assert await count(db, AuditLog, AuditLog.action == "security.ban_lift") == 1
    async with make_client(ATTACKER) as c:
        assert (await c.get("/")).status_code == 200
        assert (await c.get("/superadmin/login")).status_code == 200
    with pytest.raises(manage.CommandError):
        await manage.unban_address(db, "not-an-ip")
    assert await manage.unban_address(db, "198.51.100.1") == []  # nothing banned: nothing lifted


def test_the_cli_lists_unban():
    from scripts import manage

    text = manage.build_parser().format_help()
    assert "unban" in text
    assert manage.main(["unban", "999.1.1.1"]) == 2


# ====================================================================== shared counting (several workers)
async def test_the_count_is_shared_by_every_worker():
    url = get_settings().redis_url
    if not url:
        pytest.skip("REDIS_URL is not set")
    import redis.asyncio as aioredis

    clients = [aioredis.from_url(url, decode_responses=True, socket_timeout=2, socket_connect_timeout=2)
               for _ in range(2)]
    try:
        try:
            await clients[0].ping()
        except Exception:  # noqa: BLE001
            pytest.skip("Redis is not reachable")

        def store(client: Any) -> SharedStore:
            async def get() -> Any:
                return client

            return SharedStore(client=get)

        a, b = (admin_login.AdminLoginFailures(store=store(cl)) for cl in clients)
        ip = "198.51.100.77"
        await a.reset(ip)
        assert [await a.fail(ip), await b.fail(ip), await a.fail(ip), await b.fail(ip)] == [1, 2, 3, 4]
        assert await b.count(ip) == 4
        assert await a.fail(ip) == 5  # the 5th - on whichever worker - and the window starts over
        assert await b.count(ip) == 0
        await a.reset(ip)
    finally:
        for cl in clients:
            await cl.aclose()


async def test_without_redis_each_worker_counts_in_memory():
    async def none() -> Any:
        return None

    w = admin_login.AdminLoginFailures(store=SharedStore(client=none))
    assert [await w.fail("198.51.100.9") for _ in range(4)] == [1, 2, 3, 4]
    assert await w.fail("198.51.100.9") == 5 and await w.count("198.51.100.9") == 0


# ====================================================================== allow-list + sessions
async def test_allowed_ips_limit_the_page_and_the_console(db, monkeypatch):
    admin = await make_admin(db)
    monkeypatch.setattr(get_settings(), "superadmin_allowed_ips", ["10.0.0.0/8", "192.0.2.10"])
    async with make_client("203.0.113.9") as c:
        assert (await c.get("/superadmin/login")).status_code == 404
        assert (await admin_post(c, ADMIN_EMAIL, DEFAULT_PASSWORD)).status_code == 404
        await login_as(c, admin)  # a stolen session used from elsewhere
        assert (await c.get("/superadmin")).status_code == 403
    async with make_client("10.20.30.40") as c:
        assert (await c.get("/superadmin/login")).status_code == 200
        r = await admin_post(c, ADMIN_EMAIL, DEFAULT_PASSWORD)
        assert r.status_code == 303 and (await c.get("/superadmin")).status_code == 200
    async with make_client("192.0.2.10") as c:
        assert (await c.get("/superadmin/login")).status_code == 200
    assert admin_login.address_allowed("10.1.1.1") and not admin_login.address_allowed("11.1.1.1")


async def test_one_session_per_account_applies(db):
    admin = await make_admin(db)
    async with make_client() as first, make_client() as second:
        assert (await admin_post(first, ADMIN_EMAIL, DEFAULT_PASSWORD)).status_code == 303
        assert (await admin_post(second, ADMIN_EMAIL, DEFAULT_PASSWORD)).status_code == 303
        assert (await second.get("/superadmin")).status_code == 200
        r = await first.get("/superadmin")
        assert r.status_code in (303, 401) and "/auth/login" in r.headers.get("location", "/auth/login")
        r = await first.get("/superadmin/login")  # the admin page says why, once, and clears the stale cookie
        assert r.status_code == 200 and "Signed out on this device" in r.text
    live = await count(db, UserSession, UserSession.user_id == admin.id, UserSession.revoked_at.is_(None))
    assert live == 1


# ====================================================================== .env rotation at start-up
async def test_a_changed_superadmin_password_applies_at_start_up(db, monkeypatch, caplog):
    from app.services.bootstrap import ensure_superadmin

    s = get_settings()
    first, second = "Rot8-Old-Passw0rd!", "Rot8-New-Passw0rd!"
    monkeypatch.setattr(s, "superadmin_email", "boot.admin@example.com")
    monkeypatch.setattr(s, "superadmin_password", first)
    admin = await ensure_superadmin(db)
    await db.commit()
    assert admin is not None and admin.role == Role.SUPERADMIN
    await login_as(make_client(), admin)  # a live session
    assert await ensure_superadmin(db) is not None  # unchanged: nothing happens
    await db.commit()
    assert await count(db, AuditLog, AuditLog.action == "superadmin.password_rotated") == 0

    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(s, "superadmin_password", second)
    await ensure_superadmin(db)  # the next start with the new .env
    await db.commit()
    fresh = await db.get(User, admin.id, populate_existing=True)
    assert await verify_password(fresh.password_hash, second) and not await verify_password(fresh.password_hash, first)
    assert await count(db, UserSession, UserSession.user_id == admin.id, UserSession.revoked_at.is_(None)) == 0
    rows = (await db.execute(select(AuditLog).where(AuditLog.action == "superadmin.password_rotated"))).scalars().all()
    assert len(rows) == 1 and second not in str(rows[0].detail)
    assert second not in caplog.text and first not in caplog.text  # never logged
    async with make_client() as c:
        assert (await admin_post(c, "boot.admin@example.com", second)).status_code == 303
    assert await count(db, User, User.role == Role.SUPERADMIN) == 1


async def test_bootstrap_never_raises_a_member_to_admin(db, monkeypatch):
    from app.services.bootstrap import ensure_superadmin

    member = await make_user(db, email="taken@example.com")
    monkeypatch.setattr(get_settings(), "superadmin_email", "taken@example.com")
    monkeypatch.setattr(get_settings(), "superadmin_password", "Some-Strong-Passw0rd!")
    await ensure_superadmin(db)
    await db.commit()
    fresh = await db.get(User, member.id, populate_existing=True)
    assert fresh.role == Role.CUSTOMER and await verify_password(fresh.password_hash, DEFAULT_PASSWORD)


async def test_a_banned_owner_never_unlocks_the_admin_page(db):
    """The admin page answers only admins: an owner's gym makes no difference (no role is revealed)."""
    owner = await make_owner(db, email="owner.two@example.com")
    await make_gym(db, owner)
    async with make_client() as c:
        r = await admin_post(c, owner.email, DEFAULT_PASSWORD)
    assert r.status_code == 400 and GENERIC in alert(r)


async def test_a_spoofed_forwarded_for_never_dodges_the_ban(db, monkeypatch):
    """The counted address is ``client_ip``: X-Forwarded-For counts only from TRUSTED_PROXIES - a client that claims to
    be the server itself (loopback is never banned) is still counted and banned under its real address."""
    monkeypatch.setattr(get_settings(), "trust_proxy_headers", True)  # even with proxy headers on: not from a proxy
    await make_admin(db)
    async with make_client(ATTACKER) as c:
        c.headers["X-Forwarded-For"] = "127.0.0.1"
        for _ in range(5):
            await admin_post(c, "nobody@example.com", "Wrong-Passw0rd!")
    assert len(await bans_of(db, ATTACKER)) == 1
    assert await count(db, IpBan, IpBan.ip == "127.0.0.1") == 0
