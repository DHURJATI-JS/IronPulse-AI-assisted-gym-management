"""Remembered accounts on the sign-in page ("like Google") - app/services/remembered_accounts.py, /auth/remembered*,
app/static/src/js/02-accounts.js.

The device keeps ONLY opaque references; the server keeps their hashes. Covered: issuance and opt-out, current details
after a name / photo change, "gone" for banned / deleted / revoked / expired / unknown / raw ids / guessed references
(one shape for all), the caps (5 per lookup, 5 per device, one per account per device, 10 per account), removal revokes
server-side, revocation on sign out everywhere / password reset / ban, the per-address rate limit, signed photo URLs
that expire, signing in with a chosen account (the password is still required), CSRF, XSS-safe rendering and CSP.
"""

from __future__ import annotations

import html
import re
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, update

from app.core.utils import sha256_hex, utcnow
from app.models import RememberedAccount, User, UserSession
from app.models.enums import TokenPurpose
from app.security.crypto import token_hash
from app.services import auth_flows as af
from app.services import remembered_accounts as ra
from app.services import storage
from tests.conftest import api, csrf_token, make_client, post_form
from tests.factories import DEFAULT_PASSWORD, make_customer, png_bytes

DEVICE = "0123456789abcdef0123456789abcdef"
OTHER_DEVICE = "fedcba9876543210fedcba9876543210"
LAPTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 "
             "Safari/537.36")
JS = __import__("pathlib").Path(__file__).resolve().parent.parent / "app" / "static" / "src" / "js" / "02-accounts.js"


@pytest.fixture(autouse=True)
def _clean_throttle():
    af.ghost_throttle.reset()
    yield
    af.ghost_throttle.reset()


def handoff(r: Any) -> str | None:
    for header in r.headers.get_list("set-cookie"):
        m = re.match(r"(?:__Host-)?ip_rmb=([A-Za-z0-9_-]+);", header)
        if m:
            return m.group(1)
    return None


async def sign_in(client: Any, email: str, *, device: str | None = DEVICE, remember: bool = True,
                  password: str = DEFAULT_PASSWORD, **extra: Any) -> Any:
    data: dict[str, Any] = {"identifier": email, "password": password, **extra}
    if remember:
        data["remember"] = "1"
    if device is not None:
        data["device_id"] = device
    return await post_form(client, "/auth/login", data)


async def remembered(email: str, *, device: str = DEVICE) -> str:
    async with make_client() as c:
        c.headers["User-Agent"] = LAPTOP_UA
        r = await sign_in(c, email, device=device)
        assert r.status_code == 303, html.unescape(r.text)[-800:]
        ref = handoff(r)
        assert ref
        return ref


async def lookup(client: Any, refs: list[str]) -> Any:
    return await api(client, "POST", "/auth/remembered", json={"refs": refs})


# ============================================================================================ issuance
async def test_a_remembered_sign_in_hands_over_an_opaque_reference_and_stores_only_its_hash(db):
    user = await make_customer(db, email="riya@example.com", full_name="Riya Kapoor")
    async with make_client() as c:
        c.headers["User-Agent"] = LAPTOP_UA
        r = await sign_in(c, "riya@example.com")
        assert r.status_code == 303
        ref = handoff(r)
        cookie = [h for h in r.headers.get_list("set-cookie") if "ip_rmb=" in h][0].lower()
        assert "httponly" not in cookie and "samesite=strict" in cookie and "max-age=300" in cookie and "path=/" in cookie
    assert ref and ra.REF_RE.fullmatch(ref) and str(user.id) != ref
    row = (await db.execute(select(RememberedAccount))).scalar_one()
    assert row.id == token_hash(ref) and row.user_id == user.id and row.device_hash == sha256_hex(DEVICE)
    assert row.label == "Chrome on Windows" and ref not in (row.id, row.device_hash, row.session_id, row.label)


@pytest.mark.parametrize("remember, device", [(False, DEVICE), (True, None), (True, "short"), (True, "x" * 200)])
async def test_no_reference_without_the_tick_or_a_valid_device_key(db, remember, device):
    await make_customer(db, email="optout@example.com")
    async with make_client() as c:
        r = await sign_in(c, "optout@example.com", device=device, remember=remember)
        assert r.status_code == 303 and handoff(r) is None
    assert await db.scalar(select(func.count()).select_from(RememberedAccount)) == 0


