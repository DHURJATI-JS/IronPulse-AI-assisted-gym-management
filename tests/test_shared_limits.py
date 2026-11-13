"""Shared security limits (app/security/ratelimit.py, app/security/bans.py, app/services/auth_flows.py, app/security/gate.py)
and the sign-in's database use (app/routers/auth.py).

With REDIS_URL set, per-address rate limits, threat scores, the gate's rejection / 404 windows and the unknown-account
sign-in lockout live in Redis, so N uvicorn workers enforce ONE allowance; without Redis - or while it fails or is slow -
each worker falls back to its own memory. Two ``SharedStore`` objects with their own Redis clients play two workers in
one process; the ``*_worker_processes_*`` tests start real separate Python processes against the same Redis.

Redis: a database of its own (REDIS_TEST_DB = 13, never 0 - the dev servers - or 9 - the load test), only this test
database's namespace ``ip:test:<db>:sec:`` is ever deleted (SCAN + UNLINK, never FLUSHALL). Tests that need Redis are
skipped when it is not reachable; the rest switch the shared store off (memory).
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.core.utils import utcnow
from app.models import User
from app.models.enums import ThreatCategory
from app.security import bans, gate, ratelimit
from app.services import auth_flows as af
from tests.conftest import login_as, make_client, post_form
from tests.factories import DEFAULT_PASSWORD, make_customer

ROOT = Path(__file__).resolve().parent.parent
REAL_REDIS_CLIENT = ratelimit._redis_client  # the app's own client factory (the autouse fixture swaps it per test)
REDIS_TEST_DB = 13
log_name = "ironpulse.security"
# the keys of this test database: ip:<env>:<db-name>:sec: (computed here, not taken from the code under test)
NAMESPACE = "ip:test:" + get_settings().database_url.rsplit("/", 1)[-1].split("?", 1)[0] + ":sec:"


# ============================================================================ Redis plumbing for the tests
def _db13_url() -> str | None:
    url = get_settings().redis_url
    if not url:
        return None
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, f"/{REDIS_TEST_DB}", p.query, p.fragment))


def _new_client(url: str) -> Any:
    import redis.asyncio as aioredis

    return aioredis.from_url(url, decode_responses=True, socket_timeout=2, socket_connect_timeout=2)


async def _keys(client: Any) -> list[str]:
    return sorted([k async for k in client.scan_iter(match=NAMESPACE + "*", count=1000)])


async def _wipe(client: Any) -> None:
    assert NAMESPACE.startswith("ip:test:") and "test" in NAMESPACE.split(":")[2]  # this test database's keys only
    keys = await _keys(client)
    if keys:
        await client.unlink(*keys)


_reachable: bool | None = None


async def _redis_up(url: str | None) -> bool:
    global _reachable
    if _reachable is None:
        _reachable = False
        if url:
            client = _new_client(url)
            try:
                _reachable = bool(await client.ping())
            except Exception:  # noqa: BLE001
                _reachable = False
            finally:
                await client.aclose()
    return _reachable


def _returning(client: Any):
    async def get() -> Any:
        return client

    return get


@pytest.fixture(autouse=True)
def _pinned_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shipped limits whatever .env says, and a Redis timeout generous enough for a busy test machine (the 100 ms
    production default is exercised by the slow-Redis test with its own value)."""
    s = get_settings()
    for name, value in (("rate_limit_per_minute", 240), ("rate_limit_auth_per_minute", 10), ("threat_ban_score", 12.0),
                        ("login_max_failures", 5), ("login_lockout_minutes", 15), ("rate_limit_redis_timeout_ms", 2000)):
        monkeypatch.setattr(s, name, value)


@pytest.fixture(autouse=True)
async def _shared_store_on_db13(monkeypatch: pytest.MonkeyPatch, _pinned_settings: None):
    """The app's own store (``ratelimit.shared_store``) uses Redis database 13 in this module, or memory without Redis."""
    url = _db13_url()
    client = _new_client(url) if await _redis_up(url) else None
    monkeypatch.setattr(ratelimit, "_redis_client", _returning(client))
    await ratelimit.shared_store.reset()
    yield
    await ratelimit.shared_store.reset()  # waits for background updates, then deletes this namespace on db 13
    if client is not None:
        await client.aclose()


@pytest.fixture
async def redis13() -> Any:
    """A client of Redis database 13; this test database's namespace is empty before and after the test."""
    url = _db13_url()
    if not await _redis_up(url):
        pytest.skip("Redis (REDIS_URL) is not reachable")
    client = _new_client(url)
    await _wipe(client)
    yield client
    await _wipe(client)
    await client.aclose()


@pytest.fixture
async def workers(redis13: Any):
    """``await workers(n)`` -> n SharedStores, each with its own Redis connection pool: n app workers."""
    clients: list[Any] = []
    url = _db13_url()

    async def make(n: int) -> list[Any]:
        stores = []
        for _ in range(n):
            client = _new_client(url)
            clients.append(client)
            store = ratelimit.SharedStore(client=_returning(client))
            stores.append(store)
        return stores

    yield make
    for client in clients:
        await client.aclose()


def memory_store() -> Any:
    return ratelimit.SharedStore(client=_returning(None))


