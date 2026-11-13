"""Double-submit protection (app/core/submit_guard.py, the csrf() macro, app/static/src/js/00-core.js).

Middleware semantics on a tiny app (so every outcome kind is exercised), then the real app: rendered forms carry a
one-time submit id (no JavaScript needed), a double POST of a real form has one effect and replays the same 303, a
double-clicked sign-in ends with ONE session, JSON APIs with X-Submit-Id, keys scoped to the browser (no replay across
users), changed fields are a new request, 4xx outcomes are not stored, webhooks / streams are never guarded, and a
broken store fails open.
"""

from __future__ import annotations

import asyncio
import html
import re
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from starlette.routing import Route

from app.config import get_settings
from app.core import submit_guard as sg
from app.models import Task, UserSession
from app.security.csrf import csrf_cookie_name
from app.security.sessions import session_cookie_name
from tests.conftest import api, csrf_token, login_as, make_client, post_form
from tests.factories import DEFAULT_PASSWORD, make_customer

SID = "abcdefghij0123456789AB"
SID2 = "zyxwvutsrq9876543210ZY"


# ============================================================================================== the middleware alone
class Effects:
    def __init__(self) -> None:
        self.n = 0
        self.gate: asyncio.Event | None = None


@pytest.fixture
def effects():
    return Effects()


@pytest.fixture
def guard_client(effects, monkeypatch):
    """The guard around a tiny app: /effect (303 + Set-Cookie), /json (JSON), /page (HTML with the CSP nonce),
    /bad (400), /file (an opaque body), /stream (SSE) and /payments/hook (exempt)."""

    async def effect(request: Request) -> Any:
        await request.form()
        effects.n += 1
        if effects.gate is not None:
            await effects.gate.wait()
        r = RedirectResponse(f"/done/{effects.n}", status_code=303)
        r.set_cookie("ip_flash", f"saved-{effects.n}", path="/")
        return r

    async def json_effect(request: Request) -> Any:
        effects.n += 1
        return JSONResponse({"n": effects.n, "ok": True}, status_code=201)

    async def page(request: Request) -> Any:
        effects.n += 1
        nonce = request.scope["state"]["csp_nonce"]
        return HTMLResponse(f'<html><script nonce="{nonce}" src="/x.js"></script><p>result {effects.n}</p></html>')

    async def bad(request: Request) -> Any:
        form = await request.form()
        effects.n += 1
        if form.get("ok") == "1":
            return RedirectResponse("/fixed", status_code=303)
        return PlainTextResponse("fix the form", status_code=400)

    async def opaque(request: Request) -> Any:
        effects.n += 1
        return PlainTextResponse("x" * 10, media_type="text/csv")

    async def stream(request: Request) -> Any:
        effects.n += 1

        async def gen():
            yield b"data: 1\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    routes = [Route("/effect", effect, methods=["POST"]), Route("/json", json_effect, methods=["POST"]),
              Route("/page", page, methods=["POST"]), Route("/bad", bad, methods=["POST"]),
              Route("/file", opaque, methods=["POST"]), Route("/stream", stream, methods=["POST"]),
              Route("/payments/hook", effect, methods=["POST"])]
    inner = sg.SubmitGuardMiddleware(Starlette(routes=routes))
    nonces = iter(f"nonce{i:04d}AAAAAAAAAAAAA" for i in range(1000))

    async def app(scope, receive, send):  # stands in for Talisman (a CSP nonce per request)
        scope.setdefault("state", {})["csp_nonce"] = next(nonces)
        await inner(scope, receive, send)

    def factory(browser: str = "a" * 40) -> httpx.AsyncClient:
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
        c.cookies.set(csrf_cookie_name(), browser)
        return c

    return factory


async def test_a_duplicate_after_the_first_finished_gets_the_same_outcome_replayed(guard_client, effects):
    async with guard_client() as c:
        r1 = await c.post("/effect", data={"submit_id": SID, "title": "x"})
        r2 = await c.post("/effect", data={"submit_id": SID, "title": "x"})
    assert effects.n == 1
    assert r1.status_code == r2.status_code == 303 and r1.headers["location"] == r2.headers["location"] == "/done/1"
    assert "saved-1" in r2.headers["set-cookie"] and r2.headers.get("x-submit-replay") == "1"
    assert "x-submit-replay" not in r1.headers


