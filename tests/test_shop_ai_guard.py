"""Shop product Q&A ("Ask AI about this product") with the AI guard and the fair AI queue
(docs/ai-guard-and-queue.md). Both endpoints - JSON and Server-Sent Events:

* a guard refusal (quota, duplicate, spam, cooldown) is the contract JSON (429/409/422 + Retry-After) on the JSON
  endpoint and a ``blocked`` event (after the deterministic safety notes) on the stream - never an answer, never a
  provider call, never cached; 'busy' keeps the answer built from the listing;
* the limits are the guard's per-user 'qa' quotas (shared with the plan advisor): another member is never affected,
  and the old in-memory per-process throttle is gone;
* the guard judges the member's own words in the scope of the product (``dedupe_scope='product:<id>'``: the same
  suggested question about another product is not a duplicate), never the prompt;
* the request's DB session is released before the AI works (queue wait + provider);
* time spent waiting in the AI queue never counts against the first words: a long wait ends as 'busy' (the listing
  answer, worded busy), never as 'AI unavailable';
* a requester who leaves stops the work: the queue ticket and the provider stream are released at once, a cut answer
  (the router's ``meta['truncated']``) is never cached, and the JSON endpoint answers 503 'cancelled'.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any

import pytest

from app.ai import policy
from app.ai import shop as shop_ai
from app.ai.client import FakeProvider, ai
from app.ai.owner import AIOwner, owner_scope
from app.ai.scheduler import scheduler
from app.config import get_settings
from app.models.shop import ProductCategory
from app.services.presence import presence
from tests.ask_ai_helpers import (  # noqa: F401 - fixtures are used by name
    BLOCKED_KEYS,
    HOLD_PROMPT,
    REFUSAL_KEYS,
    HeldStream,
    admitted,
    guard_on,
    leaving_client,
    pause_ai,
    qa_used,
    steady_minute,
    tracked_sessions,
    wait_until,
)
from tests.conftest import api, csrf_token, login_as, make_client
from tests.factories import make_customer
from tests.test_shop import make_product, shop_setup, sse_events

PAGE = "shop-tab-0001-test"
META = "\n<<META>>" + json.dumps({"suitable_for_you": "unsure", "suggested_product_ids": [], "safety_flags": []})
STATUS = {"quota": 429, "cooldown": 429, "duplicate": 409, "spam": 422}


async def stream(client, product, question: str, **headers: str):
    hdrs = {"X-CSRF-Token": await csrf_token(client), "Accept": "text/event-stream", **headers}
    return await client.post(f"/api/shop/products/{product.id}/ask/stream", json={"question": question}, headers=hdrs)


async def ask_json(client, product, question: str, **headers: str):
    return await api(client, "POST", f"/api/shop/products/{product.id}/ask", json={"question": question},
                     headers=headers)


def reply(text: str, **kw: Any) -> str:
    """What the model answers: the JSON endpoint asks for a schema, the stream for text + the META trailer."""
    if kw.get("schema") is None:
        return text + META
    return json.dumps({"answer": text, "suitable_for_you": "unsure"})


def answering(text: str = "Creatine supports strength and power.") -> FakeProvider:
    provider = FakeProvider("gateway", handler=lambda **kw: reply(text, **kw))
    ai.use_providers([provider])
    return provider


async def creatine_setup(db):
    _, gym, _ = await shop_setup(db)
    creatine = await make_product(db, gym, name="Creatine Monohydrate", is_supplement=True)
    cust = await make_customer(db, conditions=["kidney disease"])
    return gym, creatine, cust


async def provoke(code: str, client, product, user) -> str:
    """Bring the member to where the guard refuses ``code``; returns the question that will be refused."""
    if code == "quota":
        for i in range(policy.QUOTAS["member"]["qa"][0]):
            assert (await ask_json(client, product, f"Shop question number {i}?")).status_code == 200
        return "One question too many?"
    if code == "duplicate":
        assert (await ask_json(client, product, "Is it good for strength training?")).status_code == 200  # cached
        return "is it good for STRENGTH training"  # the same words: a different prompt (no cache hit), a duplicate
    if code == "spam":
        return "!" * 60
    await pause_ai(user.id)
    return "Is it safe for me?"


def check_retry_after(code: str, seconds: int) -> None:
    if code == "quota":
        assert 0 < seconds <= 60
    elif code == "duplicate":
        assert 0 < seconds <= policy.policy_for("shop").dedupe_seconds
    elif code == "spam":
        assert seconds == 0
    else:
        assert 60 < seconds <= policy.COOLDOWN_LADDER[0][1]


# ============================================================================================ refusals
@pytest.mark.parametrize("code", ["quota", "duplicate", "spam", "cooldown"])
async def test_stream_refusals_are_blocked_events_after_the_safety_notes(client, db, guard_on, steady_minute, code):
    _, creatine, cust = await creatine_setup(db)
    await login_as(client, cust)
    provider = answering()
    question = await provoke(code, client, creatine, cust)
    calls, cached = len(provider.prompts), len(shop_ai._cache)

    r = await stream(client, creatine, question)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = sse_events(r.text)
    assert [e["type"] for e in events] == ["start", "blocked", "done"], events
    assert any(s["title"] == "Creatine and kidney disease" for s in events[0]["safety"])  # deterministic safety first
    blocked = events[1]
    assert set(blocked) == BLOCKED_KEYS and blocked["code"] == code and blocked["detail"]
    check_retry_after(code, blocked["retry_after"])
    assert events[2] == {"type": "done", "blocked": True}
    assert len(provider.prompts) == calls and len(shop_ai._cache) == cached  # no AI, nothing cached


@pytest.mark.parametrize("code", ["quota", "duplicate", "spam", "cooldown"])
async def test_json_refusals_are_the_contract_json(client, db, guard_on, steady_minute, code):
    _, creatine, cust = await creatine_setup(db)
    await login_as(client, cust)
    provider = answering()
    question = await provoke(code, client, creatine, cust)
    calls, cached = len(provider.prompts), len(shop_ai._cache)

    r = await ask_json(client, creatine, question)
    assert r.status_code == STATUS[code], r.text
    body = r.json()
    assert set(body) == REFUSAL_KEYS and body["code"] == code and body["ai_available"] is True and body["detail"]
    check_retry_after(code, body["retry_after"])
    if body["retry_after"]:
        assert r.headers["Retry-After"] == str(body["retry_after"])
    else:
        assert "retry-after" not in r.headers
    assert len(provider.prompts) == calls and len(shop_ai._cache) == cached


async def test_busy_queue_keeps_the_listing_answer_on_both_endpoints(client, db, guard_on, monkeypatch):
    _, creatine, cust = await creatine_setup(db)
    await login_as(client, cust)
    provider = answering()
    monkeypatch.setattr(get_settings(), "ai_queue_max_total", 0)  # the AI queue refuses everyone: 'busy'

    events = sse_events((await stream(client, creatine, "How should I take it?")).text)
    assert [e["type"] for e in events] == ["start", "done"]
    done = events[1]
    assert done["busy"] is True and done["ai_available"] is False and done["provider"] is None
    assert done["answer"].startswith(shop_ai.BUSY_LEAD) and any("kidney" in f.lower() for f in done["safety_flags"])

    r = await ask_json(client, creatine, "Does it contain lactose?")
    assert r.status_code == 200
    body = r.json()
    assert body["busy"] is True and body["ai_available"] is False and body["answer"].startswith(shop_ai.BUSY_LEAD)
    assert provider.prompts == [] and not shop_ai._cache
    assert await qa_used(cust.id, expect=0) == 0  # refunded: nobody pays for a busy queue


async def test_ai_down_still_answers_from_the_listing_and_says_unavailable(client, db, ai_down):
    _, creatine, cust = await creatine_setup(db)
    await login_as(client, cust)
    body = (await ask_json(client, creatine, "How should I take it?")).json()
    assert body["busy"] is False and body["ai_available"] is False and body["answer"].startswith(shop_ai.UNAVAILABLE_LEAD)


# ============================================================================================ per-user limits
async def test_guard_limits_are_per_user_and_shared_with_the_plan_advisor(client, db, guard_on, steady_minute):
    gym, creatine, alice = await creatine_setup(db)
    shaker = await make_product(db, gym, name="Shaker Bottle", category=ProductCategory.ACCESSORIES)
    bob = await make_customer(db)
    await login_as(client, alice)
    provider = answering()
    minute = policy.QUOTAS["member"]["qa"][0]
    for i in range(minute):  # alice uses up her 'qa' minute, half on each endpoint
        if i % 2:
            assert (await ask_json(client, creatine, f"Alice question {i}?")).status_code == 200
        else:
            assert sse_events((await stream(client, shaker, f"Alice question {i}?")).text)[-1]["type"] == "done"
    assert (await ask_json(client, creatine, "Alice asks again?")).json()["code"] == "quota"
    assert sse_events((await stream(client, shaker, "Alice asks again?")).text)[1]["code"] == "quota"
    # the same 'qa' quota covers the plan advisor
    r = await api(client, "POST", "/api/plans/ask", json={"question": "Which plan suits me?"})
    plan_events = [json.loads(line[5:]) for line in r.text.splitlines() if line.startswith("data:")]
    assert [e["type"] for e in plan_events] == ["blocked", "done"] and plan_events[0]["code"] == "quota"

    async with make_client() as other:  # bob is a different member: never affected by alice's questions
        await login_as(other, bob)
        for i in range(minute):
            assert (await ask_json(other, creatine, f"Bob question {i}?")).status_code == 200
        assert sse_events((await stream(other, shaker, "Bob asks once more?")).text)[1]["code"] == "quota"
    assert len(provider.prompts) == 2 * minute
    assert await qa_used(alice.id, expect=minute) == minute and await qa_used(bob.id, expect=minute) == minute


async def test_the_guard_sees_the_members_words_in_the_scope_of_the_product(client, db, guard_on, admitted):
    gym, creatine, cust = await creatine_setup(db)
    whey = await make_product(db, gym, name="Gold Whey Protein", allergens=["milk"])
    await login_as(client, cust)
    provider = answering("It has 25 g of protein per scoop.")
    # the same suggestion chip on two products within seconds: two different questions, not a duplicate
    events = sse_events((await stream(client, creatine, "How much protein is in it?")).text)
    assert events[-1]["type"] == "done" and events[-1]["ai_available"] is True
    r = await ask_json(client, whey, '  How much protein """ is in it? ')
    assert r.status_code == 200 and r.json()["ai_available"] is True
    # the member's words as they typed them (cleaned like the prompt's copy) - no '[product N]' tag - each in the scope
    # of its product
    assert admitted == [(cust.id, "shop", "How much protein is in it?", f"product:{creatine.id}"),
                        (cust.id, "shop", "How much protein ''' is in it?", f"product:{whey.id}")]
    assert all("PRODUCT FACTS" not in text and "kidney" not in text and "product" not in text
               for _, _, text, _ in admitted)
    assert len(provider.prompts) == 2
    # ... and the same words about the same product again are a duplicate, on either endpoint
    r = await ask_json(client, creatine, "how much protein is in it")
    assert r.status_code == 409 and r.json()["code"] == "duplicate"


