"""WP-A2 authentication tests: signup (member / owner / minor), identifier sign-in, lockout, safe redirects,
logout, password reset, e-mail verification, customer invites and staff invites."""

from __future__ import annotations

import html
import re
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.core.utils import utcnow
from app.models import (
    AuthToken,
    CustomerProfile,
    EmailOutbox,
    IdentityDocument,
    Job,
    Notification,
    User,
    UserSession,
)
from app.models.enums import EmploymentStatus, IdStatus, NotificationCategory, Role, ThreatCategory, TokenPurpose
from app.security.bans import security_events
from app.security.crypto import token_hash
from app.security.passwords import needs_rehash, verify_password
from app.security.sessions import session_cookie_name
from app.services import auth_flows as af
from app.services import storage
from tests.conftest import login_as, make_client, post_form
from tests.factories import DEFAULT_PASSWORD, make_customer, make_employee, make_gym, make_owner, make_user, png_bytes

STRONG = "Gl0wing-Kettlebell!"
NEW_PASSWORD = "Fresh-Barbell-2026!"


@pytest.fixture(autouse=True)
def _reset_ghost_throttle():
    af.ghost_throttle.reset()
    yield
    af.ghost_throttle.reset()


# ------------------------------------------------------------------ helpers
def page(r) -> str:
    return html.unescape(r.text)


def years_ago(years: int, days: int = 0) -> str:
    today = date.today()
    try:
        d = today.replace(year=today.year - years)
    except ValueError:
        d = today.replace(year=today.year - years, day=28)
    return (d - timedelta(days=days)).isoformat()


def signup_data(**over) -> dict:
    data = {
        "role": "customer", "full_name": "Priya Sharma", "email": "priya@example.com", "phone": "98765 43210",
        "phone_country": "IN", "date_of_birth": years_ago(25), "country": "IN", "city": "Bengaluru",
        "timezone": "Asia/Kolkata", "password": STRONG, "password_confirm": STRONG, "accept_terms": "1",
        "id_doc_type": "aadhaar", "id_country": "IN",
    }
    data.update(over)
    return {k: v for k, v in data.items() if v is not None}


def id_files(photo: bool = False, front: bytes | None = None) -> dict:
    files = {"id_front": ("front.png", front if front is not None else png_bytes((320, 200), "white", "ID"), "image/png")}
    if photo:
        files["photo"] = ("me.png", png_bytes((240, 240), "skyblue"), "image/png")
    return files


async def fresh(db, model, *where):
    """Query bypassing stale identity-map state (rows changed by the app in another session)."""
    return await db.scalar(select(model).where(*where).execution_options(populate_existing=True))


async def count(db, model, *where) -> int:
    return int(await db.scalar(select(func.count()).select_from(model).where(*where)) or 0)


def cookie_from(r) -> str | None:
    return r.cookies.get(session_cookie_name())


async def login(client, identifier: str, password: str = DEFAULT_PASSWORD, **extra):
    return await post_form(client, "/auth/login", {"identifier": identifier, "password": password, **extra})


# ------------------------------------------------------------------ pages
async def test_auth_pages_render_without_inline_scripts_or_styles(client):
    for path in ("/auth/login", "/auth/signup", "/auth/signup?role=owner", "/auth/forgot", "/auth/forgot?sent=1"):
        r = await client.get(path)
        assert r.status_code == 200, path
        assert ' style="' not in r.text and " onclick=" not in r.text
    r = await client.get("/auth/signup?role=owner")
    assert re.search(r'value="owner"[^>]*\bchecked\b', r.text) and not re.search(r'value="customer"[^>]*\bchecked\b', r.text)


async def test_signed_in_users_are_sent_home_from_auth_pages(client, db):
    owner = await make_owner(db)
    await login_as(client, owner)
    r = await client.get("/auth/login")
    assert r.status_code == 303 and r.headers["location"] == "/owner"
    r = await client.get("/auth/signup")
    assert r.status_code == 303 and r.headers["location"] == "/owner"


# ------------------------------------------------------------------ signup
async def test_signup_customer_creates_account_profile_id_card_token_and_session(client, db):
    r = await post_form(client, "/auth/signup", signup_data(), files=id_files())
    assert r.status_code == 303 and r.headers["location"] == "/me"
    assert cookie_from(r)
    user = await fresh(db, User, User.email == "priya@example.com")
    assert user.role == Role.CUSTOMER and user.phone_normalized == "+919876543210" and user.timezone == "Asia/Kolkata"
    assert user.username == "priya.sharma" and user.city == "Bengaluru" and user.country_code == "IN"
    assert user.id_status == IdStatus.PENDING and user.email_verified_at is None and user.last_login_at is not None
    assert await verify_password(user.password_hash, STRONG)
    assert await db.get(CustomerProfile, user.id) is not None
    doc = await db.scalar(select(IdentityDocument).where(IdentityDocument.user_id == user.id))
    assert doc is not None and doc.front_key.startswith("private/") and doc.uploaded_by_id == user.id
    assert await count(db, Job, Job.kind == "verify_identity") == 1

    # e-mail verification: 48 h token, only its hash stored; the raw token travels only in the e-mail
    tok = await db.scalar(select(AuthToken).where(AuthToken.user_id == user.id, AuthToken.purpose == TokenPurpose.EMAIL_VERIFY))
    assert tok is not None and len(tok.token_hash) == 64 and tok.expires_at > utcnow() + timedelta(hours=47)
    mail = await db.scalar(select(EmailOutbox).where(EmailOutbox.to_email == "priya@example.com", EmailOutbox.subject.contains("Confirm")))
    raw = re.search(r"verify-email\?token=([A-Za-z0-9_-]+)", mail.text_body).group(1)
    assert token_hash(raw) == tok.token_hash and raw != tok.token_hash
    notes = (await db.execute(select(Notification).where(Notification.user_id == user.id))).scalars().all()
    assert any(n.title.startswith("Welcome") for n in notes)
    assert all(raw not in (n.link or "") and raw not in n.body for n in notes)

    # signed in straight away
    r = await client.get("/account")
    assert r.status_code == 200 and "Priya Sharma" in r.text and "@priya.sharma" in r.text


