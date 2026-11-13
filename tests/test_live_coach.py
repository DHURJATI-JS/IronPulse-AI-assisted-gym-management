"""WP-K live AI workout companion: pages, intent + safety layer, AI validation, access control, N+1 guard, and no pooled
DB connection held while the coach's AI thinks."""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import select

from app.db import QueryCounter
from app.models import (
    AuditLog,
    CustomerProfile,
    Exercise,
    HealthAlert,
    Notification,
    SuspensionEvent,
    TrainerClient,
    WorkoutPlan,
    WorkoutSession,
    WorkoutSessionEvent,
)
from app.models.enums import AnomalyLevel, Goal, ShiftMode
from app.services import live_coach as live
from tests.ask_ai_helpers import (  # noqa: F401 - pool_probe is a fixture
    connections_while_the_ai_works,
    pool_probe,
    wait_until,
)
from tests.conftest import api, login_as, post_form
from tests.factories import make_customer, make_employee, make_gym, make_owner, make_plans, make_subscription

# ====================================================================================== fixtures / helpers
LIBRARY = [
    # slug, name, category, muscles, contraindications, impact, intensity, cues
    ("bodyweight-squat", "Bodyweight squat", "calisthenics", ["quads", "glutes"], [], "low", "moderate", ["Knees track over toes", "Chest up"]),
    ("jump-squat", "Jump squat", "calisthenics", ["quads", "glutes"], ["knee_injury", "ankle_injury", "pregnancy"], "high", "high", ["Land softly"]),
    ("reverse-lunge", "Reverse lunge", "calisthenics", ["quads", "glutes"], ["knee_injury"], "low", "moderate", ["Step back softly"]),
    ("push-up", "Push-up", "calisthenics", ["chest", "triceps", "shoulders"], ["wrist_injury"], "low", "moderate", ["Elbows at 45 degrees"]),
    ("incline-push-up", "Incline push-up", "calisthenics", ["chest", "triceps"], ["wrist_injury"], "low", "low", ["Brace your core"]),
    ("glute-bridge", "Glute bridge", "calisthenics", ["glutes", "hamstrings"], ["pregnancy"], "low", "low", ["Squeeze at the top"]),
    ("bird-dog", "Bird dog", "calisthenics", ["core", "glutes", "back"], [], "low", "low", ["Keep the hips level"]),
    ("dead-bug", "Dead bug", "calisthenics", ["core"], ["pregnancy"], "low", "low", ["Low back stays down"]),
    ("forearm-plank", "Forearm plank", "calisthenics", ["core"], [], "low", "moderate", ["Breathe normally"]),
    ("inverted-row", "Inverted row", "calisthenics", ["back", "lats", "biceps"], [], "low", "moderate", ["Squeeze the shoulder blades"]),
    ("lat-pulldown", "Lat pulldown", "strength", ["back", "lats", "biceps"], [], "low", "moderate", ["Lead with the elbows"]),
    ("overhead-press", "Dumbbell overhead press", "strength", ["shoulders", "triceps"], ["shoulder_injury"], "low", "moderate", ["Ribs down"]),
    ("goblet-squat", "Goblet squat", "strength", ["quads", "glutes"], [], "low", "moderate", ["Chest up"]),
    ("brisk-walk", "Brisk incline walk", "cardio", ["legs", "heart"], [], "low", "low", ["You should still be able to talk"]),
    ("stationary-bike", "Stationary bike", "cardio", ["legs", "heart"], [], "low", "moderate", ["Saddle at hip height"]),
    ("easy-jog", "Easy jog", "cardio", ["legs", "heart"], ["knee_injury", "ankle_injury"], "high", "moderate", ["Short, quick steps"]),
    ("cat-cow", "Cat-cow", "mobility", ["spine", "core"], [], "low", "low", ["Move with your breath"]),
    ("shoulder-circles", "Shoulder circles", "mobility", ["shoulders"], [], "low", "low", ["Stay below any pain"]),
    ("hip-flexor-stretch", "Hip flexor stretch", "mobility", ["hips", "quads"], [], "low", "low", ["Squeeze the back glute"]),
    ("diaphragmatic-breathing", "Diaphragmatic breathing", "breathing", ["diaphragm", "core"], [], "low", "low", ["Inhale 4, exhale 6"]),
]
KNEE_UNSAFE = {"Jump squat", "Reverse lunge", "Easy jog"}


async def seed_library(db) -> None:
    for slug, name, cat, muscles, contra, impact, intensity, cues in LIBRARY:
        db.add(Exercise(slug=slug, name=name, category=cat, goals=["general_fitness"], muscles=muscles, equipment="none",
                        impact=impact, intensity=intensity, description=f"{name} description.", cues=cues,
                        contraindications=contra))
    await db.commit()


async def make_member(db, *, ai: bool = True, conditions: list[str] | None = None, country: str | None = "IN",
                      subscribed: bool = True, **kwargs):
    owner = await make_owner(db)
    gym = await make_gym(db, owner, country=country or "IN")
    plans = {p.code: p for p in await make_plans(db, gym)}
    customer = await make_customer(db, conditions=conditions, country_code=country, **kwargs)
    if subscribed:
        await make_subscription(db, customer, plans["online_trainer_ai" if ai else "online_trainer"])
    return customer, owner, gym


async def assign_trainer(db, gym, customer):
    emp = await make_employee(db, gym)
    db.add(TrainerClient(employee_id=emp.id, customer_id=customer.id, mode=ShiftMode.ONLINE, active=True))
    await db.commit()
    return emp


async def make_plan(db, customer, days: list[dict] | None = None) -> WorkoutPlan:
    days = days or [
        {"name": "Lower body", "focus": "Legs", "items": [
            {"exercise": "Bodyweight squat", "sets": 3, "reps": 12, "rest_s": 60, "load": "bodyweight"},
            {"exercise": "Jump squat", "sets": 3, "reps": 10, "rest_s": 60},
            {"exercise": "Glute bridge", "sets": 2, "reps": 15, "rest_s": 45},
        ]},
        {"name": "Upper body", "focus": "Push / pull", "items": [
            {"exercise": "Push-up", "sets": 3, "reps": 10, "rest_s": 60},
            {"exercise": "Inverted row", "sets": 3, "reps": 10, "rest_s": 60},
        ]},
    ]
    plan = WorkoutPlan(customer_id=customer.id, goal=Goal.GENERAL_FITNESS, source="template", title="Starter plan",
                       plan={"days": days})
    db.add(plan)
    await db.commit()
    return plan