# ============================================================================================ waiting in the AI queue
class SlotHolder:
    """Takes the AI queue's only slot with a system call until ``release()``, so the next question has to wait."""

    def __init__(self, monkeypatch: Any, answer: str = "Creatine supports strength.") -> None:
        monkeypatch.setattr(get_settings(), "ai_queue_concurrency", 1)
        self.released = asyncio.Event()
        self.answer = answer
        self.provider = FakeProvider("gateway", handler=self.handle)
        ai.use_providers([self.provider])
        self.task: asyncio.Task | None = None

    async def handle(self, **kw: Any) -> str:
        if kw.get("prompt") == HOLD_PROMPT:
            await self.released.wait()
            return "OK"
        return reply(self.answer, **kw)

    async def __aenter__(self) -> SlotHolder:
        self.task = asyncio.create_task(ai.complete(feature="selftest", system="s", prompt=HOLD_PROMPT))
        await wait_until(lambda: len(self.provider.prompts) == 1, what="the system call to take the slot")
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.release()
        if self.task is not None:
            await self.task

    def release(self) -> None:
        self.released.set()

    @property
    def questions(self) -> int:  # provider calls apart from the holder's
        return sum(1 for p in self.provider.prompts if p.get("prompt") != HOLD_PROMPT)


async def test_a_question_that_waits_too_long_in_the_queue_is_answered_busy_not_unavailable(client, db, guard_on,
                                                                                             monkeypatch):
    """The queue gives up after the shop policy's max_wait and says 'busy' (the listing answer, worded busy, refunded).
    No first-token clock of the shop's own may run while the question waits there - one ended a queued stream as
    'AI unavailable' before the queue could answer, and stopped its ticket."""
    _, creatine, cust = await creatine_setup(db)
    await login_as(client, cust)
    monkeypatch.setitem(policy.FEATURES, "shop", replace(policy.FEATURES["shop"], max_wait=0.8))
    monkeypatch.setattr(shop_ai, "FIRST_TOKEN_DEADLINE", 0.3)  # shorter than the wait in the queue
    async with SlotHolder(monkeypatch) as slot:
        events = sse_events((await stream(client, creatine, "How should I take it?")).text)
        body = (await ask_json(client, creatine, "Does it contain lactose?")).json()
        assert slot.questions == 0  # neither question reached a provider
    assert [e["type"] for e in events] == ["start", "done"], events
    done = events[1]
    assert done["busy"] is True and done["ai_available"] is False and done["answer"].startswith(shop_ai.BUSY_LEAD)
    assert body["busy"] is True and body["ai_available"] is False and body["answer"].startswith(shop_ai.BUSY_LEAD)
    counters = scheduler.snapshot()["counters"]
    assert counters["expired"] == 2 and counters["cancelled"] == {}  # the queue's own 'busy', no ticket stopped early
    assert not shop_ai._cache and await qa_used(cust.id, expect=0) == 0  # refunded: nobody pays for a busy queue


