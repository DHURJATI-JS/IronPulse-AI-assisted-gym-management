"""Degrading quietly while the database is overloaded (the 10-minute stress test of 2026-11-13: its 1,050- and
1,600-user steps logged 100 tracebacks / 52 "Exception in ASGI application" although no client got a 500).

Pool exhaustion is real here wherever it can be: ``tiny_pool`` gives the app a pool of ONE connection that the test
holds, so the app's own code meets the genuine ``sqlalchemy.exc.TimeoutError`` ("QueuePool limit of size 1 overflow 0
reached, connection timed out, timeout 0.20"); a few paths patch one step to raise it instead. Every path asserts that
no traceback (nor ERROR) is logged, the right outcome (a 1013 close code, a 503 kept for requests, back-off), and that
the deferred work still happens once the database answers.

* (a) /ws - a pool timeout in the session check or its re-check closes the socket with 1013 "try again later"; the
  page reconnects with a jittered back-off (00-core.js, run in JavaScriptCore);
* (b) presence (resume / exit / sign-out), the job runner (runs, final status writes, presence checks) and the worker
  loops - one warning a minute per call site, retried later, nothing lost;
* (c) a client that went away (ClientDisconnect while its body was read, a response task cancelled mid-ROLLBACK) is
  no error, and its transaction is rolled back cleanly;
* (d) a request that meets an exhausted pool still gets the friendly 503.
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import anyio
import pytest
from fastapi import Request
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.util import await_

from app import db as dbmod
from app.config import get_settings
from app.core import overload
from app.core.deps import DB
from app.core.overload import ThrottledWarnings, throttled
from app.core.utils import utcnow
from app.db import REQUEST_SESSION_STATE, db_unavailable, engine_options, release_request_session
from app.main import app as fastapi_app
from app.models import EmailOutbox, Job, User, UserSession
from app.models.enums import EmailStatus, JobStatus
from app.routers import core as core_router
from app.security import sessions as sessions_mod
from app.security.crypto import new_token, token_hash
from app.services import jobs as J
from app.services import presence as presence_mod
from app.services.presence import presence
from app.services.realtime import hub
from tests.conftest import csrf_token, login_as, run_jobs
from tests.factories import make_customer
from tests.test_ai_frontend_static import CORE, HARNESS, _jsc, needs_jsc
from tests.test_presence import FakeWS, open_ws

POOL_TIMEOUT = 0.2  # seconds the tiny pool lets a caller wait
GRACE = 0.2
PAGE = "pageA-1234567890"
DRY = "QueuePool limit of size 10 overflow 5 reached, connection timed out, timeout 10.00"


# ============================================================================================ helpers / fixtures
def db_url() -> str:
    return dbmod.get_engine().url.render_as_string(hide_password=False)


def tracebacks(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Every record that carries a traceback or is an ERROR - none may appear while the database is merely busy."""
    return [f"{r.name}: {r.getMessage()}" for r in caplog.records if r.exc_info or r.levelno >= logging.ERROR]


def warnings(caplog: pytest.LogCaptureFixture, logger: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == logger and r.levelno == logging.WARNING]


class TinyPool:
    """The app's engine for one test: a QueuePool of ONE connection with POOL_TIMEOUT s of waiting. ``hold()`` takes
    that connection away - every other caller then gets the genuine QueuePool TimeoutError - and ``release()`` gives
    it back."""

    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self.held: Any = None

    async def hold(self) -> None:
        if self.held is None:
            self.held = await self.engine.connect()

    async def release(self) -> None:
        held, self.held = self.held, None
        if held is not None:
            await held.close()


@pytest.fixture
async def tiny_pool() -> AsyncIterator[TinyPool]:
    url = db_url()
    engine = dbmod.configure_engine(url, pool_size=1, max_overflow=0, pool_timeout=POOL_TIMEOUT)
    pool = TinyPool(engine)
    try:
        yield pool
    finally:
        await pool.release()
        await presence.reset()  # nothing of this test may still use the tiny pool
        await engine.dispose()
        dbmod.configure_engine(url, poolclass=NullPool)  # the suite's engine again (tests/conftest.py)


@pytest.fixture(autouse=True)
def _quiet_setup(caplog, monkeypatch):
    """Per-process presence (no Redis), the log captured from DEBUG up."""

    async def none() -> None:
        return None

    monkeypatch.setattr(presence, "_redis", none)
    caplog.set_level(logging.DEBUG)


@pytest.fixture
def grace(monkeypatch):
    monkeypatch.setattr(get_settings(), "presence_grace_seconds", GRACE)
    return GRACE


@pytest.fixture
def registry():
    """Register test job handlers; the previous registrations come back afterwards."""
    saved: dict[str, tuple[Any, Any]] = {}

    def register(kind: str, fn: Callable[..., Any], *, on_exit: str | None = None) -> Callable[..., Any]:
        saved.setdefault(kind, (J._handlers.get(kind), J._exit_policies.get(kind)))
        J.job_handler(kind, on_exit=on_exit)(fn)
        return fn

    yield register
    for kind, (handler, policy) in saved.items():
        if handler is None:
            J._handlers.pop(kind, None)
        else:
            J._handlers[kind] = handler
        if policy is None:
            J._exit_policies.pop(kind, None)
        else:
            J._exit_policies[kind] = policy