async def test_a_duplicate_arriving_while_the_first_runs_waits_and_gets_its_outcome(guard_client, effects):
    effects.gate = asyncio.Event()
    async with guard_client() as c:
        first = asyncio.create_task(c.post("/effect", data={"submit_id": SID, "title": "x"}))
        await asyncio.sleep(0.05)
        second = asyncio.create_task(c.post("/effect", data={"submit_id": SID, "title": "x"}))
        third = asyncio.create_task(c.post("/effect", data={"submit_id": SID, "title": "x"}))
        await asyncio.sleep(0.15)
        assert effects.n == 1 and not second.done()  # waiting, not processed
        effects.gate.set()
        rs = await asyncio.gather(first, second, third)
    assert effects.n == 1
    assert {r.headers["location"] for r in rs} == {"/done/1"} and all(r.status_code == 303 for r in rs)


async def test_different_ids_changed_fields_and_other_browsers_are_new_requests(guard_client, effects):
    async with guard_client("a" * 40) as a, guard_client("b" * 40) as b:
        await a.post("/effect", data={"submit_id": SID, "title": "x"})
        await a.post("/effect", data={"submit_id": SID2, "title": "x"})  # another render of the form
        await a.post("/effect", data={"submit_id": SID, "title": "changed"})  # same form, edited and sent again
        r = await b.post("/effect", data={"submit_id": SID, "title": "x"})  # another browser guessing the id
        assert r.headers["location"] == "/done/4" and "x-submit-replay" not in r.headers  # never A's outcome
        # page_id / csrf_token never make a resend look new (a bfcache restore gives the page a new id)
        await a.post("/effect", data={"submit_id": SID, "title": "x", "page_id": "pageZ-1234567890",
                                      "csrf_token": "whatever"})
    assert effects.n == 4


async def test_json_apis_with_x_submit_id_replay_status_and_body(guard_client, effects):
    async with guard_client() as c:
        h = {"X-Submit-Id": SID, "Accept": "application/json"}
        r1 = await c.post("/json", json={"a": 1}, headers=h)
        r2 = await c.post("/json", json={"a": 1}, headers=h)
        r3 = await c.post("/json", json={"a": 1}, headers={**h, "X-Submit-Id": SID2})
    assert (r1.status_code, r1.json()) == (r2.status_code, r2.json()) == (201, {"n": 1, "ok": True})
    assert r3.json()["n"] == 2 and effects.n == 2


async def test_a_replayed_page_gets_the_new_requests_csp_nonce(guard_client, effects):
    async with guard_client() as c:
        r1 = await c.post("/page", data={"submit_id": SID})
        r2 = await c.post("/page", data={"submit_id": SID})
    assert effects.n == 1 and "result 1" in r2.text
    n1 = re.search(r'nonce="([^"]+)"', r1.text).group(1)
    n2 = re.search(r'nonce="([^"]+)"', r2.text).group(1)
    assert n1 != n2 and n1 not in r2.text  # the replay's scripts run under the CSP of the request that gets it


async def test_error_outcomes_are_not_stored_so_a_corrected_resend_runs(guard_client, effects):
    async with guard_client() as c:
        r1 = await c.post("/bad", data={"submit_id": SID})
        r2 = await c.post("/bad", data={"submit_id": SID})  # same values: runs again (nothing happened the first time)
        r3 = await c.post("/bad", data={"submit_id": SID, "ok": "1"})
    assert (r1.status_code, r2.status_code, r3.status_code) == (400, 400, 303) and effects.n == 3


async def test_an_outcome_that_cannot_be_replayed_is_reported_as_already_done(guard_client, effects):
    async with guard_client() as c:
        await c.post("/file", data={"submit_id": SID})
        r = await c.post("/file", data={"submit_id": SID}, headers={"Accept": "application/json"})
        assert r.status_code == 409 and r.json()["code"] == "duplicate_submit"
        r = await c.post("/file", data={"submit_id": SID}, headers={"Referer": "http://testserver/shop/orders?x=1"})
        assert r.status_code == 303 and r.headers["location"] == "/shop/orders?x=1"  # back where the form was
        r = await c.post("/file", data={"submit_id": SID}, headers={"Referer": "https://evil.example/x"})
        assert r.headers["location"] == "/"  # never to another site
    assert effects.n == 1


