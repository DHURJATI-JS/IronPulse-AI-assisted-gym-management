"""Per-address rate limits (token buckets), shared by every worker through Redis.

Buckets: ``global`` (all dynamic requests), ``auth`` (login/signup/reset POSTs), ``otp`` (desk OTP),
``ai`` (AI endpoints), ``live`` (live workouts: progress events, commands and chat messages, coach voice),
``upload`` (multipart POSTs), ``static`` (assets, tiles, public media). A bucket holds ``capacity`` tokens (the burst)
and refills at ``per_second`` (the RATE_LIMIT_* settings, see ``limits()``). The security gate charges one token to
``global`` and one to the request's own bucket (static files only to ``static``), stops at the first empty one and
answers 429 with Retry-After = the whole seconds until that bucket holds a token again (rounded up). Addresses in
RATE_LIMIT_WHITELIST are never limited.

Shared store (``SharedStore``, one per worker: ``shared_store``). With REDIS_URL set, every worker - and every app copy
on that Redis - charges the SAME buckets, so N uvicorn workers give a client one allowance, not N. A check is one Lua
script (one round trip, atomic: refill + charge of all of the request's buckets, timed by the Redis server's clock),
keys ``ip:<env>:<db>:sec:{<ip>}:rl:<bucket>`` (the AI guard's ``ip:<env>:<db>:`` namespace) that expire as soon as
the bucket would be full again, so nothing grows without bound. The same store holds the IP threat scores
(app/security/bans.py), the gate's rejection and 404 windows (``RateLimiter.note``) and the unknown-account sign-in
lockout (app/services/auth_flows.py).

Fallback. Without REDIS_URL each worker keeps all of this in its own bounded memory - the behaviour of a single process.
If Redis errors or answers slower than RATE_LIMIT_REDIS_TIMEOUT_MS (this client never retries), that request is judged
from the worker's memory. The deadline times Redis only: it starts once one of this worker's MAX_CONNECTIONS call slots
is free (a burst of simultaneous checks queues for a slot - up to QUEUE_WAIT_SECONDS, then memory decides as
"overloaded" - instead of blaming that wait on Redis), and a deadline that fires late because this worker's event loop
was busy gives the call another timeout (``_within``). Once calls have failed for OUTAGE_SECONDS without one success -
an outage, not one slow reply - a short breaker opens: Redis is skipped for RETRY_SECONDS (+-25 % jitter, so the
workers do not retry in step), then ONE probe call goes to Redis while the other requests stay on memory; a failed
probe opens it again, a successful one closes it. "Redis unavailable" / "overloaded" are logged at most once a minute.
The fallback keeps protecting: every shared verdict also writes the buckets' remaining tokens into this worker's memory
(``RateLimiter.check``), so a fallback continues from the shared state instead of handing out a fresh burst (with N
workers at most N x what was left, never N x the allowance), and the threat scores do the same (app/security/bans.py).
Bans are persisted in PostgreSQL and shared across processes.

IPv6: a host owns at least a /64 and can send every request from a new address of it, so the security gate keys an
IPv6 client by its /64 (``address_bucket``) for limits, threat scores and bans; IPv4 (and IPv4-mapped IPv6) by the
address.

These limits are per address, and a whole gym behind one NAT shares an address: they stop floods, not members. Each
member's own AI limits (quotas, duplicates, cooldowns) are the AI guard's (app/ai/guard.py, limits in
app/ai/policy.py).
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import math
import random
import re
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

from app.config import get_settings
from app.core.state import register_reset

log = logging.getLogger("ironpulse.security")

T = TypeVar("T")

RETRY_SECONDS = 2.0  # during an outage this worker judges from its own memory for this long, then asks Redis again
RETRY_JITTER = 0.25  # ... +- this share of it, drawn per pause: the workers' probes never arrive in step
# An outage: Redis calls have failed for this long without one success. One slow reply or one reset connection (a
# Redis fork for BGSAVE, an fsync stall, a CPU-starved moment - measured here: ~100 ms) only costs the requests it hit.
OUTAGE_SECONDS = 0.25
LOG_EVERY_SECONDS = 60.0  # "Redis unavailable" / "overloaded" are logged at most this often per worker
DEFAULT_TIMEOUT_MS = 250  # RATE_LIMIT_REDIS_TIMEOUT_MS when the setting is missing
# A burst of simultaneous checks queues for one of this worker's MAX_CONNECTIONS call slots; the wait is this worker's
# own queue, not Redis being slow, so the deadline starts only once a slot is free. A request that waits longer than
# this is judged from memory as "overloaded" (no breaker, no "unavailable" warning).
QUEUE_WAIT_SECONDS = 1.0
# The timeout must judge Redis, not this worker. A busy event loop (a big template, a GC pause, CPU contention) runs
# timers late, and the reply that Redis sent in time sits unread meanwhile: measured under load, healthy calls "timed
# out" after 150-3000 ms. A deadline that fired more than LATE_SLACK (or a quarter of the timeout) late gives the call
# another timeout - MAX_WAITS in all. A request whose deadlines all fired late is judged from memory without the
# RETRY_SECONDS pause or the "Redis unavailable" warning, unless LATE_STREAK calls in a row end like that.
LATE_SLACK = 0.025
MAX_WAITS = 3
LATE_STREAK = 8
# Redis connections per worker for the shared limits. A call waits for a free one (within its timeout) rather than
# failing: redis-py's default pool raises at 100 connections in use, which a burst of requests reaches. Measured: 32
# serve a cold burst of 200 simultaneous checks in ~80 ms and a warm one in ~30 ms (one event loop is the bottleneck).
MAX_CONNECTIONS = 32
MAX_PENDING = 1000  # background shared updates in flight per worker (beyond: this worker's memory only)
REJECTION_WINDOW = 60.0  # seconds: the gate grades a 429 by the address's rejections within this window ...
WINDOW_CAP = 64  # ... remembering at most this many timestamps per window (rejections, 404s)


@dataclass(slots=True)
class Bucket:
    tokens: float
    updated: float


@dataclass(frozen=True, slots=True)
class Limit:
    capacity: float  # burst size
    per_second: float  # refill rate

    @classmethod
    def per_minute(cls, n: float, burst: float | None = None) -> "Limit":
        return cls(capacity=burst if burst is not None else n, per_second=n / 60.0)

    @property
    def ttl_ms(self) -> int:
        """How long an untouched bucket takes to fill up again. Its shared key expires then: a missing bucket is a
        full one, so expiry never changes a verdict."""
        if self.per_second <= 0:
            return 3_600_000
        return int(math.ceil(self.capacity / self.per_second * 1000)) + 100


@dataclass(frozen=True, slots=True)
class Verdict:
    """The outcome of ``RateLimiter.check``."""

    allowed: bool
    bucket: str | None = None  # the bucket that was empty
    retry_after: float = 0.0  # seconds until it holds a token again (at least 1 when refused)
    recent: int = 0  # the address's rejections within REJECTION_WINDOW, this one included
    shared: bool = False  # judged by the shared store (False: this worker's memory)

    @property
    def retry_after_header(self) -> str:
        """Whole seconds, rounded UP: a client that waits exactly this long finds a token."""
        return str(max(1, math.ceil(self.retry_after - 1e-9)))


# =========================================================================== shared store (Redis)
_ns_cache: tuple[tuple[str, str], str] | None = None


def namespace() -> str:
    """``ip:<env>:<db-name>:sec:`` - like the AI guard's ``...:ai:``: the dev servers, the load test, the app copies
    of another database and each test database never share counters."""
    global _ns_cache
    s = get_settings()
    marker = (s.environment, s.database_url)
    if _ns_cache is None or _ns_cache[0] != marker:
        db_name = s.database_url.rsplit("/", 1)[-1].split("?", 1)[0]
        env = re.sub(r"[^A-Za-z0-9_.-]", "_", s.environment) or "default"
        db = re.sub(r"[^A-Za-z0-9_.-]", "_", db_name) or "default"
        _ns_cache = (marker, f"ip:{env}:{db}:sec:")
    return _ns_cache[1]


def shared_key(*parts: str) -> str:
    """A key of the shared store: ``ip:<env>:<db>:sec:<part>:<part>...``."""
    return namespace() + ":".join(parts)


def address_key(ip: str, *parts: str) -> str:
    """A per-address key, ``ip:<env>:<db>:sec:{<ip>}:<part>...``. The braces are a Redis Cluster hash tag: every key of
    one address lives in one slot, so one script may touch them all."""
    return namespace() + "{" + ip.replace("{", "_").replace("}", "_") + "}:" + ":".join(parts)


IPV6_PREFIX = 64  # one IPv6 host owns at least a /64 (an ISP's or a mobile carrier's smallest per-customer allocation)
# ... but on the local network every device has its own address in the LAN's one /64 (and every link-local address in
# fe80::/64): link-local and unique-local (ULA) clients - the phones of the LAN mode - are keyed by their address
_LOCAL_V6 = (ipaddress.ip_network("fe80::/10"), ipaddress.ip_network("fc00::/7"))


def address_bucket(ip: str) -> str:
    """The key the security gate limits, scores and bans a client by: an IPv4 address as it is (also when written as
    IPv4-mapped IPv6, ``::ffff:a.b.c.d``), an internet IPv6 address by its /64 (``2001:db8:1:2::/64``) - otherwise one
    host gets a fresh allowance, threat score and ban key with every new address of its /64 (and every new address
    costs new Redis keys). Loopback, link-local and unique-local IPv6 addresses (devices on the same LAN) are keyed by
    themselves, like LAN IPv4 addresses. Anything that is not an address is returned unchanged."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if addr.version == 4:
        return ip
    mapped = addr.ipv4_mapped
    if mapped is not None:
        return str(mapped)
    if addr.is_loopback or addr.is_unspecified or any(addr in net for net in _LOCAL_V6):
        return ip
    return str(ipaddress.ip_network(f"{addr}/{IPV6_PREFIX}", strict=False))