async def start(client, *, quick: str | None = "full_body", **body) -> dict:
    if quick and "plan_id" not in body:
        body.setdefault("quick", quick)
    r = await api(client, "POST", "/api/live/sessions", json=body)
    assert r.status_code == 200, r.text
    return r.json()["session"]


async def say(client, sid: int, text: str, **state) -> dict:
    r = await api(client, "POST", f"/api/live/sessions/{sid}/message",
                  json={"text": text, "state": {"index": 0, "set": 1, "phase": "work", **state}})
    assert r.status_code == 200, r.text
    return r.json()


async def event(client, sid: int, etype: str, **state) -> dict:
    r = await api(client, "POST", f"/api/live/sessions/{sid}/event",
                  json={"type": etype, "state": {"index": 0, "set": 1, **state}})
    assert r.status_code == 200, r.text
    return r.json()


async def reload(db, model, ident):
    db.expire_all()
    return await db.get(model, ident)


async def notified(db, user_id: int) -> list[Notification]:
    db.expire_all()
    return list((await db.execute(select(Notification).where(Notification.user_id == user_id))).scalars().all())


def ai_json(reply: str, emotion: str = "encouraging", action: str = "none", suggestion: str | None = None) -> str:
    return json.dumps({"reply": reply, "emotion": emotion, "action": action, "exercise_suggestion": suggestion})


# ====================================================================================== intent detection (pure)
RED_FLAGS = [
    ("my chest feels tight", "chest_pain"),
    ("I have chest pain", "chest_pain"),
    ("there's a lot of pressure in my chest", "chest_pain"),
    ("chest pian", "chest_pain"),
    ("my chets hurts", "chest_pain"),
    ("not sure, my chest feels tight", "chest_pain"),
    ("I don't think it's a heart attack but my chest really hurts", "chest_pain"),
    ("pain going down my left arm", "chest_pain"),
    ("I think I'm having a heart attack", "heart_attack"),
    ("hart attack", "heart_attack"),
    ("I can't breathe", "breathing"),
    ("cant breath", "breathing"),
    ("I'm gasping for air", "breathing"),
    ("I'm having trouble breathing", "breathing"),
    ("I am very short of breath", "breathing"),
    ("my throat is closing", "allergic_reaction"),
    ("I think I'm going to pass out", "fainting"),
    ("I feel faint", "fainting"),
    ("I blacked out for a second", "fainting"),
    ("everything is spinning", "dizziness"),
    ("I feel really dizzy", "dizziness"),
    ("I feel dizy", "dizziness"),
    ("my vision is going blurry", "dizziness"),
    ("my heart is racing", "heart_rhythm"),
    ("my heart is pounding like crazy", "heart_rhythm"),
    ("my hart is racing", "heart_rhythm"),
    ("I'm having palpitations", "heart_rhythm"),
    ("my heartbeat feels irregular", "heart_rhythm"),
    ("numbness on one side of my body", "stroke"),
    ("my face is drooping", "stroke"),
    ("my speech is slurred", "stroke"),
    ("worst headache of my life", "severe_headache"),
    ("sudden severe headache", "severe_headache"),
    ("I have a headache", "headache"),
    ("I hit my head on the bar", "headache"),
    ("I threw up", "vomiting"),
    ("I'm going to throw up", "vomiting"),
    ("I felt a pop and a sharp pain in my knee", "pop_injury"),
    ("something popped in my shoulder", "pop_injury"),
    ("I think I tore my hamstring", "pop_injury"),
    ("the pain is unbearable", "severe_pain"),
    ("my sugar is low", "low_sugar"),
    ("I think I'm overheating", "overheating"),
    ("I'm having a seizure", "seizure"),
    ("call an ambulance", "emergency"),
]
NOT_RED_FLAGS = [
    "no chest pain, just tired", "my chest doesn't hurt", "chest press is heavy", "the chest stretch feels good",
    "I can breathe fine", "I am not dizzy", "my legs are burning", "I feel the burn", "I broke a sweat",
    "help me with my form", "next time I'll do better", "my heart rate monitor is fine", "headstand next?",
    "skipping rope is fun", "I went to the shop", "I'm not out of breath at all",
]


@pytest.mark.parametrize("text,flag", RED_FLAGS)
def test_red_flags_detected_in_many_phrasings(text, flag):
    intent = live.detect_intent(text)
    assert intent.kind == "red_flag", (text, intent)
    assert intent.name == flag, (text, intent.name)
    assert intent.level in ("critical", "life_threatening")


@pytest.mark.parametrize("text", NOT_RED_FLAGS)
def test_negations_and_exercise_names_are_not_red_flags(text):
    assert live.detect_intent(text).kind != "red_flag", text


DISCOMFORT = [
    ("my knee hurts", "knee", "moderate"),
    ("my nee hurts", "knee", "moderate"),
    ("I feel a twinge in my knee", "knee", "mild"),
    ("pain in my lower back", "back", "moderate"),
    ("I think I tweaked my back", "back", "moderate"),
    ("my back is killing me", "back", "moderate"),
    ("my sholder hurts", "shoulder", "moderate"),
    ("I hurt my shoulder", "shoulder", "moderate"),
    ("my wrist is a bit sore", "wrist", "mild"),
    ("my rist hurts", "wrist", "moderate"),
    ("I have a cramp in my calf", "ankle", "moderate"),
    ("I rolled my ankle", "ankle", "moderate"),
    ("my neck is stiff", "neck", "moderate"),
    ("I feel sick", "stomach", "moderate"),
    ("I've got a side stitch", "stomach", "moderate"),
    ("I pulled my hamstring", "other", "moderate"),
    ("ouch", "other", "moderate"),
    ("my knee hurts really bad", "knee", "severe"),
]