async def test_the_sign_in_page_has_a_visible_remember_checkbox_ticked_by_default(client):
    text = (await client.get("/auth/login")).text
    assert re.search(r'<input type="checkbox" name="remember" value="1" class="checkbox mt-0\.5"\s+checked', text)
    assert "Remember this account on this device" in text
    assert 'name="account_ref"' in text and 'name="device_id"' in text and 'data-component="account-chooser"' in text


# ============================================================================================ lookup
async def test_lookup_returns_current_details_and_follows_name_and_photo_changes(db, client):
    user = await make_customer(db, email="abhishek@gmail.com", full_name="Abhishek Rao")
    uid = user.id
    ref = await remembered("abhishek@gmail.com")
    r = await lookup(client, [ref])
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    acc = r.json()["accounts"][0]
    assert acc == {"ref": ref, "state": "ok", "name": "Abhishek Rao", "initials": "AR", "email": "ab***@gmail.com",
                   "photo": None}
    stored = await storage.save_public_image(png_bytes(), kind="avatars")
    await db.execute(update(User).where(User.id == uid).values(full_name="Abhi Rao", photo_key=stored.key))
    await db.commit()
    acc = (await lookup(client, [ref])).json()["accounts"][0]
    assert acc["name"] == "Abhi Rao" and acc["photo"].startswith("/auth/remembered/photo/")
    photo = await client.get(acc["photo"])
    assert photo.status_code == 200 and photo.headers["content-type"] == "image/jpeg"
    assert photo.headers["cache-control"] == "private, max-age=300"


async def test_gone_looks_the_same_for_banned_deleted_revoked_expired_unknown_and_guessed(db, client):
    from app.services.accounts import soft_delete_user

    refs = {}
    for name in ("banned", "deleted", "revoked", "expired", "alive"):
        await make_customer(db, email=f"{name}@example.com")
        refs[name] = await remembered(f"{name}@example.com", device=sha256_hex(name)[:32])
    banned = await db.scalar(select(User).where(User.email == "banned@example.com"))
    banned.is_active = False
    deleted = await db.scalar(select(User).where(User.email == "deleted@example.com"))
    await soft_delete_user(db, deleted, reason="test")
    await db.execute(update(RememberedAccount).where(RememberedAccount.id == token_hash(refs["expired"]))
                     .values(last_used_at=utcnow() - timedelta(days=ra.UNUSED_DAYS + 1)))
    await db.commit()
    await ra.forget(db, refs["revoked"])
    await db.commit()
    alive_id = (await db.scalar(select(User.id).where(User.email == "alive@example.com")))
    unknown = "A" * 43  # well-formed, never issued
    probes = [refs["banned"], refs["deleted"], refs["revoked"], refs["expired"], unknown]
    r = await lookup(client, probes)
    assert r.status_code == 200
    assert r.json()["accounts"] == [{"ref": p, "state": "gone"} for p in probes]
    # raw / sequential ids and other guesses never resolve to anyone
    guesses = [str(alive_id), f"{alive_id:043d}", "IP-%06d" % alive_id, token_hash(refs["alive"])[:43]]
    r = await lookup(client, guesses)
    assert [a["state"] for a in r.json()["accounts"]] == ["gone"] * 4
    assert (await lookup(client, [refs["alive"]])).json()["accounts"][0]["state"] == "ok"


async def test_lookup_takes_at_most_five_references_and_refuses_junk(client):
    r = await lookup(client, ["A" * 43] * 6)
    assert r.status_code == 422
    for junk in ([1, 2], ["x" * 129], "notalist", [""], None):
        r = await api(client, "POST", "/auth/remembered", json={"refs": junk})
        assert r.status_code == 422, junk
    r = await api(client, "POST", "/auth/remembered", json=["A" * 43])
    assert r.status_code == 422


async def test_lookup_needs_the_csrf_token_and_the_same_origin(client):
    r = await client.post("/auth/remembered", json={"refs": []})
    assert r.status_code == 403
    r = await client.post("/auth/remembered", json={"refs": []},
                          headers={"X-CSRF-Token": await csrf_token(client), "Origin": "https://evil.example"})
    assert r.status_code == 403


async def test_lookup_and_removal_are_rate_limited_per_address(anon_client_factory):
    c = anon_client_factory("203.0.113.77")
    for _ in range(ra.LOOKUPS_PER_MINUTE):
        assert (await lookup(c, [])).status_code == 200
    r = await lookup(c, [])
    assert r.status_code == 429 and r.headers["retry-after"] == "60"
    r = await api(c, "POST", "/auth/remembered/forget", json={"ref": "A" * 43})
    assert r.status_code == 429
    other = anon_client_factory("203.0.113.78")
    assert (await lookup(other, [])).status_code == 200  # per address