def redis_timeout() -> float:
    """RATE_LIMIT_REDIS_TIMEOUT_MS in seconds: the most a shared-limit call may take before memory decides."""
    ms = getattr(get_settings(), "rate_limit_redis_timeout_ms", DEFAULT_TIMEOUT_MS)
    return max(0.001, float(ms) / 1000.0)


@dataclass(frozen=True, slots=True)
class Script:
    name: str
    source: str
    sha: str


# Every script reads the Redis server's clock (``now``, epoch seconds as a float): every worker and every app host
# judge by the same clock. replicate_commands() lets Redis < 5 write after TIME (a no-op from Redis 5 on).
_LUA_PRELUDE = """
if redis.replicate_commands then pcall(redis.replicate_commands) end
local clock = redis.call('TIME')
local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000
local function fmt(x) return string.format('%.6f', x) end
local function window_hit(key, window, cap, clear_at)
  redis.call('LPUSH', key, fmt(now))
  redis.call('LTRIM', key, 0, cap - 1)
  local recent = 0
  for _, ts in ipairs(redis.call('LRANGE', key, 0, -1)) do
    if now - tonumber(ts) < window then recent = recent + 1 end
  end
  if clear_at > 0 and recent >= clear_at then
    redis.call('DEL', key)
  else
    redis.call('PEXPIRE', key, math.ceil(window * 1000))
  end
  return recent
end
"""