@pytest.mark.parametrize("text,part,severity", DISCOMFORT)
def test_discomfort_body_parts_and_severity(text, part, severity):
    intent = live.detect_intent(text)
    assert intent.kind == "discomfort", (text, intent)
    assert intent.body_part == part and intent.severity == severity, (text, intent)


def test_negated_discomfort_is_ignored():
    for text in ("my knee doesn't hurt anymore", "no pain at all", "it doesn't hurt"):
        assert live.detect_intent(text).kind != "discomfort", text


COMMANDS = [
    ("stop", "quit"), ("stpo", "quit"), ("quit", "quit"), ("end workout", "quit"), ("finish the session", "quit"),
    ("I'm done", "quit"), ("that's enough", "quit"), ("that is enough for today", "quit"), ("I give up", "quit"),
    ("let's call it a day", "quit"), ("I want to stop", "quit"),
    ("pause", "pause"), ("pasue", "pause"), ("puase", "pause"), ("hold on", "pause"), ("give me a minute", "pause"),
    ("I need a break", "pause"), ("stop for a sec", "pause"), ("wait", "pause"),
    ("resume", "resume"), ("contiune", "resume"), ("let's go", "resume"), ("keep going", "resume"), ("I'm ready", "resume"),
    ("next", "next"), ("nxet", "next"), ("next exercise", "next"), ("skip", "next"), ("skip this one", "next"),
    ("move on", "next"),
    ("done", "set_done"), ("next set", "set_done"), ("done with this set", "set_done"),
    ("something else", "change"), ("switch to cardio", "change"), ("swich to cardio", "change"),
    ("change the workout", "change"), ("chnage the exercise", "change"), ("I don't like this exercise", "change"),
    ("let's do yoga instead", "change"),
    ("easier", "intensity_down"), ("esier", "intensity_down"), ("too hard", "intensity_down"),
    ("lower the weight", "intensity_down"), ("fewer reps", "intensity_down"), ("slow down", "intensity_down"),
    ("I'm exhausted", "intensity_down"),
    ("harder", "intensity_up"), ("hardr", "intensity_up"), ("more reps", "intensity_up"), ("this is too easy", "intensity_up"),
    ("push me", "intensity_up"),
    ("what's next", "info_next"), ("how many reps", "info_reps"), ("any tips", "info_form"), ("how am I doing", "progress"),
    ("can you repeat that", "repeat"), ("help", "help"), ("I can't catch my breath", "breather"),
    ("stop listening", "mic_off"), ("turn off the mic", "mic_off"), ("be quiet", "voice_off"), ("stop talking", "voice_off"),
    ("unmute", "voice_on"),
]


@pytest.mark.parametrize("text,command", COMMANDS)
def test_every_command_including_typos(text, command):
    intent = live.detect_intent(text)
    assert intent.kind == "command", (text, intent)
    assert intent.name == command, (text, intent.name)


def test_change_command_extracts_category_and_scope():
    i = live.detect_intent("switch to cardio")
    assert (i.category, i.scope) == ("cardio", "exercise")
    i = live.detect_intent("change the workout to mobility")
    assert (i.category, i.scope) == ("mobility", "workout")
    assert live.detect_intent("give me something for core").muscles[:1] == ("core",)


def test_everything_else_is_chat():
    for text in ("tell me a joke", "is this good form?", "thanks coach", "what muscles does this work?"):
        assert live.detect_intent(text).kind == "chat", text


@pytest.mark.parametrize("country,number", [("IN", "112"), ("US", "911"), ("CA", "911"), ("GB", "999"), ("AE", "999"),
                                            ("AU", "000"), ("NZ", "111"), ("SG", "995"), ("DE", "112"), ("FR", "112"),
                                            ("PT", "112"), (None, "112"), ("ZZ", "112")])
def test_emergency_number_by_country(country, number):
    info = live.emergency_info(country)
    assert info["number"] == number and info["tel"] == f"tel:{number}"
    assert ("NHS 111" in (info["note"] or "")) == (country == "GB")


# ====================================================================================== prescriptions (pure)
def test_intensity_rep_and_rest_math():
    assert [live.scale(10, i) for i in range(-3, 4)] == [7, 8, 9, 10, 11, 12, 13]
    assert [live.scale(12, i) for i in range(-3, 4)] == [8, 10, 11, 12, 13, 14, 16]  # 10.8 -> 11, 13.2 -> 13 (half-up)
    assert live.scale(15, -1) == 14 and live.scale(1, -3) == 1
    assert [live.adjusted_rest(60, i) for i in range(-3, 4)] == [105, 90, 75, 60, 45, 30, 15]
    assert live.adjusted_rest(20, 3) == 10 and live.adjusted_rest(290, -3) == 300
    item = {"exercise": "Goblet squat", "category": "strength", "sets": 3, "reps": 10, "rest_s": 60, "load": "moderate",
            "equipment": "dumbbell"}
    assert live.effective(item, -2)["load_note"] == "moderate - go lighter"
    assert "heavier" in live.effective(item, 1)["load_note"]
    assert "slow the tempo" in live.effective(dict(item, load_cap=True), 2)["load_note"]  # health cap: never heavier
    timed = {"exercise": "Forearm plank", "category": "calisthenics", "sets": 2, "duration_s": 30, "rest_s": 45}
    assert live.effective(timed, 1)["duration_s"] == 33 and live.dose_text(timed) == "2 rounds of 30 seconds"


def test_quick_sessions_filter_safety_tags():
    lib = live.Library(live.LibItem.from_any(type("E", (), dict(
        slug=s, name=n, category=c, muscles=m, contraindications=ct, impact=imp, intensity=it, cues=cu, goals=[],
        equipment="none", difficulty="beginner", video_slug=None, description=""))) for s, n, c, m, ct, imp, it, cu in LIBRARY)
    for key in live.QUICK_SESSIONS:
        items, _ = live.build_quick_items(key, lib, {"knee_injury"})
        assert items and not ({i["exercise"] for i in items} & KNEE_UNSAFE), key
    items, _ = live.build_quick_items("full_body", lib, {"wrist_injury"})
    assert not ({i["exercise"] for i in items} & {"Push-up", "Incline push-up"})