async def wait_until(predicate: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


async def add_job(db: Any, kind: str, *, user_id: int | None = None, policy: str = "keep",
                  status: JobStatus = JobStatus.QUEUED) -> int:
    job = await J.enqueue(db, kind, {"n": 1}, user_id=user_id, exit_policy=policy,
                          dedupe_key=f"{kind}:{user_id}:{policy}:{time.monotonic_ns()}")
    job.status = status
    if status == JobStatus.PAUSED:
        job.paused_at = utcnow()
    await db.commit()
    return job.id


async def job_row(db: Any, job_id: int) -> Any:
    row = (await db.execute(select(Job.status, Job.attempts, Job.locked_at, Job.run_after, Job.last_error,
                                   Job.cancel_reason).where(Job.id == job_id))).one()
    await db.commit()  # end the read: the next one sees what the app committed meanwhile
    return row


async def new_session(db: Any, user: User) -> str:
    token = new_token(32)
    db.add(UserSession(id=token_hash(token), user_id=user.id, expires_at=utcnow() + timedelta(hours=2),
                       ip="127.0.0.1", user_agent="pytest"))
    await db.commit()
    return token


def connect(uid: int, conn: str, session: str | None = "sess-1") -> None:
    presence.connected(uid, conn_id=conn, page_id=PAGE, session_hash=session)


def disconnect(uid: int, conn: str, code: int = 1001) -> None:
    presence.disconnected(uid, conn_id=conn, page_id=PAGE, code=code)


async def after_grace() -> None:
    """Let a grace timer fire and its exit run (an exit that meets the dry pool waits POOL_TIMEOUT first)."""
    await asyncio.sleep(GRACE * 1.5)
    await wait_until(lambda: presence.snapshot()["grace_timers"] == 0, timeout=5)
    await presence.drain()


async def asgi(method: str, path: str, *, query: str = "", headers: list[tuple[bytes, bytes]] | None = None) -> list[dict]:
    """The whole app, called the way uvicorn does, for a client that is already gone: every receive() says
    http.disconnect (what uvicorn answers once the connection closed)."""
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
             "method": method, "scheme": "http", "path": path, "raw_path": path.encode(), "root_path": "",
             "query_string": query.encode(), "client": ("127.0.0.1", 51234), "server": ("testserver", 80),
             "headers": [(b"host", b"testserver"), (b"user-agent", b"pytest-ironpulse/1.0"), *(headers or [])]}
    await fastapi_app(scope, receive, send)
    return sent


def status_of(sent: list[dict]) -> int:
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


# ============================================================================================ building blocks
async def test_what_counts_as_an_unavailable_database():
    class Orig(Exception):
        def __init__(self, sqlstate: str) -> None:
            super().__init__(f"sqlstate {sqlstate}")
            self.sqlstate = sqlstate

    assert db_unavailable(PoolTimeoutError(DRY)) == "pool"
    statement = DBAPIError("SELECT 1", {}, Orig("57014"))
    assert db_unavailable(statement) is None  # a slow query may be slow every time: not "unavailable" ...
    assert db_unavailable(statement, statement_timeouts=True) == "statement"  # ... unless the step is a tiny one
    assert db_unavailable(DBAPIError("connect", {}, Orig("53300"))) == "connections"  # too many clients
    assert db_unavailable(DBAPIError("SELECT 1", {}, Orig("08006"))) == "connection"  # connection failure
    assert db_unavailable(DBAPIError("SELECT 1", {}, Orig("57P01"))) == "connection"  # admin shutdown
    assert db_unavailable(DBAPIError("SELECT 1", {}, RuntimeError("closed"), connection_invalidated=True)) == "connection"
    assert db_unavailable(DBAPIError("SELECT 1/0", {}, Orig("22012"))) is None  # a bug stays a bug
    assert db_unavailable(ValueError("x")) is None
    engine = create_async_engine("postgresql+asyncpg://nobody:x@127.0.0.1:1/none", poolclass=NullPool)
    try:
        with pytest.raises(OSError) as refused:  # asyncpg's connect raises a plain ConnectionRefusedError ...
            async with engine.connect():
                pass
    finally:
        await engine.dispose()
    assert db_unavailable(refused.value) == "connection"
    assert db_unavailable(ConnectionRefusedError(61, "a job's HTTP call")) is None  # ... any other client's is not


def test_one_warning_a_minute_per_call_site_with_the_suppressed_count(monkeypatch, caplog):
    now = [1000.0]
    monkeypatch.setattr(overload.time, "monotonic", lambda: now[0])
    warn = ThrottledWarnings(every=60.0)
    logger = logging.getLogger("tests.overload")
    assert warn.warn(logger, "presence (resume)", "first %s", 1)
    now[0] += 5
    assert not warn.warn(logger, "presence (resume)", "second") and not warn.warn(logger, "presence (resume)", "third")
    assert warn.suppressed("presence (resume)") == 2
    assert warn.warn(logger, "worker", "another call site has its own minute")
    now[0] += 56
    assert warn.warn(logger, "presence (resume)", "again")
    assert [r.getMessage() for r in caplog.records if r.name == "tests.overload"] == [
        "first 1", "another call site has its own minute", "again (2 more like this in the last 61 s)"]
    assert warn.suppressed("presence (resume)") == 0


