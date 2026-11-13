"""End-to-end scenarios across the AI guard, the AI queue, the AI router, presence and background jobs
(docs/ai-guard-and-queue.md). Everything runs the real implementations together - the real guard on this test
database's Redis namespace (memory when Redis is away), the real fair queue, presence and job runner, and the real app
over httpx's ASGITransport - with only the AI provider faked.

(a) a signed-in member is refused by the guard over HTTP: 429/409/422 + ``code`` + ``Retry-After`` exactly as the
    contract's refusal JSON says (and the HTML error page for browsers) - the assistant endpoint included
(b) two users flooding the queue are served round-robin, each sees only their own tab's queue positions, and the
    flooder's extra requests are refused ('too_many'), earn strikes and finally pause their AI
(c) a user leaves (last socket closed, grace period over): their interactive AI call ends with AICancelled, their
    pause-policy job becomes PAUSED (the provider saw both cancellations), and on return the job is queued again and
    runs to DONE - the requester hears about it through ``task.update`` events
(d) closing one tab (close code 4001) stops only that tab's AI call; another tab's call of the same user continues
(e) logout stops that session's AI calls at once; the last session's logout is an exit right away
(f) jobs of a user run at BACKGROUND priority (behind interactive requests) and are never refused by the guard
(g) system work (no owner) bypasses the guard
plus: streams stop at once when their tab closes, a return over plain HTTP resumes paused jobs, GET /healthz.

``ASK`` is a route mounted for these tests only: it is what a migrated AI endpoint looks like (no fallback, so a
refusal reaches app/core/errors.py).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

import pytest
from fastapi import Request

from app import db as dbmod
from app.ai import policy
from app.ai import scheduler as scheduler_module
from app.ai.client import FakeProvider, ai, call_log_queue
from app.ai.errors import AICancelled, AIRejected
from app.ai.guard import guard
from app.ai.owner import OWNER_JOB, OWNER_USER, AIOwner, current_owner
from app.ai.scheduler import scheduler
from app.config import get_settings
from app.core.deps import AuthUser
from app.models import Job
from app.models.enums import JobStatus, Role
from app.security.crypto import token_hash
from app.security.sessions import session_cookie_name
from app.services import jobs as J
from app.services.presence import PAGE_CLOSED, presence
from app.services.realtime import hub
from tests.conftest import api, login_as, make_client, post_form, run_jobs
from tests.factories import make_customer, make_user

QUEUED, RUNNING, DONE, PAUSED = JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.DONE, JobStatus.PAUSED
PAGE_A = "tab-a-0001-integration"
PAGE_B = "tab-b-0002-integration"
ASK = "/api/integration/ask"  # JSON (wants_json: /api/ prefix)
ASK_PAGE = "/integration/ask"  # the same route outside /api: a browser gets the HTML error page
ANSWER = '{"reply": "AI answer", "actions": []}'
REFUSAL_KEYS = {"error", "detail", "code", "retry_after", "ai_available"}


# ============================================================================================ helpers / fixtures
async def wait_until(predicate: Callable[[], Any], *, timeout: float = 5.0, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.01)


async def ticks(n: int = 20) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


class Switchboard:
    """The fake provider's handler. Answers every call at once, except prompts containing a ``hold``-ed marker, which
    wait until released. Records (in provider order) every start, cancellation and finish, plus the AI owner and a
    queue snapshot seen by each call."""

    def __init__(self) -> None:
        self.started: list[str] = []
        self.cancelled: list[str] = []
        self.finished: list[str] = []
        self.owners: dict[str, AIOwner | None] = {}
        self.snapshots: dict[str, dict[str, Any]] = {}
        self._gates: dict[str, asyncio.Event] = {}

    def hold(self, *markers: str) -> None:
        for marker in markers:
            self._gates[marker] = asyncio.Event()

    def release(self, *markers: str) -> None:
        for marker in markers or tuple(self._gates):
            self._gates[marker].set()

    def seen(self, marker: str) -> bool:
        return any(marker in prompt for prompt in self.started)

    def prompt(self, marker: str) -> str:
        return next(p for p in self.started if marker in p)

    def was_cancelled(self, marker: str) -> bool:
        return any(marker in prompt for prompt in self.cancelled)

    async def __call__(self, **kwargs: Any) -> str:
        prompt = str(kwargs.get("prompt", ""))
        self.started.append(prompt)
        self.owners[prompt] = current_owner()  # the child task runs in the caller's context
        self.snapshots[prompt] = scheduler.snapshot()
        gate = next((g for marker, g in self._gates.items() if marker in prompt), None)
        try:
            if gate is not None:
                await gate.wait()
        except asyncio.CancelledError:
            self.cancelled.append(prompt)
            raise
        self.finished.append(prompt)
        return ANSWER


@pytest.fixture(autouse=True)
def _guard_on(monkeypatch):
    """The guard as configured by default (whatever the local .env says)."""
    monkeypatch.setattr(get_settings(), "ai_guard_enabled", True)
    monkeypatch.setattr(get_settings(), "ai_quota_scale", 1.0)


@pytest.fixture
def board():
    sb = Switchboard()
    ai.use_providers([FakeProvider("gateway", handler=sb, max_concurrency=16)])
    call_log_queue.clear()
    yield sb
    sb.release()  # nothing may stay blocked into the next test
    ai.use_providers(None)


@pytest.fixture
def grace(monkeypatch):
    monkeypatch.setattr(get_settings(), "presence_grace_seconds", 0.1)
    return 0.1


async def _ask(request: Request, user: AuthUser) -> dict[str, Any]:
    """A migrated AI endpoint: no fallback, so AIRejected / AICancelled reach the app's error handlers."""
    body = await request.json()
    message = str(body.get("message") or "")
    res = await ai.complete(feature=str(body.get("feature") or "assistant"), system="integration test",
                            prompt=f"{user.id}|{message}", user_text=message)
    owner = current_owner()
    return {"text": res.text, "page_id": owner.page_id if owner is not None else None}