# ====================================================================================== pages
async def test_start_page_renders_plan_days_quick_sessions_and_gear(client, db):
    await seed_library(db)
    customer, *_ = await make_member(db, conditions=["Old ACL tear, right knee", "Lower back pain"])
    await make_plan(db, customer)
    await login_as(client, customer)
    r = await client.get("/me/live")
    assert r.status_code == 200, r.text
    html = r.text
    assert "Start live session" in html and 'data-component="live-start"' in html
    assert "Lower body" in html and "Upper body" in html  # plan days
    assert "10-minute full body" in html and "Mobility &amp; breathing" in html and "Calisthenics basics" in html
    assert "safety swap" in html  # jump squat is contraindicated for the knee
    assert "Back support strap" in html  # protective gear card (back problem)
    assert 'name="pick"' in html and 'name="csrf_token"' in html
    assert "AI can make mistakes" in html
    assert 'style="' not in html.split("<main", 1)[1]


async def test_start_page_quick_sessions_exclude_contraindicated(client, db):
    await seed_library(db)
    customer, *_ = await make_member(db, conditions=["knee injury"])
    await login_as(client, customer)
    s = await start(client, quick="full_body")
    names = [i["exercise"] for i in s["items"]]
    assert names and not (set(names) & KNEE_UNSAFE)
    for key in ("exercise", "slug", "sets", "reps", "duration_s", "rest_s", "load", "cues", "video_slug"):
        assert key in s["items"][0]


async def test_session_page_renders_and_other_customers_get_404(client, db, anon_client_factory):
    await seed_library(db)
    customer, *_ = await make_member(db)
    await login_as(client, customer)
    s = await start(client)
    r = await client.get(f"/me/live/{s['id']}")
    assert r.status_code == 200
    html = r.text
    assert 'data-component="live-session"' in html and 'data-component="coach-face"' in html
    assert "I feel discomfort" in html and "Hold to talk" in html and "AI can make mistakes" in html
    assert 'aria-live="polite"' in html and 'data-act="ptt"' in html
    other, *_ = await make_member(db)
    c2 = anon_client_factory("127.0.0.1")
    await login_as(c2, other)
    assert (await c2.get(f"/me/live/{s['id']}")).status_code == 404
    for path, body in ((f"/api/live/sessions/{s['id']}/message", {"text": "next"}),
                       (f"/api/live/sessions/{s['id']}/event", {"type": "started"}),
                       (f"/api/live/sessions/{s['id']}/discomfort", {"body_part": "knee"}),
                       (f"/api/live/sessions/{s['id']}/end", {"reason": "x"})):
        assert (await api(c2, "POST", path, json=body)).status_code == 404
    assert (await c2.get(f"/api/live/sessions/{s['id']}")).status_code == 404


async def test_form_fallback_starts_a_session(client, db):
    await seed_library(db)
    customer, *_ = await make_member(db)
    plan = await make_plan(db, customer)
    await login_as(client, customer)
    r = await post_form(client, "/me/live/start", {"pick": "day-1", "plan_id": str(plan.id), "voice": "1"})
    assert r.status_code == 303 and r.headers["location"].startswith("/me/live/")
    s = (await db.execute(select(WorkoutSession).where(WorkoutSession.customer_id == customer.id))).scalars().one()
    assert s.title.endswith("Upper body") and [i["exercise"] for i in s.items] == ["Push-up", "Inverted row"]


async def test_plan_day_snapshot_swaps_unsafe_items_and_validates_input(client, db):
    await seed_library(db)
    customer, *_ = await make_member(db, conditions=["knee pain"])
    plan = await make_plan(db, customer)
    await login_as(client, customer)
    s = await start(client, quick=None, plan_id=plan.id, day_index=0)
    names = [i["exercise"] for i in s["items"]]
    assert "Jump squat" not in names and any(i["swapped_from"] == "Jump squat" for i in s["items"])
    assert (await api(client, "POST", "/api/live/sessions", json={"plan_id": plan.id, "day_index": 9})).status_code == 400
    assert (await api(client, "POST", "/api/live/sessions", json={"quick": "nope"})).status_code == 400
    other, *_ = await make_member(db)
    foreign = await make_plan(db, other)
    assert (await api(client, "POST", "/api/live/sessions", json={"plan_id": foreign.id, "day_index": 0})).status_code == 404
    assert (await api(client, "POST", "/api/live/sessions", json={})).status_code == 400


async def test_start_page_query_count_is_constant(client, db):
    await seed_library(db)
    customer, *_ = await make_member(db, conditions=["asthma"])
    await login_as(client, customer)
    for _ in range(2):
        await start(client)
    await client.get("/me/live")  # warm-up (CSRF cookie, caches)
    with QueryCounter() as small:
        assert (await client.get("/me/live")).status_code == 200
    await make_plan(db, customer)
    for i in range(8):
        db.add(Exercise(slug=f"extra-{i}", name=f"Extra move {i}", category="mobility", goals=[], muscles=["hips"],
                        equipment="none", description="x", cues=[], contraindications=[]))
    await db.commit()
    for _ in range(6):
        await start(client)
    with QueryCounter() as big:
        assert (await client.get("/me/live")).status_code == 200
    assert big.count == small.count  # independent of sessions, exercises, plan days (no N+1)
    assert big.count <= 10


# ====================================================================================== access control
async def test_anonymous_gets_401_json_everywhere(client):
    for method, path, body in (
        ("POST", "/api/live/sessions", {"quick": "full_body"}),
        ("POST", "/api/live/sessions/1/message", {"text": "hi"}),
        ("POST", "/api/live/sessions/1/event", {"type": "started"}),
        ("POST", "/api/live/sessions/1/discomfort", {"body_part": "knee"}),
        ("POST", "/api/live/sessions/1/end", {"reason": "x"}),
        ("GET", "/api/live/sessions/1", None),
    ):
        r = await api(client, method, path, json=body)
        assert r.status_code == 401, (path, r.status_code)
        assert r.headers["content-type"].startswith("application/json")
    r = await client.get("/me/live")
    assert r.status_code == 303 and "/auth/login" in r.headers["location"]


