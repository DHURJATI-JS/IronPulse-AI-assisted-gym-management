"""One session per account (SINGLE_SESSION_PER_ACCOUNT, app/security/sessions.py + app/services/auth_flows.py).

* every successful sign-in ends the account's sessions on other devices - the newest wins; two sign-ins at the same
  moment leave exactly ONE live session, the later one (a per-account advisory lock around create + revoke);
* the ended device: its sockets close with 4409 "signed in elsewhere", its AI work stops, its next page request lands on
  the sign-in page with "signed out because your account signed in on another device (<browser>, <time>)", a JSON
  call gets 401 + ``X-Signed-Out: elsewhere``; its Redis cache entry is gone and a stale one never signs anyone in;
* tabs of one session, a re-sign-in on the same browser and other revocations (sign-out, password reset) are not
  "another device";
* recorded as a security event + audit row (no secrets); the setting off restores several devices.
"""

from __future__ import annotations

import asyncio
import html
import re
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.core.utils import utcnow
from app.models import AuditLog, SecurityEvent, User, UserSession
from app.models.enums import ThreatCategory, TokenPurpose
from app.security import sessions as sess_mod
from app.security.crypto import token_hash
from app.security.sessions import CLOSE_SIGNED_IN_ELSEWHERE, device_label, session_cookie_name
from app.services import auth_flows as af
from app.services.realtime import CloseRequest, hub
from tests.conftest import api, login_as, make_client, post_form
from tests.factories import DEFAULT_PASSWORD, make_customer, make_owner

PHONE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/18.1 Mobile/15E148 Safari/604.1")
LAPTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 "
             "Safari/537.36")
STRONG = "Gl0wing-Kettlebell!"


@pytest.fixture(autouse=True)
def _clean_throttle():
    af.ghost_throttle.reset()
    yield
    af.ghost_throttle.reset()


class SchedulerSpy:
    """Records the AI queue's cancel calls (the old device's AI work must stop) and forwards them."""

    def __init__(self, real: Any) -> None:
        self.real = real
        self.calls: list[dict[str, Any]] = []

    def cancel(self, **kwargs: Any) -> int:
        self.calls.append(kwargs)
        return int(self.real.cancel(**kwargs) or 0)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.real, name)


@pytest.fixture
def spy(monkeypatch):
    import app.ai.scheduler as scheduler_mod

    s = SchedulerSpy(scheduler_mod.scheduler)
    monkeypatch.setattr(scheduler_mod, "scheduler", s)
    return s


def device(ua: str, ip: str = "127.0.0.1"):
    c = make_client(ip)
    c.headers["User-Agent"] = ua
    return c


async def sign_in(client: Any, email: str, password: str = DEFAULT_PASSWORD, **extra: Any) -> Any:
    r = await post_form(client, "/auth/login", {"identifier": email, "password": password, **extra})
    assert r.status_code == 303, html.unescape(r.text)[-1500:]
    return r


async def live_sessions(db: Any, user_id: int) -> list[UserSession]:
    db.expire_all()
    rows = await db.execute(select(UserSession).where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None),
                                                      UserSession.expires_at > utcnow()))
    return list(rows.scalars().all())


def drain(q: asyncio.Queue) -> list[Any]:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


