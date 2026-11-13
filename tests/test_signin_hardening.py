"""Sign-in hardening (production review findings in app/routers/auth.py + app/services/auth_flows.py):

* a correct password that was being verified while wrong guesses locked the account must NOT sign in (the lock is
  decided by one conditional write - ``auth_flows.clear_failed_sign_ins``), and guesses verified after the lock neither
  count nor extend it;
* the unknown-account lockout covers every spelling of an identifier, like a real account's ('@x' / 'x' / 'X', phone
  formats, member-ID forms) - otherwise the lock message tells real accounts apart;
* identifiers of deleted / banned accounts are refused at sign-up, invitation acceptance and staff sign-up
  (``auth_flows.blocked_identity`` -> trust_safety.registration_blocked) and banned accounts cannot sign in;
* one-time links (password reset, invitations) used twice at the same moment change things once.
"""

from __future__ import annotations

import asyncio
import html
from typing import Any

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.models import AuthToken, Notification, User, UserSession
from app.models.enums import TokenPurpose
from app.routers import auth as auth_router
from app.services import auth_flows as af
from tests.conftest import make_client, post_form
from tests.factories import DEFAULT_PASSWORD, make_customer, make_owner

STRONG = "Gl0wing-Kettlebell!"


@pytest.fixture(autouse=True)
def _clean_throttle():
    af.ghost_throttle.reset()
    yield
    af.ghost_throttle.reset()


def text_of(r: Any) -> str:
    return html.unescape(r.text)


# ============================================================================== review finding: lockout in flight
async def test_a_right_password_in_flight_while_the_account_locks_does_not_sign_in(db, monkeypatch):
    """5 wrong guesses and the right one arrive together; the right one is verified only after the others locked the
    account. Before the fix it got 303 and a session although the account was locked in the database."""
    user = await make_customer(db, email="victim@example.com")
    uid = user.id
    real_verify = auth_router.verify_password
    wrong_done = asyncio.Event()
    finished = 0

    async def verify(hashed: Any, password: str) -> bool:
        nonlocal finished
        if password == DEFAULT_PASSWORD:
            await asyncio.wait_for(wrong_done.wait(), 20)  # read the account before the lock, verified after it
        return await real_verify(hashed, password)

    monkeypatch.setattr(auth_router, "verify_password", verify)
    clients = [make_client() for _ in range(6)]

    async def attempt(i: int) -> Any:
        nonlocal finished
        pw = DEFAULT_PASSWORD if i == 5 else f"Wrong-{i}-Passw0rd!"
        try:
            return await post_form(clients[i], "/auth/login", {"identifier": "victim@example.com", "password": pw})
        finally:
            if i != 5:
                finished += 1
                if finished == 5:
                    wrong_done.set()

    try:
        results = await asyncio.gather(*(attempt(i) for i in range(6)))
    finally:
        for c in clients:
            await c.aclose()
    statuses = [r.status_code for r in results]
    assert statuses == [400] * 6, statuses
    assert "Too many failed sign-in attempts" in text_of(results[5])
    assert await db.scalar(select(func.count()).select_from(UserSession).where(UserSession.user_id == uid)) == 0
    db.expire_all()
    locked = await db.get(User, uid)
    assert locked.locked_until is not None  # still locked: the in-flight success neither reset nor bypassed it
    # exactly one lock notification, however many guesses were in flight
    notes = await db.scalar(select(func.count()).select_from(Notification).where(
        Notification.user_id == uid, Notification.title == "Sign-in paused after failed attempts"))
    assert notes == 1


async def test_wrong_guesses_verified_after_the_lock_neither_count_nor_extend_it(db):
    from app.core.utils import utcnow

    user = await make_customer(db, email="steady@example.com")
    uid = user.id
    for i in range(get_settings().login_max_failures):
        assert not await af.count_failed_sign_in(db, user, utcnow()) or i == get_settings().login_max_failures - 1
    await db.commit()
    db.expire_all()
    first = (await db.get(User, uid)).locked_until
    assert first is not None
    user = await db.get(User, uid)
    assert await af.count_failed_sign_in(db, user, utcnow()) is False  # locked: not counted
    assert user.locked_until == first  # the caller sees the lock that is on
    await db.commit()
    db.expire_all()
    again = await db.get(User, uid)
    assert again.locked_until == first and again.failed_login_count == 0