def test_backoff_doubles_up_to_its_cap_with_jitter():
    assert [overload.backoff_delay(n, 2.0, 30.0, jitter=0) for n in (1, 2, 3, 4, 5, 6, 40)] == [2, 4, 8, 16, 30, 30, 30]
    spread = {round(overload.backoff_delay(3, 2.0, 30.0), 3) for _ in range(50)}
    assert len(spread) > 1 and all(6.4 <= d <= 9.6 for d in spread)


# ============================================================================================ (c) clients that went away
async def test_a_request_cancelled_while_its_transaction_is_released_rolls_back_cleanly(caplog):
    """The 30 'Exception terminating connection' + CancelledError tracebacks: a FileResponse / StreamingResponse sends
    its first message from a task of its own and gives the request's connection back right then
    (``release_request_session``); a client that gave up meanwhile cancels that task (Starlette's disconnect watcher)
    in the middle of the ROLLBACK. The release now finishes the ROLLBACK first: the connection goes back to the pool
    healthy (the very same one: its TEMP table is still there), the uncommitted write is gone, nothing is logged."""
    engine = create_async_engine(db_url(), **engine_options(pool_size=1, max_overflow=0, pool_timeout=POOL_TIMEOUT))
    original = engine.dialect.do_rollback
    in_flight = asyncio.Event()

    def slow_rollback(dbapi_connection: Any) -> None:  # the ROLLBACK takes a while (an overloaded server)
        in_flight.set()
        await_(asyncio.sleep(0.3))
        original(dbapi_connection)

    try:
        async with engine.begin() as conn:  # a table of the pool's only connection (TEMP: gone if it were dropped)
            await conn.execute(text("CREATE TEMP TABLE t_release (n int)"))
        engine.dialect.do_rollback = slow_rollback  # type: ignore[method-assign]
        session = async_sessionmaker(engine, expire_on_commit=False)()
        await session.execute(text("INSERT INTO t_release VALUES (1)"))  # the request's open, uncommitted transaction
        scope = {"type": "http", "state": {REQUEST_SESSION_STATE: session}}
        async with anyio.create_task_group() as tg:
            tg.start_soon(release_request_session, scope)
            await in_flight.wait()
            tg.cancel_scope.cancel()  # the client went away: Starlette cancels the response task
        engine.dialect.do_rollback = original  # type: ignore[method-assign]
        assert engine.pool.checkedout() == 0  # back in the pool ...
        async with engine.connect() as conn:  # ... healthy and the same connection (its TEMP table still there) ...
            assert await conn.scalar(text("SELECT count(*) FROM t_release")) == 0  # ... and the write rolled back
        assert not tracebacks(caplog), tracebacks(caplog)
        assert not [r for r in caplog.records if "terminating connection" in r.getMessage() and r.levelno > logging.DEBUG]
    finally:
        engine.dialect.do_rollback = original  # type: ignore[method-assign]
        await engine.dispose()


def test_a_connection_dropped_because_its_task_was_cancelled_is_one_debug_line(caplog):
    """Where a cancellation still lands mid-statement (a streaming body's query when its client leaves), SQLAlchemy drops
    the connection and logs 'Exception terminating connection' with the CancelledError traceback: an expected event -
    one DEBUG line instead. Any other pool error keeps its ERROR and traceback."""
    pool_log = logging.getLogger("sqlalchemy.pool.impl.AsyncAdaptedQueuePool")
    try:
        raise asyncio.CancelledError("Cancelled via cancel scope 1 by <Task RequestResponseCycle.run_asgi()>")
    except asyncio.CancelledError:
        pool_log.error("Exception terminating connection %r", "<conn-1>", exc_info=True)
    try:
        raise RuntimeError("a real problem")
    except RuntimeError:
        pool_log.error("Exception terminating connection %r", "<conn-2>", exc_info=True)
    pool_records = [r for r in caplog.records if r.name.startswith("sqlalchemy.pool")]
    assert [(r.getMessage(), r.levelno, bool(r.exc_info)) for r in pool_records] == [
        ("Exception terminating connection '<conn-2>'", logging.ERROR, True)]
    [debug] = [r for r in caplog.records if r.name == "ironpulse.db"]
    assert debug.levelno == logging.DEBUG and "'<conn-1>'" in debug.getMessage() and not debug.exc_info


async def test_a_client_gone_while_its_body_is_read_is_no_error(client, db, caplog):
    """The 2 x 'unhandled error on POST /shop/cart/add' + 'Exception in ASGI application' (ClientDisconnect): the
    client gave up waiting before its JSON body was read."""
    user = await make_customer(db)
    await login_as(client, user)
    token = await csrf_token(client)
    cookie = "; ".join(f"{k}={v}" for k, v in client.cookies.items())
    sent = await asgi("POST", "/shop/cart/add", headers=[
        (b"content-type", b"application/json"), (b"content-length", b"32"), (b"accept", b"application/json"),
        (b"x-csrf-token", token.encode()), (b"cookie", cookie.encode())])
    assert status_of(sent) == 499  # never a 500 (nobody receives it anyway)
    assert not tracebacks(caplog), tracebacks(caplog)
    assert not [r for r in caplog.records if "unhandled error" in r.getMessage()]
    assert any(r.levelno == logging.DEBUG and "client went away during POST /shop/cart/add" in r.getMessage()
               for r in caplog.records)