async def test_time_spent_in_the_queue_never_counts_against_the_first_words(client, db, guard_on, monkeypatch):
    """A question that waited longer than FIRST_TOKEN_DEADLINE for a queue slot still gets the AI's answer: the clock
    for the first words starts once a provider holds its slot."""
    _, creatine, cust = await creatine_setup(db)
    await login_as(client, cust)
    monkeypatch.setattr(shop_ai, "FIRST_TOKEN_DEADLINE", 0.5)
    async with SlotHolder(monkeypatch) as slot:
        task = asyncio.create_task(stream(client, creatine, "Is it good for strength?"))
        await wait_until(lambda: scheduler.snapshot()["lanes"]["llm"]["queued"] == 1, what="the question to queue")
        await asyncio.sleep(1.0)  # twice the first-token deadline, waiting for the slot
        assert not task.done()
        slot.release()
        events = sse_events((await asyncio.wait_for(task, 5)).text)
    done = events[-1]
    assert done["type"] == "done" and done["ai_available"] is True and done["busy"] is False
    assert done["answer"] == "Creatine supports strength." and slot.questions == 1
    assert scheduler.snapshot()["counters"]["cancelled"] == {} and len(shop_ai._cache) == 1


async def test_a_complete_answer_that_ends_like_a_cut_one_is_cached(client, db):
    """Whether an answer was cut short is the router's flag (``meta['truncated']``), not the look of its last chunk."""
    _, creatine, cust = await creatine_setup(db)
    await login_as(client, cust)
    provider = FakeProvider("gateway", chunks=["Creatine supports strength - ask your doctor first", " …"])
    ai.use_providers([provider])
    first = sse_events((await stream(client, creatine, "Is it good for strength?")).text)
    assert first[-1]["type"] == "done" and first[-1]["ai_available"] is True and first[-1]["cached"] is False
    assert first[-1]["answer"] == "Creatine supports strength - ask your doctor first …"
    again = sse_events((await stream(client, creatine, "Is it good for strength?")).text)
    assert again[-1]["cached"] is True and again[-1]["answer"] == first[-1]["answer"]
    assert len(provider.prompts) == 1


