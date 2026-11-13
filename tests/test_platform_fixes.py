"""Platform fixes (app/main.py, app/db.py, app/core/static.py, app/core/errors.py, app/security/{ratelimit,bans,gate}.py,
tests/conftest.py) - one regression test per defect, each failing on the code before the fix:

(a) versioned static assets carried no Cache-Control (every visitor re-downloaded ~720 KB of CSS/JS);
(b) the shared Redis limits fell back to per-worker memory under load: a burst queueing for one of the client's pooled
    connections was blamed on Redis (100 ms deadline), every fallback handed out a fresh burst per worker, and the
    workers retried Redis in step;
(c) several workers starting together raced each other's schema changes (UniqueViolation on index creation);
(d) pooled connections sat 'idle in transaction': the request's transaction stayed open while the response body went
    out (FastAPI ends get_db after the last byte) and while csrf_protect read a whole upload (after load_user's query);
(e) review findings: an admin's Lift did not reach the other workers' threat scores / held bans; synchronous reports
    banned from a stale local view without the claim; two workers' ban flushes raced into a 'repeat offence'; a busy
    NAT got banned for throttling; IPv6 clients got a fresh allowance from every address of their /64; tests used the
    dev servers' Redis database 0.

Redis: the test Redis database (tests/conftest.py: TEST_REDIS_DB, default 15, never 0 or 9); only this test database's
namespace is touched (the conftest's reset deletes it after every test). Tests that need Redis skip without it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi import Depends, FastAPI
from fastapi.responses import StreamingResponse
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app import db as dbmod
from app.config import APP_DIR, Settings, get_settings
from app.core import static as static_mod
from app.core.templating import static_url
from app.core.utils import utcnow
from app.main import ReleaseDBOnResponseMiddleware
from app.main import app as fastapi_app
from app.models import IpBan
from app.models.enums import ThreatCategory
from app.security import bans, gate, ratelimit
from tests.conftest import login_as, make_client
from tests.factories import make_customer

LOG = "ironpulse.security"
ALLOWED = [0, "0", 0]


# ============================================================================ helpers
def _new_client(url: str) -> Any:
    import redis.asyncio as aioredis

    return aioredis.from_url(url, decode_responses=True, socket_timeout=3, socket_connect_timeout=3)


def _returning(client: Any):
    async def get() -> Any:
        return client

    return get


_reachable: bool | None = None


async def _redis_up() -> bool:
    global _reachable
    if _reachable is None:
        _reachable = False
        url = get_settings().redis_url
        if url:
            c = _new_client(url)
            try:
                _reachable = bool(await c.ping())
            except Exception:  # noqa: BLE001
                _reachable = False
            finally:
                await c.aclose()
    return _reachable


@pytest.fixture(autouse=True)
def _pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shipped limits whatever .env says, no whitelist, and a Redis deadline generous enough for a busy machine
    (tests about the deadline set their own)."""
    s = get_settings()
    for name, value in (("rate_limit_per_minute", 240), ("rate_limit_auth_per_minute", 10), ("threat_ban_score", 12.0),
                        ("rate_limit_static_per_minute", 1500), ("rate_limit_redis_timeout_ms", 2000),
                        ("rate_limit_whitelist", [])):
        monkeypatch.setattr(s, name, value)


@pytest.fixture
async def workers() -> Any:
    """``await workers(n)`` -> n SharedStores on the test Redis database, each with its own client: n app workers."""
    if not await _redis_up():
        pytest.skip("Redis (REDIS_URL) is not reachable")
    clients: list[Any] = []

    async def make(n: int) -> list[ratelimit.SharedStore]:
        out = []
        for _ in range(n):
            c = _new_client(get_settings().redis_url)
            clients.append(c)
            out.append(ratelimit.SharedStore(client=_returning(c)))
        return out

    yield make
    for c in clients:
        await c.aclose()


async def _drain(*stores: Any) -> None:
    for store in stores:
        pending = list(store._pending)
        if pending:
            await asyncio.wait(pending, timeout=5)


class _Downstream:
    def __init__(self, status: int = 200) -> None:
        self.status, self.calls = status, 0

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        self.calls += 1
        await send({"type": "http.response.start", "status": self.status, "headers": []})
        await send({"type": "http.response.body", "body": b""})