@pytest.fixture
def half_written_route(app: Any):  # noqa: ANN201
    async def write_then_read(request: Request, db: DB) -> dict[str, bool]:
        await db.execute(update(User).where(User.id == int(request.query_params["u"])).values(full_name="Half Written"))
        await request.body()  # the client is gone: ClientDisconnect, mid-transaction
        await db.commit()
        return {"ok": True}

    before = list(app.router.routes)
    app.add_api_route("/test-overload/write-then-read", write_then_read, methods=["GET"])
    yield
    app.router.routes[:] = before


async def test_a_request_whose_client_left_mid_transaction_rolls_back(half_written_route, db, caplog):
    user = await make_customer(db, full_name="Original Name")
    sent = await asgi("GET", "/test-overload/write-then-read", query=f"u={user.id}")
    assert status_of(sent) == 499
    assert (await db.execute(select(User.full_name).where(User.id == user.id))).scalar_one() == "Original Name"
    assert not tracebacks(caplog), tracebacks(caplog)


# ============================================================================================ (d) requests keep the 503
async def test_a_request_without_a_free_connection_still_gets_the_friendly_503(db, tiny_pool, client, caplog):
    user = await make_customer(db)
    await login_as(client, user)
    await tiny_pool.hold()
    r = await client.get("/api/notifications/unread", headers={"Accept": "application/json"})
    assert r.status_code == 503 and r.headers["retry-after"] == "5" and r.json()["code"] == "timeout"
    page = await client.get("/me", headers={"Accept": "text/html"})
    assert page.status_code == 503 and "Taking too long" in page.text
    assert not tracebacks(caplog), tracebacks(caplog)
    assert any(m.startswith("database pool timeout on GET /api/notifications/unread")
               for m in warnings(caplog, "ironpulse.errors"))


# ============================================================================================ (a) /ws: 1013
async def test_the_ws_session_check_without_a_free_connection_closes_with_1013(db, tiny_pool, caplog):
    """43 + 7 tracebacks of the stress test: _live_user / lookup_session in the /ws handshake met an exhausted pool."""
    user = await make_customer(db)
    token = await new_session(db, user)
    await tiny_pool.hold()
    for _ in range(3):  # three pages turned away within the minute: ONE warning line
        ws = FakeWS(token)
        await asyncio.wait_for(core_router.websocket_endpoint(ws), 5)  # type: ignore[arg-type]
        assert ws.accepted and ws.closed == core_router.WS_CLOSE_TRY_AGAIN == 1013 and ws.sent == []
    assert not hub.is_online(user.id) and not presence.is_connected(user.id)
    assert not tracebacks(caplog), tracebacks(caplog)
    [line] = warnings(caplog, "ironpulse.core")
    assert line.startswith("database pool timeout in WS /ws: session check - socket closed with 1013 (try again later)")
    assert "QueuePool limit of size 1 overflow 0 reached" in line and throttled.suppressed("WS /ws") == 2
    await tiny_pool.release()
    ws = FakeWS(token)  # the page's next try, once the database answers
    task = await open_ws(ws)
    assert ws.sent[0]["type"] == "hello" and presence.is_connected(user.id)
    ws.bye(1000)
    await asyncio.wait_for(task, 3)


async def test_the_ws_session_recheck_without_a_free_connection_closes_with_1013(db, tiny_pool, caplog, monkeypatch):
    """The pool runs dry between the handshake's check and its re-check (after the hub subscription): 1013, the socket
    is unsubscribed, and presence hears a short absence that its grace period covers when the page comes back."""
    monkeypatch.setattr(get_settings(), "presence_grace_seconds", 30)
    user = await make_customer(db)
    token = await new_session(db, user)
    real = sessions_mod.lookup_session
    calls = 0

    async def dry_on_the_recheck(session: Any, tok: str | None) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            await tiny_pool.hold()
        return await real(session, tok)

    monkeypatch.setattr(sessions_mod, "lookup_session", dry_on_the_recheck)
    ws = FakeWS(token)
    await asyncio.wait_for(core_router.websocket_endpoint(ws), 5)  # type: ignore[arg-type]
    assert calls == 2 and ws.accepted and ws.closed == 1013 and ws.sent == []  # no hello
    assert not hub.is_online(user.id) and not presence.is_connected(user.id)
    assert presence.snapshot()["grace_timers"] == 1  # not taken for gone: the grace period runs
    await tiny_pool.release()
    await presence.drain()
    monkeypatch.setattr(sessions_mod, "lookup_session", real)
    ws2 = FakeWS(token)  # the page comes back after its back-off
    task = await open_ws(ws2)
    assert ws2.sent[0]["type"] == "hello" and presence.snapshot()["grace_timers"] == 0 and not presence.is_away(user.id)
    ws2.bye(1000)
    await asyncio.wait_for(task, 3)
    assert not tracebacks(caplog), tracebacks(caplog)
    assert any(m.startswith("database pool timeout in WS /ws: session re-check - socket closed with 1013")
               for m in warnings(caplog, "ironpulse.core"))