@pytest.fixture
def strict_route(app):
    app.add_api_route(ASK, _ask, methods=["POST"])
    app.add_api_route(ASK_PAGE, _ask, methods=["POST"])
    added = [r for r in app.router.routes if getattr(r, "path", None) in (ASK, ASK_PAGE)]
    yield ASK
    for route in added:
        app.router.routes.remove(route)


@pytest.fixture
def job_kind():
    """Register job kinds for one test; earlier registrations are restored afterwards."""
    saved: list[tuple[str, Any, Any]] = []

    def register(kind: str, fn: Callable[..., Any], *, on_exit: str | None = "pause") -> str:
        saved.append((kind, J._handlers.get(kind), J._exit_policies.get(kind)))
        J.job_handler(kind, on_exit=on_exit)(fn)
        return kind

    yield register
    for kind, handler, exit_policy in reversed(saved):
        if handler is None:
            J._handlers.pop(kind, None)
        else:
            J._handlers[kind] = handler
        if exit_policy is None:
            J._exit_policies.pop(kind, None)
        else:
            J._exit_policies[kind] = exit_policy


def ai_job(results: list[str], feature: str = "coach") -> Callable[..., Any]:
    """A job handler that asks the AI (prompt ``job|<tag>``) and keeps the answer."""

    async def handler(db: Any, payload: dict[str, Any]) -> None:
        res = await ai.complete(feature=feature, system="background job", prompt=f"job|{payload['tag']}")
        results.append(res.text)

    return handler


async def enqueue_job(kind: str, user_id: int | None, tag: str) -> int:
    async with dbmod.get_sessionmaker()() as s:
        job = await J.enqueue(s, kind, {"tag": tag}, user_id=user_id, dedupe_key=f"{kind}:{tag}")
        await s.commit()
        return job.id


async def job_row(job_id: int) -> Job:
    async with dbmod.get_sessionmaker()() as s:
        row = await s.get(Job, job_id)
        assert row is not None
        return row


def received(q: asyncio.Queue, kind: str) -> list[dict[str, Any]]:
    out = []
    while not q.empty():
        evt = q.get_nowait()
        if isinstance(evt, dict) and evt.get("type") == kind:
            out.append(evt)
    return out


def member_minute_quota(group: str = "chat") -> int:
    return max(1, round(policy.QUOTAS["member"][group][0] * float(get_settings().ai_quota_scale)))


def session_hash_of(client: Any) -> str:
    return token_hash(client.cookies.get(session_cookie_name()))


def assert_refusal(resp: Any, status: int, code: str) -> dict[str, Any]:
    """The contract's refusal JSON (docs section 7) plus a matching Retry-After header."""
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert set(body) == REFUSAL_KEYS, body
    assert body["code"] == code and body["ai_available"] is True
    assert body["error"] == AIRejected.TITLES[code] and body["detail"]
    assert isinstance(body["retry_after"], int) and body["retry_after"] > 0
    assert resp.headers["Retry-After"] == str(body["retry_after"])
    return body