# ============================================================================================ the newest sign-in wins
async def test_a_sign_in_ends_the_other_device_which_is_told_why_on_its_next_request(db):
    user = await make_customer(db, email="riya@example.com", timezone="Asia/Kolkata")
    uid = user.id
    async with device(PHONE_UA) as phone, device(LAPTOP_UA) as laptop:
        await sign_in(phone, "riya@example.com")
        phone_token = phone.cookies.get(session_cookie_name())
        assert (await phone.get("/me")).status_code == 200

        await sign_in(laptop, "riya@example.com")
        live = await live_sessions(db, uid)
        assert [s.id for s in live] == [token_hash(laptop.cookies.get(session_cookie_name()))]
        assert (await laptop.get("/me")).status_code == 200  # the newest device stays signed in

        # a JSON call from the old device: 401, the reason and the header 00-core.js acts on
        r = await phone.get("/api/notifications", headers={"Accept": "application/json"})
        assert r.status_code == 401 and r.headers.get("x-signed-out") == "elsewhere"
        assert "signed in on another device (Chrome on Windows" in r.json()["detail"]

        # its next page lands on the sign-in page ...
        r = await phone.get("/me")
        assert r.status_code == 303 and r.headers["location"].startswith("/auth/login?next=")
        r = await phone.get("/auth/login?next=%2Fme")
        text = html.unescape(r.text)
        assert r.status_code == 200 and "Signed out on this device" in text
        assert "You were signed out because your account signed in on another device (Chrome on Windows, " in text
        assert "IST). If this was not you, reset your password." in text and 'href="/auth/forgot"' in text
        assert phone_token not in text  # never echoes anything secret
        # ... which says it once and clears the stale cookie: from now on this browser is simply signed out
        assert session_cookie_name() not in phone.cookies
        token = re.search(r'name="csrf_token" value="([^"]+)"', text).group(1)
        r = await phone.get("/auth/login")
        assert "Signed out on this device" not in html.unescape(r.text)
        # the page's own form works: its CSRF token matches the signed-out state (not the cleared session)
        r = await phone.post("/auth/login", data={"identifier": "riya@example.com", "password": DEFAULT_PASSWORD,
                                                  "csrf_token": token})
        assert r.status_code == 303, r.text[:300]
        assert (await laptop.get("/me")).status_code == 303  # ... and now the laptop is the device signed out

    # recorded for the super-admin (security events, category 'other') and in the audit log - no secrets
    events = (await db.execute(select(SecurityEvent).where(SecurityEvent.user_id == uid)
                               .order_by(SecurityEvent.id))).scalars().all()
    assert len(events) == 2  # the laptop ended the phone, then the phone (signing in again) ended the laptop
    event = events[0]
    assert event.category == ThreatCategory.OTHER and event.action == "logged" and event.severity == 1
    assert event.detail.startswith("signed out elsewhere: account") and "Safari on iPhone" in event.detail
    assert phone_token not in event.detail and token_hash(phone_token) not in event.detail
    audits = (await db.execute(select(AuditLog).where(AuditLog.action == "auth.signed_out_elsewhere")
                               .order_by(AuditLog.id))).scalars().all()
    assert [a.detail["device"] for a in audits] == ["Chrome on Windows", "Safari on iPhone"]
    assert audits[0].actor_id == uid and audits[0].detail["ended"] == 1


async def test_concurrent_sign_ins_leave_exactly_one_live_session_the_later_one(db):
    uid = (await make_customer(db, email="race@example.com")).id
    for round_ in range(4):
        clients = [device(PHONE_UA if i % 2 else LAPTOP_UA) for i in range(3)]
        try:
            await asyncio.gather(*(sign_in(c, "race@example.com") for c in clients))
            live = await live_sessions(db, uid)
            assert len(live) == 1, f"round {round_}: {len(live)} live sessions"  # never zero, never two
            newest = await db.scalar(select(func.max(UserSession.created_at)).where(UserSession.user_id == uid))
            assert live[0].created_at == newest  # the later sign-in won
            statuses = [(await c.get("/me")).status_code for c in clients]
            assert sorted(statuses) == [200, 303, 303], statuses
        finally:
            for c in clients:
                await c.aclose()


async def test_simultaneous_sign_ins_are_serialised_by_the_account_lock(db):
    """start_session itself (every path that signs in calls it, not all of them write the user row first): both create
    their session and end 'the others' with a pause before COMMIT - without the per-account lock each would miss the
    other's uncommitted session and two would stay live; with it the second waits and ends the first."""
    from starlette.requests import Request
    from starlette.responses import Response

    from app import db as dbmod

    uid = (await make_customer(db, email="window@example.com")).id

    def request(ua: str) -> Request:
        return Request({"type": "http", "method": "POST", "path": "/auth/login", "root_path": "", "scheme": "http",
                        "query_string": b"", "headers": [(b"user-agent", ua.encode()), (b"host", b"testserver")],
                        "client": ("127.0.0.1", 50000), "server": ("testserver", 80), "state": {}})

    async def sign(ua: str) -> str:
        async with dbmod.get_sessionmaker()() as s:
            user = await s.get(User, uid)
            raw = await af.start_session(s, request(ua), Response(), user)
            await asyncio.sleep(0.3)  # still inside the sign-in's transaction: its new session is not committed yet
            await s.commit()
            return raw

    tokens = await asyncio.gather(sign(PHONE_UA), sign(LAPTOP_UA))
    live = await live_sessions(db, uid)
    assert len(live) == 1, f"{len(live)} live sessions"
    assert live[0].id in {token_hash(t) for t in tokens}


