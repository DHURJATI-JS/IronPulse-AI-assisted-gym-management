"""WP-A2 account tests: profile edits (+ notification/e-mail), e-mail re-verification, username, photo rules,
password change, sessions, deletion (+ blocked owners), notification history + JSON APIs (scoped, N+1-free)
and the location API."""

from __future__ import annotations

import html
import itertools
import re
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from app.core.utils import utcnow
from app.db import QueryCounter
from app.models import AuthToken, EmailOutbox, Notification, User, UserSession
from app.models.enums import IdStatus, NotificationCategory, Role, TokenPurpose
from app.security.crypto import token_hash
from app.security.passwords import verify_password
from app.security.sessions import session_cookie_name
from app.services import storage
from app.services.notify import notify
from tests.conftest import api, login_as, make_client, post_form
from tests.factories import DEFAULT_PASSWORD, make_customer, make_employee, make_gym, make_owner, make_user, png_bytes

NEW_PASSWORD = "Fresh-Barbell-2026!"
_phones = itertools.count(1)


def page(r) -> str:
    return html.unescape(r.text)


def years_ago(years: int) -> str:
    today = date.today()
    try:
        return today.replace(year=today.year - years).isoformat()
    except ValueError:
        return today.replace(year=today.year - years, day=28).isoformat()


async def count(db, model, *where) -> int:
    return int(await db.scalar(select(func.count()).select_from(model).where(*where)) or 0)


def profile_form(user: User, **over) -> dict:
    data = {
        "full_name": user.full_name, "email": user.email, "username": user.username or "", "phone": user.phone or "",
        "phone_country": "IN", "date_of_birth": user.date_of_birth.isoformat() if user.date_of_birth else "",
        "country": user.country_code or "IN", "city": user.city or "Bengaluru", "timezone": user.timezone,
    }
    data.update(over)
    return data


async def member(db, **kw) -> User:
    kw.setdefault("country_code", "IN")
    kw.setdefault("city", "Bengaluru")
    kw.setdefault("phone", f"+91 97{next(_phones):08d}")
    return await make_customer(db, **kw)


# ------------------------------------------------------------------ access + rendering
async def test_account_pages_require_sign_in(client):
    for path in ("/account", "/account/notifications", "/account/delete"):
        r = await client.get(path)
        assert r.status_code == 303 and r.headers["location"].startswith("/auth/login?next=%2Faccount")
    r = await client.get("/api/notifications")
    assert r.status_code == 401 and r.headers["content-type"].startswith("application/json")
    r = await api(client, "POST", "/api/me/location", json={"lat": 12.97, "lon": 77.64})
    assert r.status_code == 401


async def test_account_pages_render_for_every_role(client, db):
    users = [await make_customer(db), await make_owner(db), await make_user(db, role=Role.STAFF),
             await make_user(db, role=Role.SUPERADMIN)]
    for user in users:
        client.cookies.clear()
        await login_as(client, user)
        for path in ("/account", "/account/notifications", "/account/delete", "/account/notifications?filter=unread&page=9"):
            r = await client.get(path)
            assert r.status_code == 200, (path, user.role, r.text[:500])
            assert ' style="' not in r.text


# ------------------------------------------------------------------ profile
async def test_profile_change_creates_notification_and_email(client, db):
    user = await member(db, email="me@example.com", phone="+91 98765 40000")
    await login_as(client, user)
    r = await post_form(client, "/account", profile_form(user, full_name="Meera Iyer", city="Mysuru", timezone="Asia/Kolkata"))
    assert r.status_code == 303 and r.headers["location"] == "/account"
    await db.refresh(user)
    assert user.full_name == "Meera Iyer" and user.city == "Mysuru"
    n = await db.scalar(select(Notification).where(Notification.user_id == user.id,
                                                   Notification.title == "Your account details were changed"))
    assert n is not None and n.category == NotificationCategory.ACCOUNT
    assert n.body.startswith("Your account details were changed: full name, city")
    mail = await db.scalar(select(EmailOutbox).where(EmailOutbox.notification_id == n.id))
    assert mail is not None and mail.to_email == "me@example.com"

    # saving again without changes sends nothing
    r = await post_form(client, "/account", profile_form(user))
    assert r.status_code == 303
    assert await count(db, Notification, Notification.user_id == user.id) == 1