async def test_a_correct_password_after_the_lock_expired_signs_in_and_resets_the_count(db):
    from datetime import timedelta

    from app.core.utils import utcnow

    from sqlalchemy import update

    uid = (await make_customer(db, email="expired@example.com")).id
    await db.execute(update(User).where(User.id == uid).values(locked_until=utcnow() - timedelta(seconds=5),
                                                               failed_login_count=3))
    await db.commit()
    async with make_client() as c:
        r = await post_form(c, "/auth/login", {"identifier": "expired@example.com", "password": DEFAULT_PASSWORD})
        assert r.status_code == 303
    db.expire_all()
    fresh = await db.get(User, uid)
    assert fresh.locked_until is None and fresh.failed_login_count == 0


# ============================================================================== review finding: ghost key spelling
@pytest.mark.parametrize("first, then", [
    ("@ghost-x", "ghost-x"),
    ("ghost-x", "GHOST-X"),
    ("Nobody@Example.com", " nobody@example.COM "),
    ("IP-000999", "ip 999"),
    ("+91 98765 43210", "98765-43210"),
])
async def test_the_unknown_account_lockout_covers_every_spelling(first, then):
    async with make_client() as c:
        for i in range(get_settings().login_max_failures):
            r = await post_form(c, "/auth/login", {"identifier": first, "password": f"Wrong-{i}-Passw0rd!"})
            assert r.status_code == 400
        assert "Too many failed sign-in attempts" in text_of(r)
        r = await post_form(c, "/auth/login", {"identifier": then, "password": "Wrong-x-Passw0rd!"})
        assert r.status_code == 400 and "Too many failed sign-in attempts" in text_of(r), then


def test_identifier_keys_match_the_lookup_rules():
    key = af.identifier_key
    assert key("@Alice") == key("alice") == key("ALICE") == "username:alice"
    assert key("Bob@Example.com") == key("bob@example.com ") == "email:bob@example.com"
    assert key("IP-000123") == key("ip 123") == key("IP123") == "member:123"
    assert key("+91 98765 43210") == key("9876543210") == key("098765 43210") == key("0091 9876543210") == "phone:+919876543210"
    assert key("not a valid thing!").startswith("other:")


# ============================================================================== blocked identities
@pytest.fixture
def registry(monkeypatch):
    """Stand-in for the trust-and-safety registry: these identifiers belonged to deleted / banned accounts."""
    from app.services import trust_safety

    blocked = {"email": {"banned@example.com"}, "phone": {"+919000011111"}}
    calls: list[dict[str, Any]] = []

    async def registration_blocked(db: Any, *, email: str | None = None, phone: str | None = None,
                                   id_number_hash: str | None = None, country_code: str | None = None,
                                   exclude_user_id: int | None = None) -> str | None:
        calls.append({"email": email, "phone": phone, "exclude_user_id": exclude_user_id})
        if email and email.strip().lower() in blocked["email"]:
            return "This e-mail address can't be used for a new IronPulse account."
        if phone and phone in blocked["phone"]:
            return "This phone number can't be used for a new IronPulse account."
        return None

    monkeypatch.setattr(trust_safety, "registration_blocked", registration_blocked)
    return calls


async def test_invitation_acceptance_refuses_a_blocked_identity(db, registry):
    from app.models import CustomerProfile

    owner = await make_owner(db)
    member = await make_customer(db, email="banned@example.com", password=None)
    mid = member.id
    prof = await db.get(CustomerProfile, mid)
    prof.registered_by_id = owner.id
    await db.commit()
    raw = await af.issue_token(db, TokenPurpose.INVITE, user_id=mid, email=member.email)
    await db.commit()
    async with make_client() as c:
        r = await post_form(c, "/auth/invite", {"token": raw, "password": STRONG, "password_confirm": STRONG,
                                                "accept_terms": "1"})
        assert r.status_code == 400 and "can't be used for a new IronPulse account" in text_of(r)
    assert {"email": "banned@example.com", "phone": None, "exclude_user_id": mid} in registry
    db.expire_all()
    assert (await db.get(User, mid)).password_hash is None  # never activated
    assert await db.scalar(select(func.count()).select_from(UserSession)) == 0


