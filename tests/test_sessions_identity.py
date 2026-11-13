"""Redis-backed sessions (session -> user id; latest user data from PostgreSQL), sign-in identifiers,
usernames and the 'forgot username' flow."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models import EmailOutbox, User
from app.security import sessions as sess_mod
from app.security.crypto import token_hash
from app.services.identity_lookup import (
    find_user_by_identifier,
    generate_username,
    member_id,
    parse_member_id,
    username_problem,
)
from tests.conftest import login_as, make_client, post_form
from tests.factories import make_customer, make_user


def _redis_configured() -> bool:
    return bool(get_settings().redis_url)


class _FakeRequest:
    headers = {"user-agent": "pytest"}
    scope = {"client": ("127.0.0.1", 1)}

    def __init__(self):
        self.client = ("127.0.0.1", 1)


async def test_session_roundtrip_reads_latest_user_data(db):
    user = await make_customer(db, full_name="Before Name")
    token = await sess_mod.create_session(db, user, _FakeRequest())
    await db.commit()
    if _redis_configured():
        r = await sess_mod.get_redis()
        assert r is not None and await r.get(sess_mod._key(token_hash(token))) == str(user.id)  # namespaced key
    user.full_name = "After Name"
    await db.commit()
    async with make_client() as c:
        c.cookies.set(sess_mod.session_cookie_name(), token)
        html = (await c.get("/profile", follow_redirects=True)).text
        assert "After Name" in html  # header/profile always show the latest DB values


async def test_revoke_clears_redis_and_db(db):
    user = await make_customer(db)
    t1 = await sess_mod.create_session(db, user, _FakeRequest())
    t2 = await sess_mod.create_session(db, user, _FakeRequest())
    await db.commit()
    await sess_mod.revoke_all_sessions(db, user.id, except_token=t2)
    await db.commit()
    assert await sess_mod.load_session(db, t1) is None
    assert (await sess_mod.load_session(db, t2)) is not None
    await sess_mod.revoke_session(db, t2)
    await db.commit()
    assert await sess_mod.load_session(db, t2) is None


async def test_falls_back_to_db_when_redis_is_unreachable(db, monkeypatch):
    user = await make_customer(db)
    monkeypatch.setattr(get_settings(), "redis_url", "redis://127.0.0.1:1/0")  # nothing listens there
    monkeypatch.setattr(sess_mod, "_redis_down_until", 0.0)
    token = await sess_mod.create_session(db, user, _FakeRequest())
    await db.commit()
    s = await sess_mod.load_session(db, token)
    assert s is not None and s.user.id == user.id
    monkeypatch.setattr(sess_mod, "_redis_down_until", 0.0)


async def test_deleted_user_session_is_rejected_even_if_cached(db):
    from app.services.accounts import soft_delete_user

    user = await make_customer(db)
    token = await sess_mod.create_session(db, user, _FakeRequest())
    await db.commit()
    await soft_delete_user(db, user, reason="test")
    await db.commit()
    assert await sess_mod.load_session(db, token) is None


def test_member_id_and_username_rules():
    assert member_id(42) == "IP-000042" and parse_member_id("ip-000042") == 42 and parse_member_id("IP 7") == 7
    assert parse_member_id("hello") is None
    assert username_problem("riya.kapoor") is None
    assert username_problem("ab") and username_problem("admin") and username_problem("bad name!")
    assert username_problem("a..b") is not None


async def test_generate_unique_username_and_identifier_login(db):
    a = await make_user(db, full_name="Riya Kapoor", email="riya@example.com", phone="+91 98450 11111")
    a.username = await generate_username(db, a.full_name)
    await db.commit()
    b = await make_user(db, full_name="Riya Kapoor", email="riya2@example.com")
    b.username = await generate_username(db, b.full_name)
    await db.commit()
    assert a.username == "riya.kapoor" and b.username != a.username and b.username.startswith("riya.kapoor")
    for ident in ("riya@example.com", "RIYA@EXAMPLE.COM", "@riya.kapoor", "riya.kapoor", "+91 98450 11111",
                  "9845011111", member_id(a)):
        found = await find_user_by_identifier(db, ident)
        assert found is not None and found.id == a.id, ident
    assert await find_user_by_identifier(db, "nobody") is None
    assert await find_user_by_identifier(db, "") is None


async def test_forgot_username_no_enumeration_and_email_sent(client, db):
    user = await make_customer(db, email="member@example.com", phone="+91 99000 12345")
    user.username = "member.one"
    await db.commit()
    r1 = await post_form(client, "/auth/forgot-username", {"contact": "member@example.com"})
    r2 = await post_form(client, "/auth/forgot-username", {"contact": "nobody@example.com"})
    assert r1.status_code == r2.status_code == 200
    assert "If an account matches" in r1.text and "If an account matches" in r2.text
    mails = (await db.execute(select(EmailOutbox).where(EmailOutbox.to_email == "member@example.com"))).scalars().all()
    assert any("@member.one" in m.text_body and "IP-" in m.text_body for m in mails)
    r3 = await post_form(client, "/auth/forgot-username", {"contact": "+91 99000 12345"})
    assert r3.status_code == 200


async def test_account_chip_shows_photo_name_and_username(db):
    user = await make_user(db, full_name="Kabir Rao", photo_key="public/avatars/" + "a" * 32 + ".jpg")
    user.username = "kabir.rao"
    await db.commit()
    async with make_client() as c:
        await login_as(c, user)
        html = (await c.get("/profile", follow_redirects=True)).text
    assert "Hi, Kabir!" in html and "@kabir.rao" in html and f"IP-{user.id:06d}" in html
    assert "/media/public/avatars/" in html