async def test_no_subscription_and_non_customers_are_blocked(client, db):
    customer, owner, _ = await make_member(db, subscribed=False)
    await login_as(client, customer)
    r = await client.get("/me/live")
    assert r.status_code == 200 and "comes with your membership" in r.text and "Start live session" not in r.text
    r = await api(client, "POST", "/api/live/sessions", json={"quick": "full_body"})
    assert r.status_code == 403
    await login_as(client, owner)
    assert (await client.get("/me/live")).status_code == 403
    assert (await api(client, "POST", "/api/live/sessions", json={"quick": "full_body"})).status_code == 403


async def test_lapsed_membership_ends_session_but_safety_still_works(client, db):
    from app.models import Subscription
    from app.models.enums import SubscriptionStatus

    await seed_library(db)
    customer, *_ = await make_member(db)
    await login_as(client, customer)
    s = await start(client)
    sub = (await db.execute(select(Subscription).where(Subscription.customer_id == customer.id))).scalars().one()
    sub.status = SubscriptionStatus.EXPIRED
    await db.commit()
    out = await say(client, s["id"], "harder")
    assert out["session"]["status"] == "stopped" and "membership" in out["reply"]
    out = await say(client, s["id"], "I feel faint")  # red flags are always handled
    assert out["session"]["status"] == "stopped_for_safety"


async def test_suspended_member_sees_calm_blocked_page(client, db):
    await seed_library(db)
    customer, *_ = await make_member(db)
    await login_as(client, customer)
    s = await start(client)
    prof = await db.get(CustomerProfile, customer.id)
    prof.activity_suspended = True
    prof.suspension_reason = "Blood pressure reading needs a doctor's review."
    await db.commit()
    r = await client.get("/me/live")
    assert r.status_code == 200 and "paused for your safety" in r.text and "admin" in r.text
    assert "Blood pressure reading" in r.text and "tel:112" in r.text
    r = await api(client, "POST", "/api/live/sessions", json={"quick": "full_body"})
    assert r.status_code == 403 and r.json()["suspended"] is True
    r = await client.get(f"/me/live/{s['id']}")
    assert "paused for your safety" in r.text and 'data-component="live-session"' not in r.text
    out = await say(client, s["id"], "let's go")
    assert any(a["type"] == "suspended" for a in out["actions"]) and out["session"]["ended"]


# ====================================================================================== events
async def test_events_drive_cues_progress_and_completion(client, db):
    await seed_library(db)
    customer, *_ = await make_member(db)
    plan = await make_plan(db, customer, days=[{"name": "Mini", "items": [
        {"exercise": "Bodyweight squat", "sets": 2, "reps": 10, "rest_s": 30},
        {"exercise": "Forearm plank", "sets": 1, "duration_s": 30, "rest_s": 20}]}])
    await login_as(client, customer)
    s = await start(client, quick=None, plan_id=plan.id, day_index=0)
    out = await event(client, s["id"], "started")
    assert "Bodyweight squat" in out["cue"] and out["emotion"] == "excited" and out["disclaimer"]
    out = await event(client, s["id"], "set_done", set=1)
    assert "Rest 30 seconds" in out["cue"] and "set 2 of 2" in out["cue"]
    out = await event(client, s["id"], "exercise_done", index=0, set=2)
    assert out["session"]["current_index"] == 1 and "Forearm plank" in out["cue"]
    out = await event(client, s["id"], "exercise_done", index=0, set=2)  # retried request: idempotent
    assert out["session"]["current_index"] == 1
    out = await event(client, s["id"], "paused")
    assert out["session"]["status"] == "paused"
    out = await event(client, s["id"], "resumed")
    assert out["session"]["status"] == "active"
    out = await event(client, s["id"], "exercise_done", index=1, set=1)
    assert out["session"]["status"] == "completed" and any(a["type"] == "completed" for a in out["actions"])
    assert "That's the workout" in out["cue"]
    titles = [n.title for n in await notified(db, customer.id)]
    assert "Live workout complete" in titles
    kinds = (await db.execute(select(WorkoutSessionEvent.kind).where(WorkoutSessionEvent.session_id == s["id"]))).scalars().all()
    assert {"started", "set_done", "exercise_done", "paused", "resumed", "completed"} <= set(kinds)


async def test_lost_event_self_heals_and_end_endpoint(client, db):
    await seed_library(db)
    customer, *_ = await make_member(db)
    await login_as(client, customer)
    s = await start(client)
    out = await event(client, s["id"], "exercise_done", index=2, set=2)  # the client got ahead (requests were lost)
    assert out["session"]["current_index"] == 3
    r = await api(client, "POST", f"/api/live/sessions/{s['id']}/end", json={"reason": "user_ended"})
    assert r.status_code == 200 and r.json()["session"]["status"] == "stopped"
    r = await api(client, "POST", f"/api/live/sessions/{s['id']}/end", json={"reason": "user_ended"})
    assert r.status_code == 200 and r.json()["session"]["status"] == "stopped"  # idempotent


# ====================================================================================== red flags
async def test_red_flag_stops_session_suspends_alerts_and_notifies(client, db):
    await seed_library(db)
    customer, owner, gym = await make_member(db)
    emp = await assign_trainer(db, gym, customer)
    cid, owner_id, trainer_id = customer.id, owner.id, emp.user_id
    await login_as(client, customer)
    s = await start(client)
    out = await say(client, s["id"], "I don't feel great, my chest feels tight")
    assert out["session"]["status"] == "stopped_for_safety" and out["emotion"] == "concerned"
    assert out["source"] == "safety" and "112" in out["reply"] and "stop" in out["reply"].lower()
    act = next(a for a in out["actions"] if a["type"] == "safety_stop")
    assert act["emergency"]["number"] == "112" and act["level"] == "life_threatening"
    prof = await reload(db, CustomerProfile, cid)
    assert prof.activity_suspended and prof.suspended_by == "rule" and "chest pain" in prof.suspension_reason
    alerts = (await db.execute(select(HealthAlert).where(HealthAlert.customer_id == cid))).scalars().all()
    assert [a.level for a in alerts] == [AnomalyLevel.LIFE_THREATENING] and alerts[0].source == "rule"
    events = (await db.execute(select(SuspensionEvent).where(SuspensionEvent.customer_id == cid))).scalars().all()
    assert len(events) == 1 and events[0].health_alert_id == alerts[0].id and events[0].actor == "rule"
    assert await notified(db, cid), "customer notified"
    assert await notified(db, owner_id), "gym owner notified"
    assert await notified(db, trainer_id), "assigned trainer notified"
    logs = (await db.execute(select(AuditLog).where(AuditLog.action == "live.safety_stop"))).scalars().all()
    assert len(logs) == 1 and logs[0].entity_id == s["id"]
    flags = (await db.execute(select(WorkoutSessionEvent).where(WorkoutSessionEvent.session_id == s["id"],
                                                                WorkoutSessionEvent.kind == "red_flag"))).scalars().all()
    assert len(flags) == 1 and flags[0].detail["flag"] == "chest_pain" and "chest" in flags[0].message