async def test_other_errors_in_the_ws_session_check_still_raise(db, monkeypatch):
    async def broken(session: Any, tok: str | None) -> Any:
        raise RuntimeError("a bug, not the weather")

    monkeypatch.setattr(sessions_mod, "lookup_session", broken)
    with pytest.raises(RuntimeError):
        await core_router.websocket_endpoint(FakeWS("token"))  # type: ignore[arg-type]


JS_TESTS = r"""
test("1013 (server busy): back off 1-3 s, then 2-6 s, 4-12 s, 8-24 s, at most 10-30 s", function () {
  var rnd = Math.random;
  try {
    document.fire("DOMContentLoaded", {});
    eq(__sockets.length, 1);
    Math.random = function () { return 0; }; // the earliest moment of every window
    var earliest = [1000, 2000, 4000, 8000, 10000, 10000];
    for (var i = 0; i < earliest.length; i++) {
      var s = lastSocket();
      wsOpen(s); // accepted ...
      wsClose(s, 1013); // ... and turned away: try again later
      advance(earliest[i] - 1);
      eq(__sockets.length, i + 1, "reconnected too early after 1013 #" + (i + 1));
      advance(1);
      eq(__sockets.length, i + 2, "not reconnected after 1013 #" + (i + 1));
    }
    eq(IP.ws.busy, 6);
    eq(IP.ws.retries, 0, "1013 keeps its own count");
    eq(IP.ws.floods, 0, "1013 is no flood");
    Math.random = function () { return 0.999999; }; // the latest moment: still within 30 s
    var last = lastSocket();
    wsOpen(last);
    wsClose(last, 1013);
    advance(29999);
    eq(__sockets.length, 7, "reconnected before the window ended");
    advance(1);
    eq(__sockets.length, 8, "a 1013 back-off may never exceed 30 s");
  } finally {
    Math.random = rnd;
  }
});

test("a socket that got its hello starts the 1013 back-off over", function () {
  var rnd = Math.random;
  try {
    var s = lastSocket();
    wsOpen(s);
    wsMsg(s, { type: "hello", user_id: 7, conn_id: "c1", page_id: IP.pageId });
    eq(IP.ws.busy, 0);
    Math.random = function () { return 0; };
    wsClose(s, 1013);
    advance(999);
    eq(__sockets.length, 8);
    advance(1);
    eq(__sockets.length, 9);
  } finally {
    Math.random = rnd;
  }
});

test("other close codes keep their own back-off", function () {
  var s = lastSocket();
  wsOpen(s);
  wsClose(s, 1006);
  advance(999);
  eq(__sockets.length, 9);
  advance(600);
  eq(__sockets.length, 10);
});

test("pagehide during a 1013 back-off never reconnects", function () {
  var s = lastSocket();
  wsOpen(s);
  wsClose(s, 1013);
  window.fire("pagehide", { persisted: false });
  advance(120000);
  eq(__sockets.length, 10);
});

test("no listener logged an error", function () {
  eq(__errors.length, 0, __errors.join(" | "));
});
"""


@needs_jsc
@pytest.mark.parametrize("minified", [False, True], ids=["source", "minified"])
def test_the_page_comes_back_after_1013_with_a_jittered_back_off(tmp_path, minified):
    """app/static/src/js/00-core.js in JavaScriptCore (the stub browser of tests/test_ai_frontend_static.py), as written
    and minified like scripts/build_assets.py does."""
    tests_mark = "/* ================================================================ tests"
    run_mark = "/* ---------------------------------------------------------------- run */"
    head, _, rest = HARNESS.partition(tests_mark)
    _, _, runner = rest.partition(run_mark)
    harness = tmp_path / "busy_harness.js"
    harness.write_text(head + JS_TESTS + run_mark + runner, encoding="utf-8")
    core = CORE
    if minified:
        import rjsmin

        core = tmp_path / "core.min.js"
        core.write_text(rjsmin.jsmin(f"/* 00-core.js */\n{CORE.read_text(encoding='utf-8')}\n;",
                                     keep_bang_comments=False), encoding="utf-8")
    proc = subprocess.run([_jsc(), str(harness), "--", str(core)], capture_output=True, text=True, timeout=120,
                          cwd=tmp_path)
    out = proc.stdout + proc.stderr
    m = re.search(r"SUMMARY passed=(\d+) failed=(\d+) total=(\d+)", out)
    assert m, out[-4000:]
    assert m.group(2) == "0" and m.group(1) == m.group(3) == "5", out[-4000:]