async def test_signup_owner_requires_a_photo(client, db):
    data = signup_data(role="owner", email="owner@example.com", phone="98765 00001", full_name="Vikram Rao")
    r = await post_form(client, "/auth/signup", data, files=id_files())
    assert r.status_code == 400 and "A profile photo is required for gym owners" in page(r)
    assert await count(db, User) == 0
    r = await post_form(client, "/auth/signup", data, files=id_files(photo=True))
    assert r.status_code == 303 and r.headers["location"] == "/owner"
    owner = await fresh(db, User, User.email == "owner@example.com")
    assert owner.role == Role.OWNER and owner.photo_key.startswith("public/avatars/")
    assert storage.path_for(owner.photo_key).is_file()
    assert await db.get(CustomerProfile, owner.id) is None


async def test_signup_owner_must_be_an_adult(client, db):
    data = signup_data(role="owner", email="young@example.com", date_of_birth=years_ago(17))
    r = await post_form(client, "/auth/signup", data, files=id_files(photo=True))
    assert r.status_code == 400 and "Gym owners must be at least 18" in page(r)


async def test_signup_minor_requires_guardian_and_guardian_gets_copies(client, db):
    data = signup_data(email="teen@example.com", phone="98765 00002", date_of_birth=years_ago(15), full_name="Aarav Shah")
    r = await post_form(client, "/auth/signup", data, files=id_files())
    text = page(r)
    assert r.status_code == 400
    for msg in ("guardian's full name", "guardian's e-mail", "guardian's phone", "parent or guardian must agree"):
        assert msg in text, msg
    assert 'data-guardian-block' in r.text and await count(db, User) == 0

    # guardian must be a different person
    data.update(guardian_name="Meena Shah", guardian_relation="parent", guardian_email="teen@example.com",
                guardian_phone="98765 00002", guardian_phone_country="IN", guardian_consent="1")
    r = await post_form(client, "/auth/signup", data, files=id_files())
    assert r.status_code == 400 and "their own e-mail" in page(r) and "different from yours" in page(r)

    data.update(guardian_email="meena@example.com", guardian_phone="98765 11111")
    r = await post_form(client, "/auth/signup", data, files=id_files())
    assert r.status_code == 303
    teen = await fresh(db, User, User.email == "teen@example.com")
    prof = await db.get(CustomerProfile, teen.id)
    assert prof.guardian_name == "Meena Shah" and prof.guardian_relation == "Parent"
    assert prof.guardian_email == "meena@example.com" and prof.guardian_phone == "+919876511111"
    welcome = await db.scalar(select(EmailOutbox).where(EmailOutbox.user_id == teen.id, EmailOutbox.subject.contains("Welcome")))
    assert welcome is not None and welcome.cc == ["meena@example.com"]


async def test_signup_requires_an_id_card(client, db):
    r = await post_form(client, "/auth/signup", signup_data())
    assert r.status_code == 400 and "front of your government ID card" in page(r)
    r = await post_form(client, "/auth/signup", signup_data(), files=id_files(front=b"definitely not an image"))
    assert r.status_code == 400 and "not a valid image" in page(r)
    assert await count(db, User) == 0 and await count(db, IdentityDocument) == 0


async def test_failed_id_upload_discards_the_already_saved_photo(client, db):
    avatars = get_settings().upload_dir / "public" / "avatars"
    before = set(avatars.glob("*")) if avatars.exists() else set()
    data = signup_data(role="owner", email="o2@example.com", phone="98765 00003")
    files = id_files(photo=True, front=b"GIF89a broken")
    r = await post_form(client, "/auth/signup", data, files=files)
    assert r.status_code == 400 and "valid image" in page(r)
    after = set(avatars.glob("*")) if avatars.exists() else set()
    assert after == before and await count(db, User) == 0


async def test_signup_rejects_duplicate_email_phone_and_username(client, db):
    taken = await make_customer(db, email="taken@example.com", phone="+91 99999 88888")
    taken.username = "taken.handle"
    await db.commit()
    r = await post_form(client, "/auth/signup", signup_data(email="TAKEN@example.com"), files=id_files())
    assert r.status_code == 400 and "An account with this email already exists" in page(r)
    r = await post_form(client, "/auth/signup", signup_data(phone="99999 88888"), files=id_files())
    assert r.status_code == 400 and "An account with this phone already exists" in page(r)
    r = await post_form(client, "/auth/signup", signup_data(username="@Taken.Handle"), files=id_files())
    assert r.status_code == 400 and "That username is taken" in page(r)
    r = await post_form(client, "/auth/signup", signup_data(username="admin"), files=id_files())
    assert r.status_code == 400 and "reserved" in page(r)
    assert await count(db, User) == 1