async def test_staff_sign_up_refuses_a_blocked_identity(db, registry):
    from app.models.enums import EmploymentStatus
    from tests.factories import make_employee, make_gym

    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    emp = await make_employee(db, gym)
    emp.user_id = None
    emp.status = EmploymentStatus.INVITED
    emp.invite_email = "banned@example.com"
    await db.commit()
    raw = await af.issue_token(db, TokenPurpose.STAFF_INVITE, employee_id=emp.id, email="banned@example.com")
    await db.commit()
    async with make_client() as c:
        r = await post_form(c, "/auth/staff-invite", {"token": raw, "action": "create", "full_name": "Kiran Rao",
                                                      "phone": "90000 22222", "phone_country": "IN",
                                                      "date_of_birth": "1990-01-01", "country": "IN",
                                                      "city": "Bengaluru", "password": STRONG,
                                                      "password_confirm": STRONG, "accept_terms": "1"})
        assert r.status_code == 400 and "can't be used for a new IronPulse account" in text_of(r)
    assert await db.scalar(select(func.count()).select_from(User).where(User.email == "banned@example.com")) == 0


async def test_a_banned_account_cannot_sign_in(db):
    user = await make_customer(db, email="ban@example.com")
    user.is_active = False  # trust_safety.ban_account: users.is_active false
    await db.commit()
    async with make_client() as c:
        r = await post_form(c, "/auth/login", {"identifier": "ban@example.com", "password": DEFAULT_PASSWORD})
        assert r.status_code == 400 and "This account has been disabled" in text_of(r)
    assert await db.scalar(select(func.count()).select_from(UserSession)) == 0


# ============================================================================== one-time links used twice at once
async def test_a_reset_link_submitted_twice_at_once_changes_the_password_once(db):
    user = await make_customer(db, email="twice@example.com")
    uid = user.id
    raw = await af.issue_token(db, TokenPurpose.PASSWORD_RESET, user_id=uid, email=user.email)
    await db.commit()
    clients = [make_client(), make_client()]
    try:
        rs = await asyncio.gather(*(post_form(c, "/auth/reset", {"token": raw, "password": STRONG,
                                                                  "password_confirm": STRONG}) for c in clients))
    finally:
        for c in clients:
            await c.aclose()
    assert sorted(r.status_code for r in rs) == [303, 400]
    changed = await db.scalar(select(func.count()).select_from(Notification).where(
        Notification.user_id == uid, Notification.title == "Your password was changed"))
    assert changed == 1
    tok = await db.scalar(select(AuthToken).where(AuthToken.user_id == uid))
    assert tok.used_at is not None


async def test_an_invitation_accepted_twice_at_once_activates_once(db):
    from app.models import CustomerProfile

    owner = await make_owner(db)
    member = await make_customer(db, email="twice-invite@example.com", password=None)
    mid = member.id
    prof = await db.get(CustomerProfile, mid)
    prof.registered_by_id = owner.id
    await db.commit()
    raw = await af.issue_token(db, TokenPurpose.INVITE, user_id=mid, email=member.email)
    await db.commit()
    clients = [make_client(), make_client()]
    try:
        rs = await asyncio.gather(*(post_form(c, "/auth/invite", {"token": raw, "password": STRONG,
                                                                   "password_confirm": STRONG, "accept_terms": "1"})
                                    for c in clients))
    finally:
        for c in clients:
            await c.aclose()
    assert sorted(r.status_code for r in rs) == [303, 400]
    ready = await db.scalar(select(func.count()).select_from(Notification).where(
        Notification.user_id == mid, Notification.title == "Your account is ready"))
    assert ready == 1
    assert await db.scalar(select(func.count()).select_from(UserSession).where(
        UserSession.user_id == mid, UserSession.revoked_at.is_(None))) == 1