async def test_a_streamed_answer_cut_at_the_time_limit_is_never_cached(client, db, monkeypatch):
    monkeypatch.setattr(get_settings(), "ai_guard_enabled", False)  # about caching: a cut answer is charged
    monkeypatch.setattr(shop_ai, "TOTAL_TIMEOUT", 0.25)
    _, creatine, cust = await creatine_setup(db)
    await login_as(client, cust)
    provider = FakeProvider("gateway", chunks=["Creatine ", "supports ", "strength ", "and ", "power."], delay=0.1)
    ai.use_providers([provider])
    for _ in range(2):
        events = sse_events((await stream(client, creatine, "Is it good for strength?")).text)
        done = events[-1]
        assert done["type"] == "done" and done["ai_available"] is True and done["cached"] is False
        assert done["answer"].startswith("Creatine") and done["answer"].endswith("…") and "power." not in done["answer"]
    assert len(provider.prompts) == 2 and not shop_ai._cache


# ============================================================================================ resources
async def test_the_db_session_is_released_before_the_ai_works(client, db, tracked_sessions):
    _, creatine, cust = await creatine_setup(db)
    await login_as(client, cust)
    seen: list[tuple[bool, int]] = []

    def handler(**kw: Any) -> str:
        session = tracked_sessions[-1]  # the request being answered opened the latest session
        seen.append((session.in_transaction(), len(session.identity_map)))
        return reply("Creatine supports strength.", **kw)

    ai.use_providers([FakeProvider("gateway", handler=handler)])
    assert sse_events((await stream(client, creatine, "Is it good for strength?")).text)[-1]["ai_available"] is True
    r = await ask_json(client, creatine, "How should I take it?")
    assert r.status_code == 200 and r.json()["ai_available"] is True
    # closed - no transaction (so no pooled connection) and nothing loaded - while the AI answered, on both endpoints
    assert seen == [(False, 0), (False, 0)]