async def test_the_old_device_sockets_close_with_4409_and_its_ai_work_stops(db, spy):
    user = await make_customer(db, email="sockets@example.com")
    uid = user.id
    async with device(PHONE_UA) as phone, device(LAPTOP_UA) as laptop:
        await sign_in(phone, "sockets@example.com")
        old = token_hash(phone.cookies.get(session_cookie_name()))
        q = hub.subscribe(uid, session_hash=old, page_id="pageA-1234567890", conn_id="c-phone")
        try:
            await sign_in(laptop, "sockets@example.com")
            assert drain(q) == [CloseRequest(CLOSE_SIGNED_IN_ELSEWHERE, "signed in elsewhere")]
            from app.services.presence import presence

            await presence.drain()
            assert {"user_id": uid, "session_hash": old, "kinds": {"user"}, "reason": "signed_out"} in spy.calls
            new = token_hash(laptop.cookies.get(session_cookie_name()))
            assert not any(c.get("session_hash") == new for c in spy.calls)  # the new device's work is untouched
        finally:
            hub.unsubscribe(uid, q)


async def test_the_old_session_is_gone_from_redis_and_a_stale_entry_never_signs_in(db):
    if not get_settings().redis_url:
        pytest.skip("needs REDIS_URL (the per-test command points it at a test database)")
    user = await make_customer(db, email="cache@example.com")
    async with device(PHONE_UA) as phone, device(LAPTOP_UA) as laptop:
        await sign_in(phone, "cache@example.com")
        old_token = phone.cookies.get(session_cookie_name())
        old = token_hash(old_token)
        assert (await phone.get("/me")).status_code == 200
        assert await sess_mod._r_get(old) == user.id  # cached
        await sign_in(laptop, "cache@example.com")
        assert await sess_mod._r_get(old) is None  # gone at once, for every worker
        # a cache entry that outlived the revocation (a failed Redis delete, a write racing the commit) is refused
        await sess_mod._r_set(old, user.id, 600)
        assert (await sess_mod.load_session(db, old_token)) is None
        assert await sess_mod._r_get(old) is None  # and dropped
        r = await phone.get("/me")
        assert r.status_code == 303 and r.headers["location"].startswith("/auth/login")


async def test_tabs_of_one_session_and_a_re_sign_in_on_the_same_browser_are_not_another_device(db):
    uid = (await make_customer(db, email="tabs@example.com")).id
    async with device(LAPTOP_UA) as tab1, device(LAPTOP_UA) as tab2:
        await sign_in(tab1, "tabs@example.com")
        tab2.cookies.set(session_cookie_name(), tab1.cookies.get(session_cookie_name()))  # a second tab: same cookie
        assert (await tab1.get("/me")).status_code == 200 and (await tab2.get("/me")).status_code == 200
        first = tab1.cookies.get(session_cookie_name())
        await sign_in(tab1, "tabs@example.com")  # signing in again on the same browser rotates its own session
        assert tab1.cookies.get(session_cookie_name()) != first
        assert len(await live_sessions(db, uid)) == 1
        lookup = await sess_mod.lookup_session(db, first)
        assert lookup.session is None and lookup.ended is None  # rotated, not "signed in elsewhere"
    assert await db.scalar(select(func.count(SecurityEvent.id))) == 0


async def test_other_revocations_are_not_reported_as_a_sign_in_elsewhere(client, db):
    user = await make_customer(db, email="reset@example.com")
    async with device(PHONE_UA) as phone:
        await sign_in(phone, "reset@example.com")
        raw = await af.issue_token(db, TokenPurpose.PASSWORD_RESET, user_id=user.id, email=user.email)
        await db.commit()
        r = await post_form(client, "/auth/reset", {"token": raw, "password": STRONG, "password_confirm": STRONG})
        assert r.status_code == 303
        r = await phone.get("/me")
        assert r.status_code == 303
        r = await phone.get("/auth/login")
        assert "Signed out on this device" not in html.unescape(r.text)  # a reset signs out everywhere, quietly


