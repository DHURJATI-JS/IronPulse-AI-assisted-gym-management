"""The live AI workout coach under the AI guard and the AI queue (docs/ai-guard-and-queue.md).

* the member's own words (never the prompt) reach the guard as ``user_text``;
* a refused reply - AI paused on the account, chat quota, a repeated phrase, spam - is still HTTP 200: one short
  spoken line (nothing at all for a repeat) plus ``refused: {code, detail, retry_after}``, and the workout goes on:
  commands, events, discomfort and red flags never reach the AI, so they are never refused;
* while the browser waits a refusal out it sends ``"ai": false``: safety checks and commands run as always, chat gets
  no AI call and no guard check (so the wait never grows);
* a full queue keeps the deterministic fallback; a member who leaves mid-reply (connection dropped, tab closed) stops
  the AI call and nothing is said - the reply is charged, the provider was already working on it.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from sqlalchemy import select

from app.ai import live_coach as ai_live
from app.ai import policy
from app.ai.errors import AIRejected
from app.ai.guard import guard
from app.ai.owner import AIOwner
from app.config import get_settings
from app.models import WorkoutSession, WorkoutSessionEvent
from app.services import live_coach as live
from app.services.presence import presence
from tests.conftest import api, login_as
from tests.test_live_coach import ai_json, make_member, reload, seed_library, start

PAGE = "live-page-0001-abcdef"


@pytest.fixture(autouse=True)
def _guard_on(monkeypatch):
    """The AI guard as configured by default, whatever the local .env says."""
    monkeypatch.setattr(get_settings(), "ai_guard_enabled", True)
    monkeypatch.setattr(get_settings(), "ai_quota_scale", 1.0)


async def message(client, sid: int, text: str, *, ai: bool | None = None, via: str = "voice",
                  headers: dict[str, str] | None = None, **state: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"text": text, "via": via, "state": {"index": 0, "set": 1, "phase": "work", **state}}
    if ai is not None:
        body["ai"] = ai
    r = await api(client, "POST", f"/api/live/sessions/{sid}/message", json=body, headers=headers or {})
    assert r.status_code == 200, r.text
    return r.json()


async def pause_ai(user_id: int) -> AIRejected:
    """Two spam refusals elsewhere (3 strike points each) pause AI on the account: cooldown ladder level 1."""
    owner = AIOwner(user_id=user_id, role="customer")
    codes = []
    refusal: AIRejected | None = None
    for _ in range(2):
        with pytest.raises(AIRejected) as refused:
            await guard.admit(owner, "assistant", "!" * 60)
        refusal = refused.value
        codes.append(refusal.code)
    assert codes == ["spam", "cooldown"] and refusal is not None
    return refusal


async def events(db, sid: int) -> list[WorkoutSessionEvent]:
    db.expire_all()
    return list((await db.execute(select(WorkoutSessionEvent).where(WorkoutSessionEvent.session_id == sid)
                                  .order_by(WorkoutSessionEvent.id))).scalars().all())


async def chat_usage(user_id: int) -> int:
    return (await guard.status(user_id))["usage"]["chat"][0]


class Member:
    """The signed-in member's ids (read once: tests expire the ORM session to see fresh rows)."""

    def __init__(self, user: Any) -> None:
        self.id = user.id


@pytest.fixture
async def member_session(client, db):
    """A member with an AI plan, signed in, with a quick session running."""
    await seed_library(db)
    customer, *_ = await make_member(db)
    await login_as(client, customer)
    s = await start(client)
    return Member(customer), s


# ============================================================================================ user_text
async def test_the_members_own_words_reach_the_guard(client, db, fake_ai, member_session, monkeypatch):
    customer, s = member_session
    provider = fake_ai(responses=[ai_json("Sit back into your heels and keep your chest up.")])
    seen: list[tuple[int | None, str, str | None]] = []
    real_admit = guard.admit

    async def spy(owner, feature, text=None):
        seen.append((owner.user_id if owner is not None else None, feature, text))
        return await real_admit(owner, feature, text)

    monkeypatch.setattr(guard, "admit", spy)
    out = await message(client, s["id"], "  how   deep should I squat ")
    assert out["source"] == "ai" and "refused" not in out
    assert seen == [(customer.id, "live_coach", "how deep should I squat")]  # the words - not the prompt
    prompt = provider.prompts[0]["prompt"]
    assert "how deep should I squat" in prompt and prompt != seen[0][2]
    # commands and safety flows never reach the AI, so the guard never judges them
    await message(client, s["id"], "next")
    await message(client, s["id"], "my knee hurts")
    assert len(seen) == 1 and len(provider.prompts) == 1