# ============================================================================================ (b) presence
async def test_a_return_the_database_cannot_serve_resumes_the_paused_jobs_later(db, tiny_pool, caplog):
    """7 x 'presence: return of user N failed' (resume_user_jobs): deferred, then done."""
    user = await make_customer(db)
    paused = await add_job(db, "overload_pres", user_id=user.id, policy="pause", status=JobStatus.PAUSED)
    await tiny_pool.hold()
    connect(user.id, "c1")  # a fresh arrival resumes their paused jobs ... the pool is dry
    await presence.drain()
    assert (await job_row(db, paused)).status == JobStatus.PAUSED and presence.snapshot()["deferred"] == 1
    assert not tracebacks(caplog), tracebacks(caplog)
    [line] = warnings(caplog, "ironpulse.presence")
    assert line.startswith(f"database pool timeout in presence (resume): resuming the paused jobs of user {user.id} "
                           "is deferred, retried in the background - QueuePool limit")
    await tiny_pool.release()
    assert await presence.retry_deferred() is True
    assert (await job_row(db, paused)).status == JobStatus.QUEUED
    snap = presence.snapshot()
    assert snap["deferred"] == 0 and snap["stats"]["deferred_done"] == 1 and snap["stats"]["jobs_resumed"] == 1


async def test_the_sweeper_retries_by_itself_and_backs_off_while_the_pool_stays_dry(db, tiny_pool, caplog, monkeypatch):
    """No one calls retry_deferred here: the sweeper does - one probe per round, waiting 0.05, 0.1, 0.2, 0.2 s (the
    production 5 s doubling to 60 s, scaled down) while the pool is dry, and picks the work up once it answers."""
    monkeypatch.setattr(presence_mod, "RETRY_FIRST_SECONDS", 0.05)
    monkeypatch.setattr(presence_mod, "RETRY_MAX_SECONDS", 0.2)
    waits: list[float] = []

    def no_jitter(failures: int, base: float, cap: float, jitter: float = 0.2) -> float:
        waits.append(min(cap, base * 2 ** (failures - 1)))
        return waits[-1]

    monkeypatch.setattr(presence_mod, "backoff_delay", no_jitter)
    user = await make_customer(db)
    paused = await add_job(db, "overload_pres", user_id=user.id, policy="pause", status=JobStatus.PAUSED)
    await tiny_pool.hold()
    connect(user.id, "c1")
    await presence.drain()
    await wait_until(lambda: len(waits) >= 4)
    assert waits[:4] == [0.05, 0.1, 0.2, 0.2]  # doubling up to its cap while every round fails
    assert (await job_row(db, paused)).status == JobStatus.PAUSED
    await tiny_pool.release()
    await wait_until(lambda: presence.snapshot()["stats"].get("deferred_done") == 1)
    assert (await job_row(db, paused)).status == JobStatus.QUEUED and presence.snapshot()["deferred"] == 0
    assert not tracebacks(caplog), tracebacks(caplog)
    lines = warnings(caplog, "ironpulse.presence")
    assert len(lines) == 2 and "presence (resume)" in lines[0] and "presence (retry)" in lines[1]  # the rest held back
    assert throttled.suppressed("presence (retry)") >= 2


async def test_an_exit_the_database_cannot_serve_is_applied_once_it_answers(db, tiny_pool, grace, caplog):
    """1 x 'presence: exit of user N failed' (apply_exit_policy): the user counts as gone at once, the exit policies
    follow later."""
    user = await make_customer(db)
    paused = await add_job(db, "overload_pres", user_id=user.id, policy="pause")
    cancelled = await add_job(db, "overload_pres", user_id=user.id, policy="cancel")
    kept = await add_job(db, "overload_pres", user_id=user.id, policy="keep")
    connect(user.id, "c1")
    await presence.drain()
    await tiny_pool.hold()
    disconnect(user.id, "c1")
    await after_grace()
    assert presence.is_away(user.id) and presence.snapshot()["deferred"] == 1
    assert {(await job_row(db, j)).status for j in (paused, cancelled, kept)} == {JobStatus.QUEUED}
    assert not tracebacks(caplog), tracebacks(caplog)
    [line] = warnings(caplog, "ironpulse.presence")
    assert f"applying the exit policies of user {user.id} is deferred" in line
    await tiny_pool.release()
    assert await presence.retry_deferred() is True
    p, c, k = await job_row(db, paused), await job_row(db, cancelled), await job_row(db, kept)
    assert (p.status, p.cancel_reason) == (JobStatus.PAUSED, "left") and c.status == JobStatus.CANCELLED
    assert k.status == JobStatus.QUEUED and presence.snapshot()["stats"]["exits"] == 1


async def test_a_deferred_exit_is_dropped_when_the_user_came_back_meanwhile(db, tiny_pool, grace, caplog):
    user = await make_customer(db)
    pending = await add_job(db, "overload_pres", user_id=user.id, policy="pause")
    connect(user.id, "c1")
    await presence.drain()
    await tiny_pool.hold()
    disconnect(user.id, "c1")
    await after_grace()
    assert presence.is_away(user.id) and presence.snapshot()["deferred"] == 1
    await tiny_pool.release()
    connect(user.id, "c2")  # they are back before the retry
    await presence.drain()
    assert await presence.retry_deferred() is True
    assert not presence.is_away(user.id) and (await job_row(db, pending)).status == JobStatus.QUEUED  # never paused
    assert not tracebacks(caplog), tracebacks(caplog)