async def test_signup_refuses_identifiers_of_deleted_accounts_for_good(client, db):
    # Updated for the owner's "one person per ID and e-mail, forever" rule (app/services/trust_safety.py): a deleted
    # account's e-mail / phone go into the blocked-identity registry, so sign-up refuses them with the registry's
    # neutral message - the old "can be used again after <date>" (end of the 30-day retention window) is no longer true.
    from app.services.accounts import soft_delete_user

    gone = await make_customer(db, email="gone@example.com", phone="+91 90000 12345")
    await soft_delete_user(db, gone, reason="test")
    await db.commit()
    for over in ({"email": "gone@example.com"}, {"phone": "90000 12345"}, {"email": " GONE@Example.com "}):
        r = await post_form(client, "/auth/signup", signup_data(**over), files=id_files())
        text = page(r)
        assert r.status_code == 400 and "can't be used for a new IronPulse account" in text, over
        assert "can be used again after" not in text, over
    assert await count(db, User) == 1


@pytest.mark.parametrize("over, message", [
    ({"password_confirm": "Different-Passw0rd!"}, "don't match"),
    ({"password": "password123", "password_confirm": "password123"}, "too common"),
    ({"password": "priyasharma99!", "password_confirm": "priyasharma99!"}, "Don't include your"),
    ({"date_of_birth": years_ago(3)}, "at least 5 years old"),
    ({"date_of_birth": "2999-01-01"}, "can't be in the future"),
    ({"date_of_birth": "17/05/1995"}, "valid date"),
    ({"accept_terms": None}, "accept the Terms"),
    ({"email": "not-an-email"}, "valid e-mail"),
    ({"role": "superadmin"}, "member or as a gym owner"),
    ({"phone": "12"}, "valid phone number"),
    ({"country": "ZZ"}, "Choose a country"),
    ({"city": ""}, "Enter your city"),
    ({"full_name": "<b>x</b>"}, "Names can contain"),
    ({"id_doc_type": "library_card"}, "type of ID"),
    ({"id_country": "India"}, "2-letter code"),
])
async def test_signup_validation_errors_rerender_the_form(client, db, over, message):
    r = await post_form(client, "/auth/signup", signup_data(**over), files=id_files())
    assert r.status_code == 400 and message in page(r)
    assert await count(db, User) == 0
    # typed values survive the error (except passwords and files)
    assert 'value="Priya Sharma"' in r.text or over.get("full_name")
    assert STRONG not in r.text


@pytest.mark.parametrize("ai_mode", ["available", "down"])
async def test_signup_and_sign_in_work_with_or_without_ai(client, db, request, ai_mode):
    if ai_mode == "available":
        request.getfixturevalue("fake_ai")(responses=['{"ok": true}'])
    else:
        request.getfixturevalue("ai_down")
    r = await post_form(client, "/auth/signup", signup_data(), files=id_files())
    assert r.status_code == 303
    client.cookies.clear()
    r = await login(client, "priya@example.com", STRONG)
    assert r.status_code == 303 and r.headers["location"] == "/me"


# ------------------------------------------------------------------ sign in
async def test_login_by_any_identifier_rotates_the_session(client, db):
    user = await make_customer(db, email="lena@example.com", phone="+91 98450 22222")
    user.username = "lena.k"
    await db.commit()
    await login_as(client, user)  # an existing session cookie must be replaced, not reused
    old = client.cookies.get(session_cookie_name())
    r = await login(client, "LENA@example.com")
    assert r.status_code == 303 and r.headers["location"] == "/me"
    new = cookie_from(r)
    assert new and new != old
    assert (await fresh(db, UserSession, UserSession.id == token_hash(old))).revoked_at is not None
    current = await fresh(db, UserSession, UserSession.id == token_hash(new))
    assert current.revoked_at is None and current.user_id == user.id
    await db.refresh(user)
    assert user.last_login_at is not None and user.last_login_ip == "127.0.0.1" and user.failed_login_count == 0

    for ident in ("@lena.k", "lena.k", "98450 22222", f"IP-{user.id:06d}"):
        client.cookies.clear()
        r = await login(client, ident)
        assert r.status_code == 303 and r.headers["location"] == "/me", ident
    # the legacy "email" field name keeps working
    client.cookies.clear()
    r = await post_form(client, "/auth/login", {"email": "lena@example.com", "password": DEFAULT_PASSWORD})
    assert r.status_code == 303


async def test_wrong_password_and_unknown_account_get_the_same_generic_error(client, db):
    await make_customer(db, email="kim@example.com")
    r1 = await login(client, "kim@example.com", "Wrong-Passw0rd!")
    r2 = await login(client, "nobody@example.com", "Wrong-Passw0rd!")
    assert r1.status_code == r2.status_code == 400
    assert "Those sign-in details didn't work" in page(r1) and "Those sign-in details didn't work" in page(r2)
    assert not cookie_from(r1) and not cookie_from(r2)
    r = await login(client, "", "")
    assert r.status_code == 400 and "and your password" in page(r)


async def test_lockout_after_repeated_failures_is_recorded(client, db):
    settings = get_settings()
    user = await make_customer(db, email="lock@example.com")
    for i in range(settings.login_max_failures):
        r = await login(client, "lock@example.com", f"Wrong-{i}-Passw0rd!")
        assert r.status_code == 400
    assert "Too many failed sign-in attempts" in page(r)
    await db.refresh(user)
    assert user.locked_until is not None and user.locked_until > utcnow()
    assert any(e.category == ThreatCategory.BRUTEFORCE for e in security_events.events)
    assert await count(db, Notification, Notification.user_id == user.id, Notification.category == NotificationCategory.SECURITY) == 1

    r = await login(client, "lock@example.com")  # right password, still locked
    assert r.status_code == 400 and "paused" in page(r) and not cookie_from(r)

    user.locked_until = utcnow() - timedelta(seconds=1)
    await db.commit()
    r = await login(client, "lock@example.com")
    assert r.status_code == 303