async def test_profile_phone_must_be_unique_but_own_number_is_fine(client, db):
    a = await member(db, phone="+91 98765 40001")
    await member(db, phone="+91 98765 40002")
    await login_as(client, a)
    r = await post_form(client, "/account", profile_form(a, phone="98765 40002"))
    assert r.status_code == 400 and "An account with this phone already exists" in page(r)
    r = await post_form(client, "/account", profile_form(a, phone="+91 98765 40001", city="Pune"))
    assert r.status_code == 303
    await db.refresh(a)
    assert a.city == "Pune" and a.phone_normalized == "+919876540001"


async def test_email_change_needs_reverification_and_warns_the_old_address(client, db):
    user = await member(db, email="old@example.com")
    await member(db, email="taken@example.com")
    await login_as(client, user)
    r = await post_form(client, "/account", profile_form(user, email="taken@example.com"))
    assert r.status_code == 400 and "already exists" in page(r)
    r = await post_form(client, "/account", profile_form(user, email="New@Example.com"))
    assert r.status_code == 303
    await db.refresh(user)
    assert user.email == "new@example.com" and user.email_verified_at is None
    old_mail = await db.scalar(select(EmailOutbox).where(EmailOutbox.to_email == "old@example.com"))
    assert old_mail is not None and "e-mail address was changed" in old_mail.subject.lower()
    tok = await db.scalar(select(AuthToken).where(AuthToken.user_id == user.id, AuthToken.purpose == TokenPurpose.EMAIL_VERIFY))
    assert tok is not None and tok.email == "new@example.com"
    assert await count(db, EmailOutbox, EmailOutbox.to_email == "new@example.com", EmailOutbox.subject.contains("Confirm")) == 1
    n = await db.scalar(select(Notification).where(Notification.user_id == user.id))
    assert "e-mail address" in n.body
    assert (await db.scalar(select(EmailOutbox).where(EmailOutbox.notification_id == n.id))).to_email == "new@example.com"


async def test_username_can_be_changed_but_must_be_valid_and_free(client, db):
    user = await member(db)
    user.username = "first.name"
    other = await member(db)
    other.username = "someone.else"
    await db.commit()
    await login_as(client, user)
    for bad, msg in (("someone.else", "taken"), ("ab", "3-30 characters"), ("support", "reserved")):
        r = await post_form(client, "/account", profile_form(user, username=bad))
        assert r.status_code == 400 and msg in page(r), bad
    r = await post_form(client, "/account", profile_form(user, username="@New.Handle"))
    assert r.status_code == 303
    await db.refresh(user)
    assert user.username == "new.handle"
    n = await db.scalar(select(Notification).where(Notification.user_id == user.id))
    assert "username" in n.body


async def test_profile_validation_errors(client, db):
    user = await member(db, id_status=IdStatus.PENDING)
    await login_as(client, user)
    r = await post_form(client, "/account", profile_form(user, timezone="Mars/Olympus_Mons"))
    assert r.status_code == 400 and "valid time zone" in page(r)
    r = await post_form(client, "/account", profile_form(user, date_of_birth=years_ago(15)))
    assert r.status_code == 400 and "guardian on file" in page(r)
    r = await post_form(client, "/account", profile_form(user, full_name="", email="nope", phone="abc", city=""))
    text = page(r)
    assert r.status_code == 400 and "Enter your full name" in text and "valid e-mail" in text and "digits only" in text
    await db.refresh(user)
    assert user.full_name.startswith("Test User")

    owner = await make_owner(db, id_status=IdStatus.PENDING, country_code="IN", city="Bengaluru", phone="+91 97000 00017")
    client.cookies.clear()
    await login_as(client, owner)
    r = await post_form(client, "/account", profile_form(owner, date_of_birth=years_ago(17)))
    assert r.status_code == 400 and "at least 18" in page(r)


async def test_date_of_birth_is_locked_once_the_id_is_verified(client, db):
    user = await member(db, id_status=IdStatus.VERIFIED)
    await login_as(client, user)
    r = await client.get("/account")
    assert "Verified against your ID card" in page(r)
    r = await post_form(client, "/account", profile_form(user, date_of_birth="2001-01-01", city="Chennai"))
    assert r.status_code == 303
    await db.refresh(user)
    assert user.date_of_birth == date(1995, 5, 17) and user.city == "Chennai"


