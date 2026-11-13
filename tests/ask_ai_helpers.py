"""Shared helpers for the AI widgets' tests (tests/test_plan_ai.py, tests/test_shop_ai_guard.py, and the "no pooled
connection while the AI works" tests of the assistant, intake, live coach and super-admin self-test): a provider
stream the test controls, a browser that goes away mid-answer, the guard's view of a member, fixtures that spy on the
guard and on the request's DB session, and a probe of the DB connections checked out of the pool while a request waits
in the AI queue and while its provider answers. Fixtures are imported by name into the test modules that use them."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest

from app.ai.client import FakeProvider, ai
from app.ai.guard import guard
from app.ai.owner import AIOwner
from app.ai.scheduler import scheduler
from app.config import get_settings

BLOCKED_KEYS = {"type", "code", "detail", "retry_after"}  # the SSE refusal (docs section 7)
REFUSAL_KEYS = {"error", "detail", "code", "retry_after", "ai_available"}  # the JSON refusal


async def wait_until(predicate: Callable[[], Any], *, timeout: float = 5.0, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.01)


async def qa_used(user_id: int, *, expect: int, timeout: float = 5.0) -> int:
    """The member's 'qa' quota units this minute, once pending guard.settle refunds have landed."""
    deadline = time.monotonic() + timeout
    while True:
        used = (await guard.status(user_id))["usage"]["qa"][0]
        if used == expect or time.monotonic() > deadline:
            return used
        await asyncio.sleep(0.02)


async def pause_ai(user_id: int) -> None:
    """Strike the account (3 points per spam strike) until it crosses the first COOLDOWN_LADDER level: AI is paused
    for every guarded feature of the account."""
    owner = AIOwner(user_id=user_id, role="customer")
    for _ in range(10):
        if (await guard.status(user_id))["cooldown_until"]:
            return
        await guard.note_rejection(owner, "plans", "spam")
    raise AssertionError("the account never got an AI cooldown")


class HeldStream(FakeProvider):
    """A provider stream the test controls: ``before`` holds before the first words; otherwise it sends ``first`` and
    then either holds (``more`` None) or keeps sending ``more`` pieces every ``every`` seconds."""

    def __init__(self, *, before: bool = False, first: str = "Online + Trainer suits you ",
                 more: list[str] | None = None, every: float = 0.05) -> None:
        super().__init__("gateway", max_concurrency=1)
        self.before, self.first, self.more, self.every = before, first, more, every
        self.entered = asyncio.Event()  # the provider holds its slot
        self.waiting = asyncio.Event()  # the first words went out; waiting to send more
        self.release = asyncio.Event()

    async def stream_text(self, **kwargs: Any):
        async with self.slot():
            self.state.calls += 1
            self.prompts.append(kwargs)
            try:
                self.entered.set()
                if self.before:
                    await self.release.wait()
                yield self.first
                self.waiting.set()
                if self.more is None:
                    await self.release.wait()
                    yield "and that is all."
                else:
                    for piece in self.more:
                        await asyncio.sleep(self.every)
                        yield piece
            finally:
                self.streams_closed += 1


def leaving_client(gone: asyncio.Event, *, spec: str) -> httpx.AsyncClient:
    """A browser that goes away mid-answer once ``gone`` is set, as two kinds of server report it:

    * ASGI spec 2.3 (uvicorn today): ``receive`` returns ``http.disconnect`` and Starlette's StreamingResponse cancels
      the task iterating the event stream (as tests/droppable_client.py does for plain requests);
    * ASGI spec 2.4: the lost client only shows when a write fails - ``send`` raises OSError, the response raises
      ClientDisconnect, and the event generator is left suspended at a ``yield`` unless the response closes it.

    httpx's ASGITransport wants every response to end, so a response cut short is ended here, as the server would
    drop the connection."""
    from app.main import app as fastapi_app

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await fastapi_app(scope, receive, send)
            return
        body_done = completed = False

        async def receive_or_disconnect() -> dict[str, Any]:
            nonlocal body_done
            if body_done and spec == "2.3":
                await gone.wait()
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request" and not message.get("more_body", False):
                body_done = True
            return message

        async def send_or_fail(message: dict[str, Any]) -> None:
            nonlocal completed
            if message["type"] == "http.response.body":
                if spec == "2.4" and gone.is_set():
                    raise OSError("the client went away")
                completed = completed or not message.get("more_body", False)
            await send(message)

        try:
            await fastapi_app(dict(scope, asgi={"version": "3.0", "spec_version": spec}), receive_or_disconnect,
                              send_or_fail)
        except Exception:  # ClientDisconnect: a server logs it and drops the connection
            if not (spec == "2.4" and gone.is_set()):
                raise
        if not completed:
            await send({"type": "http.response.body", "body": b"", "more_body": False})

    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 51234))
    return httpx.AsyncClient(transport=transport, base_url="http://testserver", follow_redirects=False,
                             headers={"User-Agent": "pytest-ironpulse/1.0"})