async def test_unknown_accounts_show_the_same_lockout(client):
    for i in range(get_settings().login_max_failures):
        r = await login(client, "ghost@example.com", f"Wrong-{i}-Passw0rd!")
    assert r.status_code == 400 and "Too many failed sign-in attempts" in page(r)


async def test_deleted_and_disabled_accounts_cannot_sign_in(client, db):
    from app.services.accounts import soft_delete_user

    gone = await make_customer(db, email="gone@example.com")
    await soft_delete_user(db, gone, reason="test")
    await db.commit()
    r = await login(client, "gone@example.com")
    assert r.status_code == 400 and "This account was deleted" in page(r) and not cookie_from(r)
    r = await login(client, "gone@example.com", "Wrong-Passw0rd!")
    assert r.status_code == 400 and "Those sign-in details didn't work" in page(r)

    await make_customer(db, email="off@example.com", is_active=False)
    r = await login(client, "off@example.com")
    assert r.status_code == 400 and "disabled" in page(r) and not cookie_from(r)


async def test_login_redirects_only_to_safe_next_targets(client, db):
    await make_owner(db, email="boss@example.com")
    r = await login(client, "boss@example.com", next="/owner/gyms?tab=branches")
    assert r.status_code == 303 and r.headers["location"] == "/owner/gyms?tab=branches"
    for evil in ("https://evil.example/x", "//evil.example", "/\\evil.example", "/auth/logout"):
        client.cookies.clear()
        r = await login(client, "boss@example.com", next=evil)
        assert r.status_code == 303 and r.headers["location"] == "/owner", evil
    client.cookies.clear()
    r = await client.get("/auth/login?next=//evil.example")
    assert r.status_code == 200 and "evil.example" not in r.text
    r = await client.get("/auth/login?next=/account")
    assert 'name="next" value="/account"' in r.text


async def test_new_sign_in_from_another_ip_is_notified(client, db):
    user = await make_customer(db, email="ip@example.com", last_login_ip="198.51.100.20")
    r = await login(client, "ip@example.com")
    assert r.status_code == 303
    assert await count(db, Notification, Notification.user_id == user.id, Notification.title == "New sign-in to your account") == 1
    client.cookies.clear()
    await login(client, "ip@example.com")  # same IP again: no new alert
    assert await count(db, Notification, Notification.user_id == user.id, Notification.title == "New sign-in to your account") == 1


async def test_login_upgrades_outdated_password_hashes(client, db):
    from argon2 import PasswordHasher

    user = await make_customer(db, email="old@example.com")
    weak = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1).hash(DEFAULT_PASSWORD)
    user.password_hash = weak
    await db.commit()
    r = await login(client, "old@example.com")
    assert r.status_code == 303
    await db.refresh(user)
    assert user.password_hash != weak and not needs_rehash(user.password_hash)


async def test_logout_revokes_the_session_and_clears_the_cookie(client, db):
    user = await make_customer(db)
    await login_as(client, user)
    token = client.cookies.get(session_cookie_name())
    r = await post_form(client, "/auth/logout", {})
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert any(h.startswith(f"{session_cookie_name()}=") and "Max-Age=0" in h for h in r.headers.get_list("set-cookie"))
    assert (await fresh(db, UserSession, UserSession.id == token_hash(token))).revoked_at is not None
    client.cookies.clear()
    client.cookies.set(session_cookie_name(), token)
    r = await client.get("/account")
    assert r.status_code == 303 and r.headers["location"].startswith("/auth/login")


# ------------------------------------------------------------------ password reset
async def test_forgot_password_never_reveals_accounts_and_reset_is_single_use(client, db):
    user = await make_customer(db, email="reset@example.com")
    async with make_client() as other_device:
        await login_as(other_device, user)
    r1 = await post_form(client, "/auth/forgot", {"identifier": "reset@example.com"})
    r2 = await post_form(client, "/auth/forgot", {"identifier": "nobody@example.com"})
    assert r1.status_code == r2.status_code == 303
    assert r1.headers["location"] == r2.headers["location"] == "/auth/forgot?sent=1"
    tokens = (await db.execute(select(AuthToken).where(AuthToken.purpose == TokenPurpose.PASSWORD_RESET))).scalars().all()
    assert len(tokens) == 1 and tokens[0].user_id == user.id
    assert tokens[0].expires_at < utcnow() + timedelta(hours=1, minutes=1)
    assert await count(db, EmailOutbox, EmailOutbox.to_email == "nobody@example.com") == 0
    mail = await db.scalar(select(EmailOutbox).where(EmailOutbox.to_email == "reset@example.com", EmailOutbox.subject.contains("Reset")))
    raw = re.search(r"/auth/reset\?token=([A-Za-z0-9_-]+)", mail.text_body).group(1)
    assert token_hash(raw) == tokens[0].token_hash

    r = await client.get(f"/auth/reset?token={raw}")
    assert r.status_code == 200 and "Choose a new password" in page(r)
    r = await post_form(client, "/auth/reset", {"token": raw, "password": "short", "password_confirm": "short"})
    assert r.status_code == 400 and "at least 10 characters" in page(r)
    r = await post_form(client, "/auth/reset", {"token": raw, "password": NEW_PASSWORD, "password_confirm": NEW_PASSWORD})
    assert r.status_code == 303 and r.headers["location"] == "/auth/login"
    await db.refresh(user)
    assert await verify_password(user.password_hash, NEW_PASSWORD)
    assert await count(db, UserSession, UserSession.user_id == user.id, UserSession.revoked_at.is_(None)) == 0
    assert await count(db, Notification, Notification.user_id == user.id, Notification.title == "Your password was changed") == 1

    r = await client.get(f"/auth/reset?token={raw}")
    assert r.status_code == 400 and "already used" in page(r)
    r = await post_form(client, "/auth/reset", {"token": raw, "password": STRONG, "password_confirm": STRONG})
    assert r.status_code == 400
    r = await login(client, "reset@example.com", NEW_PASSWORD)
    assert r.status_code == 303