# ------------------------------------------------------------------ photo
async def test_photo_upload_replace_and_remove_rules(client, db):
    user = await member(db)
    await login_as(client, user)
    photo = {"photo": ("me.png", png_bytes((200, 200), "orange"), "image/png")}
    r = await post_form(client, "/account/photo", {}, files=photo)
    assert r.status_code == 303 and r.headers["location"] == "/account#photo"
    await db.refresh(user)
    first = user.photo_key
    assert first.startswith("public/avatars/") and storage.path_for(first).is_file()

    r = await post_form(client, "/account/photo", {}, files={"photo": ("me2.png", png_bytes((90, 90), "teal"), "image/png")})
    assert r.status_code == 303
    await db.refresh(user)
    assert user.photo_key != first and not storage.path_for(first).exists()

    r = await post_form(client, "/account/photo", {}, files={"photo": ("evil.png", b"<html>not an image</html>", "image/png")})
    assert r.status_code == 400 and "not a valid image" in page(r)
    r = await post_form(client, "/account/photo", {})
    assert r.status_code == 400 and "Choose a photo" in page(r)

    second = user.photo_key
    r = await post_form(client, "/account/photo", {"action": "remove"})
    assert r.status_code == 303
    await db.refresh(user)
    assert user.photo_key is None and not storage.path_for(second).exists()
    titles = (await db.execute(select(Notification.title).where(Notification.user_id == user.id))).scalars().all()
    assert "Your profile photo was updated" in titles and "Your profile photo was removed" in titles


@pytest.mark.parametrize("role", [Role.OWNER, Role.STAFF])
async def test_owners_and_staff_cannot_remove_their_photo(client, db, role):
    user = await make_user(db, role=role)
    key = user.photo_key
    await login_as(client, user)
    r = await post_form(client, "/account/photo", {"action": "remove"})
    assert r.status_code == 400 and "must keep a profile photo" in page(r)
    await db.refresh(user)
    assert user.photo_key == key


# ------------------------------------------------------------------ password + sessions
async def test_password_change_requires_current_password_and_signs_out_other_devices(client, db):
    user = await member(db)
    async with make_client() as other_device:
        await login_as(other_device, user)
        other_token = other_device.cookies.get(session_cookie_name())
    await login_as(client, user)
    mine = client.cookies.get(session_cookie_name())

    form = {"current_password": "Wrong-Passw0rd!", "new_password": NEW_PASSWORD, "new_password_confirm": NEW_PASSWORD}
    r = await post_form(client, "/account/password", form)
    assert r.status_code == 400 and "current password isn't correct" in page(r)
    r = await post_form(client, "/account/password", {**form, "current_password": DEFAULT_PASSWORD, "new_password": "weakpass",
                                                      "new_password_confirm": "weakpass"})
    assert r.status_code == 400 and "at least 10 characters" in page(r)
    r = await post_form(client, "/account/password", {"current_password": DEFAULT_PASSWORD, "new_password": DEFAULT_PASSWORD,
                                                      "new_password_confirm": DEFAULT_PASSWORD})
    assert r.status_code == 400 and "different from your current one" in page(r)
    r = await post_form(client, "/account/password", {**form, "current_password": DEFAULT_PASSWORD})
    assert r.status_code == 303 and r.headers["location"] == "/account#security"

    await db.refresh(user)
    assert await verify_password(user.password_hash, NEW_PASSWORD) and user.failed_login_count == 0
    assert (await db.get(UserSession, token_hash(other_token), populate_existing=True)).revoked_at is not None
    # this browser's session is renewed (hunt F14): the old token is revoked, the browser carries on with a new one
    assert (await db.get(UserSession, token_hash(mine), populate_existing=True)).revoked_at is not None
    fresh = renewed(client, r)
    assert fresh != mine and (await db.get(UserSession, token_hash(fresh), populate_existing=True)).revoked_at is None
    n = await db.scalar(select(Notification).where(Notification.user_id == user.id, Notification.title == "Your password was changed"))
    assert n is not None and n.category == NotificationCategory.SECURITY
    assert (await client.get("/account")).status_code == 200