async def _request(app: Any, ip: str, method: str, path: str, query: bytes = b"") -> int:
    scope = {"type": "http", "method": method, "path": path, "raw_path": path.encode(), "query_string": query,
             "headers": [(b"content-type", b"application/x-www-form-urlencoded"), (b"content-length", b"0"),
                         (b"user-agent", b"Mozilla/5.0 (platform-fixes test)")],
             "client": (ip, 51000), "server": ("testserver", 80), "scheme": "http", "state": {}}
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return next(m for m in sent if m["type"] == "http.response.start")["status"]


def _gate(store: Any, downstream: Any = None) -> gate.SecurityGateMiddleware:
    return gate.SecurityGateMiddleware(downstream or _Downstream(), rate_limiter=ratelimit.RateLimiter(store=store),
                                       threats=bans.ThreatTracker(store=store))


SQLI = b"q=1%27%20UNION%20SELECT%20password%20FROM%20users--"  # severity 4: 10 threat points, blocked


# ============================================================================ (a) static assets
def _asset() -> tuple[str, str]:
    url = static_url("dist/app.min.css")
    path, _, query = url.partition("?")
    return path, query


async def test_a_versioned_asset_is_cached_for_a_year_and_unversioned_ones_revalidate(client: httpx.AsyncClient) -> None:
    path, query = _asset()
    assert query.startswith("v=") and len(query) == 2 + static_mod.VERSION_LENGTH
    versioned = await client.get(f"{path}?{query}")
    assert versioned.status_code == 200
    assert versioned.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert versioned.headers["etag"] and "content-security-policy" in versioned.headers  # CSP still added
    assert versioned.headers["x-content-type-options"] == "nosniff"
    plain = await client.get(path)
    assert plain.status_code == 200 and plain.headers["cache-control"] == "no-cache"  # revalidated every time ...
    again = await client.get(path, headers={"If-None-Match": plain.headers["etag"]})
    assert again.status_code == 304 and again.headers["cache-control"] == "no-cache"  # ... 304 while unchanged
    assert again.headers["etag"] == plain.headers["etag"] and not again.content
    cached = await client.get(f"{path}?{query}", headers={"If-None-Match": versioned.headers["etag"]})
    assert cached.status_code == 304 and cached.headers["cache-control"] == "public, max-age=31536000, immutable"


async def test_a_version_that_is_not_the_files_is_never_cached_for_a_year(client: httpx.AsyncClient) -> None:
    """An old page after a deploy, or a new page reaching an old app copy during a rolling deploy: pinning those bytes
    to the URL for a year would serve the wrong asset."""
    path, _query = _asset()
    for stale in ("v=0123456789", "v=", "x=1"):
        r = await client.get(f"{path}?{stale}")
        assert r.status_code == 200 and r.headers["cache-control"] == "no-cache", stale


async def test_pages_link_assets_by_the_version_the_server_caches(client: httpx.AsyncClient) -> None:
    page = await client.get("/auth/login")
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"  # pages are never cached
    links = [part.split('"', 1)[0] for part in page.text.split('"/static/')[1:]]
    versioned = [f"/static/{link}" for link in links if "?v=" in link]
    assert any("app.min.css" in link for link in versioned) and any("app.min.js" in link for link in versioned)
    for link in versioned:
        r = await client.get(link)
        assert r.status_code == 200 and r.headers["cache-control"] == "public, max-age=31536000, immutable", link


def test_the_version_follows_the_files_bytes(tmp_path: Any) -> None:
    f = tmp_path / "x.js"
    f.write_text("one")
    first = static_mod.content_version(f)
    assert first == static_mod.content_version(f)  # remembered
    f.write_text("two!")  # rebuilt in place: a new version at once, no restart
    os.utime(f, ns=(time.time_ns(), time.time_ns() + 1_000_000))
    assert static_mod.content_version(f) != first
    assert static_url("dist/app.min.css").endswith(static_mod.content_version(APP_DIR / "static" / "dist" / "app.min.css"))