# ============================================================================================ caps
async def test_one_reference_per_account_per_device_and_at_most_five_per_device(db, client):
    for i in range(7):
        await make_customer(db, email=f"family{i}@example.com")
    first = await remembered("family0@example.com")
    again = await remembered("family0@example.com")  # the same account on the same device: replaced
    assert first != again
    states = [a["state"] for a in (await lookup(client, [first, again])).json()["accounts"]]
    assert states == ["gone", "ok"]
    refs = [again] + [await remembered(f"family{i}@example.com") for i in range(1, 7)]
    count = await db.scalar(select(func.count()).select_from(RememberedAccount)
                            .where(RememberedAccount.device_hash == sha256_hex(DEVICE)))
    assert count == ra.MAX_PER_DEVICE == 5
    states = [a["state"] for a in (await lookup(client, refs[:5])).json()["accounts"]]
    assert states[:2] == ["gone", "gone"] and states[2:] == ["ok"] * 3  # the least recently used went first


async def test_at_most_ten_devices_per_account(db):
    await make_customer(db, email="busy@example.com")
    for i in range(ra.MAX_PER_USER + 3):
        await remembered("busy@example.com", device=f"{i:032x}")
    assert await db.scalar(select(func.count()).select_from(RememberedAccount)) == ra.MAX_PER_USER == 10


# ============================================================================================ removal / revocation
async def test_remove_from_this_device_revokes_the_reference_on_the_server(db, client):
    await make_customer(db, email="remove@example.com")
    ref = await remembered("remove@example.com")
    r = await api(client, "POST", "/auth/remembered/forget", json={"ref": ref})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert await db.scalar(select(func.count()).select_from(RememberedAccount)) == 0
    assert (await lookup(client, [ref])).json()["accounts"][0]["state"] == "gone"
    r = await api(client, "POST", "/auth/remembered/forget", json={"ref": "B" * 43})  # unknown: same answer
    assert r.json() == {"ok": True}


async def test_sign_out_everywhere_keeps_only_this_devices_reference(db):
    await make_customer(db, email="everywhere@example.com")
    phone_ref = await remembered("everywhere@example.com", device=OTHER_DEVICE)
    async with make_client() as laptop:
        r = await sign_in(laptop, "everywhere@example.com")
        laptop_ref = handoff(r)
        r = await post_form(laptop, "/account/sessions/revoke-others", {})
        assert r.status_code == 303
        states = [a["state"] for a in (await lookup(laptop, [phone_ref, laptop_ref])).json()["accounts"]]
    assert states == ["gone", "ok"]


async def test_a_password_reset_and_a_ban_revoke_every_reference(db, client):
    from app.security.sessions import revoke_all_sessions

    user = await make_customer(db, email="reset-me@example.com")
    uid = user.id
    ref = await remembered("reset-me@example.com")
    raw = await af.issue_token(db, TokenPurpose.PASSWORD_RESET, user_id=uid, email="reset-me@example.com")
    await db.commit()
    r = await post_form(client, "/auth/reset", {"token": raw, "password": "Gl0wing-Kettlebell!",
                                                "password_confirm": "Gl0wing-Kettlebell!"})
    assert r.status_code == 303
    assert (await lookup(client, [ref])).json()["accounts"][0]["state"] == "gone"
    async with make_client() as c:
        r = await sign_in(c, "reset-me@example.com", password="Gl0wing-Kettlebell!")
        ref2 = handoff(r)
    assert ref2 and (await lookup(client, [ref2])).json()["accounts"][0]["state"] == "ok"
    await revoke_all_sessions(db, uid)  # what trust_safety.ban_account does (with users.is_active = false)
    await db.commit()
    assert (await lookup(client, [ref2])).json()["accounts"][0]["state"] == "gone"