async def test_red_flag_uses_the_members_emergency_number(client, db):
    await seed_library(db)
    customer, *_ = await make_member(db, country="US")
    await login_as(client, customer)
    s = await start(client)
    out = await say(client, s["id"], "I'm going to pass out")
    assert "911" in out["reply"] and out["emergency"]["number"] == "911"
    gb, *_ = await make_member(db, country="GB")
    await login_as(client, gb)
    s = await start(client)
    out = await say(client, s["id"], "I can't breathe")
    assert "999" in out["reply"] and "NHS 111" in out["reply"]


async def test_ai_can_never_continue_after_a_red_flag(client, db, fake_ai):
    await seed_library(db)
    customer, *_ = await make_member(db)
    provider = fake_ai(responses=[ai_json("Great, let's keep going!", "excited", "resume")])
    await login_as(client, customer)
    s = await start(client)
    await say(client, s["id"], "my heart is racing and I feel dizzy")
    out = await say(client, s["id"], "I'm fine now, let's continue")
    assert out["session"]["status"] == "stopped_for_safety"
    assert "can't restart" in out["reply"] and not provider.prompts  # the AI was never asked
    out = await event(client, s["id"], "resumed")
    assert out["session"]["status"] == "stopped_for_safety"
    out = await say(client, s["id"], "resume")
    assert out["session"]["status"] == "stopped_for_safety"
    r = await api(client, "POST", "/api/live/sessions", json={"quick": "mobility"})
    assert r.status_code == 403
    page = await client.get(f"/me/live/{s['id']}")
    assert page.status_code == 200 and "Workout stopped for your safety" in page.text and 'data-mode="safety"' in page.text
    assert "Sip some water" in page.text  # the guidance given at the red flag (dizziness), not a generic text


async def test_severe_or_chest_discomfort_button_is_a_red_flag(client, db):
    await seed_library(db)
    customer, *_ = await make_member(db)
    await login_as(client, customer)
    s = await start(client)
    r = await api(client, "POST", f"/api/live/sessions/{s['id']}/discomfort", json={"body_part": "knee", "severity": "severe"})
    assert r.status_code == 200 and r.json()["session"]["status"] == "stopped_for_safety"
    other, *_ = await make_member(db)
    await login_as(client, other)
    s = await start(client)
    r = await api(client, "POST", f"/api/live/sessions/{s['id']}/discomfort", json={"body_part": "chest", "severity": "mild"})
    out = r.json()
    assert out["session"]["status"] == "stopped_for_safety" and out["actions"][0]["flag"] == "chest_pain"