# ============================================================================ (b) shared limits under load
class _PooledRedis:
    """A healthy Redis behind a client pool of ``size`` connections (like redis-py's BlockingConnectionPool): each call
    takes ``latency`` s once it has a connection - a burst queues inside the client."""

    def __init__(self, size: int, latency: float) -> None:
        self.pool = asyncio.Semaphore(size)
        self.latency = latency
        self.calls = 0
        self.in_flight = self.peak = 0

    async def evalsha(self, *args: Any, **kwargs: Any) -> Any:
        async with self.pool:
            self.calls += 1
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            try:
                await asyncio.sleep(self.latency)
            finally:
                self.in_flight -= 1
            return ALLOWED


async def test_a_burst_queueing_for_a_connection_is_not_blamed_on_redis(monkeypatch: pytest.MonkeyPatch,
                                                                         caplog: pytest.LogCaptureFixture) -> None:
    """Measured (review): 500 simultaneous checks on a healthy Redis, 100 ms deadline: 125 'failed' - the time spent
    waiting for one of the 32 pooled connections counted as Redis being slow, and the fallback gave each worker a
    fresh burst. The deadline now starts once one of this worker's call slots is free."""
    monkeypatch.setattr(get_settings(), "rate_limit_redis_timeout_ms", 50)
    monkeypatch.setattr(ratelimit, "MAX_CONNECTIONS", 4)
    redis = _PooledRedis(size=4, latency=0.03)  # 30 ms per call, 4 at a time: the 40th waits ~270 ms for a slot
    store = ratelimit.SharedStore(client=_returning(redis))
    limiter = ratelimit.RateLimiter(store=store)
    with caplog.at_level(logging.WARNING, logger=LOG):
        verdicts = await asyncio.gather(*(limiter.check(f"198.51.100.{i}", ["global"]) for i in range(40)))
    assert all(v.shared for v in verdicts) and store.failures == 0 and store.queued_out == 0
    assert redis.calls == 40 and redis.peak <= 4  # never more calls in flight than the client has connections
    assert "Redis unavailable" not in caplog.text