def lua_script(name: str, body: str) -> Script:
    """A script for ``SharedStore.run``: ``body`` sees ``now``, ``fmt(x)`` and ``window_hit(key, window, cap,
    clear_at)`` (a sliding window of event times: at most ``cap`` remembered, the count within ``window`` seconds is
    returned, at ``clear_at`` > 0 the window starts over)."""
    source = _LUA_PRELUDE + body
    return Script(name, source, hashlib.sha1(source.encode()).hexdigest())  # noqa: S324 - Redis names scripts by SHA1


# KEYS: the request's buckets in order, then the address's rejection window.
# ARGV: capacity, per_second, cost, ttl_ms for every bucket, then the rejection window (s) and its cap.
# -> {0, '0', 0, left...} allowed | {i, retry_after, rejections, left...} bucket i was empty (the buckets before it stay
# charged). ``left``: the tokens each bucket it touched (1..i) holds now - the worker mirrors them into its memory.
TAKE = lua_script("take", """
local n = #KEYS - 1
local left = {}
for i = 1, n do
  local o = (i - 1) * 4
  local cap, rate = tonumber(ARGV[o + 1]), tonumber(ARGV[o + 2])
  local cost, ttl = tonumber(ARGV[o + 3]), tonumber(ARGV[o + 4])
  local state = redis.call('HMGET', KEYS[i], 't', 'u')
  local tokens, updated = tonumber(state[1]), tonumber(state[2])
  if tokens == nil or updated == nil then
    tokens = cap
  elseif now > updated then
    tokens = math.min(cap, tokens + (now - updated) * rate)
  end
  local ok = tokens >= cost
  if ok then tokens = tokens - cost end
  redis.call('HSET', KEYS[i], 't', fmt(tokens), 'u', fmt(now))
  redis.call('PEXPIRE', KEYS[i], ttl)
  left[i] = fmt(tokens)
  if not ok then
    local retry = 60
    if rate > 0 then retry = (cost - tokens) / rate end
    return {i, fmt(retry), window_hit(KEYS[n + 1], tonumber(ARGV[n * 4 + 1]), tonumber(ARGV[n * 4 + 2]), 0), unpack(left)}
  end
end
return {0, '0', 0, unpack(left)}
""")