async def test_sign_out_other_devices(client, db):
    user = await member(db)
    async with make_client() as other_device:
        await login_as(other_device, user)
        other_token = other_device.cookies.get(session_cookie_name())
    await login_as(client, user)
    r = await client.get("/account")
    assert "Signed in on 2 devices" in page(r)
    r = await post_form(client, "/account/sessions/revoke-others", {})
    assert r.status_code == 303
    assert (await db.get(UserSession, token_hash(other_token), populate_existing=True)).revoked_at is not None
    renewed(client, r)
    assert (await client.get("/account")).status_code == 200
    assert "Signed in on 1 device" in page(await client.get("/account"))


def renewed(client, r) -> str:
    """The session cookie a renewal set on ``r`` - and the client's jar holding only that one (``login_as`` set the old
    one without a domain, the response's carries the host's)."""
    raw = r.cookies.get(session_cookie_name())
    assert raw, "the response did not renew this browser's session"
    client.cookies.delete(session_cookie_name())
    client.cookies.set(session_cookie_name(), raw)
    return raw


async def test_a_copied_session_cookie_dies_with_a_password_change_or_sign_out_of_other_devices(db):
    """Hunt F14: both handlers kept the CURRENT session token, so a copy of the cookie (a shared or public computer,
    malware, a leaked log) kept full access - health data, chat, AI - while the page said every other device was signed
    out. Now every session of the account ends, this browser's too, and the browser gets a fresh one."""
    from app.models import RememberedAccount
    from app.security.crypto import new_token

    user = await member(db)
    async with make_client() as victim, make_client("198.51.100.31") as thief:
        thief.headers["User-Agent"] = "curl/8.0"
        await login_as(victim, user)
        stolen = victim.cookies.get(session_cookie_name())
        thief.cookies.set(session_cookie_name(), stolen)
        assert (await thief.get("/account")).status_code == 200
        # this device's account-chooser entry (issued with this session) and another device's
        db.add_all([RememberedAccount(id=token_hash(new_token(32)), user_id=user.id, device_hash="d" * 64,
                                      session_id=token_hash(stolen), label="Chrome on macOS"),
                    RememberedAccount(id=token_hash(new_token(32)), user_id=user.id, device_hash="e" * 64,
                                      session_id=token_hash(new_token(32)), label="Safari on iPhone")])
        await db.commit()

        r = await post_form(victim, "/account/password", {"current_password": DEFAULT_PASSWORD,
                                                          "new_password": NEW_PASSWORD, "new_password_confirm": NEW_PASSWORD})
        assert r.status_code == 303 and r.headers["location"] == "/account#security"
        fresh = renewed(victim, r)
        assert fresh != stolen
        r = await thief.get("/account")
        assert r.status_code == 303 and r.headers["location"].startswith("/auth/login")  # the copy is signed out
        assert (await victim.get("/account")).status_code == 200  # the person's own browser carries on
        rows = (await db.execute(select(RememberedAccount.label, RememberedAccount.session_id)
                                 .where(RememberedAccount.user_id == user.id)
                                 .execution_options(populate_existing=True))).all()
        assert [(label, sid) for label, sid in rows] == [("Chrome on macOS", token_hash(fresh))]  # follows the new one

        thief.cookies.delete(session_cookie_name())
        thief.cookies.set(session_cookie_name(), fresh)  # copied again
        assert (await thief.get("/account")).status_code == 200
        r = await post_form(victim, "/account/sessions/revoke-others", {})
        assert r.status_code == 303 and r.headers["location"] == "/account#security"
        newest = renewed(victim, r)
        assert newest != fresh
        assert (await thief.get("/account")).status_code == 303
        assert (await victim.get("/account")).status_code == 200
    live = (await db.execute(select(UserSession.id).where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None))
                             .execution_options(populate_existing=True))).scalars().all()
    assert live == [token_hash(newest)]
    # renewed, not "signed in on another device": the copy's next page says nothing about a sign-in elsewhere
    from app.security.sessions import lookup_session

    assert (await lookup_session(db, stolen)).ended is None and (await lookup_session(db, fresh)).ended is None


