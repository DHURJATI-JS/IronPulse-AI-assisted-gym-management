"""Presence hardening (docs/ai-guard-and-queue.md sections 5, 6 and 10) - regression tests for:

* a job the runner parks because its requester looked away, while they come back at that moment, is resumed - never
  left PAUSED for someone who is present (socket or HTTP return, one process or another one);
* someone else signing in on the same browser ends the previous session: its sockets close (4401) and its AI work
  stops, and with nothing of that user left it is their exit;
* several workers without Redis fail safe: a process that cannot see the others never treats a closed tab as an exit
  (or parks a job on a mark that may be stale), and says so at startup;
* sign-out is immediate even when a tab of that session holds its socket in another process, and the other processes
  stop that session's AI work as soon as the PG bridge tells them;
* a close request can never be pushed out of a slow socket's drop-oldest queue;
* the 'Paused while you were away - resuming now' note reaches the page whichever way the return was noticed.

"Another process" is a second ``Presence`` (and its own view of Redis); the AI queue and the hub are shared in a test.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import update

from app import db as dbmod
from app.ai.errors import AICancelled
from app.ai.owner import AIOwner
from app.config import get_settings
from app.core.utils import utcnow
from app.models import CustomerProfile, UserSession
from app.models.enums import JobStatus, TokenPurpose
from app.routers import core as core_router
from app.security.crypto import token_hash
from app.security.sessions import revoke_session, session_cookie_name
from app.services import auth_flows as af
from app.services import jobs as J
from app.services import presence as presence_mod
from app.services.presence import Presence, presence, redis_namespace
from app.services.realtime import CloseRequest, hub
from tests.conftest import login_as, post_form, run_jobs
from tests.factories import make_customer, make_owner
from tests.test_presence import (
    PAGE_A,
    PAGE_B,
    FakeRedis,
    FakeWS,
    SchedulerSpy,
    add_job,
    job,
    new_session,
    open_ws,
    wait_until,
)

GRACE = 0.2
STRONG = "Gl0wing-Kettlebell!"
QUEUED, RUNNING, PAUSED = JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.PAUSED


# ============================================================================================ fixtures / helpers
async def _no_redis_here() -> None:
    return None


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    monkeypatch.setattr(presence, "_redis", _no_redis_here)


@pytest.fixture
def fake_redis(monkeypatch):
    fake = FakeRedis()

    async def get() -> FakeRedis:
        return fake

    monkeypatch.setattr(presence, "_redis", get)
    return fake


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


@pytest.fixture
def several_workers(monkeypatch):
    """``manage.py run --workers N``: several processes share the users (live events over the PG bridge)."""
    monkeypatch.setattr(get_settings(), "realtime_pg_bridge", True)


@pytest.fixture
def registry():
    saved: dict[str, tuple[Any, Any]] = {}

    def register(kind: str, fn: Callable[..., Any], *, on_exit: str | None = None) -> None:
        saved.setdefault(kind, (J._handlers.get(kind), J._exit_policies.get(kind)))
        J.job_handler(kind, on_exit=on_exit)(fn)

    yield register
    for kind, (handler, policy) in saved.items():
        if handler is None:
            J._handlers.pop(kind, None)
            J._exit_policies.pop(kind, None)
        else:
            J._handlers[kind], J._exit_policies[kind] = handler, policy


def other_process(monkeypatch, redis: FakeRedis | None = None) -> Presence:
    """A second worker process: its own sockets, timers and away marks; Redis shared with this one (or none)."""
    worker = Presence()

    async def get() -> FakeRedis | None:
        return redis

    monkeypatch.setattr(worker, "_redis", get)
    return worker


def task_updates(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in events if isinstance(e, dict) and e.get("type") == "task.update"]


def drain_queue(q: asyncio.Queue) -> list[Any]:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


async def revoke_in_db(session_hash: str) -> None:
    async with dbmod.get_sessionmaker()() as s:
        await s.execute(update(UserSession).where(UserSession.id == session_hash).values(revoked_at=utcnow()))
        await s.commit()


async def enqueue_for(kind: str, user_id: int, payload: dict[str, Any]) -> int:
    async with dbmod.get_sessionmaker()() as s:
        new = await J.enqueue(s, kind, payload, user_id=user_id)
        await s.flush()
        job_id = new.id
        await s.commit()
    return job_id


async def close_ws(ws: FakeWS, task: asyncio.Task, code: int = 1001) -> None:
    if not task.done():
        ws.bye(code)
        await asyncio.wait_for(task, 3)


# ======================================================================================== the runner's park vs a return
@pytest.mark.parametrize("returns", ["socket", "request", "socket-elsewhere"])
async def test_a_return_while_the_runner_parks_the_job_resumes_it(db, registry, monkeypatch, returns):
    """The runner saw 'away' and is writing PAUSED (here: behind the watchdog's heartbeat, which holds the job-row
    write lock) when the member comes back. Their return's resume ran while the row was still RUNNING and matched
    nothing - the runner must notice they are back once its PAUSED row is written."""
    redis = FakeRedis() if returns != "socket" else None
    if redis is not None:
        async def get() -> FakeRedis:
            return redis

        monkeypatch.setattr(presence, "_redis", get)
    user = await make_customer(db)
    ran: list[dict[str, Any]] = []

    async def handler(db: Any, payload: dict[str, Any]) -> None:
        ran.append(payload)

    registry("t_park_race", handler, on_exit="pause")
    await presence.on_left(user.id)  # they left ...
    assert presence.is_away(user.id)
    job_id = await enqueue_for("t_park_race", user.id, {"n": 1})  # ... and a job of theirs comes due (a retry ...)

    lock = J._rt.write_lock()
    looked, release = asyncio.Event(), asyncio.Event()
    real_away_now = presence.away_now

    async def away_now(uid: int) -> bool:
        verdict = await real_away_now(uid)
        if uid == user.id:
            looked.set()
        return verdict

    async def heartbeat() -> None:  # the watchdog's heartbeat holds the write lock (up to 2 s per locked row)
        await looked.wait()
        async with lock:
            await release.wait()

    monkeypatch.setattr(presence, "away_now", away_now)
    holder = asyncio.create_task(heartbeat())
    runner = asyncio.create_task(run_jobs())
    try:
        await asyncio.wait_for(looked.wait(), 5)  # the runner's verdict: away -> park it
        await wait_until(lock.locked)
        await asyncio.sleep(0.05)  # its PAUSED write waits behind the heartbeat
        assert (await job(job_id)).status == RUNNING
        # ... and right now the member comes back
        if returns == "socket":
            presence.connected(user.id, conn_id="c1", page_id=PAGE_A, session_hash="s1")
            await presence.drain()
        elif returns == "request":
            presence.touch(user.id)
            await presence.drain()
        else:  # their next page's socket lands in another worker process (Redis shared)
            elsewhere = other_process(monkeypatch, redis)
            elsewhere.connected(user.id, conn_id="c1", page_id=PAGE_A, session_hash="s1")
            await elsewhere.drain()
        assert not await real_away_now(user.id)
        assert (await job(job_id)).status == RUNNING  # their return resumed nothing: the row was not PAUSED yet
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(runner, holder), 10)
    await presence.drain()
    row = await job(job_id)
    assert (row.status, row.paused_at) == (QUEUED, None)  # not left PAUSED for someone who is here
    assert await run_jobs() == 1 and ran == [{"n": 1}]


async def test_a_parked_job_stays_paused_while_its_requester_is_still_away(db, registry):
    ran: list[dict[str, Any]] = []

    async def handler(db: Any, payload: dict[str, Any]) -> None:
        ran.append(payload)

    registry("t_park_away", handler, on_exit="pause")
    user = await make_customer(db)
    await presence.on_left(user.id)
    job_id = await enqueue_for("t_park_away", user.id, {"n": 1})
    assert await run_jobs() == 1
    await presence.drain()
    row = await job(job_id)
    assert (row.status, row.cancel_reason) == (PAUSED, "away") and ran == []
    assert await run_jobs() == 0


# ============================================================================================ sign-in on a used browser
async def _invited_member(db: Any, owner: Any) -> str:
    member = await make_customer(db, email="invited@example.com", password=None, full_name="Dev Malhotra")
    prof = await db.get(CustomerProfile, member.id)
    prof.registered_by_id = owner.id
    await db.commit()
    raw = await af.issue_token(db, TokenPurpose.INVITE, user_id=member.id, email=member.email)
    await db.commit()
    return raw


async def test_someone_else_signing_in_on_this_browser_ends_the_previous_session(client, db, spy):
    owner = await make_owner(db, full_name="Rhea Kapoor")
    owner_id = owner.id
    raw = await _invited_member(db, owner)
    plan = await add_job(owner_id, "pause")
    await login_as(client, owner)  # the front-desk browser is signed in as the owner ...
    token = client.cookies.get(session_cookie_name())
    ws = FakeWS(token, page=PAGE_A)  # ... and one of its tabs holds a live socket
    task = await open_ws(ws)
    try:
        # the new member activates their invite in another tab of the same browser
        r = await post_form(client, "/auth/invite", {"token": raw, "password": STRONG, "password_confirm": STRONG,
                                                     "accept_terms": "1"})
        assert r.status_code == 303 and r.headers["location"] == "/me"
        await asyncio.wait_for(task, 3)  # the owner's socket is closed: signed out, no reconnect
        assert ws.closed == 4401
        await presence.drain()
        assert not hub.is_online(owner_id)
        assert presence.is_away(owner_id)  # nothing of the owner is left: their exit, right away
        row = await job(plan)
        assert (row.status, row.cancel_reason) == (PAUSED, "signed_out")
        assert {"user_id": owner_id, "session_hash": token_hash(token), "kinds": {"user"},
                "reason": "signed_out"} in spy.calls
        await hub.publish([owner_id], {"type": "notification", "title": "owner-only"})
        assert not any(e.get("title") == "owner-only" for e in ws.sent)
    finally:
        await close_ws(ws, task)


async def test_the_previous_users_other_devices_keep_them_present(client, db, spy):
    owner = await make_owner(db, full_name="Rhea Kapoor")
    owner_id = owner.id
    raw = await _invited_member(db, owner)
    plan = await add_job(owner_id, "pause")
    await new_session(owner)  # the owner is also signed in on their phone
    await login_as(client, owner)
    token = client.cookies.get(session_cookie_name())
    q = hub.subscribe(owner_id, session_hash=token_hash(token), page_id=PAGE_A, conn_id="c1")
    presence.connected(owner_id, conn_id="c1", page_id=PAGE_A, session_hash=token_hash(token))
    try:
        r = await post_form(client, "/auth/invite", {"token": raw, "password": STRONG, "password_confirm": STRONG,
                                                     "accept_terms": "1"})
        assert r.status_code == 303
        assert drain_queue(q) == [CloseRequest(4401, "signed out")]  # this browser's session is gone ...
        await presence.drain()
        assert not presence.is_away(owner_id) and (await job(plan)).status == QUEUED  # ... the owner is not
        assert spy.reasons() == ["signed_out"]  # only that session's AI work stopped
    finally:
        hub.unsubscribe(owner_id, q)


async def test_revoke_session_ends_it_only_after_commit_and_sign_out_is_not_ended_twice(client, db, spy):
    user = await make_customer(db)
    uid = user.id
    token = await new_session(user)
    q = hub.subscribe(uid, session_hash=token_hash(token), page_id=PAGE_A, conn_id="c1")
    try:
        await revoke_session(db, token)
        await db.rollback()  # rolled back: nothing ends
        assert q.empty() and spy.calls == []
        await revoke_session(db, token)
        assert q.empty()  # not before the COMMIT
        await db.commit()
        assert drain_queue(q) == [CloseRequest(4401, "signed out")]
        await presence.drain()
        assert presence.is_away(uid) and spy.reasons() == ["signed_out", "signed_out"]
    finally:
        hub.unsubscribe(uid, q)

    other = await make_customer(db)
    await login_as(client, other)
    assert (await post_form(client, "/auth/logout", {})).status_code == 303
    await presence.drain()
    assert presence.is_away(other.id)
    assert [c["reason"] for c in spy.calls if c.get("user_id") == other.id] == ["logout", "logout"]  # one exit
    assert presence.snapshot()["stats"]["exits"] == 2 and presence.snapshot()["stats"]["logouts"] == 1


# ============================================================================================ several workers, no Redis
async def test_several_workers_without_redis_never_turn_a_navigation_into_an_exit(db, grace, spy, several_workers,
                                                                                   monkeypatch):
    user = await make_customer(db)
    pending = await add_job(user.id, "pause")
    worker1, worker2 = presence, other_process(monkeypatch)
    worker1.connected(user.id, conn_id="a", page_id=PAGE_A, session_hash="s1")
    await worker1.drain()
    worker1.disconnected(user.id, conn_id="a", page_id=PAGE_A, code=4001)  # a click: page A closes on worker 1 ...
    worker2.connected(user.id, conn_id="b", page_id=PAGE_B, session_hash="s1")  # ... page B's socket is on worker 2
    await worker2.drain()
    await asyncio.sleep(GRACE * 1.5)
    await wait_until(lambda: not worker1.snapshot()["grace_timers"], timeout=5)
    await worker1.drain()
    assert not worker1.is_away(user.id) and "left" not in spy.reasons()  # page A's own AI work still stopped (4001)
    assert (await job(pending)).status == QUEUED
    assert worker1.snapshot()["stats"]["kept_unseen"] == 1


async def test_several_workers_fail_safe_while_redis_is_down(db, grace, spy, several_workers, fake_redis):
    user = await make_customer(db)
    pending = await add_job(user.id, "pause")
    presence.connected(user.id, conn_id="a", page_id=PAGE_A, session_hash="s1")
    await presence.drain()
    fake_redis.broken = True  # the leases can't be read: another worker may hold their next page
    presence.disconnected(user.id, conn_id="a", page_id=PAGE_A, code=1001)
    await asyncio.sleep(GRACE * 1.5)
    await wait_until(lambda: not presence.snapshot()["grace_timers"], timeout=5)
    await presence.drain()
    assert not presence.is_away(user.id) and spy.calls == [] and (await job(pending)).status == QUEUED


async def test_sign_out_stays_an_immediate_exit_with_several_workers_and_no_redis(client, db, several_workers):
    user = await make_customer(db)
    pending = await add_job(user.id, "pause")
    await login_as(client, user)
    assert (await post_form(client, "/auth/logout", {})).status_code == 303  # the database knows every session
    assert presence.is_away(user.id)
    assert (await job(pending)).status == PAUSED


async def test_several_workers_without_redis_never_park_a_job_on_a_stale_away_mark(db, registry, several_workers):
    ran: list[dict[str, Any]] = []

    async def handler(db: Any, payload: dict[str, Any]) -> None:
        ran.append(payload)

    registry("t_blind_park", handler, on_exit="pause")
    user = await make_customer(db)
    await presence.sessions_ended(user.id, ["laptop"], reason="logout")  # they signed out (served by this worker)
    assert presence.is_away(user.id)
    first = await enqueue_for("t_blind_park", user.id, {"n": 1})
    assert await run_jobs() == 1 and ran == []  # signed in nowhere: certainly away, parked
    assert (await job(first)).status == PAUSED
    await new_session(user)  # they sign in again - through another worker, which this one cannot see
    second = await enqueue_for("t_blind_park", user.id, {"n": 2})
    assert await run_jobs() == 1 and ran == [{"n": 2}]  # this worker's mark may be stale: never park on it
    assert (await job(second)).status == JobStatus.DONE


async def test_the_startup_check_says_when_workers_cannot_see_each_other(several_workers, fake_redis, caplog,
                                                                         monkeypatch):
    with caplog.at_level(logging.WARNING, logger="ironpulse.presence"):
        assert await presence.startup_check() is True  # Redis answers: presence is shared
    assert "REDIS_URL" not in caplog.text

    monkeypatch.setattr(presence, "_redis", _no_redis_here)
    calls: list[str] = []

    class FakeListener:
        async def add_listener(self, channel: str, fn: Any) -> None:
            calls.append(channel)

        async def close(self) -> None:
            calls.append("closed")

    async def connect(dsn: str) -> FakeListener:
        return FakeListener()

    import asyncpg

    monkeypatch.setattr(asyncpg, "connect", connect)
    caplog.clear()
    try:
        with caplog.at_level(logging.WARNING, logger="ironpulse.presence"):
            await hub.start_bridge("postgresql+asyncpg://u:p@localhost/x")  # what every worker's lifespan does
    finally:
        await hub.stop_bridge()
    assert calls == ["ironpulse_rt", "closed"]
    warning = " ".join(r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    assert "REDIS_URL" in warning and "sign-out" in warning


async def test_one_process_without_redis_needs_no_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="ironpulse.presence"):
        assert await presence.startup_check() is True
    assert caplog.records == []


# ============================================================================================ sign-out across workers
async def test_sign_out_is_immediate_when_a_tab_of_that_session_is_on_another_worker(client, db, fake_redis, spy,
                                                                                     monkeypatch):
    user = await make_customer(db)
    pending = await add_job(user.id, "pause")
    await login_as(client, user)
    h = token_hash(client.cookies.get(session_cookie_name()))
    worker2 = other_process(monkeypatch, fake_redis)
    presence.connected(user.id, conn_id="a", page_id=PAGE_A, session_hash=h)  # tab A on this worker
    worker2.connected(user.id, conn_id="b", page_id=PAGE_B, session_hash=h)  # tab B (same session) on worker 2
    await presence.drain()
    await worker2.drain()
    assert (await post_form(client, "/auth/logout", {})).status_code == 303  # signed out from tab A
    assert presence.is_away(user.id)  # tab B's lease belongs to the signed-out session: no presence
    row = await job(pending)
    assert (row.status, row.cancel_reason) == (PAUSED, "logout")
    assert f"{redis_namespace()}:away:{user.id}" in fake_redis.strings


async def test_a_sign_out_elsewhere_stops_that_sessions_ai_work_here_at_once(db, monkeypatch):
    user = await make_customer(db)
    uid = user.id
    h = token_hash(await new_session(user))
    await revoke_in_db(h)  # another worker served the sign-out and committed it ...
    worker2 = other_process(monkeypatch)
    monkeypatch.setattr(presence_mod, "presence", worker2)  # ... this process is worker 2
    q = hub.subscribe(uid, session_hash=h, page_id=PAGE_B, conn_id="b")
    worker2.connected(uid, conn_id="b", page_id=PAGE_B, session_hash=h)
    monkeypatch.setattr(get_settings(), "ai_queue_concurrency", 1)
    from app.ai.scheduler import scheduler

    blocker = await scheduler.acquire(None, "assistant")
    waiter = asyncio.create_task(scheduler.acquire(AIOwner(user_id=uid, role="customer", session_hash=h,
                                                           page_id=PAGE_B), "assistant"))
    try:
        await asyncio.sleep(0.05)  # tab B's question waits in the queue here
        # the PG bridge delivers the sign-out: close that session's sockets here ...
        hub._on_control({"op": "close_sessions", "sessions": [h], "code": 4401, "reason": "signed out",
                         "user_id": uid, "exit": "logout"})
        assert drain_queue(q) == [CloseRequest(4401, "signed out")]
        with pytest.raises(AICancelled) as stopped:  # ... and stop its AI work here, now - not 45 s later
            await asyncio.wait_for(waiter, 2)
        assert stopped.value.reason == "logout"
        await worker2.drain()
        assert worker2.is_away(uid)  # nothing of theirs is left: an exit here too
        worker2.disconnected(uid, conn_id="b", page_id=PAGE_B, code=1005)  # that socket closing is no "drop"
        assert worker2.snapshot()["grace_timers"] == 0
    finally:
        scheduler.release(blocker, outcome="done")
        hub.unsubscribe(uid, q)


async def test_a_bridge_close_for_sessions_unknown_here_does_nothing(db, monkeypatch):
    worker2 = other_process(monkeypatch)
    monkeypatch.setattr(presence_mod, "presence", worker2)
    hub._on_control({"op": "close_sessions", "sessions": ["nobody-here"], "code": 4401, "user_id": 424242})
    hub._on_control({"op": "close_sessions", "sessions": ["x"], "user_id": "not-a-number"})
    hub._on_control({"op": "close_sessions", "sessions": ["x"], "user_id": True})
    await worker2.drain()
    assert worker2.snapshot()["stats"].get("exits") is None and not worker2.is_away(424242)


async def test_sign_out_tells_the_other_processes_who_and_why(monkeypatch):
    sent: list[dict[str, Any]] = []

    class FakeBridge:
        async def execute(self, sql: str, channel: str, payload: str) -> None:
            sent.append(json.loads(payload))

    monkeypatch.setattr(hub, "_bridge_conn", FakeBridge())
    assert hub.close_sessions(["abc"], user_id=7, exit_reason="logout") == 0
    await wait_until(lambda: bool(sent))
    assert sent[0]["ctl"] == {"op": "close_sessions", "sessions": ["abc"], "code": 4401, "reason": "signed out",
                              "user_id": 7, "exit": "logout"}


async def test_cancel_ai_reaches_every_process(monkeypatch, spy):
    sent: list[dict[str, Any]] = []

    class FakeBridge:
        async def execute(self, sql: str, channel: str, payload: str) -> None:
            sent.append(json.loads(payload))

    monkeypatch.setattr(hub, "_bridge_conn", FakeBridge())
    hub.cancel_ai(user_id=9, reason="account_deleted", policies={"keep", "pause", "cancel"})  # e.g. account deletion
    assert spy.calls == [{"user_id": 9, "reason": "account_deleted", "policies": {"keep", "pause", "cancel"}}]
    await wait_until(lambda: bool(sent))
    assert sent[0]["ctl"] == {"op": "cancel_ai", "user_id": 9, "reason": "account_deleted",
                              "policies": ["cancel", "keep", "pause"]}
    hub._on_control(sent[0]["ctl"])  # what another process does with it
    assert spy.calls[-1] == {"user_id": 9, "reason": "account_deleted", "policies": {"keep", "pause", "cancel"}}
    hub._on_control({"op": "cancel_ai", "user_id": 9, "reason": "x", "policies": ["sometimes"]})  # malformed: ignored
    hub._on_control({"op": "cancel_ai", "user_id": None, "reason": "x"})
    assert len(spy.calls) == 2


# ============================================================================================ close requests are kept
class StallingWS(FakeWS):
    """A client that stops reading: ``send_json`` blocks (TCP back-pressure) until ``reading`` is set again."""

    def __init__(self, token: str | None, **kwargs: Any) -> None:
        super().__init__(token, **kwargs)
        self.reading = asyncio.Event()
        self.reading.set()

    async def send_json(self, data: Any) -> None:
        await self.reading.wait()
        await super().send_json(data)


async def test_a_close_request_is_never_pushed_out_of_a_stalled_sockets_queue(db):
    victim = await make_customer(db)
    stolen = await new_session(victim)  # e.g. a cookie taken from the victim's browser
    ws = StallingWS(stolen)
    task = await open_ws(ws)
    try:
        ws.reading.clear()  # the client stops reading
        await hub.publish([victim.id], {"type": "notification", "title": "in flight"})  # the sender blocks on it
        await asyncio.sleep(0.05)
        for n in range(300):  # the queue fills with the account's events
            await hub.publish([victim.id], {"type": "notification", "title": f"before {n}"})
        assert hub.close_sessions([token_hash(stolen)]) == 1  # the victim signs out other devices
        for n in range(600):  # the account keeps living
            await hub.publish([victim.id], {"type": "notification", "title": f"after {n}"})
        await hub.publish([victim.id], {"type": "chat.message", "body": "private message"})
        assert hub.close_sessions([token_hash(stolen)]) == 0  # already closing
        ws.reading.set()  # the client reads again
        await asyncio.wait_for(task, 3)
        assert ws.closed == 4401
        assert [e.get("title") for e in ws.sent if e.get("type") != "hello"] == ["in flight"]
    finally:
        await close_ws(ws, task)


async def test_a_closing_socket_gets_no_more_events():
    q = hub.subscribe(31, session_hash="gone", conn_id="c1")
    keep = hub.subscribe(31, session_hash="kept", conn_id="c2")
    try:
        for n in range(5):
            await hub.publish([31], {"type": "notification", "n": n})
        assert hub.close_sessions(["gone"], code=4401, reason="signed out") == 1
        await hub.publish([31], {"type": "notification", "n": 99})
        assert drain_queue(q) == [CloseRequest(4401, "signed out")]  # events still waiting for it are dropped too
        assert [e["n"] for e in drain_queue(keep)] == [0, 1, 2, 3, 4, 99]
    finally:
        hub.unsubscribe(31, q)
        hub.unsubscribe(31, keep)


# ============================================================================================ the "resuming now" note
@pytest.mark.parametrize("where", ["no-redis", "redis", "redis-other-process"])
async def test_the_resumed_note_reaches_the_page_opened_by_an_http_return(client, db, monkeypatch, where):
    redis = FakeRedis() if where != "no-redis" else None
    if redis is not None:
        async def get() -> FakeRedis:
            return redis

        monkeypatch.setattr(presence, "_redis", get)
    user = await make_customer(db)
    plan = await add_job(user.id, "pause", kind="coach_plan")
    await presence.on_left(user.id)
    assert (await job(plan)).status == PAUSED
    await login_as(client, user)
    token = client.cookies.get(session_cookie_name())
    assert (await client.get("/api/ai/status")).status_code == 200  # back: the browser loads a page first ...
    await presence.drain()
    assert (await job(plan)).status == QUEUED  # ... and that request already resumed the plan
    if where == "redis-other-process":  # the page's socket lands in another worker process
        monkeypatch.setattr(core_router, "presence", other_process(monkeypatch, redis))
    ws = FakeWS(token, page=PAGE_A)  # ... then the page's socket connects
    task = await open_ws(ws)
    try:
        await core_router.presence.drain()
        await wait_until(lambda: bool(task_updates(ws.sent)), timeout=2)
        [note] = task_updates(ws.sent)
        assert (note["task_id"], note["kind"], note["status"], note["resumed"]) == (plan, "coach_plan", "queued", True)
        ws2 = FakeWS(token, page=PAGE_B)  # kept for the next socket once, not repeated by every page
        task2 = await open_ws(ws2)
        await core_router.presence.drain()
        await asyncio.sleep(0.05)
        assert task_updates(ws2.sent) == []
        await close_ws(ws2, task2)
    finally:
        await close_ws(ws, task)
    if redis is not None:
        assert not redis.lists  # nothing left behind


async def test_the_resumed_note_is_not_kept_forever(client, db, monkeypatch):
    monkeypatch.setattr(presence_mod, "NOTE_KEEP_SECONDS", 0.05)
    user = await make_customer(db)
    await add_job(user.id, "pause", kind="coach_plan")
    await presence.on_left(user.id)
    await login_as(client, user)
    assert (await client.get("/api/ai/status")).status_code == 200
    await presence.drain()
    await asyncio.sleep(0.1)  # the page never opened a socket in time
    ws = FakeWS(client.cookies.get(session_cookie_name()), page=PAGE_A)
    task = await open_ws(ws)
    try:
        await presence.drain()
        await asyncio.sleep(0.05)
        assert task_updates(ws.sent) == []
    finally:
        await close_ws(ws, task)


async def test_a_socket_first_return_announces_the_resume_once(client, db):
    user = await make_customer(db)
    plan = await add_job(user.id, "pause", kind="coach_plan")
    await presence.on_left(user.id)
    await login_as(client, user)
    ws = FakeWS(client.cookies.get(session_cookie_name()), page=PAGE_A)  # e.g. a back/forward-cache restore
    task = await open_ws(ws)
    try:
        await presence.drain()
        await wait_until(lambda: bool(task_updates(ws.sent)), timeout=2)
        assert (await client.get("/api/ai/status")).status_code == 200
        await presence.drain()
        await asyncio.sleep(0.05)
        assert [(n["task_id"], n.get("resumed")) for n in task_updates(ws.sent)] == [(plan, True)]
    finally:
        await close_ws(ws, task)


async def test_a_socket_that_connects_during_an_http_return_takes_its_note_with_it(db, grace, monkeypatch):
    """The page's socket connects while the request's return is still resuming: the note kept afterwards belongs to
    that page (it may get it twice - IP.ai ignores a repeat), never to the next page a minute later."""
    user = await make_customer(db)
    plan = await add_job(user.id, "pause", kind="coach_plan")
    await presence.on_left(user.id)
    real_resume = J.resume_user_jobs
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_resume(user_id: int, **kwargs: Any) -> int:
        entered.set()
        await release.wait()
        return await real_resume(user_id, **kwargs)

    monkeypatch.setattr(J, "resume_user_jobs", slow_resume)
    presence.touch(user.id)  # back: their request starts the resume ...
    await asyncio.wait_for(entered.wait(), 3)
    page_a = hub.subscribe(user.id, session_hash="s1", page_id=PAGE_A, conn_id="a")
    presence.connected(user.id, conn_id="a", page_id=PAGE_A, session_hash="s1")  # ... and the page's socket arrives
    release.set()
    await presence.drain()
    await asyncio.sleep(0.05)
    assert (await job(plan)).status == QUEUED
    assert {n["task_id"] for n in task_updates(drain_queue(page_a))} == {plan}
    hub.unsubscribe(user.id, page_a)
    presence.disconnected(user.id, conn_id="a", page_id=PAGE_A, code=1001)  # a navigation to the next page
    page_b = hub.subscribe(user.id, session_hash="s1", page_id=PAGE_B, conn_id="b")
    try:
        presence.connected(user.id, conn_id="b", page_id=PAGE_B, session_hash="s1")
        await presence.drain()
        await asyncio.sleep(0.05)
        assert task_updates(drain_queue(page_b)) == []  # no stale 'resuming now' on it
    finally:
        hub.unsubscribe(user.id, page_b)


async def test_the_runners_late_resume_is_kept_for_the_next_socket_too(db, registry, monkeypatch):
    """The park race again, with an HTTP return: the runner's own resume happens while no socket of theirs is
    connected yet, so its note must wait for the page's socket like any other."""

    async def handler(db: Any, payload: dict[str, Any]) -> None:
        return None

    registry("coach_plan", handler, on_exit="pause")
    user = await make_customer(db)
    await presence.on_left(user.id)
    job_id = await enqueue_for("coach_plan", user.id, {"n": 1})
    real_away_now = presence.away_now
    first = True

    async def away_now(uid: int) -> bool:
        nonlocal first
        verdict = await real_away_now(uid)
        if first:
            first = False
            presence.touch(uid)  # their request lands right after the runner's verdict
            await presence.drain()
        return verdict

    monkeypatch.setattr(presence, "away_now", away_now)
    assert await run_jobs() == 1
    await presence.drain()
    assert (await job(job_id)).status == QUEUED
    q = hub.subscribe(user.id, session_hash="s1", page_id=PAGE_A, conn_id="c1")
    try:
        presence.connected(user.id, conn_id="c1", page_id=PAGE_A, session_hash="s1")
        await presence.drain()
        await asyncio.sleep(0.05)
        notes = task_updates(drain_queue(q))
        assert [(n["task_id"], n["status"], n.get("resumed")) for n in notes] == [(job_id, "queued", True)]
    finally:
        hub.unsubscribe(user.id, q)


def test_note_keys_live_in_the_presence_namespace():
    assert presence._notes_key(5) == f"{redis_namespace()}:notes:5"  # removed with the rest by presence.reset()