# ============================================================================================ AI paused (cooldown)
async def test_ai_pause_refusal_is_spoken_and_the_workout_carries_on(client, db, fake_ai, member_session):
    customer, s = member_session
    sid = s["id"]
    provider = fake_ai(responses=[ai_json("Great question!", "happy", "intensity_up")])
    await pause_ai(customer.id)
    strikes = (await guard.status(customer.id))["strikes"]

    out = await message(client, sid, "how deep should I squat")
    assert out["source"] == "refused" and out["ai_available"] is True
    assert out["reply"] == "Let's keep going - I'll be back to chat in 2 minutes. Commands like 'next' still work."
    assert out["emotion"] == "calm" and out["actions"] == []
    refused = out["refused"]
    assert refused["code"] == "cooldown" and "paused" in refused["detail"]
    assert 60 < refused["retry_after"] <= policy.COOLDOWN_LADDER[0][1]
    assert out["session"]["status"] == "active" and out["session"]["intensity"] == 0  # no AI action was applied
    assert not provider.prompts
    logged = [e for e in await events(db, sid) if e.kind == "ai_refused"]
    assert len(logged) == 1 and logged[0].detail["code"] == "cooldown" and logged[0].message == out["reply"]
    assert (await guard.status(customer.id))["strikes"] == strikes  # waiting out a pause earns no strikes

    # the workout itself never needed the AI
    out = await message(client, sid, "next")
    assert out["session"]["current_index"] == 1 and out["actions"][0]["type"] == "next"
    out = await message(client, sid, "easier")
    assert out["session"]["intensity"] == -1 and out["source"] == "rule"
    r = await api(client, "POST", f"/api/live/sessions/{sid}/event", json={"type": "set_done", "state": {"index": 1}})
    assert r.status_code == 200 and r.json()["reply"]
    assert not provider.prompts
    db_s = await reload(db, WorkoutSession, sid)
    assert db_s.ai_messages == 0 and db_s.status == "active"


async def test_safety_flows_are_never_refused_during_an_ai_pause(client, db, fake_ai, member_session):
    customer, s = member_session
    sid = s["id"]
    provider = fake_ai(responses=[ai_json("should never be used")])
    await pause_ai(customer.id)
    out = await message(client, sid, "my knee hurts")  # spoken discomfort: the deterministic flow, as always
    assert out["intent"]["kind"] == "discomfort" and "refused" not in out
    assert {a["type"] for a in out["actions"]} >= {"pause"}
    r = await api(client, "POST", f"/api/live/sessions/{sid}/discomfort", json={"body_part": "back", "severity": "mild"})
    assert r.status_code == 200 and "refused" not in r.json()
    out = await message(client, sid, "I feel dizzy")  # a red flag: stopped for safety, never refused
    assert out["source"] == "safety" and out["session"]["status"] == "stopped_for_safety" and "refused" not in out
    out = await message(client, sid, "let's continue, I'm fine")
    assert out["session"]["status"] == "stopped_for_safety" and out["source"] == "safety"
    assert not provider.prompts


# ============================================================================================ spam / repeats
async def test_spam_is_answered_with_i_didnt_catch_that(client, db, fake_ai, member_session):
    customer, s = member_session
    provider = fake_ai(responses=[ai_json("should never be used")])
    out = await message(client, s["id"], "a" * 30)
    assert out["source"] == "refused" and out["reply"] == "I didn't catch that."
    assert out["refused"]["code"] == "spam" and out["refused"]["retry_after"] == 0
    assert out["actions"] == [] and not provider.prompts
    assert (await guard.status(customer.id))["strikes"] == policy.STRIKE_POINTS["spam"]