# ============================================================================================ signing in with one
async def test_choosing_an_account_still_needs_the_password(db):
    user = await make_customer(db, email="chosen@example.com")
    uid = user.id
    ref = await remembered("chosen@example.com")
    async with make_client() as c:
        r = await post_form(c, "/auth/login", {"account_ref": ref, "password": "Wrong-Passw0rd!"})
        text = html.unescape(r.text)
        assert r.status_code == 400 and "Those sign-in details didn't work" in text
        assert f'name="account_ref" value="{ref}"' in r.text  # the chosen account stays chosen
        r = await post_form(c, "/auth/login", {"account_ref": ref})
        assert r.status_code == 400  # never without a password
        r = await post_form(c, "/auth/login", {"account_ref": ref, "password": DEFAULT_PASSWORD})
        assert r.status_code == 303
        live = await db.scalar(select(func.count()).select_from(UserSession).where(
            UserSession.user_id == uid, UserSession.revoked_at.is_(None)))
        assert live == 1  # one session per account still applies
    r = await post_form(make_client(), "/auth/login", {"account_ref": "C" * 43, "password": DEFAULT_PASSWORD})
    assert r.status_code == 400 and "Those sign-in details didn't work" in html.unescape(r.text)


async def test_a_gone_reference_cannot_sign_in_even_with_the_right_password(db):
    user = await make_customer(db, email="gone-ref@example.com")
    ref = await remembered("gone-ref@example.com")
    user.is_active = False
    await db.commit()
    async with make_client() as c:
        r = await post_form(c, "/auth/login", {"account_ref": ref, "password": DEFAULT_PASSWORD})
        assert r.status_code == 400 and "Those sign-in details didn't work" in html.unescape(r.text)


# ============================================================================================ photo URLs
async def test_signed_photo_urls_expire_and_cannot_be_forged(client, monkeypatch):
    stored = await storage.save_public_image(png_bytes(), kind="avatars")
    url = ra.photo_url(stored.key)
    assert (await client.get(url)).status_code == 200
    monkeypatch.setattr(ra, "PHOTO_URL_SECONDS", -1)  # expired
    assert (await client.get(url)).status_code == 404
    monkeypatch.setattr(ra, "PHOTO_URL_SECONDS", 600)
    token = url.rsplit("/", 1)[1]
    assert (await client.get(url[:-3] + ("AAA" if not url.endswith("AAA") else "BBB"))).status_code == 404
    assert (await client.get("/auth/remembered/photo/" + token[::-1])).status_code == 404
    assert ra.photo_url("private/idcards/" + "0" * 32 + ".jpg") is None  # never a private file
    assert ra.photo_key_from(ra._photo_signer().dumps("private/idcards/" + "0" * 32 + ".jpg")) is None


# ============================================================================================ front end
def _code() -> str:
    import rjsmin

    return rjsmin.jsmin(JS.read_text(encoding="utf-8"), keep_bang_comments=False)


def test_the_chooser_renders_data_only_through_text_and_stores_only_references():
    code = _code()
    for token in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "new Function(", "document.write(",
                  'setAttribute("style"', "javascript:"):
        assert token not in code, token
    assert 'text:acc.name' in code.replace(" ", "") and 'text:acc.email' in code.replace(" ", "")
    # what goes into localStorage: the device key and {r, t} entries - nothing from the lookup answer
    assert re.search(r'JSON\.stringify\(\{device:state\.device,refs:state\.refs\.slice\(0,MAX\)\}\)', code.replace(" ", ""))
    assert "acc.photo.indexOf(PHOTO_PREFIX)===0" in code.replace(" ", "")  # only our own signed photo URLs
    assert 'a.state==="gone"' in code.replace(" ", "")  # only an explicit "gone" removes an entry


def test_the_bundle_contains_the_chooser():
    from pathlib import Path

    bundle = (Path(__file__).resolve().parent.parent / "app" / "static" / "dist" / "app.min.js").read_text()
    assert 'IP.register("account-chooser"' in bundle and "/auth/remembered" in bundle


async def test_the_sign_in_page_stays_csp_clean(client):
    r = await client.get("/auth/login")
    text = r.text
    assert "'unsafe-inline'" not in r.headers["content-security-policy"]
    assert not re.search(r"<script(?![^>]*\bnonce=)[^>]*>", text)  # every script carries the nonce
    assert not re.search(r"\son[a-z]+\s*=", text)  # no inline event handlers
    assert "account-chooser" in text and "02-accounts" not in text  # the chooser's code comes from the bundle only


async def test_lookup_answers_are_data_not_markup(db, client):
    await make_customer(db, email="xss@example.com", full_name="Eve Example")
    await db.execute(update(User).where(User.email == "xss@example.com").values(full_name="<img src=x onerror=alert(1)>"))
    await db.commit()
    ref = await remembered("xss@example.com")
    r = await lookup(client, [ref])
    assert r.headers["content-type"].startswith("application/json")
    assert r.json()["accounts"][0]["name"] == "<img src=x onerror=alert(1)>"  # the page puts it in textContent