async def test_forgot_accepts_username_and_throttles_repeats(client, db):
    user = await make_customer(db, email="handle@example.com")
    user.username = "handle.user"
    await db.commit()
    await post_form(client, "/auth/forgot", {"identifier": "@handle.user"})
    await post_form(client, "/auth/forgot", {"identifier": "handle@example.com"})
    assert await count(db, AuthToken, AuthToken.user_id == user.id, AuthToken.purpose == TokenPurpose.PASSWORD_RESET) == 1
    r = await post_form(client, "/auth/forgot", {"identifier": "x"})
    assert r.status_code == 400


async def test_expired_and_bogus_reset_links_are_rejected(client, db):
    user = await make_customer(db)
    raw = await af.issue_token(db, TokenPurpose.PASSWORD_RESET, user_id=user.id, email=user.email, ttl=timedelta(seconds=-1))
    await db.commit()
    r = await client.get(f"/auth/reset?token={raw}")
    assert r.status_code == 400 and "expired" in page(r)
    r = await client.get("/auth/reset?token=not-a-real-token-at-all-123")
    assert r.status_code == 400 and "isn't valid" in page(r)


# ------------------------------------------------------------------ e-mail verification
async def test_verify_email_link_is_single_use_and_bound_to_the_address(client, db):
    user = await make_customer(db, email="v@example.com")
    user.email_verified_at = None
    await db.commit()
    raw = await af.issue_token(db, TokenPurpose.EMAIL_VERIFY, user_id=user.id, email=user.email)
    stale = await af.issue_token(db, TokenPurpose.EMAIL_VERIFY, user_id=user.id, email="previous@example.com", supersede=False)
    await db.commit()
    r = await client.get(f"/auth/verify-email?token={stale}")
    assert r.status_code == 400
    r = await client.get(f"/auth/verify-email?token={raw}")
    assert r.status_code == 200 and "E-mail confirmed" in page(r)
    await db.refresh(user)
    assert user.email_verified_at is not None
    r = await client.get(f"/auth/verify-email?token={raw}")
    assert r.status_code == 400 and "already used" in page(r)


async def test_resending_the_verification_link_is_throttled(client, db):
    user = await make_customer(db, email="resend@example.com")
    user.email_verified_at = None
    await db.commit()
    await login_as(client, user)
    r = await post_form(client, "/auth/verify-email/resend", {})
    assert r.status_code == 303 and r.headers["location"] == "/account"
    r = await post_form(client, "/auth/verify-email/resend", {})
    assert r.status_code == 303
    assert await count(db, AuthToken, AuthToken.user_id == user.id, AuthToken.purpose == TokenPurpose.EMAIL_VERIFY) == 1


# ------------------------------------------------------------------ customer invite (created by WP-F)
async def test_customer_invite_sets_password_and_signs_in(client, db):
    owner = await make_owner(db, full_name="Rhea Kapoor")
    member = await make_customer(db, email="invited@example.com", password=None, full_name="Dev Malhotra")
    prof = await db.get(CustomerProfile, member.id)
    prof.registered_by_id = owner.id
    prof.intake_source = "owner"
    await db.commit()
    raw = await af.issue_token(db, TokenPurpose.INVITE, user_id=member.id, email=member.email)
    await db.commit()

    r = await client.get(f"/auth/invite?token={raw}")
    assert r.status_code == 200 and "Rhea Kapoor registered you" in page(r)
    r = await post_form(client, "/auth/invite", {"token": raw, "password": STRONG, "password_confirm": STRONG})
    assert r.status_code == 400 and "accept the Terms" in page(r)
    r = await post_form(client, "/auth/invite", {"token": raw, "password": STRONG, "password_confirm": "nope", "accept_terms": "1"})
    assert r.status_code == 400 and "don't match" in page(r)
    r = await post_form(client, "/auth/invite", {"token": raw, "password": STRONG, "password_confirm": STRONG,
                                                 "accept_terms": "1", "timezone": "Asia/Kolkata"})
    assert r.status_code == 303 and r.headers["location"] == "/me" and cookie_from(r)
    await db.refresh(member)
    assert await verify_password(member.password_hash, STRONG) and member.email_verified_at is not None
    assert member.username
    assert await count(db, Notification, Notification.user_id == member.id, Notification.title == "Your account is ready") == 1
    r = await client.get(f"/auth/invite?token={raw}")
    assert r.status_code == 400 and "already used" in page(r)


# ------------------------------------------------------------------ staff invite (created by WP-C1)
async def _staff_invite(db, email: str = "coach@example.com"):
    owner = await make_owner(db, full_name="Sana Iqbal")
    gym = await make_gym(db, owner, name="Iron Temple")
    emp = await make_employee(db, gym, status=EmploymentStatus.INVITED)
    emp.invite_email = email
    emp.full_name = "Arjun Rao"
    await db.commit()
    raw = await af.issue_token(db, TokenPurpose.STAFF_INVITE, employee_id=emp.id, email=email)
    await db.commit()
    return owner, gym, emp, raw


def staff_form(raw: str, **over) -> dict:
    data = {
        "token": raw, "action": "create", "full_name": "Arjun Rao", "phone": "98450 12345", "phone_country": "IN",
        "date_of_birth": years_ago(28), "country": "IN", "city": "Bengaluru", "timezone": "Asia/Kolkata",
        "password": STRONG, "password_confirm": STRONG, "accept_terms": "1", "id_doc_type": "driving_license", "id_country": "IN",
    }
    data.update(over)
    return {k: v for k, v in data.items() if v is not None}