async def test_closing_the_event_stream_at_a_yield_releases_the_ticket_at_once(db):
    """contextlib.aclosing: when the response stops iterating mid-answer (client gone), closing the generator closes
    ai.stream right away - queue ticket, guard admission and provider stream - not at garbage collection."""
    _, creatine, cust = await creatine_setup(db)
    ctx = await shop_ai.build_context(db, creatine, cust)
    provider = FakeProvider("gateway", chunks=["Creatine ", "supports ", "strength."], delay=0.01, max_concurrency=1)
    ai.use_providers([provider])
    with owner_scope(AIOwner(user_id=cust.id, role="customer")):
        gen = shop_ai.stream_events(ctx, "Is it good for strength?")
    assert json.loads((await anext(gen))[6:])["type"] == "start"
    first = json.loads((await anext(gen))[6:])  # the splitter holds back a possible "<<META>>" start
    assert first["type"] == "delta" and "Creatine ".startswith(first["text"])
    assert scheduler.snapshot()["lanes"]["llm"]["running"] == 1 and provider.semaphore.locked()
    await gen.aclose()  # what EventStreamResponse does when the client went away
    snap = scheduler.snapshot()
    assert snap["lanes"]["llm"]["running"] == 0 and snap["counters"]["cancelled"] == {"aborted": 1}
    assert provider.streams_closed == 1 and not provider.semaphore.locked() and not shop_ai._cache


@pytest.mark.parametrize("server", ["spec 2.3: cancelled on http.disconnect", "spec 2.4: ClientDisconnect on send"])
async def test_a_client_that_disconnects_mid_answer_releases_its_ai_ticket(db, guard_on, server):
    _, creatine, cust = await creatine_setup(db)
    gone = asyncio.Event()
    spec = server[5:8]
    provider = HeldStream(first="Creatine supports strength ",
                          more=None if spec == "2.3" else [f"and more {i} " for i in range(200)])
    ai.use_providers([provider])
    async with leaving_client(gone, spec=spec) as c:
        await login_as(c, cust)
        task = asyncio.create_task(stream(c, creatine, "Is it good for strength?"))
        await asyncio.wait_for(provider.waiting.wait(), 5)
        assert scheduler.snapshot()["lanes"]["llm"]["running"] == 1
        gone.set()
        r = await asyncio.wait_for(task, 5)
        assert scheduler.snapshot()["lanes"]["llm"]["running"] == 0  # released by the time the response is over
        assert provider.streams_closed == 1 and not provider.semaphore.locked()
    assert [e["type"] for e in sse_events(r.text)][:2] == ["start", "delta"] and '"type":"done"' not in r.text
    assert not shop_ai._cache and provider.state.failures == 0
    assert scheduler.snapshot()["counters"]["cancelled"] == {"aborted": 1}
    # its first words went out before the client left: charged (only work that never reached a provider is free)
    from app.ai.guard import guard

    assert (await guard.status(cust.id))["usage"]["qa"][2] == 1