async def test_a_repeated_phrase_is_ignored_silently(client, db, fake_ai, member_session):
    customer, s = member_session
    sid = s["id"]
    provider = fake_ai(responses=[ai_json("Mostly your quads and glutes.", "happy")])
    first = await message(client, sid, "what muscles does this work")
    assert first["source"] == "ai" and first["reply"] == "Mostly your quads and glutes."
    again = await message(client, sid, "What muscles does this work?")  # the recogniser heard it twice
    assert again["source"] == "refused" and again["reply"] == "" and again["emotion"] == "neutral"
    assert again["refused"]["code"] == "duplicate" and again["actions"] == []
    assert 0 < again["refused"]["retry_after"] <= policy.policy_for("live_coach").dedupe_seconds
    assert len(provider.prompts) == 1
    assert (await guard.status(customer.id))["strikes"] == 0  # the first repeats are free
    said = [e.kind for e in await events(db, sid) if e.kind in ("ai_said", "ai_refused")]
    assert said == ["ai_said", "ai_refused"]
    assert (await reload(db, WorkoutSession, sid)).ai_messages == 1


# ============================================================================================ quota + "ai": false
async def test_quota_refusal_says_when_chat_is_back_and_paused_chat_skips_the_ai(client, db, fake_ai, member_session,
                                                                               monkeypatch):
    # an AI member gets 1 chat reply per minute AND per hour: the refusal never depends on a minute boundary
    monkeypatch.setattr(get_settings(), "ai_quota_scale", 1 / 150)
    customer, s = member_session
    sid = s["id"]
    provider = fake_ai(responses=[ai_json("Brace your core and breathe out as you stand.")])
    seen: list[str] = []
    real_admit = guard.admit

    async def spy(owner, feature, text=None):
        seen.append(feature)
        return await real_admit(owner, feature, text)

    monkeypatch.setattr(guard, "admit", spy)
    assert (await message(client, sid, "question one about my form"))["source"] == "ai"
    out = await message(client, sid, "question two about breathing")
    assert out["refused"]["code"] == "quota" and "chat messages" in out["refused"]["detail"]
    assert out["refused"]["retry_after"] > 0
    wait = ai_live.spoken_wait(out["refused"]["retry_after"])
    assert out["reply"] == f"Let's keep going - I'll be back to chat in {wait}. Commands like 'next' still work."
    assert seen == ["live_coach", "live_coach"]

    # the browser now sends "ai": false until the wait is over: no AI, no guard - safety and commands as always
    out = await message(client, sid, "thanks coach", ai=False)
    assert out["reply"].startswith("You're welcome") and out["chat_paused"] is True and out["source"] == "rule"
    out = await message(client, sid, "tell me about protein", ai=False)
    assert out["reply"] == "" and out["source"] == "chat_paused" and out["actions"] == []
    out = await message(client, sid, "next", ai=False)
    assert out["session"]["current_index"] == 1
    out = await message(client, sid, "I feel dizzy", ai=False)
    assert out["session"]["status"] == "stopped_for_safety"
    assert seen == ["live_coach", "live_coach"] and len(provider.prompts) == 1
    assert await chat_usage(customer.id) == 1  # refusals and paused chat are never charged


async def test_paused_chat_flag_is_optional_and_validated(client, db, fake_ai, member_session):
    _, s = member_session
    fake_ai(responses=[ai_json("Nice and steady.")])
    r = await api(client, "POST", f"/api/live/sessions/{s['id']}/message", json={"text": "how is my pace", "ai": "maybe"})
    assert r.status_code == 422
    assert (await message(client, s["id"], "how is my pace"))["source"] == "ai"  # default: AI on


# ============================================================================================ queue
async def test_busy_queue_keeps_the_deterministic_fallback(client, db, fake_ai, member_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "ai_queue_max_total", 0)  # the AI queue takes no more waiting work
    customer, s = member_session
    provider = fake_ai(responses=[ai_json("should never be used")])
    out = await message(client, s["id"], "how deep should I squat")
    assert out["source"] == "fallback" and out["reply"] == ai_live.REFUSAL_REPLIES["busy"]
    assert out["refused"]["code"] == "busy" and out["refused"]["retry_after"] > 0 and out["actions"] == []
    out = await message(client, s["id"], "thanks coach")
    assert out["reply"].startswith("You're welcome") and out["refused"]["code"] == "busy"
    assert not provider.prompts and await chat_usage(customer.id) == 0  # refunded