# ------------------------------------------------------------------ deletion
async def test_delete_account_flow(client, db):
    from app.services.accounts import identifier_conflict

    user = await member(db, email="bye@example.com", phone="+91 98765 40099")
    await login_as(client, user)
    r = await client.get("/account/delete")
    assert r.status_code == 200 and "What happens next" in page(r) and "bye@example.com" in page(r)
    r = await post_form(client, "/account/delete", {"password": "Wrong-Passw0rd!", "confirm_text": "DELETE"})
    assert r.status_code == 400 and "isn't correct" in page(r)
    r = await post_form(client, "/account/delete", {"password": DEFAULT_PASSWORD, "confirm_text": "delete"})
    assert r.status_code == 400 and "Type DELETE" in page(r)
    r = await post_form(client, "/account/delete", {"password": DEFAULT_PASSWORD, "confirm_text": "DELETE", "reason": "privacy"})
    assert r.status_code == 400 and "can never be used for a new IronPulse account" in page(r)  # the identity warning
    r = await post_form(client, "/account/delete", {"password": DEFAULT_PASSWORD, "confirm_text": "DELETE", "reason": "privacy",
                                                    "confirm_identity": "1"})
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert any(h.startswith(f"{session_cookie_name()}=") and "Max-Age=0" in h for h in r.headers.get_list("set-cookie"))

    await db.refresh(user)
    assert user.deleted_at is not None and not user.is_active
    assert user.purge_after - user.deleted_at == timedelta(days=30)
    assert "Privacy concerns" in user.deletion_reason
    assert await count(db, UserSession, UserSession.user_id == user.id, UserSession.revoked_at.is_(None)) == 0
    assert (await identifier_conflict(db, email="bye@example.com")) is not None
    assert (await client.get("/account")).status_code == 303
    client.cookies.clear()
    r = await post_form(client, "/auth/login", {"identifier": "bye@example.com", "password": DEFAULT_PASSWORD})
    assert r.status_code == 400 and "This account was deleted" in page(r)


async def test_owner_with_active_staff_sees_why_deletion_is_blocked(client, db):
    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    await make_employee(db, gym)
    await login_as(client, owner)
    r = await post_form(client, "/account/delete", {"password": DEFAULT_PASSWORD, "confirm_text": "DELETE",
                                                    "confirm_identity": "1"})
    assert r.status_code == 400 and "can't be deleted yet" in page(r) and "active employee" in page(r)
    await db.refresh(owner)
    assert owner.deleted_at is None and owner.is_active


async def test_superadmin_cannot_delete_their_account(client, db):
    admin = await make_user(db, role=Role.SUPERADMIN)
    await login_as(client, admin)
    r = await post_form(client, "/account/delete", {"password": DEFAULT_PASSWORD, "confirm_text": "DELETE",
                                                    "confirm_identity": "1"})
    assert r.status_code == 400 and "cannot be deleted" in page(r)


# ------------------------------------------------------------------ notifications
async def _notes(db, user, n, prefix="Note"):
    rows = []
    for i in range(n):
        rows.append(await notify(db, user, f"{prefix} {i}", f"Body {i}", category=NotificationCategory.SYSTEM, email=False))
    await db.commit()
    return rows


async def test_notifications_api_is_scoped_to_the_signed_in_user(client, db):
    me = await member(db)
    other = await member(db)
    mine = await _notes(db, me, 3, "Mine")
    theirs = (await _notes(db, other, 1, "Theirs"))[0]
    await login_as(client, me)

    r = await client.get("/api/notifications?limit=2")
    data = r.json()
    assert r.status_code == 200 and data["unread"] == 3 and [i["title"] for i in data["items"]] == ["Mine 2", "Mine 1"]
    assert set(data["items"][0]) == {"id", "title", "body", "severity", "category", "link", "created_at", "read"}
    assert data["items"][0]["read"] is False and data["items"][0]["category"] == "system"
    for bad in ("0", "51", "abc"):
        assert (await client.get(f"/api/notifications?limit={bad}")).status_code == 422

    r = await api(client, "POST", f"/api/notifications/{theirs.id}/read")
    assert r.status_code == 404
    r = await api(client, "POST", f"/api/notifications/{mine[0].id}/read")
    assert r.status_code == 200 and r.json() == {"ok": True, "id": mine[0].id, "unread": 2}
    r = await api(client, "POST", "/api/notifications/read-all")
    assert r.status_code == 200 and r.json()["updated"] == 2 and r.json()["unread"] == 0
    await db.refresh(theirs)
    assert theirs.read_at is None

    r = await client.get("/account/notifications")
    assert r.status_code == 200 and "Mine 0" in r.text and "Theirs" not in r.text