async def test_staff_invite_creates_and_links_a_new_staff_account(client, db):
    owner, gym, emp, raw = await _staff_invite(db)
    r = await client.get(f"/auth/staff-invite?token={raw}")
    assert r.status_code == 200 and "Join Iron Temple" in page(r) and "Create your staff account" in page(r)
    assert "Sana Iqbal" in page(r) and "coach@example.com" in page(r)

    r = await post_form(client, "/auth/staff-invite", staff_form(raw))
    text = page(r)
    assert r.status_code == 400 and "photo is required for staff" in text and "front of your government ID" in text
    r = await post_form(client, "/auth/staff-invite", staff_form(raw, date_of_birth=years_ago(15)), files=id_files(photo=True))
    assert r.status_code == 400 and "at least 16" in page(r)
    assert await count(db, User, User.role == Role.STAFF) == 0

    r = await post_form(client, "/auth/staff-invite", staff_form(raw), files=id_files(photo=True))
    assert r.status_code == 303 and r.headers["location"] == "/staff" and cookie_from(r)
    staff = await fresh(db, User, User.email == "coach@example.com")
    assert staff.role == Role.STAFF and staff.photo_key.startswith("public/avatars/") and staff.email_verified_at
    assert staff.id_status == IdStatus.PENDING and staff.username
    await db.refresh(emp)
    assert emp.user_id == staff.id and emp.status == EmploymentStatus.PENDING_VERIFICATION
    tok = await db.scalar(select(AuthToken).where(AuthToken.employee_id == emp.id))
    assert tok.used_at is not None
    job = await db.scalar(select(Job).where(Job.dedupe_key == f"staff_activation_check:{emp.id}"))
    assert job is not None and job.kind == "staff_activation_check" and job.payload == {"employee_id": emp.id}
    doc = await db.scalar(select(IdentityDocument).where(IdentityDocument.user_id == staff.id))
    assert doc is not None and doc.employee_id == emp.id
    assert await count(db, Notification, Notification.user_id == owner.id, Notification.title.contains("accepted your staff invitation")) == 1
    assert await count(db, Notification, Notification.user_id == staff.id, Notification.title == "You joined Iron Temple") == 1

    client.cookies.clear()
    r = await client.get(f"/auth/staff-invite?token={raw}")
    assert r.status_code == 400 and "already used" in page(r)


async def test_staff_invite_links_an_existing_signed_in_staff_account(client, db):
    owner, gym, emp, raw = await _staff_invite(db, email="trainer@example.com")
    staff = await make_user(db, role=Role.STAFF, email="trainer@example.com")
    await login_as(client, staff)
    r = await client.get(f"/auth/staff-invite?token={raw}")
    assert r.status_code == 200 and "Link my account" in page(r)
    r = await post_form(client, "/auth/staff-invite", {"token": raw, "action": "link"})
    assert r.status_code == 303 and r.headers["location"] == "/staff"
    await db.refresh(emp)
    assert emp.user_id == staff.id and emp.status == EmploymentStatus.PENDING_VERIFICATION
    assert await count(db, Job, Job.dedupe_key == f"staff_activation_check:{emp.id}") == 1


async def test_staff_invite_refuses_a_different_e_mail(client, db):
    owner, gym, emp, raw = await _staff_invite(db, email="trainer@example.com")
    other = await make_user(db, role=Role.STAFF, email="someone@example.com")
    await login_as(client, other)
    r = await client.get(f"/auth/staff-invite?token={raw}")
    assert r.status_code == 200 and "This invitation was sent to" in page(r) and "Link my account" not in page(r)
    r = await post_form(client, "/auth/staff-invite", {"token": raw, "action": "link"})
    assert r.status_code == 403
    await db.refresh(emp)
    assert emp.user_id is None and emp.status == EmploymentStatus.INVITED
    assert await count(db, Job, Job.kind == "staff_activation_check") == 0


async def test_staff_invite_refuses_member_and_owner_accounts(client, db):
    owner, gym, emp, raw = await _staff_invite(db, email="member@example.com")
    member = await make_customer(db, email="member@example.com")
    await login_as(client, member)
    r = await post_form(client, "/auth/staff-invite", {"token": raw, "action": "link"})
    assert r.status_code == 403 and "can't be linked to a staff" in page(r)
    client.cookies.clear()
    await login_as(client, owner)
    r = await post_form(client, "/auth/staff-invite", {"token": raw, "action": "link"})
    assert r.status_code == 403
    # the create path can't reuse the member's e-mail either
    client.cookies.clear()
    r = await post_form(client, "/auth/staff-invite", staff_form(raw), files=id_files(photo=True))
    assert r.status_code == 400 and "already exists" in page(r)
    await db.refresh(emp)
    assert emp.user_id is None


async def test_staff_invite_link_requires_sign_in_and_bad_tokens_fail(client, db):
    owner, gym, emp, raw = await _staff_invite(db)
    r = await post_form(client, "/auth/staff-invite", {"token": raw, "action": "link"})
    assert r.status_code == 303 and r.headers["location"].startswith("/auth/login?next=%2Fauth%2Fstaff-invite")
    r = await client.get("/auth/staff-invite?token=this-token-does-not-exist-123")
    assert r.status_code == 400 and "isn't valid" in page(r)
    # a token for an invite whose e-mail was changed by the owner no longer works
    emp.invite_email = "changed@example.com"
    await db.commit()
    r = await client.get(f"/auth/staff-invite?token={raw}")
    assert r.status_code == 400