async def test_a_sign_out_the_database_cannot_judge_now_is_judged_later(db, tiny_pool, caplog):
    """5 x 'presence: end of sessions of user N failed' (_other_session_alive): the judgement - and the exit it leads
    to - follows once the database answers."""
    user = await make_customer(db)
    paused = await add_job(db, "overload_pres", user_id=user.id, policy="pause")
    await tiny_pool.hold()
    await presence.logout(user.id, session_hash="ended-session")
    assert not presence.is_away(user.id) and presence.snapshot()["deferred"] == 1
    assert (await job_row(db, paused)).status == JobStatus.QUEUED
    assert not tracebacks(caplog), tracebacks(caplog)
    [line] = warnings(caplog, "ironpulse.presence")
    assert f"judging the ended sessions of user {user.id} is deferred" in line
    await tiny_pool.release()
    assert await presence.retry_deferred() is True
    row = await job_row(db, paused)
    assert presence.is_away(user.id) and (row.status, row.cancel_reason) == (JobStatus.PAUSED, "logout")


async def test_a_deferred_resume_is_dropped_when_the_user_left_again(db, tiny_pool, grace, caplog):
    user = await make_customer(db)
    paused = await add_job(db, "overload_pres", user_id=user.id, policy="pause", status=JobStatus.PAUSED)
    await tiny_pool.hold()
    connect(user.id, "c1")  # the resume of this arrival is deferred ...
    await presence.drain()
    await tiny_pool.release()
    disconnect(user.id, "c1")  # ... and they leave again before the retry: the exit owns their jobs now
    await after_grace()
    assert presence.is_away(user.id)
    assert await presence.retry_deferred() is True
    assert (await job_row(db, paused)).status == JobStatus.PAUSED
    assert not tracebacks(caplog), tracebacks(caplog)


# ============================================================================================ (b) job runner
async def test_a_job_run_cut_short_by_the_pool_is_queued_again_with_its_attempt_given_back(db, registry, caplog):
    calls: list[int] = []

    async def handler(session: Any, payload: dict[str, Any]) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise PoolTimeoutError(DRY)

    registry("overload_run", handler)
    job_id = await add_job(db, "overload_run")
    assert await run_jobs() == 1
    r = await job_row(db, job_id)
    assert (r.status, r.attempts, r.locked_at) == (JobStatus.QUEUED, 0, None)  # an overload never uses up attempts
    assert r.last_error == "deferred: the database was unavailable (pool)"
    assert r.run_after > utcnow() + timedelta(seconds=J.DB_RETRY_SECONDS * 0.7)
    assert not tracebacks(caplog), tracebacks(caplog)
    [line] = warnings(caplog, "ironpulse.jobs")
    assert line.startswith(f"database pool timeout in jobs (run): job {job_id} (overload_run) queued again in ")
    async with dbmod.get_sessionmaker()() as s:  # due again
        await s.execute(update(Job).where(Job.id == job_id).values(run_after=utcnow()))
        await s.commit()
    assert await run_jobs() == 1
    r = await job_row(db, job_id)
    assert (r.status, r.attempts, r.last_error) == (JobStatus.DONE, 1, None) and len(calls) == 2


async def test_a_final_status_write_waits_out_the_overload(db, registry, caplog, monkeypatch):
    real_scope = J.session_scope
    dry = {"left": 0}

    @asynccontextmanager
    async def flaky_scope() -> AsyncIterator[Any]:
        if dry["left"]:
            dry["left"] -= 1
            raise PoolTimeoutError(DRY)
        async with real_scope() as s:
            yield s

    async def handler(session: Any, payload: dict[str, Any]) -> None:
        dry["left"] = 3  # the DONE write meets a dry pool three times

    registry("overload_write", handler)
    monkeypatch.setattr(J, "session_scope", flaky_scope)
    monkeypatch.setattr(J, "backoff_delay", lambda *a, **k: 0.01)
    job_id = await add_job(db, "overload_write")
    assert await run_jobs() == 1
    r = await job_row(db, job_id)
    assert (r.status, r.attempts) == (JobStatus.DONE, 1) and dry["left"] == 0
    assert not tracebacks(caplog), tracebacks(caplog)
    [line] = warnings(caplog, "ironpulse.jobs")
    assert line.startswith(f"database pool timeout in jobs (status write): job {job_id} (overload_write) not marked "
                           "done yet - trying again")


async def test_a_final_status_write_that_never_gets_through_is_left_to_the_stale_job_reclaim(db, registry, caplog,
                                                                                              monkeypatch):
    real_scope = J.session_scope
    dry = {"on": False}

    @asynccontextmanager
    async def flaky_scope() -> AsyncIterator[Any]:
        if dry["on"]:
            raise PoolTimeoutError(DRY)
        async with real_scope() as s:
            yield s

    async def handler(session: Any, payload: dict[str, Any]) -> None:
        dry["on"] = True

    registry("overload_stuck", handler)
    monkeypatch.setattr(J, "session_scope", flaky_scope)
    monkeypatch.setattr(J, "backoff_delay", lambda *a, **k: 0.02)
    monkeypatch.setattr(J, "WRITE_RETRY_SECONDS", 0.1)
    job_id = await add_job(db, "overload_stuck")
    assert await run_jobs() == 1
    r = await job_row(db, job_id)
    assert r.status == JobStatus.RUNNING and r.locked_at is not None  # STALE_AFTER: another claim runs it again
    assert not tracebacks(caplog), tracebacks(caplog)
    assert any("left RUNNING for the stale-job reclaim" in m for m in warnings(caplog, "ironpulse.jobs"))