# ============================================================================================ (a) guard over HTTP
async def test_a_quota_then_cooldown_refusals_reach_the_client_as_contract_json(client, db, board, strict_route):
    user = await make_customer(db)  # no subscription: tier 'member'
    await login_as(client, user)
    minute = member_minute_quota("chat")

    for i in range(minute):
        resp = await api(client, "POST", ASK, json={"message": f"question number {i} about my training"})
        assert resp.status_code == 200 and resp.json()["text"] == ANSWER
    body = assert_refusal(await api(client, "POST", ASK, json={"message": "one question too many"}), 429, "quota")
    assert body["retry_after"] <= 60 and "minute" in body["detail"]
    assert len(board.started) == minute  # the refused request never reached a provider

    # hammering on: every refusal earns strikes, and soon AI is paused for the account
    codes = []
    for i in range(8):
        resp = await api(client, "POST", ASK, json={"message": f"hammering away, attempt {i}"})
        codes.append(resp.json().get("code"))
        if codes[-1] == "cooldown":
            break
    assert codes[-1] == "cooldown" and set(codes[:-1]) <= {"quota"}, codes
    body = assert_refusal(resp, 429, "cooldown")
    assert 60 < body["retry_after"] <= policy.COOLDOWN_LADDER[0][1] and "paused" in body["detail"]

    status = await guard.status(user.id)
    assert status["cooldown_until"] and status["tier"] == "member" and status["strikes"] >= policy.COOLDOWN_LADDER[0][0]
    assert status["usage"]["chat"][0] == minute  # refusals are never charged

    # a browser (no JSON) gets the error page - same status, same Retry-After
    page = await api(client, "POST", ASK_PAGE, json={"message": "asked from a page"}, headers={"Accept": "text/html"})
    assert page.status_code == 429 and int(page.headers["Retry-After"]) > 60
    assert page.headers["content-type"].startswith("text/html") and AIRejected.TITLES["cooldown"] in page.text
    assert len(board.started) == minute


async def test_a_duplicate_and_spam_refusals_over_http(client, db, board, strict_route):
    user = await make_customer(db)
    await login_as(client, user)
    first = await api(client, "POST", ASK, json={"message": "How long should I rest between sets?"})
    assert first.status_code == 200
    # the same words again (case and punctuation do not matter) within the feature's dedupe window
    again = await api(client, "POST", ASK, json={"message": "how long should I rest between sets"})
    assert assert_refusal(again, 409, "duplicate")["retry_after"] <= policy.policy_for("assistant").dedupe_seconds
    spam = await api(client, "POST", ASK, json={"message": "!" * 60})
    assert spam.status_code == 422 and spam.json()["code"] == "spam" and set(spam.json()) == REFUSAL_KEYS
    assert len(board.started) == 1
    status = await guard.status(user.id)
    assert status["usage"]["chat"][0] == 1 and status["strikes"] == policy.STRIKE_POINTS["spam"]  # duplicates: free


async def test_a_assistant_endpoint_answers_guard_refusals_with_the_contract_json(client, db, board):
    """POST /api/assistant/chat no longer answers a guard refusal with its deterministic fallback: past the quota it
    answers with the contract's refusal JSON - no provider is called for it, nothing is charged or stored."""
    from sqlalchemy import func, select

    from app.models import AssistantMessage

    user = await make_customer(db)
    await login_as(client, user)
    minute = member_minute_quota("chat")
    for i in range(minute):
        resp = await api(client, "POST", "/api/assistant/chat", json={"message": f"what should I train today, take {i}"})
        assert resp.status_code == 200 and resp.json()["ai_available"] is True
    for i in range(minute, minute + 2):
        resp = await api(client, "POST", "/api/assistant/chat", json={"message": f"what should I train today, take {i}"})
        assert_refusal(resp, 429, "quota")
    assert len(board.started) == minute
    assert await db.scalar(select(func.count(AssistantMessage.id))) == 2 * minute  # refusals store no history
    status = await guard.status(user.id)
    assert status["usage"]["chat"][0] == minute and status["strikes"] == 2 * policy.STRIKE_POINTS["quota"]