# KEYS[1]: a window of event times. ARGV: window (s), cap, clear_at (0 = never). -> events within the window
WINDOW = lua_script("window", """
return window_hit(KEYS[1], tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3]))
""")

# KEYS: keys to delete -> how many existed
FORGET = lua_script("forget", """
return redis.call('DEL', unpack(KEYS))
""")


async def _evalsha(client: Any, script: Script, keys: Sequence[str], args: Sequence[Any]) -> Any:
    try:
        return await client.evalsha(script.sha, len(keys), *keys, *args)
    except Exception as exc:
        if type(exc).__name__ != "NoScriptError" and "NOSCRIPT" not in str(exc):
            raise
    await client.script_load(script.source)  # first use since Redis started (or SCRIPT FLUSH)
    return await client.evalsha(script.sha, len(keys), *keys, *args)


class _LoopLate(Exception):
    """Every deadline of a shared-limit call fired late: this worker's event loop is overloaded (not Redis's fault)."""

    def __init__(self, late: float) -> None:
        super().__init__(f"event loop {late * 1000:.0f} ms late")
        self.late = late


def _consume(task: asyncio.Task[Any]) -> None:
    if not task.cancelled():
        task.exception()  # an abandoned call's error is never "never retrieved"


async def _within(client: Any, script: Script, keys: Sequence[str], args: Sequence[Any], timeout: float) -> Any:
    """``_evalsha`` with a deadline only Redis can miss. ``asyncio.wait`` (unlike ``wait_for``) never throws away a
    reply that arrived while the loop was busy; a deadline that fired late gives the call another ``timeout``.
    Raises TimeoutError when a deadline fired on time without an answer, _LoopLate when all MAX_WAITS fired late."""
    loop = asyncio.get_running_loop()
    call = loop.create_task(_evalsha(client, script, keys, args))
    slack = max(LATE_SLACK, timeout / 4)
    try:
        late = 0.0
        for _ in range(MAX_WAITS):
            deadline = loop.time() + timeout
            done, _pending = await asyncio.wait((call,), timeout=timeout)
            if done:
                return call.result()
            late = loop.time() - deadline
            if late <= slack:  # on time: Redis did not answer within the timeout
                raise TimeoutError(f"no answer from Redis within {timeout * 1000:.0f} ms")
        raise _LoopLate(late)
    finally:
        if not call.done():
            call.cancel()  # redis-py drops that connection: the next call gets a clean one
            call.add_done_callback(_consume)


def _glob_escape(text: str) -> str:
    return re.sub(r"([*?\[\]\\])", r"\\\1", text)


async def _delete_prefix(client: Any, prefix: str) -> int:
    deleted, batch = 0, []
    async for key in client.scan_iter(match=_glob_escape(prefix) + "*", count=1000):
        batch.append(key)
        if len(batch) >= 500:
            deleted += int(await client.unlink(*batch))
            batch.clear()
    if batch:
        deleted += int(await client.unlink(*batch))
    return deleted


_client: Any = None
_client_for: tuple[Any, ...] | None = None


async def _redis_client() -> Any | None:
    """This worker's Redis client for the shared limits (one per event loop), or None without REDIS_URL. Not the
    session store's client: it never retries (redis-py would otherwise retry a dead Redis for seconds), queues for one
    of MAX_CONNECTIONS connections instead of failing at the default pool's cap, and its failures never move sign-ins
    to PostgreSQL. The tight RATE_LIMIT_REDIS_TIMEOUT_MS is ``_within``'s deadline; redis-py's own socket / pool
    timeouts are asyncio timers too (they would fire late on a busy loop as well), so they are only a backstop for
    what the deadline cannot catch. Module-level so tests can swap it."""
    global _client, _client_for
    s = get_settings()
    if not s.redis_url:
        return None
    timeout = redis_timeout()
    marker = (s.redis_url, asyncio.get_running_loop(), timeout)
    if _client is None or _client_for != marker:
        import redis.asyncio as aioredis
        from redis.asyncio.retry import Retry
        from redis.backoff import NoBackoff

        backstop = max(1.0, (MAX_WAITS + 1) * timeout)
        pool = aioredis.BlockingConnectionPool.from_url(
            s.redis_url, max_connections=MAX_CONNECTIONS, timeout=backstop, decode_responses=True,
            socket_timeout=backstop, socket_connect_timeout=backstop, retry=Retry(NoBackoff(), 0),
            health_check_interval=30)
        _client = aioredis.Redis(connection_pool=pool)
        _client_for = marker
    return _client