# ------------------------------------------------------------------ fixtures (import them by name)
@pytest.fixture
def guard_on(monkeypatch):
    """The guard as configured by default (whatever the local .env says)."""
    monkeypatch.setattr(get_settings(), "ai_guard_enabled", True)
    monkeypatch.setattr(get_settings(), "ai_quota_scale", 1.0)


@pytest.fixture
def steady_minute(monkeypatch):
    """The guard's clock held in the middle of the current minute. Quota windows are fixed clock minutes, so a test
    that fills a per-minute quota with a few requests would otherwise see the window roll over half-way (about one run
    in thirty) and its last request admitted. Everything else - duplicate windows, the queue - keeps real time."""
    pinned = math.floor(time.time() / 60) * 60 + 30.0
    monkeypatch.setattr(guard, "clock", lambda: pinned)
    return pinned


@pytest.fixture
def admitted(monkeypatch):
    """Every guard.admit call as (user id, feature, text, dedupe scope) - the real guard still decides."""
    calls: list[tuple[int | None, str, str | None, str | None]] = []
    real = guard.admit

    async def spy(owner: Any, feature: str, text: str | None = None, *, scope: str | None = None) -> Any:
        calls.append((owner.user_id if owner is not None else None, feature, text, scope))
        return await real(owner, feature, text, scope=scope)

    monkeypatch.setattr(guard, "admit", spy)
    return calls


@pytest.fixture
def tracked_sessions(app):
    """Every request's DB session (get_db), recorded so a test can look at it while the AI works."""
    from app.db import get_db, get_sessionmaker

    sessions: list[Any] = []

    async def tracked_get_db():
        async with get_sessionmaker()() as session:
            sessions.append(session)
            yield session

    app.dependency_overrides[get_db] = tracked_get_db
    yield sessions
    app.dependency_overrides.pop(get_db, None)


# ------------------------------------------------------------------ DB connections while the AI works
class PoolProbe:
    """The database connections checked out of the engine's pool right now, counted from the pool's own checkout and
    checkin events (so it works with the tests' NullPool too). Only connections checked out after the probe started
    count, so a session that was already open when the test began cannot push the figure below zero."""

    def __init__(self) -> None:
        self._out: set[int] = set()

    @property
    def checked_out(self) -> int:
        return len(self._out)

    def checkout(self, _dbapi_connection: Any, record: Any, _proxy: Any) -> None:
        self._out.add(id(record))

    def checkin(self, _dbapi_connection: Any, record: Any) -> None:
        self._out.discard(id(record))


@pytest.fixture
def pool_probe():
    from sqlalchemy import event

    from app.db import get_engine

    engine = get_engine().sync_engine
    probe = PoolProbe()
    event.listen(engine, "checkout", probe.checkout)
    event.listen(engine, "checkin", probe.checkin)
    try:
        yield probe
    finally:
        event.remove(engine, "checkout", probe.checkout)
        event.remove(engine, "checkin", probe.checkin)


HOLD_PROMPT = "hold the only AI slot"


async def connections_while_the_ai_works(send: Callable[[], Awaitable[Any]], probe: PoolProbe,
                                         answer: Callable[..., Any], monkeypatch: Any) -> tuple[Any, int, list[int]]:
    """Run ``send()`` (one request) while the AI queue's only slot is taken by a system call, so the request first waits
    in the AI queue and then gets the provider, whose reply is ``answer(**kwargs)``. Returns (the request's result, the
    connections checked out while it waited in the queue, the connections checked out each time the provider answered
    it). A route that keeps its request's session (and so a pooled connection) open while it waits for the AI shows up
    as 1 in both; the test's own session must not hold a transaction meanwhile."""
    monkeypatch.setattr(get_settings(), "ai_queue_concurrency", 1)
    release = asyncio.Event()
    answering: list[int] = []

    async def handler(**kwargs: Any) -> Any:
        if kwargs.get("prompt") == HOLD_PROMPT:
            await release.wait()
            return "OK"
        answering.append(probe.checked_out)
        return answer(**kwargs)

    provider = FakeProvider("gateway", handler=handler)
    ai.use_providers([provider])
    holder = asyncio.create_task(ai.complete(feature="selftest", system="s", prompt=HOLD_PROMPT))
    request: asyncio.Task | None = None
    try:
        await wait_until(lambda: len(provider.prompts) == 1, what="the system call to take the only AI slot")
        request = asyncio.create_task(send())
        await wait_until(lambda: scheduler.snapshot()["lanes"]["llm"]["queued"] == 1 or request.done(),
                         what="the request to wait in the AI queue")
        assert not request.done(), "the request finished without waiting for the AI"
        await asyncio.sleep(0.05)  # it stays queued: nothing it holds is about to be released
        queued = probe.checked_out
    finally:
        release.set()
        await holder
    result = await asyncio.wait_for(request, 10)
    return result, queued, answering
