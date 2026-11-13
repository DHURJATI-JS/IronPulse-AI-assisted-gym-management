"""Presence - "did the user leave?" (docs/ai-guard-and-queue.md section 5): grace period, reconnects, several tabs,
page-closed (4001) cancels, restarts (1012) and shutdown, logout / revocation / account deletion, return resumes
paused jobs, the cross-process Redis lease (fake and real Redis), the /ws endpoint (page id, conn id, close codes,
flood reporting, per-user socket cap, control events) and the reset used between tests.

Redis is switched off for every test here (per-process presence) unless it asks for ``fake_redis`` or
``real_redis``; the real one only touches keys under ``ip:test:<test-db>:presence:`` and the reset removes them.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, update

from app import db as dbmod
from app.config import get_settings
from app.core.utils import utcnow
from app.models import Job, UserSession
from app.models.enums import JobStatus, ThreatCategory
from app.routers import core as core_router
from app.security.bans import security_events, threat_tracker
from app.security.crypto import new_token, token_hash
from app.security.sessions import revoke_all_sessions, session_cookie_name
from app.services import jobs as J
from app.services import presence as presence_mod
from app.services.presence import LEASE_TTL_SECONDS, PAGE_CLOSED, SERVICE_RESTART, presence, redis_namespace
from app.services.realtime import CloseRequest, call_after_commit, hub
from tests.conftest import login_as, make_client, post_form
from tests.factories import make_customer

GRACE = 0.2
PAGE_A = "pageA-1234567890"
PAGE_B = "pageB-1234567890"


# ============================================================================================ helpers / fixtures
class SchedulerSpy:
    """Stands in for the AI queue singleton: records every cancel and forwards it to the real one (so the calls
    are also checked against its signature)."""

    def __init__(self, real: Any) -> None:
        self.real = real
        self.calls: list[dict[str, Any]] = []
        self.result = 0
        self.fail = False

    def cancel(self, **kwargs: Any) -> int:
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("AI queue unavailable")
        n = self.real.cancel(**kwargs)
        return self.result or int(n or 0)

    def reasons(self) -> list[str]:
        return [c.get("reason") for c in self.calls]

    def __getattr__(self, name: str) -> Any:
        return getattr(self.real, name)


@pytest.fixture
def spy(monkeypatch):
    import app.ai.scheduler as scheduler_mod

    s = SchedulerSpy(scheduler_mod.scheduler)
    monkeypatch.setattr(scheduler_mod, "scheduler", s)
    return s


@pytest.fixture
def grace(monkeypatch):
    monkeypatch.setattr(get_settings(), "presence_grace_seconds", GRACE)
    return GRACE


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    async def none() -> None:
        return None

    monkeypatch.setattr(presence, "_redis", none)


class FakePipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self.redis = redis
        self.ops: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Callable[..., FakePipeline]:
        def buffer(*args: Any, **kwargs: Any) -> FakePipeline:
            self.ops.append((name, args, kwargs))
            return self

        return buffer

    async def execute(self) -> list[Any]:
        if self.redis.broken:
            raise ConnectionError("redis is down")
        return [getattr(self.redis, "_" + name)(*args, **kwargs) for name, args, kwargs in self.ops]

    async def __aenter__(self) -> FakePipeline:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


class FakeRedis:
    """The handful of Redis commands presence uses, in memory (ZSETs, strings, lists, TTLs ignored)."""

    def __init__(self) -> None:
        self.zsets: dict[str, dict[str, float]] = {}
        self.strings: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}
        self.broken = False

    def pipeline(self, transaction: bool = True) -> FakePipeline:
        return FakePipeline(self)

    def __getattr__(self, name: str) -> Callable[..., Any]:
        impl = object.__getattribute__(self, "_" + name)

        async def call(*args: Any, **kwargs: Any) -> Any:
            if self.broken:
                raise ConnectionError("redis is down")
            return impl(*args, **kwargs)

        return call

    @staticmethod
    def _bound(value: Any) -> tuple[float, bool]:
        text = str(value)
        if text in ("-inf", "+inf", "inf"):
            return float(text.replace("+", "")), False
        if text.startswith("("):
            return float(text[1:]), True
        return float(text), False

    def _zadd(self, key: str, mapping: dict[str, float]) -> int:
        z = self.zsets.setdefault(key, {})
        added = sum(1 for m in mapping if m not in z)
        z.update({m: float(s) for m, s in mapping.items()})
        return added

    def _zrem(self, key: str, *members: str) -> int:
        z = self.zsets.get(key, {})
        return sum(1 for m in members if z.pop(m, None) is not None)

    def _zrangebyscore(self, key: str, lo: Any, hi: Any) -> list[str]:
        (lo_v, lo_x), (hi_v, hi_x) = self._bound(lo), self._bound(hi)
        return [m for m, s in sorted(self.zsets.get(key, {}).items(), key=lambda kv: kv[1])
                if (s > lo_v if lo_x else s >= lo_v) and (s < hi_v if hi_x else s <= hi_v)]

    def _zremrangebyscore(self, key: str, lo: Any, hi: Any) -> int:
        doomed = self._zrangebyscore(key, lo, hi)
        for m in doomed:
            self.zsets[key].pop(m, None)
        return len(doomed)

    def _expire(self, key: str, seconds: int) -> bool:
        return key in self.zsets or key in self.strings or key in self.lists

    def _exists(self, *keys: str) -> int:
        return sum(1 for k in keys if k in self.strings or self.zsets.get(k) or self.lists.get(k))

    def _set(self, key: str, value: str, ex: int | None = None) -> bool:
        self.strings[key] = value
        return True

    def _get(self, key: str) -> str | None:
        return self.strings.get(key)

    def _delete(self, *keys: str) -> int:
        n = 0
        for k in keys:
            n += sum(store.pop(k, None) is not None for store in (self.strings, self.zsets, self.lists))
        return n

    @staticmethod
    def _span(n: int, start: int, stop: int) -> tuple[int, int]:
        """Redis list bounds (inclusive, negative = from the end) as a Python slice."""
        start = max(0, n + start if start < 0 else start)
        stop = min(n - 1, n + stop if stop < 0 else stop)
        return start, stop + 1

    def _rpush(self, key: str, *values: str) -> int:
        items = self.lists.setdefault(key, [])
        items.extend(values)
        return len(items)

    def _lrange(self, key: str, start: int, stop: int) -> list[str]:
        items = self.lists.get(key, [])
        a, b = self._span(len(items), start, stop)
        return items[a:b]

    def _ltrim(self, key: str, start: int, stop: int) -> bool:
        items = self.lists.get(key, [])
        a, b = self._span(len(items), start, stop)
        if items[a:b]:
            self.lists[key] = items[a:b]
        else:
            self.lists.pop(key, None)
        return True

    def _ping(self) -> bool:
        return True

    async def scan_iter(self, match: str = "*", count: int = 100):
        for k in [*self.strings, *self.zsets, *self.lists]:
            if fnmatch.fnmatch(k, match):
                yield k


@pytest.fixture
def fake_redis(monkeypatch):
    fake = FakeRedis()

    async def get() -> FakeRedis:
        return fake

    monkeypatch.setattr(presence, "_redis", get)
    return fake


@pytest.fixture
async def real_redis(monkeypatch):
    from app.security.sessions import get_redis

    r = await get_redis()
    if r is None:
        pytest.skip("Redis is not configured")
    try:
        await r.ping()
    except Exception:  # noqa: BLE001
        pytest.skip("Redis is not reachable")
    monkeypatch.delattr(presence, "_redis")  # back to the class method (app.security.sessions.get_redis)
    yield r
    keys = [k async for k in r.scan_iter(match=f"{redis_namespace()}:*")]
    if keys:
        await r.delete(*keys)


async def wait_until(predicate: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


async def after_grace() -> None:
    """Let a grace timer fire and its exit finish (or prove that none was running)."""
    await asyncio.sleep(GRACE * 1.5)  # any timer that was started has expired by now
    await wait_until(lambda: presence.snapshot()["grace_timers"] == 0, timeout=5)  # ... even on a slow machine
    await presence.drain()


async def add_job(user_id: int | None, policy: str, *, kind: str = "presence_test", status: JobStatus | None = None) -> int:
    async with dbmod.get_sessionmaker()() as s:
        job = await J.enqueue(s, kind, {"p": policy}, user_id=user_id, exit_policy=policy,
                              dedupe_key=f"{kind}:{user_id}:{policy}:{time.monotonic_ns()}")
        if status is not None:
            job.status = status
        await s.commit()
        return job.id


async def job(job_id: int) -> Job:
    async with dbmod.get_sessionmaker()() as s:
        return await s.get(Job, job_id)


async def new_session(user: Any) -> str:
    token = new_token(32)
    async with dbmod.get_sessionmaker()() as s:
        s.add(UserSession(id=token_hash(token), user_id=user.id, expires_at=utcnow() + timedelta(hours=2),
                          ip="127.0.0.1", user_agent="pytest"))
        await s.commit()
    return token


def connect(uid: int, conn: str, page: str | None = PAGE_A, session: str | None = "sess-1") -> None:
    presence.connected(uid, conn_id=conn, page_id=page, session_hash=session)


def disconnect(uid: int, conn: str, page: str | None = PAGE_A, code: int | None = 1001) -> None:
    presence.disconnected(uid, conn_id=conn, page_id=page, code=code)


# ============================================================================================ grace / tabs / codes
async def test_last_socket_closing_is_an_exit_after_the_grace_period(db, grace, spy):
    user = await make_customer(db)
    other = await make_customer(db)
    paused = await add_job(user.id, "pause")
    cancelled = await add_job(user.id, "cancel")
    kept = await add_job(user.id, "keep")
    other_job = await add_job(other.id, "pause")
    system_job = await add_job(None, "pause")

    connect(user.id, "c1")
    await presence.drain()
    disconnect(user.id, "c1")
    await asyncio.sleep(GRACE / 3)
    assert not presence.is_away(user.id) and spy.calls == []  # still inside the grace period
    await after_grace()

    assert presence.is_away(user.id) and not presence.is_away(other.id)
    assert {"user_id": user.id, "kinds": {"user"}, "reason": "left"} in spy.calls
    p, c, k = await job(paused), await job(cancelled), await job(kept)
    assert p.status == JobStatus.PAUSED and p.paused_at is not None and p.cancel_reason == "left"
    assert c.status == JobStatus.CANCELLED and c.finished_at is not None
    assert k.status == JobStatus.QUEUED  # 'keep' is an obligation
    assert (await job(other_job)).status == JobStatus.QUEUED and (await job(system_job)).status == JobStatus.QUEUED
    assert presence.snapshot()["stats"]["exits"] == 1


async def test_reconnect_inside_the_grace_period_is_not_an_exit(db, grace, spy):
    user = await make_customer(db)
    pending = await add_job(user.id, "pause")
    connect(user.id, "c1", PAGE_A)
    disconnect(user.id, "c1", PAGE_A)  # page navigation: the old socket closes ...
    await asyncio.sleep(GRACE / 4)
    connect(user.id, "c2", PAGE_B)  # ... and the next page connects
    await after_grace()
    assert not presence.is_away(user.id) and spy.calls == []
    assert presence.snapshot()["grace_timers"] == 0 and presence.is_connected(user.id)
    assert (await job(pending)).status == JobStatus.QUEUED


async def test_several_tabs_only_the_last_one_starts_the_grace_period(db, grace, spy):
    user = await make_customer(db)
    connect(user.id, "tab1", PAGE_A)
    connect(user.id, "tab2", PAGE_B)
    assert presence.snapshot()["sockets"] == 2 and presence.snapshot()["pages"] == 2
    disconnect(user.id, "tab1", PAGE_A)
    assert presence.snapshot()["grace_timers"] == 0
    await after_grace()
    assert not presence.is_away(user.id)
    disconnect(user.id, "tab2", PAGE_B)
    assert presence.snapshot()["grace_timers"] == 1
    await after_grace()
    assert presence.is_away(user.id) and spy.reasons() == ["left"]


async def test_page_closed_4001_cancels_that_tabs_ai_at_once(db, grace, spy):
    user = await make_customer(db)
    connect(user.id, "tab1", PAGE_A)
    connect(user.id, "tab2", PAGE_B)
    disconnect(user.id, "tab1", PAGE_A, code=PAGE_CLOSED)
    # immediately - no await in between - and scoped to the caller's own tickets
    assert spy.calls == [{"page_id": PAGE_A, "user_id": user.id, "kinds": {"user"}, "reason": "page_closed"}]
    await after_grace()
    assert not presence.is_away(user.id)  # the other tab is still open
    disconnect(user.id, "tab2", None, code=PAGE_CLOSED)  # page id remembered from the connect
    assert spy.calls[-1] == {"page_id": PAGE_B, "user_id": user.id, "kinds": {"user"}, "reason": "page_closed"}
    await after_grace()
    assert presence.is_away(user.id) and spy.reasons() == ["page_closed", "page_closed", "left"]


async def test_page_left_validates_the_page_id_and_reports_the_count(spy):
    spy.result = 3
    assert presence.page_left(PAGE_A, user_id=7, reason="page_closed") == 3
    assert presence.page_left("x", user_id=7) == 0 and presence.page_left("bad page id!", user_id=7) == 0
    assert len(spy.calls) == 1


async def test_server_restart_1012_is_never_an_exit(db, grace, spy):
    user = await make_customer(db)
    connect(user.id, "c1")
    disconnect(user.id, "c1", code=SERVICE_RESTART)
    assert presence.snapshot()["grace_timers"] == 0
    await after_grace()
    assert not presence.is_away(user.id) and spy.calls == []


async def test_shutdown_never_turns_closing_sockets_into_exits(db, grace, spy):
    user = await make_customer(db)
    pending = await add_job(user.id, "pause")
    connect(user.id, "c1")
    disconnect(user.id, "c1")  # a grace timer is running when the server starts shutting down
    presence.begin_shutdown()
    assert presence.snapshot()["grace_timers"] == 0 and presence.snapshot()["shutting_down"]
    connect(user.id, "c2")
    disconnect(user.id, "c2", code=1006)  # uvicorn drops the rest
    await after_grace()
    assert not presence.is_away(user.id) and spy.calls == []
    assert (await job(pending)).status == JobStatus.QUEUED
    assert await presence.on_left(user.id) is None  # even a direct exit is refused while shutting down


async def test_unknown_close_code_counts_like_any_other_disconnect(db, grace, spy):
    user = await make_customer(db)
    connect(user.id, "c1")
    disconnect(user.id, "c1", code=None)
    await after_grace()
    assert presence.is_away(user.id)


# ============================================================================================ logout / revocation
async def test_logout_is_an_immediate_exit(client, db, spy):
    user = await make_customer(db)
    paused = await add_job(user.id, "pause")
    await login_as(client, user)
    h = token_hash(client.cookies.get(session_cookie_name()))
    q = hub.subscribe(user.id, session_hash=h, page_id=PAGE_A, conn_id="c1")
    connect(user.id, "c1", PAGE_A, session=h)
    try:
        r = await post_form(client, "/auth/logout", {})
        assert r.status_code == 303
        assert presence.is_away(user.id)  # no grace period
        assert q.get_nowait() == CloseRequest(4401, "signed out")  # the session's socket is told to close
        assert {"user_id": user.id, "session_hash": h, "kinds": {"user"}, "reason": "logout"} in spy.calls
        assert {"user_id": user.id, "kinds": {"user"}, "reason": "logout"} in spy.calls
        row = await job(paused)
        assert row.status == JobStatus.PAUSED and row.cancel_reason == "logout"
        # the socket then closes (4401): the user already counts as gone, so no grace timer starts
        disconnect(user.id, "c1", PAGE_A, code=4401)
        assert presence.snapshot()["grace_timers"] == 0
    finally:
        hub.unsubscribe(user.id, q)


async def test_logout_with_another_live_session_is_not_an_immediate_exit(db, spy):
    user = await make_customer(db)
    pending = await add_job(user.id, "pause")
    async with make_client() as laptop, make_client() as phone:
        await login_as(laptop, user)
        await login_as(phone, user)
        h = token_hash(laptop.cookies.get(session_cookie_name()))
        assert (await post_form(laptop, "/auth/logout", {})).status_code == 303
    assert not presence.is_away(user.id)
    assert spy.calls == [{"user_id": user.id, "session_hash": h, "kinds": {"user"}, "reason": "logout"}]
    assert (await job(pending)).status == JobStatus.QUEUED


async def test_logout_while_another_session_has_a_socket_is_not_an_exit(db, spy):
    user = await make_customer(db)
    connect(user.id, "phone", PAGE_B, session="other-session")
    await presence.logout(user.id, session_hash="this-session")
    assert not presence.is_away(user.id)
    assert spy.reasons() == ["logout"] and spy.calls[0]["session_hash"] == "this-session"


async def test_revoke_all_closes_the_revoked_sessions_sockets_after_commit(db, spy):
    user = await make_customer(db)
    uid = user.id  # the rollback below expires the instance
    current, other = await new_session(user), await new_session(user)
    q_current = hub.subscribe(uid, session_hash=token_hash(current))
    q_other = hub.subscribe(uid, session_hash=token_hash(other))
    try:
        await revoke_all_sessions(db, uid, except_token=current)  # "sign out other devices"
        assert q_other.empty()  # nothing happens before COMMIT
        await db.commit()
        assert q_other.get_nowait() == CloseRequest(4401, "signed out") and q_current.empty()
        await presence.drain()
        assert not presence.is_away(uid)  # the current session is still alive
        assert spy.calls == [{"user_id": uid, "session_hash": token_hash(other), "kinds": {"user"},
                              "reason": "signed_out"}]
        # rolled back: nothing is closed
        third = await new_session(user)
        q_third = hub.subscribe(uid, session_hash=token_hash(third))
        await revoke_all_sessions(db, uid, except_token=current)
        await db.rollback()
        assert q_third.empty()
        hub.unsubscribe(uid, q_third)
    finally:
        hub.unsubscribe(uid, q_current)
        hub.unsubscribe(uid, q_other)


async def test_revoking_every_session_is_an_immediate_exit(db, spy):
    user = await make_customer(db)
    paused = await add_job(user.id, "pause")
    await new_session(user)
    await revoke_all_sessions(db, user.id)  # e.g. password reset: every device signed out
    await db.commit()
    await presence.drain()
    assert presence.is_away(user.id) and (await job(paused)).status == JobStatus.PAUSED


async def test_account_deletion_cancels_every_job_and_ai_request(db, spy):
    from app.services.accounts import soft_delete_user

    user = await make_customer(db)
    other = await make_customer(db)
    ids = [await add_job(user.id, p) for p in ("keep", "pause", "cancel")]
    ids.append(await add_job(user.id, "pause", status=JobStatus.PAUSED))
    done = await add_job(user.id, "keep", status=JobStatus.DONE)
    other_job = await add_job(other.id, "keep")
    await soft_delete_user(db, user, reason="test deletion")
    assert not any(c.get("reason") == "account_deleted" for c in spy.calls)  # only after COMMIT
    assert (await job(ids[0])).status == JobStatus.QUEUED  # not visible outside the transaction yet
    await db.commit()
    await presence.drain()
    for job_id in ids:
        row = await job(job_id)
        assert row.status == JobStatus.CANCELLED and row.cancel_reason == "account_deleted", job_id
    assert (await job(done)).status == JobStatus.DONE and (await job(other_job)).status == JobStatus.QUEUED
    assert {"user_id": user.id, "reason": "account_deleted", "policies": {"keep", "pause", "cancel"}} in spy.calls


# ============================================================================================ return
async def test_return_resumes_paused_jobs(db, grace, spy):
    user = await make_customer(db)
    paused = await add_job(user.id, "pause")
    await presence.on_left(user.id, reason="left")
    assert presence.is_away(user.id) and (await job(paused)).status == JobStatus.PAUSED
    connect(user.id, "c1")
    assert not presence.is_away(user.id)
    await presence.drain()
    row = await job(paused)
    assert row.status == JobStatus.QUEUED and row.paused_at is None and row.cancel_reason is None
    assert presence.snapshot()["stats"]["returns"] == 1

    # HTTP activity of an away user works the same way
    disconnect(user.id, "c1")
    await after_grace()
    assert presence.is_away(user.id) and (await job(paused)).status == JobStatus.PAUSED
    presence.touch(user.id)
    assert not presence.is_away(user.id)
    await presence.drain()
    assert (await job(paused)).status == JobStatus.QUEUED


async def test_touch_of_a_present_user_does_nothing(monkeypatch):
    calls = []

    async def resume(user_id: int, **kwargs: Any) -> int:
        calls.append(user_id)
        return 0

    monkeypatch.setattr(J, "resume_user_jobs", resume)
    presence.touch(42)
    await presence.drain()
    assert calls == [] and presence.snapshot()["stats"].get("returns") is None


async def test_fresh_arrival_resumes_but_a_navigation_does_not_touch_the_db(db, grace, monkeypatch):
    calls = []

    async def resume(user_id: int, **kwargs: Any) -> int:
        calls.append(user_id)
        return 0

    monkeypatch.setattr(J, "resume_user_jobs", resume)
    connect(5, "c1")  # first socket after a restart: PAUSED rows must never be stranded
    await presence.drain()
    assert calls == [5]
    disconnect(5, "c1")
    connect(5, "c2", PAGE_B)  # navigation inside the grace period
    await presence.drain()
    assert calls == [5]


async def test_fresh_arrival_resumes_rows_paused_before_a_restart(db):
    user = await make_customer(db)
    paused = await add_job(user.id, "pause", status=JobStatus.PAUSED)
    connect(user.id, "c1")  # this process never saw them leave
    await presence.drain()
    assert (await job(paused)).status == JobStatus.QUEUED


async def test_http_activity_during_the_grace_period_postpones_the_exit(db, spy, monkeypatch):
    grace = 0.6
    monkeypatch.setattr(get_settings(), "presence_grace_seconds", grace)
    user = await make_customer(db)
    pending = await add_job(user.id, "pause")
    connect(user.id, "c1")
    await presence.drain()
    disconnect(user.id, "c1")  # the socket drops and keeps failing to reconnect ...
    for _ in range(3):  # ... while they keep browsing
        await asyncio.sleep(grace * 0.5)
        presence.touch(user.id)
        last_request = time.monotonic()
        assert not presence.is_away(user.id) and presence.snapshot()["grace_timers"] == 1
    await asyncio.sleep(grace * 0.5)  # long past the original end of the grace period, half of one after the request
    assert not presence.is_away(user.id) and spy.calls == [] and (await job(pending)).status == JobStatus.QUEUED
    await wait_until(lambda: presence.is_away(user.id), timeout=5)  # they stopped browsing: they are gone once ...
    assert time.monotonic() - last_request >= grace - 0.05  # ... a whole grace period passed since their last request
    await presence.drain()
    assert spy.reasons() == ["left"] and (await job(pending)).status == JobStatus.PAUSED
    assert presence.snapshot()["stats"]["kept_by_http"] == 1


async def test_requests_keep_a_user_whose_socket_keeps_failing_present(client, db, spy, monkeypatch):
    grace = 0.6
    monkeypatch.setattr(get_settings(), "presence_grace_seconds", grace)
    user = await make_customer(db)
    await login_as(client, user)
    pending = await add_job(user.id, "pause")
    connect(user.id, "c1", session=token_hash(client.cookies.get(session_cookie_name())))
    await presence.drain()
    disconnect(user.id, "c1", code=1006)
    for _ in range(3):
        await asyncio.sleep(grace * 0.5)
        assert (await client.get("/api/ai/status")).status_code == 200  # load_user -> presence.touch
    assert not presence.is_away(user.id) and spy.calls == [] and (await job(pending)).status == JobStatus.QUEUED
    await after_grace()
    assert presence.is_away(user.id) and (await job(pending)).status == JobStatus.PAUSED


# ============================================================================================ cross-process lease
async def test_a_lease_of_another_process_keeps_the_user_present(db, grace, spy, fake_redis):
    user = await make_customer(db)
    key = f"{redis_namespace()}:{user.id}"
    fake_redis._zadd(key, {"other-host:1:abc": time.time() + 60})  # their tab lives on another worker
    connect(user.id, "c1")
    await presence.drain()
    disconnect(user.id, "c1")
    await after_grace()
    assert not presence.is_away(user.id) and spy.calls == []
    assert presence.process_id not in fake_redis.zsets[key]  # our expired claim is dropped

    fake_redis._zadd(key, {"other-host:1:abc": time.time() - 1})  # that worker's lease ran out
    connect(user.id, "c2")
    await presence.drain()
    disconnect(user.id, "c2")
    await after_grace()
    assert presence.is_away(user.id) and spy.reasons() == ["left"]
    assert f"{redis_namespace()}:away:{user.id}" in fake_redis.strings


async def test_lease_is_written_on_arrival_refreshed_and_released(db, grace, fake_redis):
    user = await make_customer(db)
    key = f"{redis_namespace()}:{user.id}"
    connect(user.id, "c1")
    await presence.drain()
    first = fake_redis.zsets[key][presence.process_id]
    assert first > time.time() + LEASE_TTL_SECONDS - 5
    await asyncio.sleep(0.02)
    assert await presence.refresh_leases() == 1
    assert fake_redis.zsets[key][presence.process_id] > first
    disconnect(user.id, "c1")
    await after_grace()
    assert presence.is_away(user.id) and presence.process_id not in fake_redis.zsets.get(key, {})
    # they come back: the shared away flag is cleared
    connect(user.id, "c2")
    await presence.drain()
    assert f"{redis_namespace()}:away:{user.id}" not in fake_redis.strings
    assert presence.process_id in fake_redis.zsets[key]


async def test_away_flag_is_shared_between_processes(db, fake_redis):
    user = await make_customer(db)
    away_key = f"{redis_namespace()}:away:{user.id}"
    assert not await presence.away_now(user.id)
    fake_redis._set(away_key, "1")  # another process saw them leave
    assert await presence.away_now(user.id) and not presence.is_away(user.id)

    # ... while they are actually connected here: the next lease refresh heals it and resumes their jobs
    paused = await add_job(user.id, "pause", status=JobStatus.PAUSED)
    connect(user.id, "c1")
    await presence.drain()  # the arrival itself already clears the flag
    assert away_key not in fake_redis.strings and (await job(paused)).status == JobStatus.QUEUED
    fake_redis._set(away_key, "1")
    async with dbmod.get_sessionmaker()() as s:
        await s.execute(update(Job).where(Job.id == paused).values(status=JobStatus.PAUSED))
        await s.commit()
    assert not await presence.away_now(user.id)  # a socket here wins
    await presence.refresh_leases()
    await presence.drain()
    assert away_key not in fake_redis.strings and (await job(paused)).status == JobStatus.QUEUED
    assert presence.snapshot()["stats"]["away_while_connected"] == 1

    # a local away mark whose shared flag another process cleared: they came back there
    disconnect(user.id, "c1", code=SERVICE_RESTART)
    await presence.on_left(user.id)
    assert presence.is_away(user.id) and away_key in fake_redis.strings
    fake_redis._delete(away_key)
    assert not await presence.away_now(user.id) and not presence.is_away(user.id)
    await presence.on_left(user.id)
    fake_redis._delete(away_key)
    await presence.refresh_leases()
    assert not presence.is_away(user.id)


async def test_a_signed_out_sessions_lingering_socket_does_not_undo_the_exit(db, fake_redis):
    user = await make_customer(db)
    paused = await add_job(user.id, "pause")
    connect(user.id, "c1", session="revoked")
    await presence.drain()
    assert await presence.sessions_ended(user.id, ["revoked"], reason="logout")  # its socket is still closing
    assert presence.is_away(user.id) and (await job(paused)).status == JobStatus.PAUSED
    key = f"{redis_namespace()}:{user.id}"
    lease_before = dict(fake_redis.zsets.get(key, {}))
    await asyncio.sleep(0.01)
    await presence.refresh_leases()  # no lease renewal, no "they are connected here" healing for it
    await presence.drain()
    assert presence.is_away(user.id) and (await job(paused)).status == JobStatus.PAUSED
    assert fake_redis.zsets.get(key, {}) == lease_before
    assert await presence.away_now(user.id)
    disconnect(user.id, "c1", code=4401)  # the socket finally closes: no grace timer, they are already gone
    assert presence.snapshot()["grace_timers"] == 0 and presence.is_away(user.id)


async def test_redis_errors_fall_back_to_per_process_presence(db, grace, spy, fake_redis):
    user = await make_customer(db)
    fake_redis.broken = True
    connect(user.id, "c1")
    await presence.drain()
    disconnect(user.id, "c1")
    await after_grace()
    assert presence.is_away(user.id) and spy.reasons() == ["left"]
    assert await presence.away_now(user.id)  # local view
    assert presence.snapshot()["stats"]["redis_errors"] >= 2


async def test_http_activity_notices_that_another_process_saw_the_user_leave(db, fake_redis):
    user = await make_customer(db)
    paused = await add_job(user.id, "pause", status=JobStatus.PAUSED)  # paused by another process's exit ...
    away_key = f"{redis_namespace()}:away:{user.id}"
    fake_redis._set(away_key, "1")  # ... which flagged them away for every process
    assert not presence.is_away(user.id) and await presence.away_now(user.id)  # this process never saw them leave
    presence.touch(user.id)  # their next request lands here (no socket of theirs anywhere)
    await presence.drain()
    assert away_key not in fake_redis.strings and not await presence.away_now(user.id)
    assert (await job(paused)).status == JobStatus.QUEUED
    assert presence.snapshot()["stats"]["away_checks"] == 1


async def test_touch_asks_redis_at_most_once_per_window_per_user(db, fake_redis, monkeypatch):
    user = await make_customer(db)
    other = await make_customer(db)
    away_key = f"{redis_namespace()}:away:{user.id}"
    for _ in range(25):  # a busy page: every request of every tab
        presence.touch(user.id)
    presence.touch(other.id)
    await presence.drain()
    assert presence.snapshot()["stats"]["away_checks"] == 2  # one per user, not one per request
    paused = await add_job(user.id, "pause", status=JobStatus.PAUSED)
    fake_redis._set(away_key, "1")  # another process sees them leave right after that check
    presence.touch(user.id)
    await presence.drain()
    assert away_key in fake_redis.strings and (await job(paused)).status == JobStatus.PAUSED  # not asked again yet
    monkeypatch.setattr(presence_mod, "AWAY_RECHECK_SECONDS", 0.05)
    await asyncio.sleep(0.06)  # the window passed
    presence.touch(user.id)
    await presence.drain()
    assert away_key not in fake_redis.strings and (await job(paused)).status == JobStatus.QUEUED
    assert presence.snapshot()["stats"]["away_checks"] == 3
    assert list(presence._away_checked) == [user.id]  # users not seen within the window are forgotten


async def test_an_exit_overtaken_by_a_return_elsewhere_leaves_nothing_paused(db, fake_redis, monkeypatch):
    user = await make_customer(db)
    pending = await add_job(user.id, "pause")
    away_key = f"{redis_namespace()}:away:{user.id}"
    pause_jobs = J.apply_exit_policy

    async def overtaken(user_id: int, *, reason: str = "left") -> dict[str, int]:
        # meanwhile their next request lands in another process: its return clears the flag and resumes their paused
        # jobs - none yet - before this exit pauses them
        fake_redis._delete(away_key)
        return await pause_jobs(user_id, reason=reason)

    monkeypatch.setattr(J, "apply_exit_policy", overtaken)
    result = await presence.on_left(user.id)
    assert result is not None and result["jobs"]["paused"] == 1
    assert (await job(pending)).status == JobStatus.QUEUED  # resumed at once: they are back
    assert not presence.is_away(user.id) and not await presence.away_now(user.id)
    assert presence.snapshot()["stats"]["exits_overtaken"] == 1

    monkeypatch.setattr(J, "apply_exit_policy", pause_jobs)  # an ordinary exit: the flag stays, so do the pauses
    await presence.on_left(user.id)
    assert (await job(pending)).status == JobStatus.PAUSED and presence.is_away(user.id)


async def test_a_request_resumes_jobs_another_process_paused_on_real_redis(client, db, real_redis):
    user = await make_customer(db)
    await login_as(client, user)
    paused = await add_job(user.id, "pause", status=JobStatus.PAUSED)
    away_key = f"{redis_namespace()}:away:{user.id}"
    await real_redis.set(away_key, "1", ex=60)  # another process saw them leave and paused their jobs
    assert (await client.get("/api/ai/status")).status_code == 200  # load_user -> presence.touch
    await presence.drain()
    assert await real_redis.exists(away_key) == 0 and (await job(paused)).status == JobStatus.QUEUED


async def test_real_redis_keys_are_namespaced_and_cleaned_up(db, real_redis):
    user = await make_customer(db)
    ns = redis_namespace()
    db_name = get_settings().database_url.rsplit("/", 1)[-1]
    assert ns == f"ip:test:{db_name}:presence"
    connect(user.id, "c1")
    await presence.drain()
    members = await real_redis.zrangebyscore(f"{ns}:{user.id}", "-inf", "+inf")
    assert members == [presence.process_id]
    assert await presence._held_elsewhere(user.id) is False
    await real_redis.zadd(f"{ns}:{user.id}", {"other-proc": time.time() + 30})
    assert await presence._held_elsewhere(user.id) is True
    await presence.on_left(user.id, ended_sessions={"sess-1"})  # its socket's session was signed out
    # flagged for every process; the socket still open here belongs to the signed-out session, so it does not count
    assert await real_redis.exists(f"{ns}:away:{user.id}") == 1 and await presence.away_now(user.id) is True
    await presence.reset()
    assert [k async for k in real_redis.scan_iter(match=f"{ns}:*")] == []


# ============================================================================================ robustness / reset
async def test_presence_callbacks_swallow_their_errors(db, grace, spy, monkeypatch):
    user = await make_customer(db)
    spy.fail = True
    assert presence.page_left(PAGE_A, user_id=user.id) == 0
    connect(user.id, "c1")
    disconnect(user.id, "c1", code=PAGE_CLOSED)  # the cancel raises inside: no exception escapes

    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("database down")

    monkeypatch.setattr(J, "apply_exit_policy", broken)
    monkeypatch.setattr(J, "resume_user_jobs", broken)
    assert await presence.on_left(user.id) is None
    presence.connected(user.id, conn_id="c2", page_id=None, session_hash=None)
    await presence.drain()
    presence.disconnected(999_999, conn_id="never-connected", page_id="??", code=4001)
    await presence.logout(user.id, session_hash=None)


async def test_reset_cancels_timers_and_forgets_everything(db, grace, spy):
    user = await make_customer(db)
    connect(user.id, "c1")
    disconnect(user.id, "c1")
    connect(user.id + 1, "c9")
    assert presence.snapshot()["grace_timers"] == 1
    await presence.reset()
    snap = presence.snapshot()
    assert snap["grace_timers"] == snap["users"] == snap["away"] == 0
    await asyncio.sleep(GRACE * 2)
    assert not presence.is_away(user.id) and spy.calls == []


# ============================================================================================ /ws endpoint
class FakeWS:
    """Just enough of Starlette's WebSocket for ``websocket_endpoint`` (uvicorn semantics: closing queues a
    disconnect carrying our own code)."""

    def __init__(self, token: str | None, *, page: str | None = PAGE_A, origin: str | None = None) -> None:
        self.cookies = {session_cookie_name(): token} if token else {}
        self.query_params = {"page": page} if page else {}
        self.headers = {"user-agent": "pytest-ws", **({"origin": origin, "host": "testserver"} if origin else {})}
        self.scope = {"type": "websocket", "client": ("198.51.100.77", 50000), "headers": [], "state": {}}
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self.accepted = False
        self.closed: int | None = None

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        if self.closed is None:
            self.closed = code
            self.inbox.put_nowait({"type": "websocket.disconnect", "code": code})

    async def send_json(self, data: Any) -> None:
        if self.closed is not None:
            raise RuntimeError("Cannot call 'send' once a close message has been sent.")
        json.dumps(data)
        self.sent.append(data)

    async def receive(self) -> dict[str, Any]:
        return await self.inbox.get()

    def text(self, obj: Any) -> None:
        self.inbox.put_nowait({"type": "websocket.receive", "text": obj if isinstance(obj, str) else json.dumps(obj)})

    def bye(self, code: int) -> None:
        self.inbox.put_nowait({"type": "websocket.disconnect", "code": code})

    def types(self) -> list[str]:
        return [e.get("type") for e in self.sent]


async def open_ws(ws: FakeWS) -> asyncio.Task:
    task = asyncio.create_task(core_router.websocket_endpoint(ws))  # type: ignore[arg-type]
    await wait_until(lambda: bool(ws.sent) or ws.closed is not None or task.done())
    return task


async def test_ws_registers_the_tab_and_reports_its_close_code(db, spy, grace):
    user = await make_customer(db)
    token = await new_session(user)
    ws = FakeWS(token, page=PAGE_A)
    task = await open_ws(ws)
    hello = ws.sent[0]
    assert hello["type"] == "hello" and hello["user_id"] == user.id and hello["page_id"] == PAGE_A and hello["conn_id"]
    [conn] = hub.connections(user.id)
    assert conn.session_hash == token_hash(token) and conn.page_id == PAGE_A and conn.conn_id == hello["conn_id"]
    assert presence.is_connected(user.id)

    ws.text({"type": "ping"})
    await hub.publish([user.id], {"type": "_ctl.internal", "secret": 1})  # control events never reach browsers
    await hub.publish([user.id], {"type": "notification", "title": "Hi"})
    await wait_until(lambda: "notification" in ws.types() and "pong" in ws.types())
    assert not any(str(t).startswith("_ctl") for t in ws.types())

    ws.bye(PAGE_CLOSED)
    await asyncio.wait_for(task, 3)
    assert spy.calls == [{"page_id": PAGE_A, "user_id": user.id, "kinds": {"user"}, "reason": "page_closed"}]
    assert not hub.is_online(user.id) and not presence.is_connected(user.id)
    await after_grace()
    assert presence.is_away(user.id)


async def test_ws_ignores_binary_frames_and_runs_registered_handlers(db):
    user = await make_customer(db)
    seen: list[tuple[int, dict[str, Any]]] = []

    @core_router.ws_handler("test.echo")
    async def echo(u: Any, msg: dict[str, Any]) -> None:
        seen.append((u.id, msg))

    try:
        ws = FakeWS(await new_session(user), page=None)
        task = await open_ws(ws)
        assert ws.sent[0]["page_id"] is None
        ws.inbox.put_nowait({"type": "websocket.receive", "bytes": b"\x00\x01binary"})
        ws.text("not json")
        ws.text({"type": "test.echo", "n": 1})
        await wait_until(lambda: bool(seen))
        assert seen == [(user.id, {"type": "test.echo", "n": 1})]
        ws.bye(1000)
        await asyncio.wait_for(task, 3)
    finally:
        core_router.ws_handlers.pop("test.echo", None)


async def test_ws_flood_is_reported_to_the_security_system_and_closed(db):
    user = await make_customer(db)
    token = await new_session(user)
    ws = FakeWS(token)
    task = await open_ws(ws)
    for _ in range(core_router.WS_FLOOD_MESSAGES + 1):
        ws.text({"type": "ping"})
    await asyncio.wait_for(task, 3)
    assert ws.closed == 4429
    [evt] = [e for e in security_events.events if e.detail == "websocket flood"]
    assert (evt.category, evt.severity, evt.method, evt.path, evt.action) == (ThreatCategory.RATE_LIMIT, 2, "WS", "/ws", "blocked")
    assert evt.session_hash == token_hash(token) and evt.ip == "198.51.100.77" and evt.user_agent == "pytest-ws"
    assert threat_tracker.score("198.51.100.77") > 0
    assert not hub.is_online(user.id)

    big = FakeWS(token)
    task = await open_ws(big)
    big.text("x" * (core_router.WS_MAX_MESSAGE_CHARS + 1))
    await asyncio.wait_for(task, 3)
    assert big.closed == 4429 and len([e for e in security_events.events if e.detail == "websocket flood"]) == 2


async def test_ws_per_user_socket_cap(db, spy):
    user = await make_customer(db)
    queues = [hub.subscribe(user.id) for _ in range(core_router.WS_MAX_SOCKETS_PER_USER)]
    try:
        ws = FakeWS(await new_session(user))
        task = await open_ws(ws)
        await asyncio.wait_for(task, 3)
        assert ws.accepted and ws.closed == 4429 and ws.sent == []
        assert hub.connection_count(user.id) == core_router.WS_MAX_SOCKETS_PER_USER
        assert not presence.is_connected(user.id)
    finally:
        for q in queues:
            hub.unsubscribe(user.id, q)


async def test_ws_without_session_or_from_a_foreign_origin_is_refused(db):
    ws = FakeWS(None)
    await asyncio.wait_for(core_router.websocket_endpoint(ws), 3)  # type: ignore[arg-type]
    assert ws.closed == 4401 and ws.sent == []
    user = await make_customer(db)
    evil = FakeWS(await new_session(user), origin="https://evil.example")
    await asyncio.wait_for(core_router.websocket_endpoint(evil), 3)  # type: ignore[arg-type]
    assert evil.closed == 4403 and evil.sent == []


async def test_close_sessions_closes_only_that_sessions_sockets(db, spy, grace):
    user = await make_customer(db)
    t1, t2 = await new_session(user), await new_session(user)
    ws1, ws2 = FakeWS(t1, page=PAGE_A), FakeWS(t2, page=PAGE_B)
    task1, task2 = await open_ws(ws1), await open_ws(ws2)
    assert hub.close_sessions([token_hash(t1)]) == 1
    await asyncio.wait_for(task1, 3)
    assert ws1.closed == 4401 and ws2.closed is None and not task2.done()
    assert hub.connection_count(user.id) == 1 and presence.is_connected(user.id)
    assert hub.close_sessions([None, ""]) == 0
    ws2.bye(1001)
    await asyncio.wait_for(task2, 3)
    assert not presence.is_connected(user.id)


async def test_close_sessions_travels_through_the_pg_bridge(monkeypatch):
    sent: list[tuple[str, dict[str, Any]]] = []

    class FakeBridge:
        async def execute(self, sql: str, channel: str, payload: str) -> None:
            sent.append((channel, json.loads(payload)))

    monkeypatch.setattr(hub, "_bridge_conn", FakeBridge())
    local = hub.subscribe(77, session_hash="abc")
    assert hub.close_sessions(["abc"]) == 1 and local.get_nowait() == CloseRequest(4401, "signed out")
    await wait_until(lambda: bool(sent))
    channel, payload = sent[0]
    assert channel == "ironpulse_rt" and payload["ctl"] == {"op": "close_sessions", "sessions": ["abc"], "code": 4401,
                                                           "reason": "signed out"}
    # the other processes close their own sockets of that session when the notification arrives
    remote = hub.subscribe(78, session_hash="def")
    hub._on_control({"op": "close_sessions", "sessions": ["def"], "code": 4401})
    assert remote.get_nowait() == CloseRequest(4401, "signed out")
    hub._on_control({"op": "something-else"})
    hub._on_control("garbage")
    assert remote.empty()
    live = hub.subscribe(78, session_hash="still-signed-in")
    await hub.publish([78], {"type": "notification", "title": "x"})  # events still use the bridge as before ...
    assert sent[-1][1]["event"]["type"] == "notification" and live.get_nowait()["title"] == "x"
    assert remote.empty()  # ... and never reach a socket that is closing


# ============================================================================================ after-commit hooks
async def test_after_commit_callbacks_wait_for_the_real_commit(db):
    calls: list[str] = []
    user = await make_customer(db)
    call_after_commit(db, lambda: calls.append("outer"))
    async with db.begin_nested():
        call_after_commit(db, lambda: calls.append("inside-released-savepoint"))
    assert calls == []  # releasing a SAVEPOINT is not the commit
    try:
        async with db.begin_nested():
            call_after_commit(db, lambda: calls.append("inside-rolled-back-savepoint"))
            raise ValueError
    except ValueError:
        pass
    await db.execute(select(UserSession.id).where(UserSession.user_id == user.id))
    await db.commit()
    assert calls == ["outer", "inside-released-savepoint"]
    await db.execute(select(UserSession.id).limit(1))  # a transaction is open (as when a route revokes sessions)
    call_after_commit(db, lambda: calls.append("rolled-back"))
    await db.rollback()
    await db.commit()
    assert calls == ["outer", "inside-released-savepoint"]
    call_after_commit(db, lambda: (_ for _ in ()).throw(RuntimeError("boom")))  # a failing callback never breaks a commit
    call_after_commit(db, lambda: calls.append("after-failure"))
    await db.execute(select(UserSession.id).limit(1))
    await db.commit()
    assert calls[-1] == "after-failure"


def test_presence_module_has_no_import_cycles():
    assert presence_mod.presence is presence and callable(presence.begin_shutdown)


async def test_lifespan_marks_the_shutdown_before_stopping_the_worker(monkeypatch):
    import app.db as dbm
    from app.main import app as fastapi_app
    from app.main import lifespan
    from app.services.worker import runner

    order: list[tuple[str, Any]] = []

    async def fake_stop() -> None:
        order.append(("runner.stop", presence.snapshot()["shutting_down"]))

    async def no_dispose() -> None:  # keep the test engine
        order.append(("dispose_engine", None))

    monkeypatch.setattr(runner, "stop", fake_stop)
    monkeypatch.setattr(dbm, "dispose_engine", no_dispose)
    async with lifespan(fastapi_app):
        assert not presence.snapshot()["shutting_down"]
    assert order == [("runner.stop", True), ("dispose_engine", None)]