class SharedStore:
    """The Redis connection behind every shared security limit, and the fallback bookkeeping.

    ``await run(script, keys, args, parse)`` returns the parsed reply of one script call, or None when the caller must
    decide from its in-memory twin: no REDIS_URL, Redis failed / timed out / sent a reply ``parse`` rejects, the
    breaker is open (an outage - OUTAGE_SECONDS of failures - paused it less than a jittered RETRY_SECONDS ago, or
    another request is probing it), no call slot came free within QUEUE_WAIT_SECONDS, or this worker's event loop was
    too late to read the reply (``_within``; those two never count against Redis). Never raises (except cancellation).
    One store per worker (``shared_store``); a test may build stores with their own ``client`` callable - each one
    plays a separate worker."""

    def __init__(self, client: Callable[[], Awaitable[Any]] | None = None) -> None:
        self._client_fn = client
        self._skip_until = 0.0
        self._open = False  # the breaker: open after an outage until a probe call succeeds
        self._probing = False  # a half-open probe is in flight: the other requests stay on memory meanwhile
        self._logged_at = -math.inf
        self._late_logged_at = -math.inf
        self._late_streak = 0
        self._announced = False  # "several workers without Redis" (once per worker)
        self._failing_since: float | None = None  # first failure (or late answer) since Redis last answered
        self._down = False
        self._slots: tuple[Any, asyncio.Semaphore] | None = None  # (event loop, MAX_CONNECTIONS call slots)
        self._pending: set[asyncio.Task[Any]] = set()
        self._local_resets: list[Callable[[], Any]] = []
        self.failures = 0  # calls that fell back to memory because Redis failed (diagnostics, tests)
        self.late = 0  # calls that fell back to memory because this worker's event loop ran late
        self.queued_out = 0  # calls that fell back to memory because no call slot came free in time

    async def client(self) -> Any | None:
        return await (self._client_fn() if self._client_fn is not None else _redis_client())

    def _call_slots(self) -> asyncio.Semaphore:
        """MAX_CONNECTIONS call slots of this worker (one set per event loop): at most that many calls are in flight,
        so the client's pool never makes a call wait inside the deadline."""
        loop = asyncio.get_running_loop()
        if self._slots is None or self._slots[0] is not loop:
            self._slots = (loop, asyncio.Semaphore(MAX_CONNECTIONS))
        return self._slots[1]

    async def run(self, script: Script, keys: Sequence[str], args: Sequence[Any],
                  parse: Callable[[Any], T] = lambda reply: reply) -> T | None:
        probe = False
        if self._open:
            if time.monotonic() < self._skip_until or self._probing:
                return None  # breaker open (or half-open with a probe in flight): this worker's memory
            probe = self._probing = True
        try:
            return await self._call(script, keys, args, parse)
        finally:
            if probe:
                self._probing = False

    async def _call(self, script: Script, keys: Sequence[str], args: Sequence[Any],
                    parse: Callable[[Any], T]) -> T | None:
        try:
            client = await self.client()  # inside the try: a bad REDIS_URL or no redis package means memory
            if client is None:
                self._without_redis()
                return None
        except Exception as exc:  # noqa: BLE001
            self._failed(exc)
            return None
        slots = self._call_slots()
        if slots.locked():  # every slot busy: wait in this worker's queue - not Redis's time, so not its deadline
            try:
                await asyncio.wait_for(slots.acquire(), timeout=QUEUE_WAIT_SECONDS)
            except TimeoutError:
                self._queued_out()
                return None
        else:
            await slots.acquire()
        try:
            value = parse(await _within(client, script, keys, args, redis_timeout()))
        except _LoopLate as exc:
            self._overloaded(exc)
            return None
        except Exception as exc:  # noqa: BLE001 - timeout, connection, server or reply error: this worker's memory
            self._failed(exc)
            return None
        finally:
            slots.release()
        self._late_streak, self._failing_since, self._open = 0, None, False
        if self._down:
            self._down = False
            log.info("Shared security limits: Redis answers again - rate limits, threat scores and the sign-in "
                     "lockout are shared by every worker again")
        return value

    def _failed(self, exc: BaseException) -> None:
        """This request is judged from memory. Only an outage - calls failing for OUTAGE_SECONDS without a single
        success - opens the breaker for a jittered RETRY_SECONDS (no request waits for a dead Redis meanwhile; then
        one probe call) and is announced, at most once a minute."""
        now = time.monotonic()
        self.failures += 1
        if self._failing_since is None:
            self._failing_since = now
        if now - self._failing_since < OUTAGE_SECONDS:
            return
        self._open = True
        self._skip_until = now + RETRY_SECONDS * random.uniform(1.0 - RETRY_JITTER, 1.0 + RETRY_JITTER)
        if now - self._logged_at >= LOG_EVERY_SECONDS:
            self._logged_at = now
            self._down = True  # announced: so is the recovery (at most one pair of lines a minute when Redis flaps)
            log.warning("Shared security limits: Redis unavailable (%s) - rate limits, threat scores and the sign-in "
                        "lockout are enforced in this worker's memory until it answers again", type(exc).__name__)

    def _without_redis(self) -> None:
        """Once per worker: several workers without REDIS_URL each keep their own limits (N x the allowance)."""
        if self._announced:
            return
        self._announced = True
        try:
            from app.db import configured_workers

            workers = configured_workers()
        except Exception:  # noqa: BLE001 - a log line must never fail a request
            return
        if workers > 1:
            log.warning("Shared security limits: REDIS_URL is not set and %d workers serve the app - rate limits, "
                        "threat scores and the sign-in lockout are kept per worker (a client gets up to %d x the "
                        "allowance); set REDIS_URL to share them", workers, workers)

    def _overloaded(self, exc: _LoopLate) -> None:
        """This request is judged from memory; Redis is neither paused nor reported - unless LATE_STREAK calls in a
        row had no answer (then it may well be away too)."""
        self.late += 1
        self._late_streak += 1
        if self._failing_since is None:
            self._failing_since = time.monotonic()  # no answer either: part of an outage, should it become one
        if self._late_streak >= LATE_STREAK:
            self._late_streak = 0
            self._failed(TimeoutError(str(exc)))
            return
        now = time.monotonic()
        if now - self._late_logged_at >= LOG_EVERY_SECONDS:
            self._late_logged_at = now
            log.warning("Shared security limits: this worker is overloaded (its event loop ran %d ms late) - a request "
                        "it could not check with Redis in time was judged from its own memory", round(exc.late * 1000))

    def _queued_out(self) -> None:
        """No call slot came free within QUEUE_WAIT_SECONDS: this worker has more checks in flight than it can send.
        Judged from memory (which mirrors the shared state); Redis is neither paused nor reported."""
        self.queued_out += 1
        now = time.monotonic()
        if now - self._late_logged_at >= LOG_EVERY_SECONDS:
            self._late_logged_at = now
            log.warning("Shared security limits: this worker is overloaded (%d shared-limit calls in flight, none free "
                        "within %.1f s) - a request was judged from its own memory", MAX_CONNECTIONS, QUEUE_WAIT_SECONDS)

    def spawn(self, coro: Coroutine[Any, Any, Any]) -> bool:
        """Run a shared update in the background (for synchronous callers). False - and the coroutine is closed -
        without a running event loop or when MAX_PENDING updates are already in flight."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            coro.close()
            return False
        if len(self._pending) >= MAX_PENDING:
            coro.close()
            return False
        task = loop.create_task(coro)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)
        return True

    async def forget(self, *keys: str) -> None:
        if keys:
            await self.run(FORGET, list(keys), [])

    def on_reset(self, fn: Callable[[], Any]) -> None:
        """``fn`` clears a worker-memory twin when the tests reset this store."""
        self._local_resets.append(fn)

    async def reset(self) -> None:
        """Tests (registered with app.core.state): let background updates finish, delete this namespace's keys (SCAN
        + UNLINK, never FLUSHALL) and clear every worker-memory twin."""
        pending = [task for task in self._pending if not task.done()]
        if pending:
            try:
                _done, late = await asyncio.wait(pending, timeout=2.0)
                for task in late:
                    task.cancel()
            except Exception:  # noqa: BLE001 - tasks of another event loop
                pass
        self._skip_until, self._logged_at, self._down = 0.0, -math.inf, False
        self._late_logged_at, self._late_streak, self._failing_since = -math.inf, 0, None
        self._open = self._probing = False
        try:
            client = await self.client()
            if client is not None:
                await asyncio.wait_for(_delete_prefix(client, namespace()), timeout=10.0)
        except Exception as exc:  # noqa: BLE001 - a reset must not fail because Redis is away
            log.warning("Shared security limits: could not clear the Redis namespace (%s)", type(exc).__name__)
        for fn in self._local_resets:
            fn()


shared_store = SharedStore()


# =========================================================================== rate limiter
class _Windows:
    """Sliding windows of event times in this worker's memory - the twin of ``window_hit``: at most WINDOW_CAP
    timestamps per key and at most ``max_keys`` keys (least recently used dropped first)."""

    def __init__(self, max_keys: int = 50_000) -> None:
        self._data: OrderedDict[Any, deque[float]] = OrderedDict()
        self._max_keys = max_keys

    def hit(self, key: Any, window: float, clear_at: int = 0) -> int:
        now = time.monotonic()
        times = self._data.get(key)
        if times is None:
            times = self._data[key] = deque(maxlen=WINDOW_CAP)
            if len(self._data) > self._max_keys:
                self._data.popitem(last=False)
        else:
            self._data.move_to_end(key)
        times.append(now)
        recent = sum(1 for t in times if now - t < window)
        if clear_at and recent >= clear_at:
            self._data.pop(key, None)
        return recent

    def clear(self) -> None:
        self._data.clear()


class RateLimiter:
    """Token buckets per (bucket, address). ``check`` - what the security gate uses - is shared by every worker
    through Redis; ``hit`` charges one bucket in THIS worker's memory (the fallback of ``check``, and all a single
    process ever needed)."""

    def __init__(self, max_keys: int = 200_000, store: SharedStore | None = None) -> None:
        self._buckets: OrderedDict[tuple[str, str], Bucket] = OrderedDict()
        self._max_keys = max_keys
        self._windows = _Windows(max_keys=max(1, max_keys // 4))
        self._store = store

    @property
    def store(self) -> SharedStore:
        return self._store if self._store is not None else shared_store

    def limits(self) -> dict[str, Limit]:
        s = get_settings()
        return {
            "global": Limit.per_minute(s.rate_limit_per_minute, burst=max(30, s.rate_limit_per_minute / 4)),
            "auth": Limit.per_minute(s.rate_limit_auth_per_minute, burst=max(3, s.rate_limit_auth_per_minute)),
            "otp": Limit(capacity=5, per_second=5 / 600.0),  # 5 per 10 minutes
            "ai": Limit.per_minute(s.rate_limit_ai_per_minute, burst=max(5, s.rate_limit_ai_per_minute / 2)),
            "live": Limit.per_minute(s.rate_limit_live_per_minute, burst=max(20, s.rate_limit_live_per_minute / 3)),
            "upload": Limit.per_minute(s.rate_limit_upload_per_minute, burst=10),
            "static": Limit.per_minute(s.rate_limit_static_per_minute, burst=300),
        }

    def hit(self, key: str, bucket: str, cost: float = 1.0) -> tuple[bool, float]:
        """Consume ``cost`` tokens of ``bucket`` in this worker's memory. Returns (allowed, retry_after_seconds)."""
        return self._take(key, bucket, self.limits()[bucket], cost)

    def _mirror(self, key: str, bucket: str, tokens: float) -> None:
        """Write a shared bucket's state (the tokens it holds now) into this worker's memory: should Redis fail next,
        memory continues from there instead of from a full burst."""
        k = (bucket, key)
        b = self._buckets.get(k)
        now = time.monotonic()
        if b is None:
            self._buckets[k] = Bucket(tokens=tokens, updated=now)
            if len(self._buckets) > self._max_keys:
                self._buckets.popitem(last=False)
        else:
            self._buckets.move_to_end(k)
            b.tokens, b.updated = tokens, now

    def _take(self, key: str, bucket: str, limit: Limit, cost: float) -> tuple[bool, float]:
        now = time.monotonic()
        k = (bucket, key)
        b = self._buckets.get(k)
        if b is None:
            b = Bucket(tokens=limit.capacity, updated=now)
            self._buckets[k] = b
            if len(self._buckets) > self._max_keys:
                self._buckets.popitem(last=False)
        else:
            self._buckets.move_to_end(k)
            b.tokens = min(limit.capacity, b.tokens + (now - b.updated) * limit.per_second)
            b.updated = now
        if b.tokens >= cost:
            b.tokens -= cost
            return True, 0.0
        retry = (cost - b.tokens) / limit.per_second if limit.per_second > 0 else 60.0
        return False, max(1.0, retry)

    async def check(self, key: str, buckets: Sequence[str], cost: float = 1.0) -> Verdict:
        """Charge ``cost`` to each of ``buckets`` in order and stop at the first that is empty (the ones before it stay
        charged, as when they are hit one by one). One Lua script round trip when Redis is configured; this worker's
        memory without it or while it fails - continuing from the remaining tokens of the last shared verdict, which
        are mirrored into memory every time. A refusal also counts towards the address's rejections within
        REJECTION_WINDOW (``Verdict.recent``). Whitelisted addresses are never charged."""
        if not buckets or key in get_settings().rate_limit_whitelist:
            return Verdict(True)
        limits = self.limits()
        chosen = [(name, limits[name]) for name in buckets]
        keys = [address_key(key, "rl", name) for name, _limit in chosen] + [address_key(key, "rej")]
        args: list[Any] = []
        for _name, limit in chosen:
            args += [float(limit.capacity), float(limit.per_second), float(cost), limit.ttl_ms]
        args += [REJECTION_WINDOW, WINDOW_CAP]

        def parse(reply: Any) -> Verdict:
            index, retry, recent = int(reply[0]), float(reply[1]), int(reply[2])
            for (name, _limit), left in zip(chosen, reply[3:]):  # the buckets the script touched, in order
                self._mirror(key, name, float(left))
            if index == 0:
                return Verdict(True, shared=True)
            return Verdict(False, chosen[index - 1][0], max(1.0, retry), recent, shared=True)

        verdict = await self.store.run(TAKE, keys, args, parse)
        if verdict is not None:
            return verdict
        for name, limit in chosen:
            allowed, retry = self._take(key, name, limit, cost)
            if not allowed:
                return Verdict(False, name, retry, self._windows.hit(("rej", key), REJECTION_WINDOW))
        return Verdict(True)

    async def note(self, key: str, kind: str, *, window: float, clear_at: int = 0) -> int:
        """Count one ``kind`` event of ``key`` in a sliding ``window`` (seconds; at most WINDOW_CAP remembered) and
        return how many fell within it, this one included; at ``clear_at`` the window starts over, so a flood is
        reported once per ``clear_at`` events. Shared through Redis like the buckets (the gate counts 404s with it)."""
        count = await self.store.run(WINDOW, [address_key(key, kind)], [float(window), WINDOW_CAP, int(clear_at)], int)
        if count is not None:
            return count
        return self._windows.hit((kind, key), window, clear_at)

    def reset(self) -> None:
        """Forget this worker's buckets and windows (tests; ``shared_store.reset`` clears the shared keys)."""
        self._buckets.clear()
        self._windows.clear()