async def test_streams_webhooks_and_requests_without_an_id_are_never_guarded(guard_client, effects):
    async with guard_client() as c:
        for _ in range(2):
            await c.post("/payments/hook", data={"submit_id": SID})
        assert effects.n == 2
        for _ in range(2):
            await c.post("/effect", data={"title": "no id"})
        assert effects.n == 4
        r1 = await c.post("/stream", headers={"X-Submit-Id": SID})
        r2 = await c.post("/stream", headers={"X-Submit-Id": SID})
        assert r1.text == "data: 1\n\n" and r2.status_code in (303, 409)  # done, never re-run
    assert effects.n == 5


async def test_without_a_browser_cookie_nothing_is_guarded(guard_client, effects):
    async with guard_client() as c:
        c.cookies.clear()
        await c.post("/effect", data={"submit_id": SID})
        await c.post("/effect", data={"submit_id": SID})
    assert effects.n == 2


async def test_multipart_forms_are_guarded_by_their_fields(guard_client, effects):
    async with guard_client() as c:
        files = {"photo": ("a.jpg", b"\xff\xd8 not really", "image/jpeg")}
        await c.post("/effect", data={"submit_id": SID, "note": "one"}, files=files)
        await c.post("/effect", data={"submit_id": SID, "note": "one"}, files=files)  # a resend (new boundary)
        await c.post("/effect", data={"submit_id": SID, "note": "two"}, files=files)  # changed text: new request
    assert effects.n == 2


async def test_a_broken_store_fails_open_to_this_workers_memory(guard_client, effects, monkeypatch, caplog):
    class Broken:
        async def claim(self, key):
            raise ConnectionError("down")

        get = complete = release = claim

    monkeypatch.setattr(sg, "redis_store", Broken())
    monkeypatch.setattr(get_settings(), "redis_url", "redis://127.0.0.1:1/15")
    async with guard_client() as c:
        r1 = await c.post("/effect", data={"submit_id": SID})
        r2 = await c.post("/effect", data={"submit_id": SID})
    assert r1.status_code == r2.status_code == 303 and effects.n == 1  # never blocked, still one effect
    assert sg.stats["store_errors"] >= 1 and "submit guard: Redis unavailable" in caplog.text


async def test_the_local_store_is_bounded():
    store = sg.LocalStore(max_entries=3, max_bytes=10_000)
    for i in range(10):
        assert await store.claim(f"k{i}")
    assert len(store._data) == 3


@pytest.mark.skipif(not get_settings().redis_url, reason="needs REDIS_URL (a test database)")
async def test_the_redis_store_is_shared_between_workers(guard_client, effects, monkeypatch):
    """Two 'workers' (the second with its own empty memory) still see one request: the key lives in Redis."""
    async with guard_client() as c:
        await c.post("/effect", data={"submit_id": SID})
        sg.local_store.reset()
        r = await c.post("/effect", data={"submit_id": SID})
    assert effects.n == 1 and r.headers.get("x-submit-replay") == "1"


# ============================================================================================== the real app
async def test_rendered_forms_carry_a_fresh_submit_id_per_render(client):
    pages = [(await client.get("/auth/login")).text for _ in range(2)]
    ids = [re.findall(r'name="submit_id" value="([A-Za-z0-9_-]{16,64})"', p) for p in pages]
    assert ids[0] and ids[1] and ids[0][0] != ids[1][0]


async def test_a_double_posted_real_form_has_one_effect_and_the_same_redirect(client, db):
    user = await make_customer(db)
    uid = user.id
    await login_as(client, user)
    data = {"title": "Buy chalk", "kind": "task", "submit_id": SID}
    r1 = await post_form(client, "/tasks", data)
    r2 = await post_form(client, "/tasks", data)
    assert r1.status_code == r2.status_code == 303 and r1.headers["location"] == r2.headers["location"]
    assert r2.headers.get("x-submit-replay") == "1"
    assert await db.scalar(select(func.count()).select_from(Task).where(Task.user_id == uid)) == 1
    r3 = await post_form(client, "/tasks", {**data, "submit_id": SID2})  # the form rendered again: a new task
    assert r3.status_code == 303
    assert await db.scalar(select(func.count()).select_from(Task).where(Task.user_id == uid)) == 2