# ============================================================================================ (b) fairness
async def test_b_two_users_flooding_the_queue_are_served_fairly(db, board, strict_route, monkeypatch):
    monkeypatch.setattr(get_settings(), "ai_queue_concurrency", 1)
    monkeypatch.setattr(get_settings(), "ai_queue_max_per_user", 4)
    monkeypatch.setattr(get_settings(), "ai_quota_scale", 10.0)
    monkeypatch.setattr(scheduler_module, "ANNOUNCE_AFTER", 0.05)
    alice, bob = await make_customer(db), await make_customer(db)
    tasks: dict[str, asyncio.Task] = {}
    async with make_client() as ca, make_client() as cb:
        await login_as(ca, alice)
        await login_as(cb, bob)
        qa, qb = hub.subscribe(alice.id), hub.subscribe(bob.id)

        def ask(c: Any, page: str, msg: str) -> asyncio.Task:
            return asyncio.create_task(api(c, "POST", ASK, json={"message": f"{msg} - a question"},
                                           headers={"X-Page-Id": page}))

        def queued() -> int:
            return scheduler.snapshot()["lanes"]["llm"]["queued"]

        board.hold("|A0 ")
        tasks["A0"] = ask(ca, PAGE_A, "A0")
        await wait_until(lambda: board.seen("|A0 "), what="A0 at the provider")
        for n, msg in enumerate(("A1", "A2", "A3"), start=1):  # Alice floods the queue ...
            tasks[msg] = ask(ca, PAGE_A, msg)
            await wait_until(lambda n=n: queued() == n, what=f"{msg} queued")
        # ... up to ai_queue_max_per_user (queued + running): her next requests are refused at once
        refused = [await api(ca, "POST", ASK, json={"message": f"{m} - a question"}, headers={"X-Page-Id": PAGE_A})
                   for m in ("A4", "A5")]
        for n, msg in enumerate(("B1", "B2", "B3"), start=4):  # Bob asks after all of that
            tasks[msg] = ask(cb, PAGE_B, msg)
            await wait_until(lambda n=n: queued() == n, what=f"{msg} queued")
        await asyncio.sleep(0.2)  # long enough for the queue to announce positions
        board.release("|A0 ")
        responses = {m: await asyncio.wait_for(t, 10) for m, t in tasks.items()}

    for resp in refused:
        body = assert_refusal(resp, 429, "too_many")
        assert body["retry_after"] == scheduler_module.BUSY_RETRY_AFTER
    assert all(r.status_code == 200 for r in responses.values())
    order = [p.split("|", 1)[1].split(" ")[0] for p in board.started]
    assert order == ["A0", "B1", "A1", "B2", "A2", "B3", "A3"]  # round-robin, although Alice queued first

    # the flooder earned strikes for the refused requests (and was not charged for them); Bob did nothing wrong
    a_status, b_status = await guard.status(alice.id), await guard.status(bob.id)
    assert a_status["strikes"] == 2 * policy.STRIKE_POINTS["too_many"] and b_status["strikes"] == 0
    # the day window: the minute one may have rolled over while the test ran (usage is [minute, hour, day])
    assert a_status["usage"]["chat"][2] == 4 and b_status["usage"]["chat"][2] == 3

    # live queue positions: each user hears only about their own tab's tickets, which all end 'done'
    await ticks()
    events_a, events_b = received(qa, "ai.queue"), received(qb, "ai.queue")
    for events, page in ((events_a, PAGE_A), (events_b, PAGE_B)):
        assert events and all(e["page_id"] == page for e in events)
        last: dict[str, str] = {}
        for e in events:
            last[e["ticket"]] = e["state"]
        assert set(last.values()) == {"done"} and len(last) == 3
    assert not {e["ticket"] for e in events_a} & {e["ticket"] for e in events_b}
    assert min(e["position"] for e in events_b if e["state"] == "queued") == 1  # Bob was next as soon as he queued


async def test_b_queue_flooding_earns_strikes_until_ai_is_paused(db, board, monkeypatch):
    monkeypatch.setattr(get_settings(), "ai_queue_max_per_user", 2)
    monkeypatch.setattr(get_settings(), "ai_quota_scale", 10.0)
    user = await make_customer(db)
    owner = AIOwner(user_id=user.id, role="customer", session_hash="sess-flood", page_id=PAGE_A)
    board.hold("held ")
    held = [asyncio.create_task(ai.complete(feature="assistant", system="s", prompt=f"held {i}", owner=owner))
            for i in range(2)]
    await wait_until(lambda: len(board.started) == 2, what="two calls running")
    codes = []
    err: Any = None
    for i in range(10):
        with pytest.raises(AIRejected) as err:
            await ai.complete(feature="assistant", system="s", prompt=f"extra {i}", owner=owner,
                              user_text=f"extra question number {i}")
        codes.append(err.value.code)
        if err.value.code == "cooldown":
            break
    assert codes[0] == "too_many" and codes[-1] == "cooldown" and len(codes) <= 6, codes
    assert err.value.retry_after > 60
    status = await guard.status(user.id)
    assert status["cooldown_until"] and status["cooldown"]["code"] == "too_many"
    board.release()
    await asyncio.gather(*held)
    # paused: even with free queue slots the account is refused - before the queue, at no cost
    with pytest.raises(AIRejected) as err:
        await ai.complete(feature="assistant", system="s", prompt="after the pause", owner=owner)
    assert err.value.code == "cooldown" and len(board.started) == 2
    assert scheduler.snapshot()["counters"]["rejected"] == {"too_many": len(codes)}