async def test_with_the_setting_off_every_device_stays_signed_in(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "single_session_per_account", False)
    uid = (await make_customer(db, email="multi@example.com")).id
    async with device(PHONE_UA) as phone, device(LAPTOP_UA) as laptop:
        await sign_in(phone, "multi@example.com")
        await sign_in(laptop, "multi@example.com")
        assert len(await live_sessions(db, uid)) == 2
        assert (await phone.get("/me")).status_code == 200 and (await laptop.get("/me")).status_code == 200
    assert await db.scalar(select(func.count(SecurityEvent.id))) == 0


async def test_invite_acceptance_also_ends_the_accounts_other_sessions(db):
    from app.models import CustomerProfile

    owner = await make_owner(db)
    member = await make_customer(db, email="invited@example.com", password=None)
    mid = member.id
    prof = await db.get(CustomerProfile, member.id)
    prof.registered_by_id = owner.id
    await db.commit()
    elsewhere = make_client()
    await login_as(elsewhere, member)  # e.g. a session the gym's front desk opened for them
    raw = await af.issue_token(db, TokenPurpose.INVITE, user_id=member.id, email=member.email)
    await db.commit()
    async with device(PHONE_UA) as phone:
        r = await post_form(phone, "/auth/invite", {"token": raw, "password": STRONG, "password_confirm": STRONG,
                                                    "accept_terms": "1"})
        assert r.status_code == 303
        live = await live_sessions(db, mid)
        assert [s.id for s in live] == [token_hash(phone.cookies.get(session_cookie_name()))]
    r = await elsewhere.get("/me")
    assert r.status_code == 303
    await elsewhere.aclose()


async def test_a_session_ended_long_ago_or_expired_is_never_reported_as_elsewhere(db):
    user = await make_customer(db, email="old@example.com")
    async with device(PHONE_UA) as phone:
        await sign_in(phone, "old@example.com")
        token = phone.cookies.get(session_cookie_name())
    # an expired-but-not-revoked session is simply over; a revoked one without a replacing sign-in says nothing
    row = await db.get(UserSession, token_hash(token))
    row.expires_at = utcnow() - timedelta(minutes=1)
    await db.commit()
    found = await sess_mod.lookup_session(db, token)
    assert found.session is None and found.ended is None
    row.revoked_at = utcnow()
    await db.commit()
    found = await sess_mod.lookup_session(db, token)
    assert found.session is None and found.ended is None


@pytest.mark.parametrize("ua, label", [
    (PHONE_UA, "Safari on iPhone"),
    (LAPTOP_UA, "Chrome on Windows"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
     "Safari on macOS"),
    ("Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/25.0 "
     "Chrome/121.0.0.0 Mobile Safari/537.36", "Samsung Internet on Android"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 "
     "Edg/131.0.0.0", "Edge on Windows"),
    ("Mozilla/5.0 (X11; Linux x86_64; rv:132.0) Gecko/20100101 Firefox/132.0", "Firefox on Linux"),
    ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/131.0 "
     "Mobile/15E148 Safari/604.1", "Chrome on iPhone"),
    ("<script>alert(1)</script>", "an unknown browser"),
    (None, "an unknown browser"),
])
def test_device_labels_are_a_fixed_vocabulary(ua, label):
    assert device_label(ua) == label


async def test_lock_keys_never_collide_between_accounts():
    assert sess_mod.SESSION_LOCK_BASE + 1 != sess_mod.SESSION_LOCK_BASE + 2
    assert sess_mod.SESSION_LOCK_BASE + 2**31 < 2**63  # inside PostgreSQL's bigint for any realistic user id


async def test_user_rows_are_untouched_by_a_session_ending(db):
    uid = (await make_customer(db, email="untouched@example.com")).id
    async with device(PHONE_UA) as phone, device(LAPTOP_UA) as laptop:
        await sign_in(phone, "untouched@example.com")
        await sign_in(laptop, "untouched@example.com")
    db.expire_all()
    fresh = await db.get(User, uid)
    assert fresh.is_active and fresh.deleted_at is None and fresh.failed_login_count == 0