async def test_a_worker_that_cannot_send_in_time_judges_from_memory_without_pausing_redis(
        monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setattr(ratelimit, "MAX_CONNECTIONS", 1)
    monkeypatch.setattr(ratelimit, "QUEUE_WAIT_SECONDS", 0.05)
    redis = _PooledRedis(size=1, latency=0.3)
    store = ratelimit.SharedStore(client=_returning(redis))
    limiter = ratelimit.RateLimiter(store=store)
    with caplog.at_level(logging.WARNING, logger=LOG):
        first, second = await asyncio.gather(limiter.check("198.51.100.60", ["auth"]),
                                             limiter.check("198.51.100.61", ["auth"]))
    assert first.shared and not second.shared and second.allowed  # memory decided the queued one
    assert store.queued_out == 1 and store.failures == 0 and not store._open  # no breaker, no outage
    assert "this worker is overloaded" in caplog.text and "Redis unavailable" not in caplog.text


async def test_the_fallback_continues_from_the_shared_state_not_a_fresh_burst(workers: Any) -> None:
    """Review probe: worker A spends the sign-in burst, worker B sees one shared refusal, then B's Redis hangs - B's
    memory allowed the full burst again (10). Every shared verdict now mirrors the buckets' tokens into memory."""
    a, b = await workers(2)
    first, second = ratelimit.RateLimiter(store=a), ratelimit.RateLimiter(store=b)
    ip = "198.51.100.62"
    capacity = int(first.limits()["auth"].capacity)
    assert all([(await first.check(ip, ["global", "auth"])).allowed for _ in range(capacity)])
    refused = await second.check(ip, ["global", "auth"])
    assert refused.shared and not refused.allowed and refused.bucket == "auth"
    broken = ratelimit.SharedStore(client=_returning(None))  # B's Redis is gone: memory only from here
    second._store = broken
    allowed = [(await second.check(ip, ["global", "auth"])).allowed for _ in range(capacity)]
    assert allowed.count(True) == 0  # what was left of the shared allowance, not a fresh burst of 10


async def test_after_an_outage_one_probe_goes_to_redis_and_the_pause_is_jittered(monkeypatch: pytest.MonkeyPatch,
                                                                                 caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setattr(ratelimit, "OUTAGE_SECONDS", 0.0)
    monkeypatch.setattr(ratelimit, "RETRY_SECONDS", 0.2)
    current: dict[str, Any] = {"client": None}

    class Broken:
        calls = 0

        async def evalsha(self, *args: Any, **kwargs: Any) -> Any:
            Broken.calls += 1
            raise ConnectionError("redis went away")

    async def client() -> Any:
        return current["client"]

    current["client"] = Broken()
    store = ratelimit.SharedStore(client=client)
    limiter = ratelimit.RateLimiter(store=store)
    pauses = []
    with caplog.at_level(logging.INFO, logger=LOG):
        for _ in range(30):  # the pause is drawn per outage: +-25 % around RETRY_SECONDS
            store._open, store._failing_since = False, None
            started = time.monotonic()
            assert not (await limiter.check("198.51.100.63", ["global"])).shared
            pauses.append(store._skip_until - started)
        assert all(0.2 * 0.75 - 0.01 <= p <= 0.2 * 1.25 + 0.01 for p in pauses) and len({round(p, 4) for p in pauses}) > 1
        calls = Broken.calls
        for _ in range(10):  # open: nobody waits for the dead Redis
            assert not (await limiter.check("198.51.100.63", ["global"])).shared
        assert Broken.calls == calls
        healthy = _PooledRedis(size=8, latency=0.1)
        current["client"] = healthy
        await asyncio.sleep(0.26)
        verdicts = await asyncio.gather(*(limiter.check("198.51.100.64", ["global"]) for _ in range(6)))
        assert healthy.calls == 1 and sum(v.shared for v in verdicts) == 1  # half-open: ONE probe, the rest memory
        assert (await limiter.check("198.51.100.64", ["global"])).shared and not store._open  # closed again
    assert caplog.text.count("Redis unavailable") == 1 and caplog.text.count("Redis answers again") == 1


def test_the_default_redis_deadline_leaves_room_for_a_busy_redis() -> None:
    assert ratelimit.DEFAULT_TIMEOUT_MS >= 250


# ============================================================================ (c) start-up serialisation
async def test_workers_starting_together_apply_the_schema_once(caplog: pytest.LogCaptureFixture) -> None:
    """Measured (load-test kit): 3 of 4 workers logged 'schema sync could not apply index ...: UniqueViolationError';
    on an empty database the same race stopped uvicorn's whole multi-worker start."""
    async with dbmod.get_engine().begin() as conn:
        await conn.execute(text("DROP INDEX IF EXISTS ix_ip_bans_lifted_at"))
        await conn.execute(text("DROP INDEX IF EXISTS ix_ip_bans_unlifted_until"))
    with caplog.at_level(logging.INFO, logger="ironpulse.db"):
        await asyncio.gather(*(dbmod.create_all() for _ in range(4)))
    assert "could not apply" not in caplog.text, caplog.text
    assert caplog.text.count("schema sync applied") == 1  # one worker applied it, the others found it in place
    async with dbmod.get_engine().connect() as conn:
        names = set((await conn.execute(text("SELECT indexname FROM pg_indexes WHERE tablename = 'ip_bans'"))).scalars())
    assert {"ix_ip_bans_lifted_at", "ix_ip_bans_unlifted_until"} <= names


async def test_the_startup_lock_is_exclusive_across_connections_and_reentrant() -> None:
    spans: list[tuple[float, float]] = []

    async def worker() -> None:
        async with dbmod.startup_lock():
            async with dbmod.startup_lock():  # create_all inside the lifespan's lock: no self-deadlock
                start = time.monotonic()
                await asyncio.sleep(0.15)
                spans.append((start, time.monotonic()))

    await asyncio.wait_for(asyncio.gather(*(worker() for _ in range(3))), timeout=20)
    spans.sort()
    assert len(spans) == 3 and all(spans[i][1] <= spans[i + 1][0] + 0.005 for i in range(2))  # one at a time
    async with dbmod.get_engine().connect() as conn:  # released: nobody holds it any more
        held = await conn.scalar(text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND objid = :k"),
                                 {"k": dbmod.STARTUP_LOCK_KEY})
    assert held == 0


def test_the_lifespan_runs_schema_sync_and_bootstrap_under_the_lock() -> None:
    import inspect

    from app import main

    source = inspect.getsource(main.lifespan)
    lock, schema, boot = source.index("startup_lock()"), source.index("create_all()"), source.index("bootstrap(db)")
    assert lock < schema < boot


# ============================================================================ (d) idle in transaction
def _probe_app() -> tuple[FastAPI, dict[str, Any]]:
    seen: dict[str, Any] = {}
    app = FastAPI(middleware=[])
    app.add_middleware(ReleaseDBOnResponseMiddleware)

    @app.get("/probe")
    async def probe(db: AsyncSession = Depends(dbmod.get_db)) -> StreamingResponse:
        await db.execute(text("SELECT 1"))  # opens the request's transaction (as load_user does)
        seen["before"] = db.in_transaction()

        async def body():
            seen["while_sending"] = db.in_transaction()  # the body goes out after the response started
            yield b"x" * 10
            await db.execute(text("SELECT 2"))  # a body that queries again still works (a fresh transaction)
            yield b"y"

        return StreamingResponse(body())

    return app, seen


async def test_the_requests_connection_goes_back_to_the_pool_when_the_response_starts() -> None:
    app, seen = _probe_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/probe")
    assert r.status_code == 200 and r.content == b"x" * 10 + b"y"
    assert seen["before"] is True and seen["while_sending"] is False


def test_the_app_releases_db_sessions_and_reads_forms_before_opening_a_transaction() -> None:
    from app.core.deps import load_user
    from app.security.csrf import csrf_protect

    assert fastapi_app.user_middleware[-1].cls is ReleaseDBOnResponseMiddleware  # innermost: sees the start first
    calls = [d.dependency for d in fastapi_app.router.dependencies]  # every route's app-wide dependencies, in order
    assert calls.index(csrf_protect) < calls.index(load_user)  # the form body is read before load_user's query


async def test_an_upload_is_read_before_the_request_opens_its_transaction(db: Any, request: Any) -> None:
    """Before: csrf_protect awaited the whole multipart body AFTER load_user's query - a phone uploading a photo for
    seconds kept a pooled connection idle in that transaction (killed after DB_IDLE_TX_TIMEOUT_MS)."""
    from sqlalchemy import event

    user = await make_customer(db)
    await db.commit()
    order: list[str] = []

    def began(conn: Any) -> None:  # any transaction of the app engine (load_user's query opens the request's)
        order.append("transaction")

    engine = dbmod.get_engine().sync_engine
    event.listen(engine, "begin", began)
    request.addfinalizer(lambda: event.remove(engine, "begin", began))
    async with make_client("203.0.113.70") as c:
        await login_as(c, user)
        await c.get("/healthz")

        async def body():
            order.append("body read")
            yield b"--b\r\nContent-Disposition: form-data; name=\"csrf_token\"\r\n\r\nnot-a-token\r\n--b--\r\n"

        order.clear()
        r = await c.post("/account/photo", content=body(), headers={"Content-Type": "multipart/form-data; boundary=b",
                                                                   "Accept": "text/html"})
    assert r.status_code == 403  # a bad token, read from the body ...
    assert order and order[0] == "body read", order  # ... before the request opened any transaction
    assert f"Account: {user.full_name}" in r.text  # the CSRF page still shows who is signed in (app/core/errors.py)


# ============================================================================ (e) bans and threat scores
async def test_a_lift_on_one_worker_reaches_the_score_another_worker_acts_on(workers: Any) -> None:
    """Review probe: after a Lift on worker A, worker B still answered with its own copy (14) - the address's next
    minor event re-banned it, and that counted as offence 2: PERMANENT for a non-subscriber."""
    a, b = await workers(2)
    first, second = bans.ThreatTracker(store=a), bans.ThreatTracker(store=b)
    ip = "198.51.100.80"
    await second.add_async(ip, 4)
    await first.add_async(ip, 2)
    assert await second.add_async(ip, 1) == pytest.approx(14.0, rel=1e-3)
    first.reset(ip)  # the Lift on worker A (lift_ban_in_memory): the shared score and claim are forgotten
    await _drain(a)
    assert await second.add_async(ip, 1) < get_settings().threat_ban_score  # the shared total decides: 1, not 15
    assert await second.score_async(ip) == pytest.approx(1.0, rel=1e-2)


async def test_a_ban_held_for_another_worker_goes_with_the_lift(monkeypatch: pytest.MonkeyPatch) -> None:
    ip = "198.51.100.81"
    bans.hold_ban(ip, 3, "another worker persists it")
    assert bans.ban_registry.get(ip).held
    bans.ban_registry.set_confirmed(ip, None)  # a sync right after the hold (the claimer has not committed yet) ...
    assert bans.ban_registry.get(ip) is not None  # ... keeps it
    monkeypatch.setattr(bans, "HOLD_GRACE_SECONDS", 0.0)
    bans.ban_registry.set_confirmed(ip, None)  # the database says: lifted
    assert bans.ban_registry.get(ip) is None  # before: blocked here for up to SEVERITY_HOURS
    bans.request_ban("198.51.100.82", 3, "queued here")  # a ban this worker persists itself stays until confirmed
    bans.ban_registry.set_confirmed("198.51.100.82", None)
    assert bans.ban_registry.get("198.51.100.82") is not None


async def test_the_ban_sync_of_a_lifted_address_forgets_this_workers_copy_of_its_score(db: Any) -> None:
    ip = "198.51.100.83"
    bans.threat_tracker._raise(ip, 14.0)  # this worker's copy, left high
    db.add(IpBan(ip=ip, reason="x", severity=3, banned_until=utcnow() + timedelta(hours=2), lifted_at=utcnow()))
    await db.commit()
    await bans._reload_addresses(db, [ip], utcnow())
    assert bans.threat_tracker.score(ip) == 0.0


def _report_request(ip: str) -> Request:
    return Request({"type": "http", "method": "POST", "path": "/somewhere", "raw_path": b"/somewhere",
                    "query_string": b"", "headers": [(b"user-agent", b"pytest")], "client": (ip, 5000),
                    "server": ("testserver", 80), "scheme": "http"})


async def test_csrf_reports_ban_through_the_shared_total_and_the_claim(workers: Any) -> None:
    """Before: record_from_request banned from this worker's view without the claim - two workers queued two bans
    (the second a 'repeat offence': PERMANENT), and a copy left high by another worker's Lift re-banned at once."""
    if not await _redis_up():
        pytest.skip("Redis (REDIS_URL) is not reachable")
    ip = "198.51.100.84"
    for _ in range(5):  # 5 x severity 2 = 15 points on the shared total
        bans.security_events.record_from_request(_report_request(ip), ThreatCategory.CSRF, 2, "missing token")
    await _drain(ratelimit.shared_store)
    assert [p.ip for p in bans.security_events.bans].count(ip) == 1
    (other,) = await workers(1)
    assert not await bans.ThreatTracker(store=other).claim_ban(ip)  # the reporting worker holds the claim

    stale = "198.51.100.85"
    bans.threat_tracker._raise(stale, 14.0)  # this worker's copy; the shared score was reset by a Lift elsewhere
    bans.security_events.record_from_request(_report_request(stale), ThreatCategory.CSRF, 2, "missing token")
    await _drain(ratelimit.shared_store)
    assert bans.ban_registry.get(stale) is None and all(p.ip != stale for p in bans.security_events.bans)


async def test_reports_without_redis_still_ban_from_this_workers_view(monkeypatch: pytest.MonkeyPatch) -> None:
    tracker = bans.ThreatTracker(store=ratelimit.SharedStore(client=_returning(None)))
    monkeypatch.setattr(bans, "threat_tracker", tracker)
    ip = "198.51.100.86"
    for _ in range(5):
        bans.security_events.record_from_request(_report_request(ip), ThreatCategory.CSRF, 2, "missing token")
    await _drain(tracker.store)
    assert bans.ban_registry.get(ip) is not None and [p.ip for p in bans.security_events.bans] == [ip]


async def test_two_workers_flushing_the_same_ban_write_one_row() -> None:
    """Review probe: two flushes for one address, each committing 200 ms later, wrote two ip_bans rows - the second
    one 'offence 2' (PERMANENT). The flush lock makes the second wait for the first commit and see its row."""
    ip = "198.51.100.87"
    maker = dbmod.get_sessionmaker()

    async def flush() -> int:
        async with maker() as s:
            n = await bans._finalise_bans(s, [bans.PendingBan(ip, 3, "probe", None)])
            await asyncio.sleep(0.2)
            await s.commit()
            return n

    written = await asyncio.wait_for(asyncio.gather(flush(), flush()), timeout=20)
    async with maker() as s:
        rows = (await s.execute(select(IpBan.offense_count, IpBan.banned_until).where(IpBan.ip == ip))).all()
    assert sorted(written) == [0, 1] and len(rows) == 1 and rows[0].offense_count == 1 and rows[0].banned_until


async def test_throttling_a_busy_address_never_bans_it(workers: Any) -> None:
    """A gym behind one NAT over the shared allowance gets 429s, never a ban (before: >10 rejections a minute were
    3 threat points each - a 2 h ban, and PERMANENT the second time)."""
    a, b = await workers(2)
    gates = [_gate(a), _gate(b)]
    ip = "198.51.100.88"
    statuses = [await _request(gates[i % 2], ip, "GET", "/gyms") for i in range(150)]
    assert statuses.count(429) >= 80 and 403 not in statuses
    assert bans.ban_registry.get(ip) is None and not bans.security_events.bans
    assert await bans.ThreatTracker(store=a).score_async(ip) == 0.0
    events = [e for e in bans.security_events.events if e.ip == ip]
    assert events and all(e.category == ThreatCategory.RATE_LIMIT for e in events)  # still logged


async def test_sign_in_floods_still_escalate_to_a_ban(workers: Any) -> None:
    (a,) = await workers(1)
    app = _gate(a)
    ip = "198.51.100.89"
    statuses = [await _request(app, ip, "POST", "/auth/login") for _ in range(25)]
    assert statuses[:10] == [200] * 10 and 429 in statuses and statuses[-1] == 403
    assert bans.ban_registry.get(ip) is not None


async def test_ipv6_clients_are_limited_scored_and_banned_by_their_64(workers: Any) -> None:
    (a,) = await workers(1)
    app = _gate(a)
    for i in range(10):
        assert await _request(app, f"2001:db8:0:1::{i + 1}", "POST", "/auth/login") == 200
    assert await _request(app, "2001:db8:0:1::ffff", "POST", "/auth/login") == 429  # same /64: same allowance
    assert await _request(app, "2001:db8:0:2::1", "POST", "/auth/login") == 200  # another /64: its own
    assert await _request(app, "2001:db8:0:3::1", "GET", "/", SQLI) == 403  # 10 points on 2001:db8:0:3::/64 ...
    assert await _request(app, "2001:db8:0:3::2", "GET", "/.env") == 403  # ... + 6 from a sibling address: banned
    assert bans.ban_registry.get("2001:db8:0:3::/64") is not None
    assert await _request(app, "2001:db8:0:3::abcd", "GET", "/healthz") == 403  # every address of that /64
    assert await _request(app, "2001:db8:0:4::1", "GET", "/healthz") == 200
    assert ratelimit.address_bucket("::ffff:198.51.100.90") == "198.51.100.90"  # IPv4-mapped: the IPv4 address
    assert ratelimit.address_bucket("198.51.100.90") == "198.51.100.90"
    assert ratelimit.address_bucket("not-an-ip") == "not-an-ip"


async def test_phones_on_the_lan_keep_an_allowance_each(workers: Any) -> None:
    """LAN mode: every device of the local network sits in the LAN's one /64 (all link-local addresses in fe80::/64).
    Grouping those would make one household's phones share - and lose to a ban - one allowance."""
    (a,) = await workers(1)
    app = _gate(a)
    for lan in ("fe80::1", "fd12:3456:789a:1::1"):
        assert [await _request(app, lan, "POST", "/auth/login") for _ in range(11)][-1] == 429
    assert await _request(app, "fe80::2", "POST", "/auth/login") == 200  # the next phone: its own allowance
    assert await _request(app, "fd12:3456:789a:1::2", "POST", "/auth/login") == 200
    assert ratelimit.address_bucket("fe80::1%en0") == "fe80::1%en0" and ratelimit.address_bucket("::1") == "::1"


async def test_a_whitelisted_ipv6_address_is_judged_by_itself(workers: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    (a,) = await workers(1)
    app = _gate(a)
    monkeypatch.setattr(get_settings(), "rate_limit_whitelist", ["2001:db8:0:5::1"])
    assert all([await _request(app, "2001:db8:0:5::1", "POST", "/auth/login") == 200 for _ in range(15)])
    assert await _request(app, "2001:db8:0:5::2", "POST", "/auth/login") == 200  # its /64 neighbours: own bucket


# ============================================================================ test hygiene
def test_tests_never_use_the_dev_or_load_test_redis_database() -> None:
    import tests.conftest as conftest

    url = get_settings().redis_url
    assert not url or conftest._test_redis_url(url, explicit=True) == url
    assert not url or not url.rstrip("/").endswith(("/0", "/9"))
    wanted = os.environ.get("TEST_REDIS_DB", "15")  # the conftest's choice (default 15) - a run may pick another one
    assert conftest._test_redis_url("redis://h:6379/0", explicit=True).endswith(f"/{wanted}")
    assert conftest._test_redis_url("redis://:pw@h:6379/9", explicit=False) == f"redis://:pw@h:6379/{wanted}"
    assert conftest._test_redis_url("redis://h:6379", explicit=True).endswith(f"/{wanted}")  # no db = db 0
    assert conftest._test_redis_url("redis://h:6379/6", explicit=True) == "redis://h:6379/6"  # an explicit test db
    assert conftest._test_redis_url("redis://h:6379/6", explicit=False).endswith(f"/{wanted}")  # from .env: test db
    assert f"db={wanted}" in conftest._test_redis_url("unix:///tmp/redis.sock?db=0", explicit=True)


def _no_dot_env(**values: Any) -> Settings:
    """Settings from these values (+ the process environment) only - never the developer's .env."""
    s = get_settings()
    secrets = {"secret_key": s.secret_key, "data_encryption_key": s.data_encryption_key, "id_hash_pepper": s.id_hash_pepper}
    return Settings(_env_file=None, **{**secrets, **values})  # type: ignore[call-arg]


def test_the_idle_transaction_limit_outside_tests_does_not_depend_on_dot_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DB_IDLE_TX_TIMEOUT_MS", raising=False)
    assert _no_dot_env(environment="development").db_idle_tx_timeout_ms == 60_000
    assert Settings.model_fields["db_idle_tx_timeout_ms"].default == 60_000


# ============================================================================ scripts (manage.py check / run)
def _script(name: str) -> Any:
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"platform_fixes_{name}", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_manage_check_flags_connection_pools_that_do_not_fit_the_server() -> None:
    """A stale .env pinning DB_MAX_OVERFLOW=20 (the old .env.example) made `manage.py run --workers 4` claim 124
    connections of the 97 PostgreSQL allows - only the workers' start-up logs said so."""
    from types import SimpleNamespace

    check_env = _script("check_env")
    row = {"max_connections": 100, "superuser_reserved": 3, "reserved": 0, "database_limit": -1, "role_limit": -1,
           "superuser": False, "database": "ironpulse"}
    stale = check_env.connection_budget_result(row, SimpleNamespace(db_pool_size=10, db_max_overflow=20))
    assert stale.status == check_env.WARN and "4 workers" in stale.detail and "120 connections" in stale.detail
    assert "DB_MAX_OVERFLOW" in stale.fix
    shipped = check_env.connection_budget_result(row, SimpleNamespace(db_pool_size=10, db_max_overflow=5))
    assert shipped.status == check_env.OK and "60 connections" in shipped.detail


def test_without_redis_the_scripts_say_the_security_limits_are_per_worker(tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
                                                                         capsys: pytest.CaptureFixture) -> None:
    from types import SimpleNamespace

    check_env = _script("check_env")
    monkeypatch.delenv("REDIS_URL", raising=False)
    env = tmp_path / "check.env"
    env.write_text("PORT=8000\n", encoding="utf-8")
    result = check_env.check_redis(check_env.Ctx(env_path=env, load_app_settings=False))
    assert "rate limits" in result.detail and "N x the allowance" in result.detail
    manage = _script("manage")
    monkeypatch.setattr(manage, "run_python", lambda args, env=None: 0)
    monkeypatch.setattr(manage, "load_settings", lambda: SimpleNamespace(
        host="127.0.0.1", port=8000, trust_proxy_headers=False, redis_url=None, trusted_proxies=["127.0.0.1"]))
    assert manage.main(["run", "--workers", "3"]) == 0
    out = capsys.readouterr().out
    assert "sign-in lockout are kept per worker (3 x the allowance)" in out