# ============================================================================================ (c) leaving and returning
async def test_c_leaving_stops_interactive_ai_and_pauses_jobs_until_the_user_returns(db, board, grace, job_kind):
    user = await make_customer(db)
    results: list[str] = []
    kind = job_kind("integration_coach_plan", ai_job(results), on_exit="pause")
    job_id = await enqueue_job(kind, user.id, "plan-1")
    updates = hub.subscribe(user.id)  # what the user's browser would hear (the hub is not presence)

    presence.connected(user.id, conn_id="conn-1", page_id=PAGE_A, session_hash="sess-c")
    await presence.drain()
    board.hold("|chat", "job|plan-1")
    owner = AIOwner(user_id=user.id, role="customer", session_hash="sess-c", page_id=PAGE_A)
    chat = asyncio.create_task(ai.complete(feature="assistant", system="s", prompt=f"{user.id}|chat", owner=owner,
                                           user_text="what should I eat after training?"))
    runner = asyncio.create_task(run_jobs())
    await wait_until(lambda: board.seen("|chat") and board.seen("job|plan-1"), what="both calls at the provider")
    job_owner = board.owners[board.prompt("job|plan-1")]
    assert job_owner is not None and (job_owner.kind, job_owner.user_id, job_owner.job_id) == (OWNER_JOB, user.id, job_id)
    assert (await job_row(job_id)).status == RUNNING

    # their last page goes away (not a tab close: e.g. the phone lost its connection) and nothing comes back
    left_at = time.monotonic()
    presence.disconnected(user.id, conn_id="conn-1", page_id=PAGE_A, code=1006)
    with pytest.raises(AICancelled) as err:
        await asyncio.wait_for(chat, 5)
    assert err.value.reason == "left" and time.monotonic() - left_at < 3
    assert await asyncio.wait_for(runner, 10) == 1  # the job run ended (stopped by its exit policy)
    await presence.drain()

    assert board.was_cancelled("|chat") and board.was_cancelled("job|plan-1")  # both provider calls were stopped
    row = await job_row(job_id)
    assert row.status == PAUSED and row.cancel_reason == "left" and row.paused_at is not None
    assert row.attempts == 0 and row.locked_at is None  # the interrupted run is not charged
    assert results == [] and presence.is_away(user.id) and await presence.away_now(user.id)
    lane = scheduler.snapshot()["lanes"]["llm"]
    assert lane["running"] == 0 and lane["queued"] == 0
    # the provider was already working on the chat when they left: charged (only work that never reached a provider is
    # free - otherwise leaving mid-answer would be a way around the quotas). The day window: no minute-boundary flake.
    assert (await guard.status(user.id))["usage"]["chat"][2] == 1
    logs = {(c.owner_kind, c.cancelled, c.user_id) for c in call_log_queue}
    assert logs == {(OWNER_USER, True, user.id), (OWNER_JOB, True, user.id)}

    # they come back: the paused job is queued again, and runs to the end this time
    presence.connected(user.id, conn_id="conn-2", page_id=PAGE_B, session_hash="sess-c")
    await presence.drain()
    row = await job_row(job_id)
    assert row.status == QUEUED and row.paused_at is None and row.cancel_reason is None
    assert not presence.is_away(user.id) and not await presence.away_now(user.id)
    board.release()
    assert await run_jobs() == 1
    row = await job_row(job_id)
    assert row.status == DONE and row.attempts == 1 and results == [ANSWER]

    await ticks()
    seen = [(e["task_id"], e["kind"], e["status"], e.get("resumed")) for e in received(updates, "task.update")]
    assert seen == [(job_id, kind, "paused", None), (job_id, kind, "queued", True), (job_id, kind, "done", None)]