async def test_concurrent_duplicates_of_a_real_form_create_one_row(client, db):
    user = await make_customer(db)
    uid = user.id
    await login_as(client, user)
    data = {"title": "Stretch", "kind": "task", "submit_id": SID, "csrf_token": await csrf_token(client)}
    rs = await asyncio.gather(*(client.post("/tasks", data=data) for _ in range(4)))
    assert [r.status_code for r in rs] == [303] * 4 and len({r.headers["location"] for r in rs}) == 1
    assert await db.scalar(select(func.count()).select_from(Task).where(Task.user_id == uid)) == 1


async def test_the_same_submit_id_from_another_account_is_never_replayed(db):
    a = await make_customer(db)
    b = await make_customer(db)
    async with make_client() as ca, make_client() as cb:
        await login_as(ca, a)
        await login_as(cb, b)
        data = {"title": "Mine", "kind": "task", "submit_id": SID}
        await post_form(ca, "/tasks", data)
        r = await post_form(cb, "/tasks", data)
        assert r.status_code == 303 and "x-submit-replay" not in r.headers
    counts = dict((await db.execute(select(Task.user_id, func.count()).group_by(Task.user_id))).all())
    assert counts == {a.id: 1, b.id: 1}


async def test_a_double_clicked_sign_in_ends_with_one_session_and_the_same_cookie(db):
    user = await make_customer(db, email="double@example.com")
    uid = user.id
    async with make_client() as c:
        data = {"identifier": "double@example.com", "password": DEFAULT_PASSWORD, "submit_id": SID,
                "csrf_token": await csrf_token(c)}
        rs = await asyncio.gather(c.post("/auth/login", data=data), c.post("/auth/login", data=data))
        assert [r.status_code for r in rs] == [303, 303]
        cookies = [r.headers.get("set-cookie", "") for r in rs]
        tokens = [re.search(session_cookie_name() + r"=([^;]+)", h).group(1) for h in cookies]
        assert tokens[0] == tokens[1]  # the replay carries the first sign-in's session cookie
    sessions = (await db.execute(select(UserSession).where(UserSession.user_id == uid))).scalars().all()
    assert len(sessions) == 1 and sessions[0].revoked_at is None  # no second session, nothing "signed out elsewhere"


async def test_stored_set_cookie_values_are_encrypted(db):
    if not get_settings().redis_url:
        pytest.skip("needs REDIS_URL (a test database)")
    from app.security.sessions import get_redis

    user = await make_customer(db, email="enc@example.com")
    async with make_client() as c:
        await post_form(c, "/auth/login", {"identifier": "enc@example.com", "password": DEFAULT_PASSWORD,
                                           "submit_id": SID})
        token = c.cookies.get(session_cookie_name())
    r = await get_redis()
    keys = [k async for k in r.scan_iter(match=sg._key("x", "y", "z", "/", "")[:-40] + "*")]
    values = [await r.get(k) for k in keys]
    assert values and all(token not in (v or "") for v in values)
    assert user.id


async def test_json_api_calls_with_x_submit_id_on_the_real_app(client, db):
    user = await make_customer(db)
    await login_as(client, user)
    h = {"X-Submit-Id": SID}
    r1 = await api(client, "POST", "/api/notifications/read-all", headers=h)
    r2 = await api(client, "POST", "/api/notifications/read-all", headers=h)
    assert r1.status_code == r2.status_code == 200 and r1.json() == r2.json()
    assert r2.headers.get("x-submit-replay") == "1"


async def test_the_csrf_check_still_guards_the_first_request(client, db):
    user = await make_customer(db)
    await login_as(client, user)
    r = await client.post("/tasks", data={"title": "x", "kind": "task", "submit_id": SID})  # no CSRF token
    assert r.status_code == 403
    r = await post_form(client, "/tasks", {"title": "x", "kind": "task", "submit_id": SID})  # 403 was not stored
    assert r.status_code == 303