async def test_notification_history_pages_and_html_fallbacks(client, db):
    me = await member(db)
    other = await member(db)
    rows = await _notes(db, me, 23)
    theirs = (await _notes(db, other, 1, "Theirs"))[0]
    await login_as(client, me)
    r = await client.get("/account/notifications")
    assert r.status_code == 200 and "Note 22" in r.text and ">Note 2<" not in r.text and "Back to the newest" not in r.text
    assert "All <span class=\"ml-1 tabular-nums muted\">23</span>" in r.text  # bounded count, exact below the cap
    older = html.unescape(re.search(r'href="(/account/notifications\?before=\d+)"', r.text).group(1))
    assert older == f"/account/notifications?before={rows[3].id}"  # keyset: the oldest note shown is the cursor
    r = await client.get(older)
    assert ">Note 0<" in r.text and ">Note 2<" in r.text and "Note 22" not in r.text and "Back to the newest" in r.text
    assert "?before=" not in r.text.split('aria-label="Pagination"', 1)[1]  # nothing older than Note 0
    newer = html.unescape(re.search(r'href="(/account/notifications\?after=\d+)"', r.text).group(1))
    assert newer == f"/account/notifications?after={rows[2].id}"
    r = await client.get(newer)
    assert ">Note 3<" in r.text and "Note 22" in r.text and ">Note 2<" not in r.text
    assert r.text.index("Note 22") < r.text.index(">Note 3<")  # newest first in every window
    r = await post_form(client, f"/account/notifications/{rows[0].id}/read", {"before": str(rows[3].id)})
    assert r.status_code == 303 and r.headers["location"] == f"/account/notifications?before={rows[3].id}"
    r = await post_form(client, f"/account/notifications/{rows[1].id}/read", {"page": "2", "filter": "unread"})
    assert r.headers["location"] == "/account/notifications?filter=unread"  # legacy form fields: back to the newest
    r = await post_form(client, f"/account/notifications/{theirs.id}/read", {})
    assert r.status_code == 404
    r = await client.get("/account/notifications?filter=unread")
    assert r.status_code == 200 and r.text.count('data-unread="1"') == 20
    r = await post_form(client, "/account/notifications/read-all", {})
    assert r.status_code == 303
    r = await client.get("/account/notifications?filter=unread")
    assert "You're all caught up" in page(r)
    await db.refresh(theirs)
    assert theirs.read_at is None


async def test_notification_pages_use_a_constant_number_of_queries(client, db):
    me = await member(db)
    await login_as(client, me)

    async def measure(path: str) -> int:
        with QueryCounter() as qc:
            r = await client.get(path)
        assert r.status_code == 200
        return qc.count

    await _notes(db, me, 2)
    few = {p: await measure(p) for p in ("/account/notifications", "/api/notifications?limit=50", "/account")}
    await _notes(db, me, 30, "More")
    many = {p: await measure(p) for p in ("/account/notifications", "/api/notifications?limit=50", "/account")}
    assert many == few and all(v > 0 for v in few.values())


# ------------------------------------------------------------------ location API
async def test_location_api_validates_and_stores(client, db):
    user = await member(db, timezone="UTC")
    await login_as(client, user)
    r = await api(client, "POST", "/api/me/location",
                  json={"lat": 12.9716, "lon": 77.6412, "accuracy": 25, "timezone": "Asia/Kolkata", "city": "Bengaluru"})
    assert r.status_code == 200 and r.json()["ok"] is True
    await db.refresh(user)
    assert (user.latitude, user.longitude) == (12.9716, 77.6412) and user.timezone == "Asia/Kolkata"
    assert user.city == "Bengaluru" and user.location_updated_at > utcnow() - timedelta(minutes=1)

    for bad in ({"lat": 95, "lon": 10}, {"lat": 10, "lon": -190}, {"lat": 0, "lon": 0}, {"lat": "abc", "lon": 1},
                {"lon": 1}, {"lat": 12.9, "lon": 77.6, "accuracy": -5}, {"lat": 12.9, "lon": 77.6, "city": "x" * 200}):
        r = await api(client, "POST", "/api/me/location", json=bad)
        assert r.status_code == 422, bad
    r = await api(client, "POST", "/api/me/location", json={"lat": 12.3, "lon": 76.6, "timezone": "Not/A_Zone"})
    assert r.status_code == 200
    await db.refresh(user)
    assert user.timezone == "Asia/Kolkata" and user.latitude == 12.3