async def test_c_a_return_over_plain_http_resumes_paused_jobs(client, db, board, grace, job_kind):
    user = await make_customer(db)
    await login_as(client, user)
    kind = job_kind("integration_note", ai_job([]), on_exit="pause")
    job_id = await enqueue_job(kind, user.id, "note-1")
    presence.connected(user.id, conn_id="conn-1", page_id=PAGE_A, session_hash=session_hash_of(client))
    presence.disconnected(user.id, conn_id="conn-1", page_id=PAGE_A, code=1001)
    await asyncio.sleep(grace * 2)
    await wait_until(lambda: presence.is_away(user.id), what="the exit")
    await presence.drain()
    assert (await job_row(job_id)).status == PAUSED

    resp = await client.get("/api/ai/status")  # any signed-in request (load_user -> presence.touch)
    assert resp.status_code == 200
    await presence.drain()
    assert (await job_row(job_id)).status == QUEUED and not presence.is_away(user.id)
    assert await run_jobs() == 1 and (await job_row(job_id)).status == DONE


# ============================================================================================ (d) one tab closes
async def test_d_closing_one_tab_stops_only_that_tabs_ai(client, db, board, grace, strict_route):
    user = await make_customer(db)
    await login_as(client, user)
    session_hash = session_hash_of(client)
    presence.connected(user.id, conn_id="sock-a", page_id=PAGE_A, session_hash=session_hash)
    presence.connected(user.id, conn_id="sock-b", page_id=PAGE_B, session_hash=session_hash)
    await presence.drain()
    board.hold("|tab A", "|tab B")
    in_a = asyncio.create_task(api(client, "POST", ASK, json={"message": "tab A question"}, headers={"X-Page-Id": PAGE_A}))
    in_b = asyncio.create_task(api(client, "POST", ASK, json={"message": "tab B question"}, headers={"X-Page-Id": PAGE_B}))
    await wait_until(lambda: board.seen("|tab A") and board.seen("|tab B"), what="both tabs' calls running")

    stranger = await make_customer(db)  # page ids are made by browsers: naming one only ever reaches your own work
    assert presence.page_left(PAGE_A, user_id=stranger.id) == 0

    closed_at = time.monotonic()
    presence.disconnected(user.id, conn_id="sock-a", page_id=PAGE_A, code=PAGE_CLOSED)  # pagehide -> 4001
    resp_a = await asyncio.wait_for(in_a, 3)
    assert time.monotonic() - closed_at < 1.5  # at once, not after a grace period
    assert resp_a.status_code == 503 and resp_a.json()["code"] == "cancelled"
    assert board.was_cancelled("|tab A") and not board.was_cancelled("|tab B") and not in_b.done()

    await asyncio.sleep(grace * 3)  # tab B is still open: closing tab A is not the user leaving
    assert not presence.is_away(user.id) and presence.snapshot()["grace_timers"] == 0
    board.release("|tab B")
    resp_b = await asyncio.wait_for(in_b, 3)
    assert resp_b.status_code == 200 and resp_b.json() == {"text": ANSWER, "page_id": PAGE_B}


async def test_d_closing_a_tab_stops_its_stream_at_once(db):
    """A stream waiting for its first words (provider stalled) is stopped the moment its tab closes - the provider
    stream is aborted, its slot freed, and it is not counted as a provider failure."""
    user = await make_customer(db)
    provider = FakeProvider("gateway", chunks=["late words"], delay=30.0, max_concurrency=1)
    ai.use_providers([provider])
    owner = AIOwner(user_id=user.id, role="customer", session_hash="sess-s", page_id=PAGE_A)
    agen = ai.stream(feature="plans", system="s", prompt="p", owner=owner, user_text="which plan suits a beginner?")

    async def first() -> str:
        return await agen.__anext__()

    waiting = asyncio.create_task(first())
    await wait_until(lambda: provider.semaphore.locked(), what="the provider stream open")
    closed_at = time.monotonic()
    assert presence.page_left(PAGE_A, user_id=user.id) == 1
    with pytest.raises(AICancelled) as err:
        await asyncio.wait_for(waiting, 3)
    assert err.value.reason == "page_closed" and time.monotonic() - closed_at < 1.0
    assert provider.streams_closed == 1 and not provider.semaphore.locked()
    assert provider.state.failures == 0 and not provider.circuit_open()
    assert scheduler.snapshot()["lanes"]["llm"]["running"] == 0
    await ticks()
    assert (await guard.status(user.id))["usage"]["qa"][0] == 0  # refunded