def test_reply_budget_fits_the_queue_wait_and_the_browser_timeout():
    """The queue answers 'busy' on its own (max_wait) shortly before the whole exchange is cut (TOTAL_TIMEOUT_S), and
    the server always answers before the browser gives up on the request (12 s)."""
    max_wait = policy.policy_for(ai_live.FEATURE).max_wait
    assert ai_live.FEATURE == "live_coach" and policy.policy_for("live_coach").priority == policy.REALTIME
    assert max_wait < ai_live.TOTAL_TIMEOUT_S < 12
    assert ai_live.TIMEOUT_S <= ai_live.TOTAL_TIMEOUT_S


@pytest.mark.parametrize("code,retry_after,expected", [
    ("cooldown", 120, "Let's keep going - I'll be back to chat in 2 minutes. Commands like 'next' still work."),
    ("cooldown", 900, "Let's keep going - I'll be back to chat in 15 minutes. Commands like 'next' still work."),
    ("cooldown", 3600, "Let's keep going - I'll be back to chat in an hour. Commands like 'next' still work."),
    ("cooldown", 86400, "Let's keep going - I'll be back to chat in 24 hours. Commands like 'next' still work."),
    ("quota", 42, "Let's keep going - I'll be back to chat in 42 seconds. Commands like 'next' still work."),
    ("duplicate", 3, ""),
    ("spam", 0, "I didn't catch that."),
    ("too_many", 5, "One moment - I'm still answering you."),
    ("busy", 5, ai_live.REFUSAL_REPLIES["busy"]),
])
def test_refusal_lines_are_short_and_spoken(code, retry_after, expected):
    reply = ai_live.refusal_reply(AIRejected(code, "detail", retry_after=retry_after))
    assert reply == expected and len(reply) <= live.REPLY_MAX
    assert live.clean_reply(reply) == reply  # nothing the voice would stumble over


# ============================================================================================ the member leaves
async def test_a_dropped_connection_cancels_an_in_flight_reply(client, db, fake_ai, member_session, monkeypatch):
    import app.ai.client as client_module
    from starlette.requests import Request

    customer, s = member_session
    sid = s["id"]
    monkeypatch.setattr(client_module, "DISCONNECT_POLL_SECONDS", 0.05)
    gone = asyncio.Event()

    async def is_disconnected(self) -> bool:
        return gone.is_set()

    monkeypatch.setattr(Request, "is_disconnected", is_disconnected)
    started: list[str] = []
    cancelled: list[str] = []

    async def thinking(**kwargs):
        started.append(kwargs["prompt"])
        gone.set()  # the browser gives up while the AI thinks (newer message, timeout, tab closed)
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append(kwargs["prompt"])
            raise
        return ai_json("too late")

    fake_ai(handler=thinking)
    began = time.monotonic()
    out = await message(client, sid, "how is my form looking today")
    assert time.monotonic() - began < 3  # stopped by the disconnect watcher, not by a timeout
    assert out["cancelled"] is True and out["source"] == "cancelled" and out["reply"] == "" and out["actions"] == []
    assert len(started) == 1 and len(cancelled) == 1  # the provider call itself was stopped
    kinds = [e.kind for e in await events(db, sid)]
    assert kinds[-1] == "user_said" and "ai_said" not in kinds and "ai_refused" not in kinds  # nothing was said
    assert (await reload(db, WorkoutSession, sid)).ai_messages == 0
    assert (await guard.status(customer.id))["usage"]["chat"][2] == 1  # charged: the provider was working


async def test_closing_the_tab_cancels_an_in_flight_reply(client, db, fake_ai, member_session):
    customer, s = member_session
    hold = asyncio.Event()
    started = asyncio.Event()

    async def thinking(**kwargs):
        started.set()
        await hold.wait()
        return ai_json("too late")

    fake_ai(handler=thinking)
    body = {"text": "should I eat before training", "state": {"index": 0, "set": 1}}
    task = asyncio.create_task(api(client, "POST", f"/api/live/sessions/{s['id']}/message", json=body,
                                   headers={"X-Page-Id": PAGE}))
    await asyncio.wait_for(started.wait(), 5)
    assert presence.page_left(PAGE, user_id=customer.id, reason="page_closed") == 1  # pagehide -> close code 4001
    r = await asyncio.wait_for(task, 5)
    hold.set()
    assert r.status_code == 200 and r.json()["cancelled"] is True and r.json()["reply"] == ""
    assert (await guard.status(customer.id))["usage"]["chat"][2] == 1  # charged: the provider was working