class BrokenRedis:
    """Redis gone mid-flight: every command fails."""

    def __init__(self) -> None:
        self.calls = 0

    async def evalsha(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        raise ConnectionError("redis went away")

    def __getattr__(self, name: str) -> Any:
        raise ConnectionError("redis went away")


class SlowRedis:
    """A Redis that hangs (a blackholed network)."""

    def __init__(self) -> None:
        self.calls = 0

    async def evalsha(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        await asyncio.sleep(5)
        return [0, "0", 0]


class CountingRedis:
    def __init__(self, real: Any) -> None:
        self.real = real
        self.evals = 0

    async def evalsha(self, *args: Any, **kwargs: Any) -> Any:
        self.evals += 1
        return await self.real.evalsha(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.real, name)


ALLOWED_REPLY = [0, "0", 0]  # the TAKE script's "allowed"


class StallDuringReply:
    """A fast Redis (1 ms) on a worker whose event loop then stalls for 300 ms (a big template, a GC pause): the reply
    arrives in time but sits unread while the timeout's timer is overdue."""

    def __init__(self) -> None:
        self.calls = 0

    async def evalsha(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        asyncio.get_running_loop().call_soon(time.sleep, 0.3)
        await asyncio.sleep(0.001)
        return ALLOWED_REPLY


class StallBeforeSend:
    """The loop stalls before the command goes out (the call's first step runs 300 ms late); Redis answers in 5 ms."""

    def __init__(self) -> None:
        self.calls = 0

    async def evalsha(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        time.sleep(0.3)
        await asyncio.sleep(0.005)
        return ALLOWED_REPLY


class AlwaysBusyLoop:
    """The worker never gets its loop back in time: 80 ms chunks of work against a 50 ms timeout, no reply ever."""

    def __init__(self) -> None:
        self.calls = 0

    async def evalsha(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        while True:
            time.sleep(0.08)
            await asyncio.sleep(0)


class FastRedis:
    def __init__(self) -> None:
        self.calls = 0

    async def evalsha(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        return ALLOWED_REPLY


async def _drain(*stores: Any) -> None:
    """Wait for the background shared updates of the given stores (threat points of synchronous callers)."""
    for store in stores:
        pending = list(store._pending)
        if pending:
            await asyncio.wait(pending, timeout=5)


# ============================================================================ ASGI helpers (the gate alone)
class _Downstream:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.calls = 0

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        self.calls += 1
        await send({"type": "http.response.start", "status": self.status, "headers": []})
        await send({"type": "http.response.body", "body": b""})


SQLI_QUERY = b"q=1%27%20UNION%20SELECT%20password%20FROM%20users--"  # a severity-4 finding: 10 threat points, blocked


async def _request(app: Any, ip: str, method: str, path: str, query: bytes = b"") -> tuple[int, dict[str, str]]:
    scope = {"type": "http", "method": method, "path": path, "raw_path": path.encode(), "query_string": query,
             "headers": [(b"content-type", b"application/x-www-form-urlencoded"), (b"content-length", b"0"),
                         (b"user-agent", b"Mozilla/5.0 (shared-limits test)")],
             "client": (ip, 51000), "server": ("testserver", 80), "scheme": "http", "state": {}}
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    return start["status"], {k.decode(): v.decode() for k, v in start.get("headers", [])}


def _gate_for(store: Any, downstream: Any) -> Any:
    """One worker's security gate: its own limiter and threat tracker on its own store."""
    return gate.SecurityGateMiddleware(downstream, rate_limiter=ratelimit.RateLimiter(store=store),
                                       threats=bans.ThreatTracker(store=store))


@pytest.fixture
def no_whitelist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "rate_limit_whitelist", [])


AUTH = ["global", "auth"]


# ============================================================================ rate limits
async def test_two_workers_share_one_allowance(workers: Any, no_whitelist: None) -> None:
    a, b = await workers(2)
    first, second = ratelimit.RateLimiter(store=a), ratelimit.RateLimiter(store=b)
    capacity = int(first.limits()["auth"].capacity)
    ip = "198.51.100.21"
    verdicts = [await (first if i % 2 else second).check(ip, AUTH) for i in range(capacity + 6)]
    assert [v.allowed for v in verdicts] == [True] * capacity + [False] * 6  # ONE burst between both workers
    assert all(v.shared for v in verdicts) and {v.bucket for v in verdicts[capacity:]} == {"auth"}
    assert [v.recent for v in verdicts[capacity:]] == [1, 2, 3, 4, 5, 6]  # rejections counted across workers
    # what each worker had before (and still has without Redis): an allowance of its own
    alone = [ratelimit.RateLimiter(store=memory_store()) for _ in range(2)]
    allowed = sum([(await alone[i % 2].check(ip, AUTH)).allowed for i in range(capacity * 2 + 2)])
    assert allowed == capacity * 2


async def test_concurrent_hits_never_exceed_the_limit(workers: Any, no_whitelist: None) -> None:
    """Tolerance: none beyond the refill that legitimately happens while the burst runs (per_second x elapsed)."""
    stores = await workers(4)
    limiters = [ratelimit.RateLimiter(store=s) for s in stores]
    limit = limiters[0].limits()["auth"]
    ip = "198.51.100.22"
    started = time.monotonic()
    verdicts = await asyncio.gather(*(limiters[i % 4].check(ip, AUTH) for i in range(400)))
    elapsed = time.monotonic() - started
    allowed = sum(v.allowed for v in verdicts)
    assert all(v.shared for v in verdicts)
    assert int(limit.capacity) <= allowed <= int(limit.capacity + limit.per_second * elapsed)
    assert allowed == int(limit.capacity) or elapsed > 1 / limit.per_second  # 10/min: refill only after 6 s


async def test_retry_after_is_whole_seconds_rounded_up_from_the_shared_bucket(workers: Any, no_whitelist: None) -> None:
    a, b = await workers(2)
    gate_a, gate_b = _gate_for(a, _Downstream()), _gate_for(b, _Downstream())
    ip = "198.51.100.23"
    statuses = [(await _request(gate_a, ip, "POST", "/auth/login"))[0] for _ in range(10)]
    assert statuses == [200] * 10  # the sign-in burst (RATE_LIMIT_AUTH_PER_MINUTE=10) ...
    status, headers = await _request(gate_b, ip, "POST", "/auth/login")  # ... is gone on the other worker too
    assert status == 429 and headers["retry-after"] == "6"  # one token every 6 s: 5.99 s is "6", never "5"


async def test_retry_after_from_the_gate_is_rounded_up(no_whitelist: None) -> None:
    """The app's own gate (any store): waiting Retry-After seconds always finds a token again."""
    app = gate.SecurityGateMiddleware(_Downstream())
    ip = "198.51.100.24"
    for _ in range(10):
        assert (await _request(app, ip, "POST", "/auth/login"))[0] == 200
    status, headers = await _request(app, ip, "POST", "/auth/login")
    assert status == 429 and headers["retry-after"] == "6"


async def test_keys_are_namespaced_and_expire_when_the_bucket_is_full_again(redis13: Any, no_whitelist: None) -> None:
    limiter = ratelimit.RateLimiter()
    limits = limiter.limits()
    ip = "198.51.100.25"
    for _ in range(int(limits["auth"].capacity) + 1):
        await limiter.check(ip, AUTH)
    keys = await _keys(redis13)
    ns = ratelimit.namespace()
    assert ns == NAMESPACE  # ip:<env>:<db>:sec: - like the AI guard's ip:<env>:<db>:ai:
    assert keys == [f"{ns}{{{ip}}}:rej", f"{ns}{{{ip}}}:rl:auth", f"{ns}{{{ip}}}:rl:global"]
    assert 0 < await redis13.pttl(keys[1]) <= limits["auth"].ttl_ms <= 61_000  # 10 tokens at 10/min: full in 60 s
    assert 0 < await redis13.pttl(keys[2]) <= limits["global"].ttl_ms <= 16_000
    assert 0 < await redis13.pttl(keys[0]) <= 60_000


async def test_an_expired_bucket_is_a_full_one(redis13: Any, monkeypatch: pytest.MonkeyPatch, no_whitelist: None) -> None:
    limiter = ratelimit.RateLimiter()
    tiny = ratelimit.Limit(capacity=3, per_second=10.0)  # full again 0.3 s after its last use
    monkeypatch.setattr(limiter, "limits", lambda: {"auth": tiny})
    ip = "198.51.100.26"
    assert [(await limiter.check(ip, ["auth"])).allowed for _ in range(4)] == [True, True, True, False]
    key = ratelimit.address_key(ip, "rl", "auth")
    assert 0 < await redis13.pttl(key) <= tiny.ttl_ms == 400
    deadline = time.monotonic() + 3
    while await redis13.exists(key) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert not await redis13.exists(key)  # gone - nothing grows without bound ...
    assert [(await limiter.check(ip, ["auth"])).allowed for _ in range(4)] == [True, True, True, False]  # ... full burst


async def test_whitelisted_addresses_are_never_limited_nor_stored(redis13: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "rate_limit_whitelist", ["198.51.100.27"])
    counting = CountingRedis(redis13)
    limiter = ratelimit.RateLimiter(store=ratelimit.SharedStore(client=_returning(counting)))
    assert all([(await limiter.check("198.51.100.27", AUTH)).allowed for _ in range(50)])
    assert counting.evals == 0 and await _keys(redis13) == []  # not even a round trip
    assert not all([(await limiter.check("198.51.100.28", AUTH)).allowed for _ in range(50)])  # everyone else is
    assert counting.evals == 50


async def test_a_redis_error_falls_back_to_this_workers_limits(monkeypatch: pytest.MonkeyPatch,
                                                                caplog: pytest.LogCaptureFixture,
                                                                no_whitelist: None) -> None:
    monkeypatch.setattr(ratelimit, "RETRY_SECONDS", 0.0)  # try Redis again on every call ...
    monkeypatch.setattr(ratelimit, "OUTAGE_SECONDS", 0.0)  # ... and every failure counts as an outage
    broken = BrokenRedis()
    store = ratelimit.SharedStore(client=_returning(broken))
    limiter = ratelimit.RateLimiter(store=store)
    capacity = int(limiter.limits()["auth"].capacity)
    with caplog.at_level(logging.WARNING, logger=log_name):
        verdicts = [await limiter.check("198.51.100.29", AUTH) for _ in range(capacity + 3)]
    assert [v.allowed for v in verdicts] == [True] * capacity + [False] * 3  # never blocks everyone, never fails open
    assert not any(v.shared for v in verdicts) and verdicts[-1].bucket == "auth" and verdicts[-1].recent == 3
    assert broken.calls == capacity + 3 and store.failures == capacity + 3
    assert caplog.text.count("Redis unavailable (ConnectionError)") == 1  # once a minute, not once per request


async def test_a_slow_redis_costs_one_timeout_then_memory_decides(monkeypatch: pytest.MonkeyPatch,
                                                                  no_whitelist: None) -> None:
    monkeypatch.setattr(get_settings(), "rate_limit_redis_timeout_ms", 50)
    monkeypatch.setattr(ratelimit, "OUTAGE_SECONDS", 0.0)  # (a lasting outage: see the tests below)
    slow = SlowRedis()
    store = ratelimit.SharedStore(client=_returning(slow))
    limiter = ratelimit.RateLimiter(store=store)
    started = time.monotonic()
    first = await limiter.check("198.51.100.30", AUTH)
    took = time.monotonic() - started
    assert first.allowed and not first.shared and took < 0.5  # 50 ms, never the 5 s Redis would take
    assert store.failures == 1 and store.late == 0  # an on-time deadline: Redis's fault, not the worker's
    started = time.monotonic()
    rest = [await limiter.check("198.51.100.30", AUTH) for _ in range(20)]
    assert time.monotonic() - started < 0.2 and slow.calls == 1  # RETRY_SECONDS without asking the slow Redis again
    assert [v.allowed for v in rest].count(True) == int(limiter.limits()["auth"].capacity) - 1  # still limited


class Hiccups:
    """A Redis that hangs on the given calls (a fork for BGSAVE, an fsync, a CPU-starved moment) and is fast otherwise."""

    def __init__(self, slow: set[int]) -> None:
        self.slow = slow
        self.calls = 0

    async def evalsha(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        if self.calls in self.slow:
            await asyncio.sleep(5)
        return ALLOWED_REPLY


async def test_one_slow_reply_costs_only_the_request_it_hit(monkeypatch: pytest.MonkeyPatch,
                                                             caplog: pytest.LogCaptureFixture,
                                                             no_whitelist: None) -> None:
    """Measured on a busy machine: Redis starved of CPU for ~100 ms timed out the calls in flight, which paused Redis
    for the whole worker (2 s of per-worker limits) and logged it as unavailable."""
    monkeypatch.setattr(get_settings(), "rate_limit_redis_timeout_ms", 50)
    redis = Hiccups(slow={1, 3})
    store = ratelimit.SharedStore(client=_returning(redis))
    limiter = ratelimit.RateLimiter(store=store)
    with caplog.at_level(logging.WARNING, logger=log_name):
        assert not (await limiter.check("198.51.100.55", AUTH)).shared  # that request: this worker's memory ...
        assert (await limiter.check("198.51.100.55", AUTH)).shared  # ... the next one: Redis again
        await asyncio.sleep(ratelimit.OUTAGE_SECONDS + 0.05)
        assert not (await limiter.check("198.51.100.55", AUTH)).shared  # another hiccup, long after the first:
        assert (await limiter.check("198.51.100.55", AUTH)).shared  # a success in between ended the first one
    assert store.failures == 2 and redis.calls == 4 and "Redis unavailable" not in caplog.text


async def test_a_lasting_outage_pauses_redis_and_is_announced(caplog: pytest.LogCaptureFixture,
                                                             no_whitelist: None) -> None:
    broken = BrokenRedis()
    limiter = ratelimit.RateLimiter(store=ratelimit.SharedStore(client=_returning(broken)))
    with caplog.at_level(logging.WARNING, logger=log_name):
        started = time.monotonic()
        while time.monotonic() - started < ratelimit.OUTAGE_SECONDS + 0.05:  # failing for longer than an outage takes
            await limiter.check("198.51.100.56", AUTH)
            await asyncio.sleep(0.02)
        calls = broken.calls
        assert calls >= 1 and caplog.text.count("Redis unavailable (ConnectionError)") == 1
        for _ in range(20):
            await limiter.check("198.51.100.56", AUTH)
        assert broken.calls == calls  # paused: RETRY_SECONDS without asking the dead Redis


@pytest.mark.parametrize("redis_cls", [StallDuringReply, StallBeforeSend])
async def test_a_busy_event_loop_is_not_a_slow_redis(redis_cls: Any, monkeypatch: pytest.MonkeyPatch,
                                                      caplog: pytest.LogCaptureFixture, no_whitelist: None) -> None:
    """Measured under load: healthy calls 'timed out' after 150-3000 ms because the worker's loop was stalled, which
    paused Redis for the whole worker and logged it as unavailable. The verdict Redis sent must be used."""
    monkeypatch.setattr(get_settings(), "rate_limit_redis_timeout_ms", 50)
    fake = redis_cls()
    store = ratelimit.SharedStore(client=_returning(fake))
    limiter = ratelimit.RateLimiter(store=store)
    with caplog.at_level(logging.WARNING, logger=log_name):
        verdict = await limiter.check("198.51.100.34", AUTH)
        assert verdict.allowed and verdict.shared  # Redis's verdict, although every timer fired 250 ms late
        assert store.failures == 0 and store.late == 0
        assert (await limiter.check("198.51.100.34", AUTH)).shared and fake.calls == 2  # Redis was not paused
    assert "Redis unavailable" not in caplog.text and "overloaded" not in caplog.text


async def test_the_apps_redis_client_survives_a_stalled_event_loop(redis13: Any, monkeypatch: pytest.MonkeyPatch,
                                                                   no_whitelist: None) -> None:
    """redis-py's own socket timeouts are asyncio timers too: set to the tight timeout they also fired late on a
    stalled loop and threw the reply away. The app's client keeps them only as a backstop."""
    from redis.asyncio.connection import AbstractConnection

    s = get_settings()
    monkeypatch.setattr(s, "redis_url", _db13_url())
    monkeypatch.setattr(s, "rate_limit_redis_timeout_ms", 50)
    monkeypatch.setattr(ratelimit, "_client", None)  # one built for this test's settings (closed below)
    store = ratelimit.SharedStore(client=REAL_REDIS_CLIENT)
    limiter = ratelimit.RateLimiter(store=store)
    ip = "198.51.100.38"
    real_send = AbstractConnection.send_packed_command

    async def send_then_stall(self: Any, *args: Any, **kwargs: Any) -> None:
        await real_send(self, *args, **kwargs)
        asyncio.get_running_loop().call_soon(time.sleep, 0.3)  # the loop stalls while the reply is on its way

    try:
        assert (await limiter.check(ip, AUTH)).shared  # a warm pooled connection
        monkeypatch.setattr(AbstractConnection, "send_packed_command", send_then_stall)
        verdict = await limiter.check(ip, AUTH)
        monkeypatch.setattr(AbstractConnection, "send_packed_command", real_send)
        assert verdict.shared and store.failures == 0 and store.late == 0
    finally:
        if ratelimit._client is not None:
            await ratelimit._client.connection_pool.disconnect()


async def test_an_overloaded_worker_judges_from_memory_without_pausing_redis(monkeypatch: pytest.MonkeyPatch,
                                                                             caplog: pytest.LogCaptureFixture,
                                                                             no_whitelist: None) -> None:
    monkeypatch.setattr(get_settings(), "rate_limit_redis_timeout_ms", 50)
    current: dict[str, Any] = {"client": AlwaysBusyLoop()}

    async def client() -> Any:
        return current["client"]

    store = ratelimit.SharedStore(client=client)
    limiter = ratelimit.RateLimiter(store=store)
    with caplog.at_level(logging.WARNING, logger=log_name):
        started = time.monotonic()
        verdict = await limiter.check("198.51.100.35", AUTH)
        took = time.monotonic() - started
        assert verdict.allowed and not verdict.shared and took < 2.0  # memory decided (MAX_WAITS late deadlines)
        assert store.late == 1 and store.failures == 0
        current["client"] = FastRedis()  # the very next request is checked with Redis again: no RETRY_SECONDS pause
        assert (await limiter.check("198.51.100.35", AUTH)).shared
    assert caplog.text.count("this worker is overloaded") == 1 and "Redis unavailable" not in caplog.text


async def test_a_long_run_of_late_deadlines_counts_as_redis_unavailable(monkeypatch: pytest.MonkeyPatch,
                                                                        caplog: pytest.LogCaptureFixture,
                                                                        no_whitelist: None) -> None:
    monkeypatch.setattr(get_settings(), "rate_limit_redis_timeout_ms", 50)
    monkeypatch.setattr(ratelimit, "LATE_STREAK", 2)
    busy = AlwaysBusyLoop()
    store = ratelimit.SharedStore(client=_returning(busy))
    limiter = ratelimit.RateLimiter(store=store)
    with caplog.at_level(logging.WARNING, logger=log_name):
        assert not (await limiter.check("198.51.100.36", AUTH)).shared
        assert store.failures == 0
        assert not (await limiter.check("198.51.100.36", AUTH)).shared  # the 2nd in a row: Redis may be away too
        assert store.failures == 1 and store.late == 2
        assert not (await limiter.check("198.51.100.36", AUTH)).shared and busy.calls == 2  # paused: RETRY_SECONDS
    assert "Redis unavailable (TimeoutError)" in caplog.text


async def test_several_workers_without_redis_are_announced_once(monkeypatch: pytest.MonkeyPatch,
                                                               caplog: pytest.LogCaptureFixture,
                                                               no_whitelist: None) -> None:
    from app import db as dbmod

    monkeypatch.setattr(dbmod, "configured_workers", lambda *args, **kwargs: 4)
    limiter = ratelimit.RateLimiter(store=memory_store())
    with caplog.at_level(logging.WARNING, logger=log_name):
        for _ in range(3):
            assert (await limiter.check("198.51.100.39", AUTH)).allowed
    assert caplog.text.count("REDIS_URL is not set and 4 workers serve the app") == 1  # once per worker
    caplog.clear()
    monkeypatch.setattr(dbmod, "configured_workers", lambda *args, **kwargs: 1)
    with caplog.at_level(logging.WARNING, logger=log_name):
        await ratelimit.RateLimiter(store=memory_store()).check("198.51.100.39", AUTH)
    assert "REDIS_URL is not set" not in caplog.text  # one process: nothing is lost


async def test_the_shared_limits_come_back_after_an_outage(redis13: Any, monkeypatch: pytest.MonkeyPatch,
                                                           caplog: pytest.LogCaptureFixture, no_whitelist: None) -> None:
    monkeypatch.setattr(ratelimit, "RETRY_SECONDS", 0.0)
    monkeypatch.setattr(ratelimit, "OUTAGE_SECONDS", 0.0)
    current: dict[str, Any] = {"client": BrokenRedis()}

    async def client() -> Any:
        return current["client"]

    limiter = ratelimit.RateLimiter(store=ratelimit.SharedStore(client=client))
    with caplog.at_level(logging.INFO, logger=log_name):
        assert not (await limiter.check("198.51.100.31", AUTH)).shared
        current["client"] = redis13
        assert (await limiter.check("198.51.100.31", AUTH)).shared
    assert "Redis answers again" in caplog.text


async def test_a_flapping_redis_is_announced_once_a_minute(redis13: Any, monkeypatch: pytest.MonkeyPatch,
                                                          caplog: pytest.LogCaptureFixture, no_whitelist: None) -> None:
    monkeypatch.setattr(ratelimit, "RETRY_SECONDS", 0.0)
    monkeypatch.setattr(ratelimit, "OUTAGE_SECONDS", 0.0)
    current: dict[str, Any] = {"client": None}

    async def client() -> Any:
        return current["client"]

    limiter = ratelimit.RateLimiter(store=ratelimit.SharedStore(client=client))
    with caplog.at_level(logging.INFO, logger=log_name):
        for _ in range(5):  # down, up, down, up ... within one minute
            current["client"] = BrokenRedis()
            assert not (await limiter.check("198.51.100.33", AUTH)).shared
            current["client"] = redis13
            assert (await limiter.check("198.51.100.33", AUTH)).shared
    assert caplog.text.count("Redis unavailable") == 1 and caplog.text.count("Redis answers again") == 1


async def test_the_404_window_is_shared_and_reports_a_flood_once(workers: Any) -> None:
    a, b = await workers(2)
    first, second = ratelimit.RateLimiter(store=a), ratelimit.RateLimiter(store=b)
    counts = [await (first if i % 2 else second).note("198.51.100.32", "404", window=300, clear_at=5) for i in range(7)]
    assert counts == [1, 2, 3, 4, 5, 1, 2]  # one window for both workers; at 5 it is reported and starts over
    here = ratelimit.RateLimiter(store=memory_store())  # the memory twin behaves the same
    assert [await here.note("198.51.100.32", "404", window=300, clear_at=5) for _ in range(7)] == [1, 2, 3, 4, 5, 1, 2]


# ============================================================================ threat scores
async def test_threat_scores_add_up_across_workers(workers: Any) -> None:
    a, b = await workers(2)
    first, second = bans.ThreatTracker(store=a), bans.ThreatTracker(store=b)
    ip = "198.51.100.40"
    assert await first.add_async(ip, 4) == pytest.approx(10.0, rel=1e-3)
    total = await second.add_async(ip, 2)
    assert total == pytest.approx(13.0, rel=1e-3) and total >= get_settings().threat_ban_score  # one score, two workers
    assert second.score(ip) == pytest.approx(13.0, rel=1e-3)
    assert await first.score_async(ip) == pytest.approx(13.0, rel=1e-3)
    alone = bans.ThreatTracker(store=memory_store())
    assert await alone.add_async(ip, 2) == pytest.approx(3.0)  # without Redis: this worker's points only


async def test_a_synchronous_report_reaches_the_shared_score_and_can_ban(workers: Any, no_whitelist: None) -> None:
    a, b = await workers(2)
    first, second = bans.ThreatTracker(store=a), bans.ThreatTracker(store=b)
    ip = "198.51.100.41"
    assert first.add(ip, 4, reason="csrf: test") == pytest.approx(10.0)  # e.g. a CSRF report on worker A
    await _drain(a)
    assert bans.ban_registry.get(ip) is None
    assert second.add(ip, 2, reason="bruteforce: test") == pytest.approx(3.0)  # worker B's own view ...
    await _drain(b)
    ban = bans.ban_registry.get(ip)  # ... but the shared total reached 13: B's background update asked for the ban
    assert ban is not None and ban.provisional and "bruteforce: test" in ban.reason
    assert second.score(ip) == pytest.approx(13.0, rel=1e-3)
    assert any(p.ip == ip for p in bans.security_events.bans)


async def test_threat_scores_expire_and_a_lift_forgets_them(redis13: Any) -> None:
    tracker = bans.threat_tracker
    ip = "198.51.100.42"
    await tracker.add_async(ip, 4)
    key = ratelimit.address_key(ip, "threat")
    ttl = await redis13.pttl(key)
    assert 0 < ttl <= 86_400_000
    # 10 points halve every 10 min: below 0.1 after log2(100) = 6.6 half-lives (~66 min), then the key goes
    assert 3_900_000 < ttl < 4_100_000
    bans.lift_ban_in_memory(ip)  # the admin's Lift (app/services/security_triage.py)
    await _drain(ratelimit.shared_store)
    assert tracker.score(ip) == 0.0 and not await redis13.exists(key)


async def test_threat_scores_fall_back_to_this_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    tracker = bans.ThreatTracker(store=ratelimit.SharedStore(client=_returning(BrokenRedis())))
    assert await tracker.add_async("198.51.100.43", 4, 2) == pytest.approx(13.0)
    assert tracker.add("198.51.100.43", 1) == pytest.approx(14.0, rel=1e-3)


# ============================================================================ the gate, two workers
async def test_two_gates_share_limits_threat_scores_and_bans(workers: Any, no_whitelist: None) -> None:
    a, b = await workers(2)
    down_a, down_b = _Downstream(), _Downstream()
    gate_a, gate_b = _gate_for(a, down_a), _gate_for(b, down_b)
    ip = "198.51.100.44"
    assert (await _request(gate_a, ip, "GET", "/", SQLI_QUERY))[0] == 403  # 10 points: blocked, not banned yet
    assert bans.ban_registry.get(ip) is None
    status, _headers = await _request(gate_b, ip, "GET", "/.env")  # +6 = 16 on the shared score: banned on worker B
    assert status == 403 and bans.ban_registry.get(ip) is not None  # (worker B alone has 6: it would let it through)
    assert down_a.calls == 0 and down_b.calls == 0


async def test_workers_banning_together_persist_one_ban(workers: Any, redis13: Any, no_whitelist: None) -> None:
    """Measured: workers that saw the shared score cross the ban score together each queued the ban; their flushes
    raced and wrote two ip_bans rows, and the second one - a 'repeat offence' - made a first offence PERMANENT."""
    a, b = await workers(2)
    gate_a, gate_b = _gate_for(a, _Downstream()), _gate_for(b, _Downstream())
    ip = "198.51.100.46"
    assert (await _request(gate_a, ip, "GET", "/", SQLI_QUERY))[0] == 403  # 10 points: blocked, not banned
    results = await asyncio.gather(_request(gate_a, ip, "GET", "/.env"), _request(gate_b, ip, "GET", "/.env"))
    assert [status for status, _headers in results] == [403, 403]  # both crossed the ban score (16, 22) ...
    assert [p.ip for p in bans.security_events.bans].count(ip) == 1  # ... and ONE ban goes to the database
    ban = bans.ban_registry.get(ip)
    assert ban is not None and ban.provisional  # blocked here at once either way
    claim = ratelimit.address_key(ip, "ban")
    assert 0 < await redis13.pttl(claim) <= bans.BAN_CLAIM_SECONDS * 1000
    bans.lift_ban_in_memory(ip)  # an admin's Lift: a new offence must be banned and persisted again
    await _drain(ratelimit.shared_store)
    assert not await redis13.exists(claim) and not await redis13.exists(ratelimit.address_key(ip, "threat"))


async def test_the_ban_claim_without_redis_and_for_whitelisted_addresses(monkeypatch: pytest.MonkeyPatch,
                                                                         redis13: Any) -> None:
    alone = [bans.ThreatTracker(store=memory_store()) for _ in range(2)]
    assert [await t.claim_ban("198.51.100.47") for t in alone] == [True, True]  # each worker decides alone
    monkeypatch.setattr(get_settings(), "rate_limit_whitelist", ["198.51.100.48"])
    await bans.threat_tracker.ban("198.51.100.48", 4, "whitelisted")
    assert bans.ban_registry.get("198.51.100.48") is None and not bans.security_events.bans
    assert not await redis13.exists(ratelimit.address_key("198.51.100.48", "ban"))  # no claim it never uses


async def test_a_gate_without_redis_still_limits_and_serves(no_whitelist: None) -> None:
    broken = ratelimit.SharedStore(client=_returning(BrokenRedis()))
    app = _gate_for(broken, _Downstream())
    statuses = [(await _request(app, "198.51.100.45", "POST", "/auth/login"))[0] for _ in range(12)]
    assert statuses == [200] * 10 + [429] * 2


# ============================================================================ the unknown-account sign-in lockout
async def test_unknown_account_lockout_is_one_count_for_every_worker(workers: Any) -> None:
    a, b = await workers(2)
    first, second = af.GhostThrottle(store=a), af.GhostThrottle(store=b)
    limit = get_settings().login_max_failures
    who = "ghost-shared@example.com"
    for i in range(limit - 1):
        assert await (first if i % 2 else second).fail(who) is None
    until = await first.fail(who.upper())  # the limit-th failure, whichever worker it lands on
    assert until is not None and until > time.time() + get_settings().login_lockout_minutes * 60 - 5
    assert await second.locked_until(who) == pytest.approx(until)
    assert await second.fail(who) == pytest.approx(until)  # no count while locked
    key = ratelimit.shared_key("login", af.GhostThrottle._key(who))
    client = await a.client()
    assert 0 < await client.pttl(key) <= get_settings().login_lockout_minutes * 60_000 + 1000  # expires with the lock
    alone = [af.GhostThrottle(store=memory_store()) for _ in range(2)]
    assert all([await alone[i % 2].fail("other@example.com") is None for i in range(limit + 1)])  # per worker: 2x


async def test_a_burst_of_failures_from_four_workers_locks_exactly_once(workers: Any) -> None:
    """Tolerance: none - the counting script is atomic, so 40 simultaneous failures count limit-1, then one lock."""
    stores = await workers(4)
    throttles = [af.GhostThrottle(store=s) for s in stores]
    limit = get_settings().login_max_failures
    results = await asyncio.gather(*(throttles[i % 4].fail("ghost-burst@example.com") for i in range(40)))
    assert sum(r is None for r in results) == limit - 1  # per worker it would be 4 x (limit - 1)
    assert len({round(r, 3) for r in results if r is not None}) == 1  # one lock: the same expiry for everybody


async def test_a_burst_of_threat_points_from_four_workers_all_count(workers: Any) -> None:
    stores = await workers(4)
    trackers = [bans.ThreatTracker(store=s) for s in stores]
    ip = "198.51.100.53"
    totals = await asyncio.gather(*(trackers[i % 4].add_async(ip, 1) for i in range(40)))
    assert await trackers[0].score_async(ip) == pytest.approx(40.0, rel=1e-3)  # every point once (decay: ~0)
    assert max(totals) == pytest.approx(40.0, rel=1e-3)  # measured before the fix: 44 (points counted twice)


class ReversedReplies:
    """One worker's two simultaneous calls: Redis runs them in order, their replies are read in reverse."""

    def __init__(self, real: Any) -> None:
        self.real = real
        self.calls = 0
        self.second_read = asyncio.Event()

    async def evalsha(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        first = self.calls == 1
        reply = await self.real.evalsha(*args, **kwargs)
        if first:
            await self.second_read.wait()
        else:
            self.second_read.set()
        return reply


async def test_replies_read_out_of_order_never_count_points_twice(redis13: Any) -> None:
    tracker = bans.ThreatTracker(store=ratelimit.SharedStore(client=_returning(ReversedReplies(redis13))))
    ip = "198.51.100.54"
    totals = await asyncio.gather(tracker.add_async(ip, 1), tracker.add_async(ip, 1))
    # each call answers with the shared total its own round trip saw (the verdict is Redis's, never this worker's
    # copy - which another worker's Lift may have left stale): 1 then 2, never 3
    assert sorted(totals) == pytest.approx([1.0, 2.0], rel=1e-3)
    assert tracker.score(ip) == pytest.approx(2.0, rel=1e-3)  # the fallback copy keeps the highest total it saw


async def test_unknown_account_lockout_falls_back_to_memory() -> None:
    throttle = af.GhostThrottle(store=ratelimit.SharedStore(client=_returning(BrokenRedis())))
    limit = get_settings().login_max_failures
    results = [await throttle.fail("nobody@example.com") for _ in range(limit)]
    assert results[:-1] == [None] * (limit - 1) and results[-1] is not None
    assert await throttle.locked_until("NOBODY@example.com") == results[-1]


async def test_the_sign_in_page_uses_the_shared_unknown_account_lockout(client: Any, workers: Any) -> None:
    (other_worker,) = await workers(1)
    limit = get_settings().login_max_failures
    who = "nobody-at-all@example.com"
    for i in range(limit - 1):
        r = await post_form(client, "/auth/login", {"identifier": who, "password": f"Wrong-{i}-Passw0rd!"})
        assert r.status_code == 400 and "Those sign-in details didn't work" in html.unescape(r.text)
    assert await af.GhostThrottle(store=other_worker).fail(who) is not None  # the last failure, on another worker
    r = await post_form(client, "/auth/login", {"identifier": who, "password": "Wrong-x-Passw0rd!"})
    assert r.status_code == 400 and "Too many failed sign-in attempts" in html.unescape(r.text)


# ============================================================================ separate worker processes
WORKER_SCRIPT = r'''
import asyncio, json, os, sys
sys.path.insert(0, os.environ["IP_ROOT"])
mode, ip, n, ident = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]


async def through_gate():
    from app.security.bans import ban_registry
    from app.security.gate import SecurityGateMiddleware

    async def downstream(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    gate = SecurityGateMiddleware(downstream)
    method, path, query = ("POST", "/auth/login", "") if mode == "rate" else ("GET", *ident.partition("?")[::2])
    out = []
    for _ in range(n):
        sent = []
        scope = {"type": "http", "method": method, "path": path, "raw_path": path.encode(),
                 "query_string": query.encode(),
                 "headers": [(b"content-type", b"application/x-www-form-urlencoded"), (b"content-length", b"0"),
                             (b"user-agent", b"Mozilla/5.0 (worker process)")],
                 "client": (ip, 50000), "server": ("testserver", 80), "scheme": "http", "state": {}}

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        await gate(scope, receive, send)
        start = next(m for m in sent if m["type"] == "http.response.start")
        headers = {k.decode(): v.decode() for k, v in start.get("headers", [])}
        out.append([start["status"], headers.get("retry-after")])
    return {"responses": out, "banned": ban_registry.get(ip) is not None}


async def sign_ins():
    import httpx
    from app.db import dispose_engine
    from app.main import app
    from app.security.csrf import compute_csrf_token, csrf_cookie_name

    out = []
    transport = httpx.ASGITransport(app=app, client=(ip, 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver",
                                 headers={"User-Agent": "Mozilla/5.0 (worker process)"}) as c:
        await c.get("/auth/login")
        token = compute_csrf_token(c.cookies.get(csrf_cookie_name()), None)
        for i in range(n):
            r = await c.post("/auth/login", data={"identifier": ident, "password": f"Wrong-{i}-Passw0rd!",
                                                  "csrf_token": token})
            out.append("locked" if "Too many failed sign-in attempts" in r.text else r.status_code)
    await dispose_engine()
    return {"responses": out}


result = asyncio.run(through_gate() if mode in ("rate", "probe") else sign_ins())
print("RESULT " + json.dumps(result))
'''


@pytest.fixture
def worker_process(redis13: Any, tmp_path: Path):
    """``await worker_process(mode, ip, n, ident)``: a separate Python process (= an app worker) on Redis db 13."""
    script = tmp_path / "worker.py"
    script.write_text(WORKER_SCRIPT, encoding="utf-8")
    env = {**os.environ, "IP_ROOT": str(ROOT), "ENVIRONMENT": "test", "REDIS_URL": _db13_url() or "",
           "DATABASE_URL": get_settings().database_url, "RATE_LIMIT_WHITELIST": "", "RUN_BACKGROUND_JOBS": "false",
           "RATE_LIMIT_PER_MINUTE": "240", "RATE_LIMIT_AUTH_PER_MINUTE": "10", "THREAT_BAN_SCORE": "12",
           "LOGIN_MAX_FAILURES": "5", "LOGIN_LOCKOUT_MINUTES": "15", "PYTHONDONTWRITEBYTECODE": "1"}

    async def run(mode: str, ip: str, n: int, ident: str = "-") -> dict:
        proc = await asyncio.create_subprocess_exec(sys.executable, str(script), mode, ip, str(n), ident, cwd=str(ROOT),
                                                    env=env, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), timeout=120)
        line = next((ln for ln in out.decode().splitlines() if ln.startswith("RESULT ")), None)
        assert proc.returncode == 0 and line, err.decode()[-3000:]
        return json.loads(line[len("RESULT "):])

    return run


async def test_separate_worker_processes_share_one_rate_limit(worker_process: Any) -> None:
    ip = "198.51.100.50"
    first = await worker_process("rate", ip, 6)
    second = await worker_process("rate", ip, 6)
    assert [s for s, _ in first["responses"]] == [200] * 6
    assert [s for s, _ in second["responses"]] == [200] * 4 + [429] * 2  # 10 a minute for BOTH processes, not 20
    assert 1 <= int(second["responses"][-1][1]) <= 6  # one token per 6 s, part of it refilled while B started


async def test_separate_worker_processes_share_threat_scores(worker_process: Any) -> None:
    ip = "198.51.100.51"
    first = await worker_process("probe", ip, 1, "/?" + SQLI_QUERY.decode())  # 10 points: blocked, no ban yet
    second = await worker_process("probe", ip, 1, "/.env")  # 6 more on the shared score: 16 >= the ban score of 12
    assert first == {"responses": [[403, None]], "banned": False}
    assert second == {"responses": [[403, None]], "banned": True}  # alone, this process would have let it through


async def test_separate_worker_processes_share_the_unknown_account_lockout(worker_process: Any) -> None:
    ip, who = "198.51.100.52", "ghost-process@example.com"
    first = await worker_process("login", ip, 3, who)
    second = await worker_process("login", ip, 2, who)
    assert first["responses"] == [400, 400, 400]
    assert second["responses"] == [400, "locked"]  # the 5th failure overall locks, on whichever worker


# ============================================================================ sign-in and the database pool
async def test_sign_in_holds_no_database_connection_while_argon2_runs(client: Any, db: Any,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand-in verifier / hasher: at the moment Argon2 would run, the request's database session must not be in a
    transaction (= no pooled connection checked out and idle in it)."""
    from argon2 import PasswordHasher

    from app.routers import auth as auth_router

    user = await make_customer(db, email="argon@example.com")
    outdated = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1).hash(DEFAULT_PASSWORD)
    user.password_hash = outdated
    await db.commit()
    await login_as(client, user)  # a signed-in browser: load_user reads its session from the database first
    request_sessions: list[Any] = []  # the db session of each sign-in request
    held: list[tuple[str, bool]] = []
    real_find, real_verify, real_hash = (auth_router.find_user_by_identifier, auth_router.verify_password,
                                         auth_router.hash_password)

    async def find(session: Any, identifier: str, **kwargs: Any) -> Any:
        request_sessions.append(session)
        return await real_find(session, identifier, **kwargs)

    async def verify(password_hash: str | None, password: str) -> bool:
        held.append(("verify", request_sessions[-1].in_transaction()))
        return await real_verify(password_hash, password)

    async def rehash(password: str) -> str:
        held.append(("hash", request_sessions[-1].in_transaction()))
        return await real_hash(password)

    monkeypatch.setattr(auth_router, "find_user_by_identifier", find)
    monkeypatch.setattr(auth_router, "verify_password", verify)
    monkeypatch.setattr(auth_router, "hash_password", rehash)

    async def sign_in(identifier: str, password: str) -> Any:
        return await post_form(client, "/auth/login", {"identifier": identifier, "password": password})

    assert (await sign_in("argon@example.com", "Wrong-Passw0rd!")).status_code == 400  # wrong password: counted
    assert (await sign_in("nobody@example.com", "Wrong-Passw0rd!")).status_code == 400  # unknown account
    r = await sign_in("argon@example.com", DEFAULT_PASSWORD)  # right password + the outdated hash is re-hashed
    assert r.status_code == 303
    fresh = await db.scalar(select(User).where(User.id == user.id).execution_options(populate_existing=True))
    assert fresh.failed_login_count == 0 and fresh.password_hash != outdated
    fresh.locked_until = utcnow().replace(year=utcnow().year + 1)
    await db.commit()
    client.cookies.clear()
    r = await sign_in("argon@example.com", DEFAULT_PASSWORD)  # locked: the dummy hash is still checked
    assert r.status_code == 400 and "paused" in html.unescape(r.text)
    assert [kind for kind, _ in held] == ["verify", "verify", "verify", "hash", "verify"]
    assert not any(in_tx for _kind, in_tx in held), held  # never a pooled connection idle in a transaction meanwhile


NEW_PASSWORD = {"new_password": "Brand-New-Passw0rd!", "new_password_confirm": "Brand-New-Passw0rd!"}


async def test_reauthentication_holds_no_database_transaction_while_argon2_runs(client: Any, db: Any,
                                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """The account page's 'current password' check (app/routers/account.py) counts towards the sign-in lockout and
    runs Argon2 like the sign-in: never inside the request's open transaction."""
    from app.routers import account as account_router

    user = await make_customer(db, email="reauth@example.com")
    await login_as(client, user)  # load_user reads the session in the request's transaction first
    sessions: list[Any] = []
    held: list[bool] = []
    real_check, real_verify = account_router._check_password, account_router.verify_password

    async def check(session: Any, request: Any, who: Any, password: str) -> Any:
        sessions.append(session)
        return await real_check(session, request, who, password)

    async def verify(password_hash: str | None, password: str) -> bool:
        held.append(sessions[-1].in_transaction())
        return await real_verify(password_hash, password)

    monkeypatch.setattr(account_router, "_check_password", check)
    monkeypatch.setattr(account_router, "verify_password", verify)
    wrong = await post_form(client, "/account/password", {"current_password": "Wrong-Passw0rd!", **NEW_PASSWORD})
    assert wrong.status_code == 400
    right = await post_form(client, "/account/password", {"current_password": DEFAULT_PASSWORD, **NEW_PASSWORD})
    assert right.status_code == 303
    assert held == [False, False]  # never a pooled connection idle in a transaction meanwhile


async def test_simultaneous_wrong_current_passwords_are_all_counted(db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A stolen session guessing the current password from several workers at once: every guess is counted."""
    from app.routers import account as account_router

    user = await make_customer(db, email="reauth-race@example.com")
    guesses = get_settings().login_max_failures
    arrived = 0
    everyone_in = asyncio.Event()

    async def slow_no(password_hash: str | None, password: str) -> bool:
        nonlocal arrived
        arrived += 1
        if arrived >= guesses:
            everyone_in.set()
        await asyncio.wait_for(everyone_in.wait(), timeout=20)  # every guess is judged at the same time
        return False

    monkeypatch.setattr(account_router, "verify_password", slow_no)
    clients = [make_client() for _ in range(guesses)]
    try:
        for c in clients:
            await login_as(c, user)
        responses = await asyncio.gather(*(
            post_form(c, "/account/password", {"current_password": f"Wrong-{i}-Passw0rd!", **NEW_PASSWORD})
            for i, c in enumerate(clients)
        ))
    finally:
        for c in clients:
            await c.aclose()
    assert all(r.status_code == 400 for r in responses)
    fresh = await db.scalar(select(User).where(User.id == user.id).execution_options(populate_existing=True))
    assert fresh.locked_until is not None and fresh.locked_until > utcnow()  # the limit-th guess locked it
    assert fresh.failed_login_count == 0
    assert sum(e.category == ThreatCategory.BRUTEFORCE for e in bans.security_events.events) == 1


async def test_simultaneous_wrong_passwords_are_all_counted(db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.routers import auth as auth_router

    settings = get_settings()
    user = await make_customer(db, email="race@example.com")
    guesses = settings.login_max_failures
    arrived = 0
    everyone_in = asyncio.Event()

    async def slow_no(password_hash: str | None, password: str) -> bool:
        nonlocal arrived
        arrived += 1
        if arrived >= guesses:
            everyone_in.set()
        await asyncio.wait_for(everyone_in.wait(), timeout=20)  # every guess is judged at the same time
        return False

    monkeypatch.setattr(auth_router, "verify_password", slow_no)
    clients = [make_client() for _ in range(guesses)]  # separate browsers (127.0.0.1: whitelisted in tests)
    try:
        responses = await asyncio.gather(*(
            post_form(c, "/auth/login", {"identifier": "race@example.com", "password": f"Wrong-{i}-Passw0rd!"})
            for i, c in enumerate(clients)
        ))
    finally:
        for c in clients:
            await c.aclose()
    fresh = await db.scalar(select(User).where(User.id == user.id).execution_options(populate_existing=True))
    assert fresh.locked_until is not None and fresh.locked_until > utcnow()  # the limit-th guess locked it
    assert fresh.failed_login_count == 0
    assert sum("Too many failed sign-in attempts" in html.unescape(r.text) for r in responses) == 1
    assert sum(e.category == ThreatCategory.BRUTEFORCE for e in bans.security_events.events) == 1