async def test_d_closing_a_tab_cuts_a_stream_between_words_at_once(db):
    class SlowSecondWords(FakeProvider):
        async def stream_text(self, **kwargs: Any):
            async with self.slot():
                try:
                    yield "first words "
                    await asyncio.sleep(30)
                    yield "never"  # pragma: no cover
                finally:
                    self.streams_closed += 1

    user = await make_customer(db)
    provider = SlowSecondWords("gateway", max_concurrency=1)
    ai.use_providers([provider])
    meta: dict[str, Any] = {}
    agen = ai.stream(feature="plans", system="s", prompt="p", meta=meta,
                     owner=AIOwner(user_id=user.id, role="customer", page_id=PAGE_A))
    got = [await agen.__anext__()]

    async def rest() -> list[str]:
        return [chunk async for chunk in agen]

    remaining = asyncio.create_task(rest())
    await asyncio.sleep(0.05)
    closed_at = time.monotonic()
    assert presence.page_left(PAGE_A, user_id=user.id) == 1
    got += await asyncio.wait_for(remaining, 3)
    assert got == ["first words "] and meta["cancelled"] == "page_closed" and time.monotonic() - closed_at < 1.0
    assert provider.streams_closed == 1 and not provider.semaphore.locked() and provider.state.failures == 0


# ============================================================================================ (e) logout
async def test_e_logout_stops_that_sessions_ai_at_once_and_the_last_logout_is_an_exit(db, board, strict_route,
                                                                                       job_kind, monkeypatch):
    monkeypatch.setattr(get_settings(), "presence_grace_seconds", 30)  # anything quick below is not the grace timer
    user = await make_customer(db)
    kind = job_kind("integration_logout_note", ai_job([]), on_exit="pause")
    job_id = await enqueue_job(kind, user.id, "note-1")  # waiting for the worker
    async with make_client() as phone, make_client() as laptop:
        await login_as(phone, user)
        await login_as(laptop, user)
        board.hold("|phone", "|laptop")
        on_phone = asyncio.create_task(api(phone, "POST", ASK, json={"message": "phone question here"}))
        on_laptop = asyncio.create_task(api(laptop, "POST", ASK, json={"message": "laptop question here"}))
        await wait_until(lambda: board.seen("|phone") and board.seen("|laptop"), what="both sessions' calls")

        out_at = time.monotonic()
        assert (await post_form(phone, "/auth/logout", {})).status_code == 303
        resp = await asyncio.wait_for(on_phone, 3)
        assert time.monotonic() - out_at < 2
        assert resp.status_code == 503 and resp.json()["code"] == "cancelled"
        assert board.was_cancelled("|phone") and not board.was_cancelled("|laptop") and not on_laptop.done()
        # the laptop is still signed in: not an exit
        assert not presence.is_away(user.id) and (await job_row(job_id)).status == QUEUED

        board.release("|laptop")
        assert (await asyncio.wait_for(on_laptop, 3)).status_code == 200
        assert (await post_form(laptop, "/auth/logout", {})).status_code == 303
        await presence.drain()
    assert presence.is_away(user.id)  # nobody is left: an exit right away, no grace period
    row = await job_row(job_id)
    assert row.status == PAUSED and row.cancel_reason == "logout"


# ============================================================================================ (f) jobs
async def test_f_user_jobs_run_at_background_priority_behind_interactive_requests(db, board, job_kind, monkeypatch):
    monkeypatch.setattr(get_settings(), "ai_queue_concurrency", 1)
    member, other = await make_customer(db), await make_customer(db)
    results: list[str] = []
    # an interactive feature, but asked by a background job: nobody waits on screen for it
    kind = job_kind("integration_interactive_feature_job", ai_job(results, feature="assistant"))
    job_id = await enqueue_job(kind, member.id, "bg-1")
    board.hold("holder")
    holder = asyncio.create_task(ai.complete(feature="security_triage", system="s", prompt="holder"))  # system work
    await wait_until(lambda: board.seen("holder"))
    runner = asyncio.create_task(run_jobs())
    await wait_until(lambda: scheduler.snapshot()["by_priority"]["background"]["queued"] == 1, what="job queued")
    interactive = asyncio.create_task(ai.complete(
        feature="assistant", system="s", prompt=f"{other.id}|interactive",
        owner=AIOwner(user_id=other.id, role="customer", page_id=PAGE_B)))
    await wait_until(lambda: scheduler.snapshot()["by_priority"]["interactive"]["queued"] == 1, what="request queued")
    board.release("holder")
    await asyncio.wait_for(asyncio.gather(holder, interactive, runner), 10)
    assert board.started == ["holder", f"{other.id}|interactive", "job|bg-1"]  # the job waited although it came first
    seen = board.snapshots["job|bg-1"]["by_priority"]
    assert seen["background"]["running"] == 1 and seen["interactive"]["running"] == 0
    assert (await job_row(job_id)).status == DONE and results == [ANSWER]