async def test_an_answer_cut_short_by_the_queue_is_never_cached_and_may_be_asked_again(client, db, guard_on):
    _, creatine, cust = await creatine_setup(db)
    await login_as(client, cust)
    provider = HeldStream(first="Creatine supports strength ")
    ai.use_providers([provider])
    task = asyncio.create_task(stream(client, creatine, "Is it good for strength?"))
    await asyncio.wait_for(provider.waiting.wait(), 5)
    assert scheduler.cancel(user_id=cust.id, kinds={"user"}, reason="left") == 1  # presence: the user left
    types = [e["type"] for e in sse_events((await asyncio.wait_for(task, 5)).text)]
    assert types[0] == "start" and set(types[1:]) == {"delta"}  # just ends - no done: nobody is listening
    assert not shop_ai._cache and provider.streams_closed == 1
    # a cut answer was refunded and its duplicate fingerprint forgotten: asking again is not refused
    answering()
    events = sse_events((await stream(client, creatine, "Is it good for strength?")).text)
    assert events[-1]["type"] == "done" and events[-1]["ai_available"] is True and len(shop_ai._cache) == 1


async def test_closing_the_tab_stops_both_endpoints(client, db, guard_on):
    _, creatine, cust = await creatine_setup(db)
    await login_as(client, cust)
    provider = HeldStream(before=True)
    ai.use_providers([provider])
    task = asyncio.create_task(stream(client, creatine, "Is it good for strength?", **{"X-Page-Id": PAGE}))
    await asyncio.wait_for(provider.entered.wait(), 5)
    assert presence.page_left(PAGE, user_id=cust.id) == 1  # pagehide -> socket closed with 4001
    events = sse_events((await asyncio.wait_for(task, 5)).text)
    assert [e["type"] for e in events] == ["start"]  # AICancelled before the first words: no answer, no fallback

    held = asyncio.Event()

    async def slow(**kw: Any) -> str:
        held.set()
        await asyncio.sleep(30)
        return "{}"  # pragma: no cover

    ai.use_providers([FakeProvider("gateway", handler=slow)])
    task = asyncio.create_task(ask_json(client, creatine, "How should I take it?", **{"X-Page-Id": PAGE}))
    await asyncio.wait_for(held.wait(), 5)
    assert presence.page_left(PAGE, user_id=cust.id) == 1
    r = await asyncio.wait_for(task, 5)
    assert r.status_code == 503 and r.json()["code"] == "cancelled"  # nobody reads an answer: no fallback either
    assert scheduler.snapshot()["lanes"]["llm"]["running"] == 0 and not shop_ai._cache
    assert scheduler.snapshot()["counters"]["cancelled"] == {"page_closed": 2}
    # the stream said nothing (free); the JSON question's provider was already working on it (charged)
    from app.ai.guard import guard

    assert (await guard.status(cust.id))["usage"]["qa"][2] == 1


async def test_product_page_ask_form_never_sticks_busy_and_has_a_wait_line(client, db):
    _, gym, _ = await shop_setup(db)
    p = await make_product(db, gym)
    await login_as(client, await make_customer(db))
    html = (await client.get(f"/shop/p/{p.id}/{p.slug}")).text
    form = html[html.index("<form", html.index('data-component="shop-ask"')):]
    form = form[:form.index(">") + 1]
    assert "data-ask-form" in form and "data-no-busy" in form  # 00-core.js never leaves its button aria-busy
    assert "data-ask-wait" in html