limiter = RateLimiter()
shared_store.on_reset(limiter.reset)
register_reset(shared_store.reset, name="app.security.shared_limits")

AUTH_POST_PREFIXES = ("/auth/login", "/auth/signup", "/auth/forgot", "/auth/reset", "/auth/invite", "/auth/staff-invite",
                      "/superadmin/login")
AI_PREFIXES = ("/api/assistant", "/api/ai/", "/api/plans/ask", "/api/shop/", "/me/coach/generate", "/me/wellness",
               "/owner/customers/intake")
STATIC_PREFIXES = ("/static/", "/media/public/", "/maps/tiles/", "/favicon")
LIVE_PREFIXES = ("/api/live/", "/api/tts")


def bucket_for(method: str, path: str, content_type: str | None) -> str | None:
    """Specific bucket for this request (the global bucket is always charged too, except static)."""
    if path.startswith(STATIC_PREFIXES):
        return "static"
    if method == "POST":
        if path.startswith(AUTH_POST_PREFIXES):
            return "auth"
        if path.startswith("/checkout/") and path.endswith(("/presence", "/otp")):
            return "otp"
        if path.startswith(LIVE_PREFIXES):
            # workout progress, commands, chat messages and coach voice: frequent, mostly deterministic. The AI behind
            # /message and /api/tts is held to each member's quotas by the AI guard, so it never spends the
            # per-address "ai" budget that a whole gym behind one NAT shares.
            return "live"
        if path.startswith(AI_PREFIXES):
            return "ai"
        if content_type and content_type.startswith("multipart/form-data"):
            return "upload"
    return None