async def test_f_jobs_are_never_refused_by_the_guard_or_the_per_user_queue_limit(db, board, job_kind, monkeypatch):
    member = await make_customer(db)
    owner = AIOwner(user_id=member.id, role="customer", session_hash="sess-f")
    monkeypatch.setattr(get_settings(), "ai_quota_scale", 0.01)  # one unit per window
    code = None
    for _ in range(12):  # real refusals until the account is paused
        try:
            await guard.admit(owner, "coach")
        except AIRejected as exc:
            code = exc.code
            if code == "cooldown":
                break
    assert code == "cooldown"
    before = await guard.status(member.id)
    assert before["cooldown_until"]
    with pytest.raises(AIRejected) as err:  # the member's own requests are refused ...
        await ai.complete(feature="coach", system="s", prompt="interactive coach", owner=owner)
    assert err.value.code == "cooldown"

    # ... but the jobs they asked for run: never judged by the guard, never limited per user by the queue
    monkeypatch.setattr(get_settings(), "ai_queue_max_per_user", 1)
    monkeypatch.setattr(get_settings(), "jobs_concurrency", 3)
    results: list[str] = []
    kind = job_kind("integration_paused_member_job", ai_job(results, feature="coach"))
    ids = [await enqueue_job(kind, member.id, f"plan-{i}") for i in range(3)]
    assert await run_jobs() == 3
    assert [(await job_row(i)).status for i in ids] == [DONE] * 3 and results == [ANSWER] * 3
    jobs_logs = [c for c in call_log_queue if c.owner_kind == OWNER_JOB]
    assert len(jobs_logs) == 3 and all(c.ok and c.user_id == member.id for c in jobs_logs)
    after = await guard.status(member.id)
    assert after["usage"] == before["usage"] and after["strikes"] == before["strikes"]  # not charged, not judged
    assert all(not p.startswith("interactive") for p in board.started)


async def test_f_job_updates_reach_only_the_requester(db, board, job_kind):
    member, other = await make_customer(db), await make_customer(db)
    mine, theirs = hub.subscribe(member.id), hub.subscribe(other.id)
    pause_kind = job_kind("integration_pause_update", ai_job([]), on_exit="pause")
    keep_kind = job_kind("integration_keep_update", ai_job([]), on_exit="keep")
    pause_id = await enqueue_job(pause_kind, member.id, "p-1")
    keep_id = await enqueue_job(keep_kind, member.id, "k-1")
    system_id = await enqueue_job(keep_kind, None, "s-1")  # system work: nobody to tell
    assert await run_jobs() == 3
    await ticks()
    got = {e["task_id"]: e for e in received(mine, "task.update")}
    assert set(got) == {pause_id, keep_id} and system_id not in got
    assert got[pause_id]["status"] == "done" and "notify" not in got[pause_id]
    # obligations announce their results with notifications of their own: the browser only tracks them
    assert got[keep_id]["status"] == "done" and got[keep_id]["notify"] is False
    assert received(theirs, "task.update") == []


# ============================================================================================ (g) system work
async def test_g_system_work_bypasses_the_guard(db, board, monkeypatch):
    monkeypatch.setattr(get_settings(), "ai_quota_scale", 0.01)  # one unit per window for every user ...
    member = await make_customer(db)
    member_owner = AIOwner(user_id=member.id, role="customer")
    await ai.complete(feature="assistant", system="s", prompt="member 1", owner=member_owner)
    with pytest.raises(AIRejected) as err:
        await ai.complete(feature="assistant", system="s", prompt="member 2", owner=member_owner)
    assert err.value.code == "quota"

    # ... but work with no owner (periodic tasks, the system) is never judged, charged or limited per user
    assert current_owner() is None and await guard.admit(None, "assistant", "same words") is None
    answers = await asyncio.gather(*(ai.complete(feature="assistant", system="s", prompt=f"system {i}",
                                                 user_text="same words") for i in range(6)))
    assert [a.text for a in answers] == [ANSWER] * 6
    system_logs = [c for c in call_log_queue if c.owner_kind == "system"]
    assert len(system_logs) == 6 and all(c.ok and c.user_id is None for c in system_logs)
    # unguarded features of a signed-in person (the superadmin's AI self-test) are not judged either
    admin = await make_user(db, role=Role.SUPERADMIN)
    assert await guard.admit(AIOwner(user_id=admin.id, role="superadmin"), "selftest", "ping") is None


# ============================================================================================ the real app
async def test_healthz_with_the_real_app():
    async with make_client() as c:
        resp = await c.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and body["database"] is True and set(body["ai"]) == {"state", "active"}