# ------------------------------------------------------------------ outdated pages (hunt F15)
# what a real browser sends with a form of this app's own pages
BROWSER = {"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "navigate",
           "Accept": "text/html,application/xhtml+xml"}


def form_token(text: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', text)
    assert m, "no CSRF token on the page"
    return m.group(1)


def flash_of(r) -> str:
    from itsdangerous import URLSafeSerializer

    from app.core.templating import FLASH_COOKIE

    raw = r.cookies.get(FLASH_COOKIE)
    if not raw:
        return ""
    return " | ".join(d["m"] for d in URLSafeSerializer(get_settings().secret_key, salt="ironpulse-flash").loads(raw))


async def test_a_sign_in_page_left_open_in_another_tab_still_works_and_never_bans(db):
    """Hunt F15: tab 2's sign-in form (rendered signed out) sent after tab 1 signed in got a bare 403 'CSRF validation
    failed - please reload the page' - and every such 403 was 3 threat points: five of them (the page's own advice was
    to reload, which re-posts the same token) banned the member's address for 2 hours."""
    from app.security.bans import ban_registry, threat_tracker

    ip = "198.51.100.40"
    await make_customer(db, email="tabs@example.com")
    async with make_client(ip) as browser:
        tab2 = form_token((await browser.get("/auth/login")).text)
        signup_tab = form_token((await browser.get("/auth/signup")).text)
        tab1 = form_token((await browser.get("/auth/login")).text)
        creds = {"identifier": "tabs@example.com", "password": DEFAULT_PASSWORD}
        r = await browser.post("/auth/login", data={**creds, "csrf_token": tab1}, headers=BROWSER)
        assert r.status_code == 303 and r.headers["location"] == "/me"
        for _ in range(6):  # the stale tab - and "reload" - again and again
            r = await browser.post("/auth/login", data={**creds, "csrf_token": tab2}, headers=BROWSER)
            assert r.status_code == 303 and r.headers["location"] == "/me", page(r)[:300]
        assert (await browser.get("/me")).status_code == 200
        r = await browser.post("/auth/signup", data={**signup_data(email="other@example.com"), "csrf_token": signup_tab},
                               headers=BROWSER)
        assert r.status_code == 303 and "already signed in" in flash_of(r)
        assert await count(db, User, User.email == "other@example.com") == 0
    assert threat_tracker.score(ip) == 0.0 and ban_registry.get(ip) is None
    assert not any(e.category == ThreatCategory.CSRF for e in security_events.events)


async def test_a_signed_in_page_left_open_after_signing_out_goes_back_with_a_notice(db):
    """The reverse: a page of a signed-in session sent after signing out (or in as someone else) in another tab. It used
    to be the 403 page telling the user to reload (= re-post the stale token) - and 3 threat points each time. Now: a
    303 back to the page it came from with a notice, nothing changed, never a threat point."""
    from app.security.bans import ban_registry, threat_tracker

    ip = "198.51.100.41"
    user = await make_customer(db, email="reverse@example.com")
    async with make_client(ip) as browser:
        r = await browser.post("/auth/login", data={"identifier": "reverse@example.com", "password": DEFAULT_PASSWORD,
                                                    "csrf_token": form_token((await browser.get("/auth/login")).text)},
                               headers=BROWSER)
        assert r.status_code == 303
        stale = form_token((await browser.get("/account")).text)
        r = await browser.post("/auth/logout", data={"csrf_token": stale}, headers=BROWSER)  # signed out "elsewhere"
        assert r.status_code == 303
        form = {"current_password": DEFAULT_PASSWORD, "new_password": NEW_PASSWORD, "new_password_confirm": NEW_PASSWORD,
                "csrf_token": stale}
        for _ in range(6):
            r = await browser.post("/account/password", data=form,
                                   headers={**BROWSER, "Referer": "http://testserver/account?tab=security"})
            assert r.status_code == 303 and r.headers["location"] == "/account?tab=security", r.text[:300]
            assert "This page was out of date" in flash_of(r) and not r.content
        r = await browser.post("/account/password", data=form, headers={**BROWSER, "Referer": "https://evil.example/x"})
        assert r.status_code == 303 and r.headers["location"] == "/"  # never a redirect off this site
        # a fetch() call with the outdated token gets a 403 that says so (no redirect to follow)
        r = await browser.post("/api/notifications/read-all", headers={**BROWSER, "Accept": "application/json",
                                                                       "X-CSRF-Token": stale})
        assert r.status_code == 403 and r.json()["detail"].startswith("This page was out of date")
    await db.refresh(user)
    assert await verify_password(user.password_hash, DEFAULT_PASSWORD)  # nothing changed
    assert threat_tracker.score(ip) == 0.0 and ban_registry.get(ip) is None
    assert not any(e.category == ThreatCategory.CSRF for e in security_events.events)


async def test_a_bad_token_without_browser_metadata_still_counts(client, db):
    """Scripts and tools (no Origin, no Sec-Fetch-Site) are still scored for CSRF failures."""
    from app.security.bans import threat_tracker

    async with make_client("198.51.100.42") as tool:
        await tool.get("/healthz")
        r = await tool.post("/auth/login", data={"identifier": "x@example.com", "password": "y", "csrf_token": "forged"})
        assert r.status_code == 403
    assert threat_tracker.score("198.51.100.42") > 0
    assert any(e.category == ThreatCategory.CSRF and e.reviewed is None for e in security_events.events)


# ------------------------------------------------------------------ staff invitations after a violation (hunt F45)
async def _pending_invite(db, gym, email: str):
    emp = await make_employee(db, gym, status=EmploymentStatus.INVITED)
    emp.invite_email = email
    emp.full_name = "Farhan Qureshi"
    await db.commit()
    raw = await af.issue_token(db, TokenPurpose.STAFF_INVITE, employee_id=emp.id, email=email)
    await db.commit()
    return emp, raw


async def _remove_for_violation(db, owner, emp, reason: str = "unprofessional_conduct") -> None:
    async with make_client() as oc:
        await login_as(oc, owner)
        r = await post_form(oc, f"/owner/staff/{emp.id}/remove",
                            {"reason": reason, "note": "Shouted at a member during a session.", "confirm": "1"})
        assert r.status_code == 303, page(r)[:500]


async def test_an_invitation_pending_before_a_violation_can_no_longer_be_linked(client, db):
    """Hunt F45: owner 13 invited Farhan to their second gym, then removed him at the first one for a conduct violation
    ("your gyms can never add them again"). A NEW invitation was refused - but the pending one was still accepted, and
    the record went ACTIVE at the owner's gym. Now the removal withdraws the pending invitation with it (trust_safety
    ``_withdraw_pending_records``); one that was not withdrawn (sent past the cap, or written some other way) is
    refused when it is accepted."""
    from app.models import Employee
    from app.services import trust_safety as TS

    owner = await make_owner(db)
    gym_a, gym_b, gym_c = await make_gym(db, owner), await make_gym(db, owner), await make_gym(db, owner)
    person = await make_user(db, role=Role.STAFF, email="farhan@example.com")
    emp_a = await make_employee(db, gym_a, user=person)
    emp_b, raw = await _pending_invite(db, gym_b, "farhan@example.com")
    await _remove_for_violation(db, owner, emp_a)

    await login_as(client, person)
    r = await client.get(f"/auth/staff-invite?token={raw}")  # withdrawn with the removal: its link is gone
    assert r.status_code == 400 and "Link my account" not in page(r)
    r = await post_form(client, "/auth/staff-invite", {"token": raw, "action": "link"})
    assert r.status_code == 400 and "Shouted" not in page(r)
    emp_b = await fresh(db, Employee, Employee.id == emp_b.id)
    assert emp_b.user_id is None and emp_b.status == EmploymentStatus.TERMINATED
    assert emp_b.removal_reason == "unprofessional_conduct"
    assert await fresh(db, AuthToken, AuthToken.employee_id == emp_b.id) is None

    # an invitation the removal did not withdraw is still refused when it is accepted - neutrally, nothing claimed
    emp_c, raw_c = await _pending_invite(db, gym_c, "farhan@example.com")
    r = await client.get(f"/auth/staff-invite?token={raw_c}")
    assert r.status_code == 403 and "can't be accepted" in page(r) and "Link my account" not in page(r)
    r = await post_form(client, "/auth/staff-invite", {"token": raw_c, "action": "link"})
    text = page(r)
    assert r.status_code == 403 and "can't be accepted" in text
    assert "Shouted" not in text and "conduct" not in text.lower() and TS.CANT_ADD not in text  # never why
    emp_c = await fresh(db, Employee, Employee.id == emp_c.id)
    assert emp_c.user_id is None and emp_c.status == EmploymentStatus.INVITED
    assert await count(db, Job, Job.kind == "staff_activation_check") == 0
    tok = await fresh(db, AuthToken, AuthToken.employee_id == emp_c.id)
    assert tok is not None and tok.used_at is None  # nothing was claimed


async def test_an_invitation_to_an_address_removed_for_a_violation_cannot_create_an_account(client, db):
    """The create path: the owner invited the same address to two gyms and removed the first (never accepted) one for a
    violation - the second invitation is withdrawn with it, and one the removal did not withdraw can't be used to sign
    up as staff at that owner's gym either."""
    from app.models import Employee

    owner = await make_owner(db)
    gym_a, gym_b, gym_c = await make_gym(db, owner), await make_gym(db, owner), await make_gym(db, owner)
    first, _ = await _pending_invite(db, gym_a, "never.linked@example.com")
    second, raw = await _pending_invite(db, gym_b, "never.linked@example.com")
    await _remove_for_violation(db, owner, first, reason="policy_violation")

    r = await client.get(f"/auth/staff-invite?token={raw}")  # withdrawn with the removal
    assert r.status_code == 400
    r = await post_form(client, "/auth/staff-invite", staff_form(raw), files=id_files(photo=True))
    assert r.status_code == 400
    assert (await fresh(db, Employee, Employee.id == second.id)).status == EmploymentStatus.TERMINATED
    assert await fresh(db, AuthToken, AuthToken.employee_id == second.id) is None

    third, raw = await _pending_invite(db, gym_c, "never.linked@example.com")  # not withdrawn: refused on use
    r = await client.get(f"/auth/staff-invite?token={raw}")
    assert r.status_code == 403 and "can't be accepted" in page(r)
    r = await post_form(client, "/auth/staff-invite", staff_form(raw), files=id_files(photo=True))
    assert r.status_code == 403 and "can't be accepted" in page(r)
    assert await count(db, User, User.email == "never.linked@example.com") == 0
    assert (await fresh(db, AuthToken, AuthToken.employee_id == third.id)).used_at is None
    # another owner's gym may still invite and add the address
    other_owner = await make_owner(db)
    _, raw_other = await _pending_invite(db, await make_gym(db, other_owner), "never.linked@example.com")
    r = await post_form(client, "/auth/staff-invite", staff_form(raw_other), files=id_files(photo=True))
    assert r.status_code == 303 and r.headers["location"] == "/staff"