async def test_a_presence_check_the_database_cannot_answer_lets_the_job_run(db, registry, caplog, monkeypatch):
    async def dry(user_id: int) -> bool:
        raise PoolTimeoutError(DRY)

    monkeypatch.setattr(presence, "away_now", dry)
    ran: list[dict[str, Any]] = []

    async def handler(session: Any, payload: dict[str, Any]) -> None:
        ran.append(payload)

    registry("overload_owned", handler, on_exit="pause")
    user = await make_customer(db)
    job_id = await add_job(db, "overload_owned", user_id=user.id, policy="pause")
    assert await run_jobs() == 1
    assert ran and (await job_row(db, job_id)).status == JobStatus.DONE  # fail safe: never parked on a guess
    assert not tracebacks(caplog), tracebacks(caplog)
    [line] = warnings(caplog, "ironpulse.jobs")
    assert f"could not tell whether user {user.id} is away - their job runs" in line


# ============================================================================================ (b) worker loops
async def test_a_worker_loop_backs_off_quietly_and_resumes_its_pace(caplog, monkeypatch):
    from app.services import worker as worker_mod

    monkeypatch.setattr(worker_mod, "backoff_delay", lambda n, base, cap, jitter=0.2: min(cap, base * 2 ** (n - 1)))
    monkeypatch.setattr(worker_mod, "DB_BACKOFF_MAX_SECONDS", 0.4)
    calls: list[float] = []

    async def flaky() -> None:
        calls.append(time.monotonic())
        if len(calls) <= 3:
            raise PoolTimeoutError(DRY)

    runner = worker_mod.BackgroundRunner()
    task = asyncio.create_task(runner._loop("flaky", 0.05, flaky, 0.0))
    await wait_until(lambda: len(calls) >= 6)
    runner._stopping.set()
    await asyncio.wait_for(task, 5)
    gaps = [b - a for a, b in zip(calls, calls[1:])]
    # 0.1, 0.2, 0.4 s after the 1st, 2nd and 3rd failure (doubling, capped at 0.4); its own 0.05 s after a success
    assert gaps[0] >= 0.09 and gaps[1] >= 0.19 and gaps[2] >= 0.39 and gaps[3] < 0.3 and gaps[4] < 0.3, gaps
    assert not tracebacks(caplog), tracebacks(caplog)
    [line] = warnings(caplog, "ironpulse.worker")
    assert line.startswith("database pool timeout in background task flaky: next round in ")
    assert throttled.suppressed("background task flaky") == 2


async def test_other_worker_errors_still_log_their_traceback(caplog):
    from app.services.worker import BackgroundRunner

    runner = BackgroundRunner()

    async def bug() -> None:
        runner._stopping.set()
        raise RuntimeError("a real bug")

    await asyncio.wait_for(runner._loop("buggy", 0.01, bug, 0.0), 5)
    assert any(r.exc_info and r.getMessage() == "background task buggy failed" for r in caplog.records)


async def test_the_jobs_and_email_loops_pick_their_work_up_once_the_pool_answers(db, tiny_pool, registry, caplog,
                                                                                  monkeypatch):
    """2 x _claim + 1 x send_pending_emails tracebacks: the loops wait, quietly, and nothing is lost."""
    from app.services import worker as worker_mod

    monkeypatch.setattr(worker_mod, "DB_BACKOFF_MAX_SECONDS", 0.3)
    monkeypatch.setattr(get_settings(), "smtp_host", "")  # spooled to var/mail
    ran: list[dict[str, Any]] = []

    async def handler(session: Any, payload: dict[str, Any]) -> None:
        ran.append(payload)

    registry("overload_loop", handler)
    await add_job(db, "overload_loop")
    mail = EmailOutbox(to_email="member@example.com", subject="Receipt", text_body="Thanks")
    db.add(mail)
    await db.commit()
    await tiny_pool.hold()
    runner = worker_mod.BackgroundRunner()
    runner._tasks += [asyncio.create_task(runner._loop("jobs", 0.05, runner._jobs, 0.0)),
                      asyncio.create_task(runner._loop("email", 0.05, runner._email, 0.0))]
    try:
        await asyncio.sleep(0.8)  # their rounds meet the real QueuePool TimeoutError
        assert ran == []
        assert (await db.execute(select(EmailOutbox.status).where(EmailOutbox.id == mail.id))).scalar_one() == \
            EmailStatus.QUEUED
        await db.commit()
        await tiny_pool.release()
        await wait_until(lambda: bool(ran))
        for _ in range(500):
            status = (await db.execute(select(EmailOutbox.status).where(EmailOutbox.id == mail.id))).scalar_one()
            await db.commit()
            if status == EmailStatus.LOGGED:
                break
            await asyncio.sleep(0.02)
        assert status == EmailStatus.LOGGED
    finally:
        await asyncio.wait_for(runner.stop(), 10)
        J.accept_jobs()
    assert not tracebacks(caplog), tracebacks(caplog)
    lines = warnings(caplog, "ironpulse.worker")
    assert any(m.startswith("database pool timeout in background task jobs:") for m in lines)
    assert any(m.startswith("database pool timeout in background task email:") for m in lines)