# ====================================================================================== discomfort
async def test_discomfort_swaps_to_safe_alternative_and_notifies_trainer(client, db):
    await seed_library(db)
    customer, owner, gym = await make_member(db)
    emp = await assign_trainer(db, gym, customer)
    cid, owner_id, trainer_id, name = customer.id, owner.id, emp.user_id, customer.full_name
    plan = await make_plan(db, customer, days=[{"name": "Legs", "items": [
        {"exercise": "Bodyweight squat", "sets": 3, "reps": 12},
        {"exercise": "Reverse lunge", "sets": 3, "reps": 10},
        {"exercise": "Push-up", "sets": 3, "reps": 10}]}])
    await login_as(client, customer)
    s = await start(client, quick=None, plan_id=plan.id, day_index=0)
    r = await api(client, "POST", f"/api/live/sessions/{s['id']}/discomfort",
                  json={"body_part": "knee", "severity": "moderate", "note": "sharp when I bend it"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["emotion"] == "empathetic" and out["session"]["status"] == "paused"
    assert {a["type"] for a in out["actions"]} == {"pause", "replace_exercise"}
    items = out["session"]["items"]
    first = items[0]
    assert first["swapped_from"] == "Bodyweight squat" and first["exercise"] not in KNEE_UNSAFE
    assert not set(first["muscles"]) & {"quads"}  # keeps away from the knee
    assert items[1]["exercise"] != "Reverse lunge"  # later knee-loading exercise swapped too
    assert "sorry" in out["reply"].lower() and first["exercise"] in out["reply"]
    alerts = (await db.execute(select(HealthAlert).where(HealthAlert.customer_id == cid))).scalars().all()
    assert [a.level for a in alerts] == [AnomalyLevel.WARNING] and alerts[0].source == "rule"
    assert "sharp when I bend it" in alerts[0].message
    assert [n.title for n in await notified(db, trainer_id)] == [f"Client discomfort: {name}"]
    assert not await notified(db, owner_id)  # discomfort is not a red flag
    assert not (await reload(db, CustomerProfile, cid)).activity_suspended
    # a later "change exercise" request still respects the knee
    out = await say(client, s["id"], "something else")
    assert out["session"]["items"][0]["exercise"] not in KNEE_UNSAFE


async def test_spoken_wrist_discomfort_avoids_contraindicated_alternatives(client, db):
    await seed_library(db)
    customer, *_ = await make_member(db)
    plan = await make_plan(db, customer, days=[{"name": "Push", "items": [{"exercise": "Push-up", "sets": 3, "reps": 10}]}])
    await login_as(client, customer)
    s = await start(client, quick=None, plan_id=plan.id, day_index=0)
    out = await say(client, s["id"], "my wrist hurts a bit")
    assert out["intent"]["kind"] == "discomfort" and out["intent"]["body_part"] == "wrist"
    new = out["session"]["items"][0]
    assert new["exercise"] not in {"Push-up", "Incline push-up"}
    ev = (await db.execute(select(WorkoutSessionEvent).where(WorkoutSessionEvent.session_id == s["id"],
                                                             WorkoutSessionEvent.kind == "discomfort"))).scalars().one()
    assert ev.detail["tag"] == "wrist_injury" and ev.message == "my wrist hurts a bit"


# ====================================================================================== commands
async def test_voice_commands_change_the_session(client, db, ai_down):
    await seed_library(db)
    customer, *_ = await make_member(db)
    await login_as(client, customer)
    s = await start(client)
    sid = s["id"]
    first = s["items"][0]
    out = await say(client, sid, "pause")
    assert out["session"]["status"] == "paused" and out["actions"] == [{"type": "pause"}]
    out = await say(client, sid, "let's go")
    assert out["session"]["status"] == "active" and out["actions"] == [{"type": "resume"}]
    out = await say(client, sid, "harder")
    assert out["session"]["intensity"] == 1
    reps = out["session"]["items"][0]["effective"]["reps"]
    assert reps == live.scale(first["reps"], 1) and out["emotion"] == "excited"
    out = await say(client, sid, "esier")
    assert out["session"]["intensity"] == 0
    out = await say(client, sid, "something else")
    assert out["session"]["items"][0]["exercise"] != first["exercise"]
    assert out["actions"][0]["type"] == "replace_exercise"
    out = await say(client, sid, "switch to cardio")
    assert out["session"]["items"][0]["category"] == "cardio"
    out = await say(client, sid, "next")
    assert out["session"]["current_index"] == 1 and out["actions"][0]["type"] == "next"
    out = await say(client, sid, "done")
    assert out["actions"] == [{"type": "set_done"}] and out["reply"] == ""
    out = await say(client, sid, "what's next")
    assert out["session"]["items"][2]["exercise"] in out["reply"]
    out = await say(client, sid, "I'm done")
    assert out["session"]["status"] == "stopped" and out["actions"] == [{"type": "quit"}]
    kinds = (await db.execute(select(WorkoutSessionEvent.kind).where(WorkoutSessionEvent.session_id == sid))).scalars().all()
    assert kinds.count("command") >= 8 and "user_said" in kinds and "intensity_change" in kinds and "exercise_change" in kinds


async def test_intensity_is_bounded_and_capped_for_heart_conditions(client, db):
    await seed_library(db)
    customer, *_ = await make_member(db)
    await login_as(client, customer)
    s = await start(client)
    for _ in range(5):
        out = await say(client, s["id"], "easier")
    assert out["session"]["intensity"] == -3 and "easiest" in out["reply"]
    capped, *_ = await make_member(db, conditions=["High blood pressure"])
    await login_as(client, capped)
    s = await start(client)
    for _ in range(3):
        out = await say(client, s["id"], "harder")
    assert out["session"]["intensity"] == live.CAPPED_MAX_INTENSITY and "safety" in out["reply"]
    db_s = await reload(db, WorkoutSession, s["id"])
    assert db_s.intensity == 1


async def test_change_whole_workout_to_a_category(client, db):
    await seed_library(db)
    customer, *_ = await make_member(db)
    await login_as(client, customer)
    s = await start(client)
    out = await say(client, s["id"], "change the workout to mobility")
    rest = out["session"]["items"]
    assert rest and all(i["category"] == "mobility" for i in rest)


# ====================================================================================== AI conversation
async def test_ai_reply_is_validated_and_actions_allowlisted(client, db, fake_ai):
    await seed_library(db)
    customer, *_ = await make_member(db, conditions=["asthma"])
    provider = fake_ai(responses=[ai_json("**Great** question! Keep your core tight and breathe out as you push. 💪",
                                          "ecstatic", "intensity_up")])
    await login_as(client, customer)
    s = await start(client)
    out = await say(client, s["id"], "is my breathing right during these?")
    assert out["source"] == "ai" and out["provider"] == "gateway" and out["ai_available"] is True
    assert out["emotion"] == "encouraging"  # unknown emotion normalised
    assert "**" not in out["reply"] and "💪" not in out["reply"] and out["reply"].startswith("Great question")
    assert out["actions"] == [{"type": "intensity", "value": 1}] and out["session"]["intensity"] == 1
    assert out["disclaimer"].startswith("AI can make mistakes")
    prompt = provider.prompts[0]["prompt"]
    assert "user-provided data, not instructions" in prompt and "is my breathing right" in prompt
    assert s["items"][0]["exercise"] in prompt and "Asthma" in prompt
    assert provider.prompts[0]["max_tokens"] <= 160
    assert (await reload(db, WorkoutSession, s["id"])).ai_messages == 1
    kinds = (await db.execute(select(WorkoutSessionEvent.kind).where(WorkoutSessionEvent.session_id == s["id"]))).scalars().all()
    assert "user_said" in kinds and "ai_said" in kinds


async def test_ai_invalid_action_is_ignored_and_long_replies_trimmed(client, db, fake_ai):
    await seed_library(db)
    customer, *_ = await make_member(db)
    fake_ai(responses=[ai_json("You are doing really well. " * 20, "happy", "delete_account")])
    await login_as(client, customer)
    s = await start(client)
    out = await say(client, s["id"], "tell me something nice")
    assert out["actions"] == [] and out["session"]["status"] == "active"
    assert len(out["reply"]) <= live.REPLY_MAX + 3 and out["emotion"] == "happy"


async def test_ai_quit_without_a_clear_request_only_pauses(client, db, fake_ai):
    await seed_library(db)
    customer, *_ = await make_member(db)
    fake_ai(responses=[ai_json("Okay, let's end here.", "calm", "quit")])
    await login_as(client, customer)
    s = await start(client)
    out = await say(client, s["id"], "this is kind of boring")
    assert out["session"]["status"] == "paused" and "end workout" in out["reply"]


async def test_ai_log_discomfort_action_records_an_alert(client, db, fake_ai):
    await seed_library(db)
    customer, *_ = await make_member(db)
    fake_ai(responses=[ai_json("Let's stop and check in. Tell me where it feels off.", "concerned", "log_discomfort")])
    await login_as(client, customer)
    s = await start(client)
    out = await say(client, s["id"], "something feels strange in my leg area")
    assert out["source"] == "ai" and out["session"]["status"] == "paused"
    alerts = (await db.execute(select(HealthAlert).where(HealthAlert.customer_id == customer.id))).scalars().all()
    assert [a.level for a in alerts] == [AnomalyLevel.WARNING]


async def test_ai_down_gives_deterministic_friendly_reply(client, db, ai_down):
    await seed_library(db)
    customer, *_ = await make_member(db)
    await login_as(client, customer)
    s = await start(client)
    out = await say(client, s["id"], "tell me a joke")
    assert out["reply"] == live.OFFLINE_REPLY and out["source"] == "fallback" and out["actions"] == []
    out = await say(client, s["id"], "thanks coach")
    assert out["reply"].startswith("You're welcome")
    out = await say(client, s["id"], "pause")  # commands keep working without AI
    assert out["session"]["status"] == "paused"


async def test_slow_ai_falls_back_quickly(client, db, fake_ai, monkeypatch):
    import app.ai.live_coach as ai_live

    async def slow(**kwargs):
        await asyncio.sleep(2)
        return ai_json("too late")

    monkeypatch.setattr(ai_live, "TIMEOUT_S", 0.2)
    monkeypatch.setattr(ai_live, "TOTAL_TIMEOUT_S", 0.3)
    fake_ai(handler=slow)
    await seed_library(db)
    customer, *_ = await make_member(db)
    await login_as(client, customer)
    s = await start(client)
    out = await say(client, s["id"], "how should I breathe?")
    assert out["source"] == "fallback" and out["reply"] == live.OFFLINE_REPLY


async def test_plan_without_ai_commands_work_and_chat_upsells(client, db, fake_ai):
    await seed_library(db)
    customer, *_ = await make_member(db, ai=False)
    provider = fake_ai(responses=[ai_json("should never be used")])
    await login_as(client, customer)
    page = await client.get("/me/live")
    assert "See AI plans" in page.text
    s = await start(client)
    out = await say(client, s["id"], "tell me about protein")
    assert out["source"] == "upsell" and {"type": "upsell"} in out["actions"] and "AI plan" in out["reply"]
    out = await say(client, s["id"], "pause")
    assert out["session"]["status"] == "paused"
    out = await say(client, s["id"], "my chest hurts")  # safety works on every plan
    assert out["session"]["status"] == "stopped_for_safety"
    assert not provider.prompts
    page = await client.get(f"/me/live/{s['id']}")
    assert "Voice commands" in page.text


async def test_no_pooled_db_connection_is_held_while_the_coach_ai_thinks(client, db, pool_probe, monkeypatch):
    """The AI turn commits what the member said, then waits for the AI - in the queue and while the provider answers -
    without a pooled connection; the reply is logged afterwards in a new transaction."""
    await seed_library(db)
    customer, *_ = await make_member(db)
    await login_as(client, customer)
    s = await start(client)
    r, queued, answering = await connections_while_the_ai_works(
        lambda: api(client, "POST", f"/api/live/sessions/{s['id']}/message",
                    json={"text": "is my breathing right?", "state": {"index": 0, "set": 1, "phase": "work"}}),
        pool_probe, lambda **kwargs: ai_json("Breathe out as you push - nice and steady."), monkeypatch)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["source"] == "ai" and out["reply"].startswith("Breathe out as you push")
    assert (queued, answering) == (0, [0])  # no connection checked out while queued, none while the provider answered
    kinds = (await db.execute(select(WorkoutSessionEvent.kind).where(WorkoutSessionEvent.session_id == s["id"])
                              .order_by(WorkoutSessionEvent.id))).scalars().all()
    assert kinds[-2:] == ["user_said", "ai_said"]  # both written: before the AI call, and after it


async def test_no_pooled_db_connection_is_held_while_the_coach_voice_is_synthesised(client, db, pool_probe,
                                                                                    monkeypatch):
    """The voice routes close the session that signed the member in before they wait for the voice lane and Kokoro:
    no pooled connection while a line waits for the only voice slot, while it is synthesised, or while the engine is
    probed."""
    from types import SimpleNamespace

    from app.ai.scheduler import scheduler
    from app.config import get_settings
    from app.services.tts import tts

    mp3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x64" * 64
    hold = asyncio.Event()
    speaking: list[tuple[str, int]] = []

    class Audio:
        async def aread(self) -> bytes:
            return mp3

    class Speech:  # AsyncOpenAI(...).audio.speech.create(...)
        async def create(self, **kwargs) -> Audio:
            if kwargs["input"] == "Hold the voice lane.":
                await hold.wait()
            else:
                speaking.append((kwargs["input"], pool_probe.checked_out))
            return Audio()

    kokoro = SimpleNamespace(audio=SimpleNamespace(speech=Speech()))
    monkeypatch.setattr(get_settings(), "ai_tts_concurrency", 1)
    customer, *_ = await make_member(db)
    await login_as(client, customer)
    tts.use_client(kokoro)
    try:
        holder = asyncio.create_task(tts.synthesize("Hold the voice lane."))  # system work takes the only voice slot
        await wait_until(lambda: scheduler.snapshot()["lanes"]["tts"]["running"] == 1, what="the voice slot taken")
        line = asyncio.create_task(api(client, "POST", "/api/tts", json={"text": "Squeeze at the top."}))
        await wait_until(lambda: scheduler.snapshot()["lanes"]["tts"]["queued"] == 1 or line.done(),
                         what="the line to wait for the voice slot")
        assert not line.done()
        await asyncio.sleep(0.05)
        queued = pool_probe.checked_out
        hold.set()
        assert await holder == mp3
        r = await asyncio.wait_for(line, 10)
        tts.use_client(kokoro)  # a fresh engine state: the status probe synthesises "Ready." once
        status = await client.get("/api/tts/status", params={"probe": "1"})
    finally:
        hold.set()
        tts.use_client(None)
    assert r.status_code == 200 and r.content == mp3
    assert status.status_code == 200 and status.json()["state"] == "up"
    assert queued == 0 and speaking == [("Squeeze at the top.", 0), ("Ready.", 0)]
