"""AI plan advisor: signed-in only, streamed answers, deterministic fallback, 30 km awareness, caching - plus the AI
guard and the fair AI queue (docs/ai-guard-and-queue.md):

* a guard refusal (quota, duplicate, spam, cooldown) is a ``blocked`` SSE event followed by ``done`` with
  ``blocked: true`` - never an answer, never a provider call, never cached; 'busy' keeps the standard answer;
* the guard judges the member's own words (tagged with the gym), never the prompt;
* the request's DB session is released before the stream starts;
* a requester who leaves (tab closed, user gone, client disconnected) stops the stream and releases the AI queue
  ticket and the provider stream at once; a cut or interrupted answer is never cached.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.ai import policy
from app.ai.client import FakeProvider, ProviderError, ai
from app.ai.owner import AIOwner
from app.ai.scheduler import scheduler
from app.config import get_settings
from app.routers import plan_ai
from app.services.presence import presence
from tests.ask_ai_helpers import (  # noqa: F401 - fixtures are used by name
    BLOCKED_KEYS,
    HeldStream,
    admitted,
    guard_on,
    leaving_client,
    pause_ai,
    qa_used,
    steady_minute,
    tracked_sessions,
)
from tests.conftest import api, login_as
from tests.factories import MYSURU, make_branch, make_customer, make_gym, make_owner, make_plans

PAGE = "plans-tab-0001-test"


def events(body: str) -> list[dict]:
    return [json.loads(line[5:]) for line in body.splitlines() if line.startswith("data:")]


async def _gym(db, name: str = "Pulse Central"):
    owner = await make_owner(db)
    gym = await make_gym(db, owner, name=name)
    await make_branch(db, gym)  # Indiranagar, Bengaluru
    await make_plans(db, gym)
    return gym


# ============================================================================================ the advisor
async def test_anonymous_users_cannot_ask(client, db):
    gym = await _gym(db)
    r = await api(client, "POST", "/api/plans/ask", json={"question": "Which plan?", "gym_slug": gym.slug})
    assert r.status_code == 401


async def test_streamed_ai_answer(client, db):
    gym = await _gym(db)
    user = await make_customer(db, conditions=["asthma"])
    await login_as(client, user)
    p = FakeProvider("gateway", responses=["Try Online + Trainer + AI - a trainer helps with asthma."])
    ai.use_providers([p])
    r = await api(client, "POST", "/api/plans/ask", json={"question": "Which plan suits me?", "gym_slug": gym.slug})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    evts = events(r.text)
    assert evts[0]["type"] == "meta" and any(e["type"] == "chunk" for e in evts)
    assert evts[-1]["type"] == "done" and "AI can make mistakes" in evts[-1]["disclaimer"]
    prompt = p.prompts[0]["prompt"]
    assert "Online + Trainer + Ai" in prompt or "online_trainer_ai" in prompt  # real plan names from the DB
    assert "asthma" in prompt and "<<<Which plan suits me?>>>" in prompt


async def test_fallback_when_ai_down_respects_30km_rule(client, db, ai_down):
    gym = await _gym(db)
    user = await make_customer(db, latitude=MYSURU[0], longitude=MYSURU[1])  # ~128 km from the branch
    await login_as(client, user)
    r = await api(client, "POST", "/api/plans/ask", json={"question": "I want to train at the gym", "gym_slug": gym.slug})
    evts = events(r.text)
    done = evts[-1]
    text = "".join(e.get("text", "") for e in evts if e["type"] == "chunk")
    assert done["fallback"] is True and "Suggested:" in text and "not near your area" in text
    assert "Online" in text.split("Suggested:")[1].split("-")[0]


async def test_answers_are_cached_for_identical_questions(client, db):
    gym = await _gym(db)
    user = await make_customer(db)
    await login_as(client, user)
    p = FakeProvider("gateway", responses=["Online is cheapest."])
    ai.use_providers([p])
    for _ in range(2):
        r = await api(client, "POST", "/api/plans/ask", json={"question": "Cheapest plan?", "gym_slug": gym.slug})
        assert r.status_code == 200
    assert len(p.prompts) == 1 and any(e.get("cached") for e in events(r.text))


async def test_validation(client, db):
    user = await make_customer(db)
    await login_as(client, user)
    r = await api(client, "POST", "/api/plans/ask", json={"question": "x"})
    assert r.status_code == 422
    r = await api(client, "POST", "/api/plans/ask", json={"question": "  x   "})  # 2+ characters only with the spaces
    assert r.status_code == 422


# ============================================================================================ guard refusals
async def test_quota_refusal_is_a_blocked_event_not_an_answer(client, db, guard_on, steady_minute):
    gym = await _gym(db)
    user = await make_customer(db)  # no AI plan: the 'member' tier
    await login_as(client, user)
    p = FakeProvider("gateway", responses=["Online + Trainer is a good start."])
    ai.use_providers([p])
    minute = policy.QUOTAS["member"]["qa"][0]
    for i in range(minute):
        r = await api(client, "POST", "/api/plans/ask", json={"question": f"Plan question number {i}?", "gym_slug": gym.slug})
        done = events(r.text)[-1]
        assert done["type"] == "done" and done["fallback"] is False
    cached = len(plan_ai._CACHE)

    r = await api(client, "POST", "/api/plans/ask", json={"question": "One question too many?", "gym_slug": gym.slug})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    evts = events(r.text)
    assert [e["type"] for e in evts] == ["blocked", "done"], evts
    blocked = evts[0]
    assert set(blocked) == BLOCKED_KEYS and blocked["code"] == "quota"
    assert 0 < blocked["retry_after"] <= 60 and "AI questions" in blocked["detail"]
    assert evts[1] == {"type": "done", "blocked": True}
    assert len(p.prompts) == minute  # the refused question never reached a provider ...
    assert len(plan_ai._CACHE) == cached  # ... and nothing was cached for it
    assert await qa_used(user.id, expect=minute) == minute  # refusals are never charged


async def test_duplicate_and_spam_refusals_are_blocked_events(client, db, guard_on):
    gym = await _gym(db)
    user = await make_customer(db)
    await login_as(client, user)
    p = FakeProvider("gateway", responses=["Rest 60-90 seconds between sets."])
    ai.use_providers([p])
    first = await api(client, "POST", "/api/plans/ask", json={"question": "Which plan helps me lose weight fastest?",
                                                              "gym_slug": gym.slug})
    assert events(first.text)[-1]["type"] == "done"
    # the same words (case and punctuation aside) are a different prompt - no cache hit - but the guard's duplicate
    again = await api(client, "POST", "/api/plans/ask", json={"question": "which plan helps me lose weight fastest",
                                                              "gym_slug": gym.slug})
    evts = events(again.text)
    assert [e["type"] for e in evts] == ["blocked", "done"] and evts[0]["code"] == "duplicate"
    assert 0 < evts[0]["retry_after"] <= policy.policy_for("plans").dedupe_seconds
    spam = await api(client, "POST", "/api/plans/ask", json={"question": "!" * 60, "gym_slug": gym.slug})
    evts = events(spam.text)
    assert [e["type"] for e in evts] == ["blocked", "done"]
    assert evts[0]["code"] == "spam" and evts[0]["retry_after"] == 0 and "spam" in evts[0]["detail"]
    assert len(p.prompts) == 1 and len(plan_ai._CACHE) == 1


async def test_cooldown_refusal_is_a_blocked_event(client, db, guard_on):
    gym = await _gym(db)
    user = await make_customer(db)
    await login_as(client, user)
    p = FakeProvider("gateway", responses=["never sent"])
    ai.use_providers([p])
    await pause_ai(user.id)
    r = await api(client, "POST", "/api/plans/ask", json={"question": "Which plan suits a beginner?", "gym_slug": gym.slug})
    evts = events(r.text)
    assert [e["type"] for e in evts] == ["blocked", "done"]
    assert evts[0]["code"] == "cooldown" and "paused" in evts[0]["detail"]
    assert 60 < evts[0]["retry_after"] <= policy.COOLDOWN_LADDER[0][1]
    assert p.prompts == [] and not plan_ai._CACHE


async def test_the_same_question_about_another_gym_is_not_a_duplicate(client, db, guard_on, admitted):
    gym_a, gym_b = await _gym(db), await _gym(db, name="Iron Temple")  # different prompts: no shared cached answer
    user = await make_customer(db)
    await login_as(client, user)
    ai.use_providers([FakeProvider("gateway", responses=["A trainer plan fits weight loss."])])
    for gym in (gym_a, gym_b):  # the same suggestion chip on two gyms' pages within seconds
        r = await api(client, "POST", "/api/plans/ask", json={"question": "Which plan fits weight loss?", "gym_slug": gym.slug})
        assert events(r.text)[-1]["type"] == "done" and not any(e["type"] == "blocked" for e in events(r.text))
    # the member's words untouched, each in the scope of its gym (no '[gym N]' tag mixed into what the guard judges)
    assert [(text, scope) for _, _, text, scope in admitted] == [("Which plan fits weight loss?", f"gym:{gym_a.id}"),
                                                                 ("Which plan fits weight loss?", f"gym:{gym_b.id}")]
    # ... while the same words about the same gym again are still a duplicate
    r = await api(client, "POST", "/api/plans/ask", json={"question": "which plan fits weight loss", "gym_slug": gym_a.slug})
    assert [e["type"] for e in events(r.text)] == ["blocked", "done"] and events(r.text)[0]["code"] == "duplicate"


async def test_the_guard_judges_the_members_words_never_the_prompt(client, db, guard_on, admitted):
    gym = await _gym(db)
    user = await make_customer(db, conditions=["asthma"])
    await login_as(client, user)
    p = FakeProvider("gateway", responses=["Online + Trainer."])
    ai.use_providers([p])
    await api(client, "POST", "/api/plans/ask", json={"question": "  Which   plan suits me? ", "gym_slug": gym.slug})
    await api(client, "POST", "/api/plans/ask", json={"question": "Is a trainer worth it?"})  # the generic plans page
    assert admitted == [(user.id, "plans", "Which plan suits me?", f"gym:{gym.id}"),
                        (user.id, "plans", "Is a trainer worth it?", None)]
    assert all("<<<" not in text and "asthma" not in text and "gym" not in text for _, _, text, _ in admitted)
    assert "<<<Which plan suits me?>>>" in p.prompts[0]["prompt"]


async def test_a_short_question_keeps_the_guards_short_double_submit_window(client, db, guard_on, monkeypatch):
    """The guard sees exactly what the member typed: a short question ('Price?') gets the guard's short-reply duplicate
    window (a double-submit guard of a few seconds) - a '[gym N] ' tag in front made it a long text with the full
    30-second window, so asking again a few seconds later was refused."""
    from app.ai import guard as guard_module

    monkeypatch.setattr(guard_module, "SHORT_REPLY_DEDUPE", 1.0)  # the few seconds, shortened for the test
    gym = await _gym(db)
    user = await make_customer(db)
    await login_as(client, user)
    p = FakeProvider("gateway", handler=lambda **kw: "Online starts at 999.")
    ai.use_providers([p])

    async def ask(question: str) -> list[dict]:  # each spelling is a different prompt: never a cached answer
        r = await api(client, "POST", "/api/plans/ask", json={"question": question, "gym_slug": gym.slug})
        return events(r.text)

    assert ask_ok(await ask("Price?"))
    double = await ask("price")  # a double submit right away: still a duplicate
    assert [e["type"] for e in double] == ["blocked", "done"] and double[0]["code"] == "duplicate"
    await asyncio.sleep(1.2)
    assert ask_ok(await ask("PRICE!")), "asked again a moment later: answered, not refused as a duplicate"
    assert len(p.prompts) == 2


def ask_ok(evts: list[dict]) -> bool:
    """An AI answer: no refusal, and a 'done' that is not the deterministic fallback."""
    return (bool(evts) and evts[-1]["type"] == "done" and evts[-1].get("fallback") is False
            and not any(e["type"] == "blocked" for e in evts))


async def test_busy_queue_keeps_the_standard_answer(client, db, guard_on, monkeypatch):
    gym = await _gym(db)
    user = await make_customer(db)
    await login_as(client, user)
    p = FakeProvider("gateway", responses=["never sent"])
    ai.use_providers([p])
    monkeypatch.setattr(get_settings(), "ai_queue_max_total", 0)  # the AI queue refuses everyone: 'busy'
    r = await api(client, "POST", "/api/plans/ask", json={"question": "Which plan should I pick?", "gym_slug": gym.slug})
    evts = events(r.text)
    assert [e["type"] for e in evts] == ["meta", "chunk", "done"]
    assert evts[0] == {"type": "meta", "provider": None, "busy": True}
    assert "Suggested:" in evts[1]["text"]
    assert evts[2]["fallback"] is True and evts[2]["busy"] is True and evts[2]["note"] == plan_ai.BUSY_NOTE
    assert p.prompts == [] and not plan_ai._CACHE
    assert await qa_used(user.id, expect=0) == 0  # the admission was refunded: nobody pays for a busy queue


# ============================================================================================ caching rules
async def test_a_complete_answer_that_ends_like_a_cut_one_is_cached(client, db):
    """Whether an answer was cut short is the router's flag (``meta['truncated']``), not the look of its last words:
    a complete answer whose last chunk happens to be the ' …' of a cut answer is cached like any other."""
    gym = await _gym(db)
    user = await make_customer(db)
    await login_as(client, user)
    p = FakeProvider("gateway", chunks=["Online is the cheapest - and Combo", " …"])
    ai.use_providers([p])
    for _ in range(2):
        r = await api(client, "POST", "/api/plans/ask", json={"question": "Cheapest plan?", "gym_slug": gym.slug})
        evts = events(r.text)
        assert "".join(e["text"] for e in evts if e["type"] == "chunk") == "Online is the cheapest - and Combo …"
        assert evts[-1]["type"] == "done" and evts[-1]["fallback"] is False
    assert len(p.prompts) == 1 and any(e.get("cached") for e in evts)  # the second ask was answered from the cache


async def test_an_answer_cut_at_the_time_limit_is_shown_but_never_cached(client, db, monkeypatch):
    # about caching, not limits: a cut answer is charged, so the guard would refuse the same question again at once
    monkeypatch.setattr(get_settings(), "ai_guard_enabled", False)
    monkeypatch.setattr(plan_ai, "TOTAL_TIMEOUT", 0.25)
    gym = await _gym(db)
    user = await make_customer(db)
    await login_as(client, user)
    p = FakeProvider("gateway", chunks=["Online ", "is ", "the ", "cheapest ", "plan ", "here."], delay=0.1)
    ai.use_providers([p])
    for _ in range(2):
        r = await api(client, "POST", "/api/plans/ask", json={"question": "Cheapest plan?", "gym_slug": gym.slug})
        evts = events(r.text)
        text = "".join(e["text"] for e in evts if e["type"] == "chunk")
        assert text.startswith("Online ") and text.endswith(" …") and "here." not in text  # cut by the router
        assert evts[-1]["type"] == "done" and not any(e.get("cached") for e in evts)
    assert len(p.prompts) == 2 and not plan_ai._CACHE  # asked the AI again: the cut answer was never cached


async def test_an_interrupted_answer_is_shown_but_never_cached(client, db, monkeypatch):
    # about caching, not limits (what the guard does after an interrupted answer: the next test)
    monkeypatch.setattr(get_settings(), "ai_guard_enabled", False)

    class Interrupted(FakeProvider):
        async def stream_text(self, **kwargs: Any):
            async with self.slot():
                self.prompts.append(kwargs)
                yield "Online plans start at "
                raise ProviderError("connection reset by the gateway")

    gym = await _gym(db)
    user = await make_customer(db)
    await login_as(client, user)
    p = Interrupted("gateway")
    ai.use_providers([p])
    for _ in range(2):
        r = await api(client, "POST", "/api/plans/ask", json={"question": "How much is online?", "gym_slug": gym.slug})
        evts = events(r.text)
        text = "".join(e["text"] for e in evts if e["type"] == "chunk")
        assert text.startswith("Online plans start at ") and text.endswith("(the answer was interrupted)")
        assert evts[-1]["type"] == "done" and not any(e.get("cached") for e in evts)
    assert len(p.prompts) == 2 and not plan_ai._CACHE  # asked the AI again: the cut answer was never cached


async def test_asking_again_right_after_an_interrupted_answer_is_not_a_duplicate(client, db, guard_on):
    """The provider broke off mid-answer: app/ai/client.py settles that call as not ok, so the member is not charged
    for the cut answer and the guard forgets its fingerprint - the same question asked again at once is answered,
    not refused with 'You just asked that'."""
    class Interrupted(FakeProvider):
        async def stream_text(self, **kwargs: Any):
            async with self.slot():
                self.prompts.append(kwargs)
                yield "Online plans start at "
                raise ProviderError("connection reset by the gateway")

    gym = await _gym(db)
    user = await make_customer(db)
    await login_as(client, user)
    p = Interrupted("gateway")
    ai.use_providers([p])
    for _ in range(2):
        r = await api(client, "POST", "/api/plans/ask", json={"question": "How much is online?", "gym_slug": gym.slug})
        evts = events(r.text)
        assert not any(e["type"] == "blocked" for e in evts), evts
        assert "".join(e["text"] for e in evts if e["type"] == "chunk").endswith("(the answer was interrupted)")
        assert await qa_used(user.id, expect=0) == 0  # refunded: nobody pays for an answer that broke off
    assert len(p.prompts) == 2


async def test_a_stream_cut_short_by_the_queue_ends_without_done_and_is_never_cached(client, db):
    gym = await _gym(db)
    user = await make_customer(db)
    await login_as(client, user)
    provider = HeldStream()
    ai.use_providers([provider])
    task = asyncio.create_task(api(client, "POST", "/api/plans/ask", json={"question": "Which plan?", "gym_slug": gym.slug}))
    await asyncio.wait_for(provider.waiting.wait(), 5)
    assert scheduler.cancel(user_id=user.id, kinds={"user"}, reason="left") == 1  # presence: the user left
    r = await asyncio.wait_for(task, 5)
    evts = events(r.text)
    assert [e["type"] for e in evts] == ["meta", "chunk"]  # just ends: nobody is listening any more
    assert not plan_ai._CACHE and provider.streams_closed == 1 and not provider.semaphore.locked()
    snap = scheduler.snapshot()
    assert snap["lanes"]["llm"]["running"] == 0 and snap["counters"]["cancelled"] == {"left": 1}


async def test_closing_the_tab_before_the_first_words_ends_the_stream_silently(client, db, guard_on):
    gym = await _gym(db)
    user = await make_customer(db)
    await login_as(client, user)
    provider = HeldStream(before=True)
    ai.use_providers([provider])
    task = asyncio.create_task(api(client, "POST", "/api/plans/ask", json={"question": "Which plan?", "gym_slug": gym.slug},
                                   headers={"X-Page-Id": PAGE}))
    await asyncio.wait_for(provider.entered.wait(), 5)
    assert presence.page_left(PAGE, user_id=user.id) == 1  # pagehide -> socket closed with 4001
    r = await asyncio.wait_for(task, 5)
    assert r.status_code == 200 and events(r.text) == []  # AICancelled: no answer, no fallback, no error
    assert not plan_ai._CACHE and provider.streams_closed == 1
    snap = scheduler.snapshot()
    assert snap["lanes"]["llm"]["running"] == 0 and snap["counters"]["cancelled"] == {"page_closed": 1}
    assert await qa_used(user.id, expect=0) == 0  # refunded


# ============================================================================================ resources
async def test_the_request_db_session_is_released_before_the_ai_streams(client, db, tracked_sessions):
    gym = await _gym(db)
    user = await make_customer(db, conditions=["asthma"])
    await login_as(client, user)
    seen: list[tuple[bool, int]] = []

    def handler(**kwargs: Any) -> str:
        session = tracked_sessions[-1]
        seen.append((session.in_transaction(), len(session.identity_map)))
        return "Online + Trainer."

    ai.use_providers([FakeProvider("gateway", handler=handler)])
    r = await api(client, "POST", "/api/plans/ask", json={"question": "Which plan suits me?", "gym_slug": gym.slug})
    assert events(r.text)[-1]["type"] == "done"
    # the request's own session (the last one opened) was closed - no transaction, so no pooled connection, and
    # nothing loaded - while the AI answered
    assert seen == [(False, 0)]


async def test_closing_the_event_stream_at_a_yield_releases_the_ticket_at_once():
    """contextlib.aclosing: when the response stops iterating mid-answer (client gone), closing the route's generator
    closes ai.stream right away - queue ticket, guard admission and provider stream - not at garbage collection."""
    provider = FakeProvider("gateway", chunks=["Online ", "is ", "best."], delay=0.01, max_concurrency=1)
    ai.use_providers([provider])
    gen = plan_ai.answer_events(prompt="p", cache_key="k-aclosing", fallback_text="standard answer",
                                user_text="Which plan?", owner=AIOwner(user_id=91, role="customer"))
    assert json.loads((await anext(gen))[5:])["type"] == "meta"
    assert json.loads((await anext(gen))[5:]) == {"type": "chunk", "text": "Online "}
    assert scheduler.snapshot()["lanes"]["llm"]["running"] == 1 and provider.semaphore.locked()
    await gen.aclose()  # what EventStreamResponse does when the client went away
    snap = scheduler.snapshot()
    assert snap["lanes"]["llm"]["running"] == 0 and snap["counters"]["cancelled"] == {"aborted": 1}
    assert provider.streams_closed == 1 and not provider.semaphore.locked()
    assert "k-aclosing" not in plan_ai._CACHE


@pytest.mark.parametrize("server", ["spec 2.3: cancelled on http.disconnect", "spec 2.4: ClientDisconnect on send"])
async def test_a_client_that_disconnects_mid_answer_releases_its_ai_ticket(db, guard_on, server):
    gym = await _gym(db)
    user = await make_customer(db)
    gone = asyncio.Event()
    if server.startswith("spec 2.3"):
        provider = HeldStream()  # holds after the first words: the cancel lands inside the stream
        c = leaving_client(gone, spec="2.3")
    else:
        provider = HeldStream(more=[f"word {i} " for i in range(200)])  # keeps writing: the next send fails
        c = leaving_client(gone, spec="2.4")
    ai.use_providers([provider])
    async with c:
        await login_as(c, user)
        task = asyncio.create_task(api(c, "POST", "/api/plans/ask", json={"question": "Which plan?", "gym_slug": gym.slug}))
        await asyncio.wait_for(provider.waiting.wait(), 5)
        assert scheduler.snapshot()["lanes"]["llm"]["running"] == 1
        gone.set()
        r = await asyncio.wait_for(task, 5)
        # released by the time the response is over - not whenever the garbage collector finalises the generators
        assert scheduler.snapshot()["lanes"]["llm"]["running"] == 0
        assert provider.streams_closed == 1 and not provider.semaphore.locked()
    assert '"type": "done"' not in r.text and not plan_ai._CACHE
    assert scheduler.snapshot()["counters"]["cancelled"] == {"aborted": 1}
    assert provider.state.failures == 0  # leaving is never a provider failure
    # its first words went out before the client left: charged (only a stream stopped before its first words is
    # free - dropping the connection mid-answer must not be a way around the quotas)
    from app.ai.guard import guard

    assert (await guard.status(user.id))["usage"]["qa"][2] == 1
