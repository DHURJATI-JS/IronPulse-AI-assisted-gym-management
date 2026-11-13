"""WP-E2 tests: safety guardrails, AI coach (fake AI / AI down / no-AI plans), suspension, videos gating, N+1, and the
click's AI-guard charge riding with the ``coach_plan`` job (kept when the AI answered, refunded otherwise) plus the
job's single 'done' event (published by the runner)."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from datetime import date
from typing import Any

import pytest
from sqlalchemy import func, select, update

from app.ai import coach as coach_ai
from app.ai.client import FakeProvider, ai
from app.ai.errors import AIRejected
from app.ai.guard import guard
from app.ai.safety import (
    LIMITS,
    LibExercise,
    check_exercise,
    filter_plan,
    normalize_tags,
    plan_is_safe,
    sanitize_load,
    strip_weight_talk,
)
from app.ai.scheduler import BUSY_MESSAGE
from app.config import get_settings
from app.core.utils import utcnow
from app.db import QueryCounter
from app.models import CustomerProfile, EmailOutbox, Exercise, Job, Notification, Plan, Subscription, Video, WorkoutPlan
from app.models.enums import JobStatus, SubscriptionStatus
from app.services import coach as coach_service
from app.services import jobs as jobs_service
from app.services.coach import BUILTIN_LIBRARY, template_plan
from app.services.presence import presence
from app.services.realtime import hub
from tests.conftest import login_as, post_form, run_jobs
from tests.factories import make_customer, make_gym, make_owner, make_plans, make_subscription

# --------------------------------------------------------------------------- helpers
LIB = list(BUILTIN_LIBRARY)


def _plan(*items: dict, title: str = "Week plan") -> dict:
    return {"title": title, "days": [{"name": "Day 1", "focus": "Full body", "items": list(items)}], "weekly_notes": []}


def _names(plan: dict) -> list[str]:
    return [i["exercise"] for d in plan["days"] for i in d["items"]]


async def _member(db, *, ai: bool = True, conditions: list[str] | None = None, **kwargs):
    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    plans = {p.code: p for p in await make_plans(db, gym)}
    customer = await make_customer(db, conditions=conditions, **kwargs)
    await make_subscription(db, customer, plans["online_ai" if ai else "online"])
    return customer, gym


async def _plan_job(db, customer) -> Job | None:
    """The member's coach_plan job (plans are built in the background since the AI queue work)."""
    stmt = select(Job).where(Job.dedupe_key == f"coach_plan:{customer.id}")
    return (await db.execute(stmt.execution_options(populate_existing=True))).scalar_one_or_none()


async def _generate(client, db, customer) -> None:
    """Click "Generate", then let the background job build the plan."""
    r = await post_form(client, "/me/coach/generate")
    job = await _plan_job(db, customer)
    assert r.status_code == 303 and job is not None and r.headers["location"] == f"/me/coach?generating={job.id}"
    assert await run_jobs() == 1


def _ai_plan_json(*items: dict, days: int = 1) -> str:
    return json.dumps({
        "title": "Progressive strength week",
        "days": [{"name": f"Day {d + 1}", "focus": "Strength", "items": list(items)} for d in range(days)],
        "weekly_notes": ["Sleep 8 hours.", "Hydrate well."],
    })


async def _seed_library(db) -> None:
    """Exercise rows like WP-I seeds them (with contraindication tags and video links)."""
    from app.ai.coach import vector_param
    from app.ai.embeddings import embed

    rows = [
        ("goblet-squat", "Goblet squat", "strength", ["general_fitness", "bulking"], [], "goblet-squat-video"),
        ("box-jump", "Box jump", "calisthenics", ["weight_loss"], ["knee_injury", "ankle_injury", "pregnancy"], None),
        ("sprint-intervals", "Sprint intervals", "cardio", ["weight_loss"], ["asthma", "cardiac", "hypertension"], None),
        ("stationary-bike", "Stationary bike", "cardio", ["weight_loss", "general_fitness"], [], None),
        ("cat-cow", "Cat-cow", "mobility", ["general_fitness"], [], None),
        ("push-up", "Push-up", "calisthenics", ["calisthenics", "general_fitness"], ["wrist_injury"], None),
        ("incline-push-up", "Incline push-up", "calisthenics", ["general_fitness"], ["wrist_injury"], None),
        ("glute-bridge", "Glute bridge", "calisthenics", ["general_fitness"], ["pregnancy"], None),
        ("lat-pulldown", "Lat pulldown", "strength", ["bulking", "general_fitness"], [], None),
        ("diaphragmatic-breathing", "Diaphragmatic breathing", "breathing", ["general_fitness"], [], None),
    ]
    for slug, name, cat, goals, contra, video in rows:
        db.add(Exercise(slug=slug, name=name, category=cat, goals=goals, muscles=["legs"] if "squat" in slug else [],
                        equipment="none", intensity="high" if slug == "sprint-intervals" else "moderate",
                        impact="high" if slug in ("box-jump", "sprint-intervals") else "low", description=f"{name} description",
                        cues=[f"{name} cue"], contraindications=contra, video_slug=video,
                        embedding=vector_param(embed(f"{name} {cat} {' '.join(goals)}"))))
    await db.commit()


# --------------------------------------------------------------------------- safety filter (pure)
def test_asthma_removes_sprints_and_hiit_with_same_category_replacement():
    plan = _plan({"exercise": "Sprint intervals", "sets": 6, "duration_s": 30}, {"exercise": "Tabata HIIT finisher", "sets": 4},
                 {"exercise": "Cold-air outdoor run", "sets": 1, "duration_s": 120}, {"exercise": "Goblet squat", "sets": 3, "reps": 10})
    safe, removed, notes = filter_plan(plan, ["asthma"], 30, LIB)
    removed_names = {r["exercise"] for r in removed}
    assert removed_names == {"Sprint intervals", "Tabata HIIT finisher", "Cold-air outdoor run"}
    assert all(r["reason"].startswith("Asthma") for r in removed)
    sprint = next(r for r in removed if r["exercise"] == "Sprint intervals")
    replacement = next(e for e in LIB if e.name == sprint["replacement"])
    assert replacement.category == "cardio" and replacement.intensity != "high"
    assert "Goblet squat" in _names(safe)
    assert not any("sprint" in n.lower() or "hiit" in n.lower() for n in _names(safe))
    assert any("inhaler" in n for n in notes) and any("Safety filter" in n for n in notes)
    assert plan_is_safe(safe, ["asthma"], LIB)


def test_hypertension_blocks_max_effort_isometrics_and_head_down_and_caps_rpe():
    plan = _plan(
        {"exercise": "1RM bench press test", "sets": 1, "reps": 1, "load": "100% of 1RM"},
        {"exercise": "Heavy isometric wall sit", "sets": 3, "duration_s": 120},
        {"exercise": "Headstand hold", "sets": 3, "duration_s": 30},
        {"exercise": "Lat pulldown", "sets": 4, "reps": 4, "load": "heavy, RPE 9, to failure"},
        {"exercise": "Forearm plank", "sets": 3, "duration_s": 150},
    )
    safe, removed, notes = filter_plan(plan, ["hypertension"], 45, LIB)
    assert {r["exercise"] for r in removed} >= {"1RM bench press test", "Heavy isometric wall sit", "Headstand hold"}
    pulldown = next(i for d in safe["days"] for i in d["items"] if i["exercise"] == "Lat pulldown")
    assert pulldown["reps"] >= 8  # no heavy low-rep sets
    assert "heavy" not in pulldown["load"].lower() and "rpe 9" not in pulldown["load"].lower() and "RPE 6-7" in pulldown["load"]
    assert "failure" not in pulldown["load"] or "short of failure" in pulldown["load"]
    plank = next(i for d in safe["days"] for i in d["items"] if i["exercise"] == "Forearm plank")
    assert plank["duration_s"] <= 45  # no long isometric holds
    assert any("blood pressure" in n.lower() for n in notes)


def test_knee_injury_blocks_jumps_deep_loaded_squats_and_impact_lunges():
    plan = _plan({"exercise": "Box jumps", "sets": 3, "reps": 8}, {"exercise": "Barbell back squat", "sets": 5, "reps": 5},
                 {"exercise": "Jumping lunges", "sets": 3, "reps": 12}, {"exercise": "Burpee", "sets": 3, "reps": 10},
                 {"exercise": "Lat pulldown", "sets": 3, "reps": 10})
    safe, removed, _ = filter_plan(plan, ["knee_injury"], 30, LIB)
    assert {r["exercise"] for r in removed} == {"Box jumps", "Barbell back squat", "Jumping lunges", "Burpee"}
    for name in _names(safe):
        ex = next((e for e in LIB if e.name == name), None)
        assert ex is None or (ex.impact != "high" and "knee_injury" not in ex.contraindications)
    assert "Lat pulldown" in _names(safe)


def test_minor_strips_1rm_and_max_wording_and_keeps_moderate_reps():
    plan = _plan({"exercise": "Max deadlift single", "sets": 1, "reps": 1}, {"exercise": "Goblet squat", "sets": 3, "reps": 3, "load": "85% of 1RM"},
                 {"exercise": "Dumbbell biceps curl", "sets": 3, "reps": 12, "load": "max weight"})
    safe, removed, notes = filter_plan(plan, [], 15, LIB)
    assert any(r["exercise"] == "Max deadlift single" and r["reason"].startswith("Under 18") for r in removed)
    squat = next(i for d in safe["days"] for i in d["items"] if i["exercise"] == "Goblet squat")
    assert "1rm" not in squat["load"].lower() and squat["reps"] >= 6
    curl = next(i for d in safe["days"] for i in d["items"] if i["exercise"] == "Dumbbell biceps curl")
    assert "max" not in curl["load"].lower()
    assert any("Under 18" in n for n in notes)
    assert normalize_tags([], 15) == ["minor"] and normalize_tags(["asthma"], 30) == ["asthma"]


def test_prescriptions_are_clamped_and_text_sanitised():
    plan = _plan({"exercise": "Goblet squat", "sets": 12, "reps": "40-60", "rest_s": 2, "load": "1RM"},
                 {"exercise": "Stationary bike", "sets": 0, "duration_s": 3600, "rest_s": 900},
                 {"exercise": "Cat-cow", "sets": "two", "reps": None, "duration_s": None})
    safe, removed, _ = filter_plan(plan, [], 30, LIB)
    assert removed == []
    for item in (i for d in safe["days"] for i in d["items"]):
        assert LIMITS["sets"][0] <= item["sets"] <= LIMITS["sets"][1]
        assert item["reps"] is None or LIMITS["reps"][0] <= item["reps"] <= LIMITS["reps"][1]
        assert item["duration_s"] is None or item["duration_s"] <= 180
        assert LIMITS["rest_s"][0] <= item["rest_s"] <= LIMITS["rest_s"][1]
    squat, bike, catcow = (i for d in safe["days"] for i in d["items"])
    assert (squat["sets"], squat["reps"], squat["rest_s"]) == (6, 30, 15) and "1rm" not in (squat["load"] or "").lower()
    assert bike["duration_s"] == 180 and bike["rest_s"] == 300 and bike["sets"] == 3
    assert catcow["duration_s"] == 60  # timed default for mobility
    assert sanitize_load("80% of 1RM", []) == "a moderate load (RPE 6-7)"
    assert "max" not in sanitize_load("max effort", []).lower()


def test_contraindications_from_library_and_fuzzy_matching():
    lib = [LibExercise(slug="kettlebell-swing", name="Kettlebell swing", category="strength", contraindications=("hernia",)),
           LibExercise(slug="farmer-carry", name="Farmer carry", category="strength", muscles=("grip",)),
           LibExercise(slug="cat-cow", name="Cat-cow", category="mobility")]
    plan = _plan({"exercise": "kettlebell-swing", "sets": 3, "reps": 15}, {"exercise": "Kettlebell Swings", "sets": 3, "reps": 15})
    safe, removed, _ = filter_plan(plan, ["hernia"], 40, lib)
    assert len(removed) == 2 and all(r["replacement"] == "Farmer carry" for r in removed[:1])
    assert check_exercise(lib[0], "Kettlebell swing", ["hernia"]) is not None
    assert check_exercise(lib[1], "Farmer carry", ["hernia"]) is None


def test_recent_surgery_only_gentle_mobility_and_eating_disorder_drops_weight_talk():
    plan = _plan({"exercise": "Goblet squat", "sets": 3, "reps": 10, "notes": "Burn calories fast."},
                 {"exercise": "Cat-cow", "sets": 2, "duration_s": 45})
    safe, removed, notes = filter_plan(plan, ["recent_surgery"], 50, LIB)
    cats = {i["category"] for d in safe["days"] for i in d["items"]}
    assert cats <= {"mobility", "breathing"} and removed[0]["exercise"] == "Goblet squat"
    plan2 = {"title": "Fat loss blast", "days": [{"name": "Day 1", "items": [
        {"exercise": "Stationary bike", "sets": 3, "duration_s": 120, "notes": "Great for fat loss. Keep a steady cadence."}]}],
        "weekly_notes": ["Stay in a calorie deficit.", "Sleep well."]}
    safe2, _, notes2 = filter_plan(plan2, ["eating_disorder"], 25, LIB)
    blob = json.dumps(safe2).lower()
    assert "calorie" not in blob and "fat loss" not in blob and "steady cadence" in blob
    assert safe2["weekly_notes"] == ["Sleep well."]
    assert strip_weight_talk("Lose 5 kg quickly. Stretch daily.") == "Stretch daily."
    assert any("wellbeing" in n for n in notes2)


@pytest.mark.parametrize("goal", ["general_fitness", "weight_loss", "bulking", "calisthenics"])
@pytest.mark.parametrize("tags", [[], ["asthma", "knee_injury"], ["hypertension", "lower_back"], ["pregnancy", "wrist_injury"],
                                  ["shoulder_injury", "minor"], ["recent_surgery"]])
def test_template_plans_are_always_safe(goal, tags):
    plan, removed, notes = template_plan(goal, "intermediate", tags)
    assert plan["days"] and all(d["items"] for d in plan["days"])
    assert plan_is_safe(plan, tags)


def test_template_for_eating_disorder_avoids_weight_loss_programme():
    plan, _, notes = template_plan("weight_loss", "beginner", ["eating_disorder"])
    assert "Weight loss" not in plan["title"] and "calori" not in json.dumps(plan).lower()


# --------------------------------------------------------------------------- coach page + generation
async def test_coach_requires_customer_and_subscription(client, db):
    owner = await make_owner(db)
    await login_as(client, owner)
    r = await client.get("/me/coach")
    assert r.status_code == 403 and "text/html" in r.headers["content-type"] and "Access denied" in r.text
    client.cookies.clear()
    r = await client.get("/me/coach")
    assert r.status_code == 303 and "/auth/login" in r.headers["location"]

    customer = await make_customer(db)
    await login_as(client, customer)
    r = await client.get("/me/coach")
    assert r.status_code == 200 and "Coaching comes with your membership" in r.text
    r = await post_form(client, "/me/coach/generate")
    assert r.status_code == 303
    assert await db.scalar(select(func.count(WorkoutPlan.id))) == 0
    assert await db.scalar(select(func.count(Job.id))) == 0  # nothing queued without a membership


async def test_ai_plan_with_unsafe_items_is_filtered(client, db, fake_ai):
    customer, gym = await _member(db, ai=True, conditions=["Asthma since childhood", "ACL tear left knee"])
    await _seed_library(db)
    provider = fake_ai(responses=[_ai_plan_json(
        {"exercise": "Cat-cow", "sets": 2, "duration_s": 45},
        {"exercise": "Box jump", "sets": 4, "reps": 10},
        {"exercise": "Sprint intervals", "sets": 8, "duration_s": 30, "load": "max effort"},
        {"exercise": "Goblet squat", "sets": 9, "reps": 50, "rest_s": 1, "load": "80% of 1RM"},
        {"exercise": "Lat pulldown", "sets": 3, "reps": 10},
        days=3,
    )])
    await login_as(client, customer)
    r = await post_form(client, "/me/coach/generate")
    job = await _plan_job(db, customer)
    assert r.status_code == 303 and r.headers["location"] == f"/me/coach?generating={job.id}"
    # the request only queues the plan: no AI call, no plan yet - the coach_plan job builds it
    assert provider.prompts == [] and await db.scalar(select(func.count(WorkoutPlan.id))) == 0
    assert (job.kind, job.user_id, job.exit_policy, job.payload["ai"]) == ("coach_plan", customer.id, "pause", True)
    assert await run_jobs() == 1
    plan = (await db.execute(select(WorkoutPlan).where(WorkoutPlan.customer_id == customer.id))).scalar_one()
    assert plan.source == "ai" and plan.ai_provider == "gateway" and plan.is_active and plan.gym_id == gym.id
    names = {i["exercise"] for d in plan.plan["days"] for i in d["items"]}
    assert "Box jump" not in names and "Sprint intervals" not in names
    assert {r["exercise"] for r in plan.removed_items} == {"Box jump", "Sprint intervals"}
    squat = next(i for i in plan.plan["days"][0]["items"] if i["exercise"] == "Goblet squat")
    assert squat["sets"] == 6 and squat["reps"] == 30 and squat["rest_s"] == 15 and "1rm" not in squat["load"].lower()
    assert squat["video_slug"] == "goblet-squat-video"
    # prompt: compact profile + pgvector allowed list without contraindicated exercises; user text is data
    prompt = provider.prompts[0]["prompt"]
    assert "asthma" in prompt and "knee_injury" in prompt and "AVOID:" in prompt and "ALLOWED EXERCISES" in prompt
    allowed_block = prompt.split("ALLOWED EXERCISES", 1)[1]
    assert "Box jump" not in allowed_block and "Sprint intervals" not in allowed_block and "Goblet squat" in allowed_block
    assert provider.prompts[0]["max_tokens"] <= 1500
    # notification (in-app + e-mail)
    n = (await db.execute(select(Notification).where(Notification.user_id == customer.id))).scalar_one()
    assert "plan" in n.title.lower() and n.link == "/me/coach"
    assert await db.scalar(select(func.count(EmailOutbox.id)).where(EmailOutbox.user_id == customer.id)) == 1
    # page shows the plan, safety swaps and reasons
    page = await client.get("/me/coach")
    assert page.status_code == 200
    assert "AI-personalised" in page.text and "Box jump" in page.text and "Asthma" in page.text and "Goblet squat" in page.text


async def test_ai_plan_progresses_previous_plan_and_deactivates_old(client, db, fake_ai, monkeypatch):
    from app.config import get_settings

    # every AI call now passes the per-user guard: one plan costs 3 'gen' units and an AI member gets 3 a minute,
    # so two plans back to back need a more generous quota (this test is about plan progression, not quotas)
    monkeypatch.setattr(get_settings(), "ai_quota_scale", 2.0)
    customer, _ = await _member(db, ai=True)
    provider = fake_ai(responses=[_ai_plan_json({"exercise": "Goblet squat", "sets": 3, "reps": 10},
                                                {"exercise": "Lat pulldown", "sets": 3, "reps": 10},
                                                {"exercise": "Cat-cow", "sets": 2, "duration_s": 40}, days=3)])
    await login_as(client, customer)
    await _generate(client, db, customer)
    await _generate(client, db, customer)  # the finished job's row is recycled for the next request
    rows = (await db.execute(select(WorkoutPlan).where(WorkoutPlan.customer_id == customer.id).order_by(WorkoutPlan.id))).scalars().all()
    assert len(rows) == 2 and [p.is_active for p in rows] == [False, True]
    assert "PREVIOUS PLAN" in provider.prompts[-1]["prompt"] and "Goblet squat 3x10" in provider.prompts[-1]["prompt"]


async def test_ai_down_falls_back_to_safe_template_plan(client, db, ai_down):
    customer, _ = await _member(db, ai=True, conditions=["High BP"])
    await login_as(client, customer)
    await _generate(client, db, customer)
    plan = (await db.execute(select(WorkoutPlan).where(WorkoutPlan.customer_id == customer.id))).scalar_one()
    assert plan.source == "template" and plan.ai_provider is None and plan.plan["meta"]["ai_status"] == "unavailable"
    assert plan_is_safe(plan.plan, ["hypertension"])
    page = await client.get("/me/coach")
    assert page.status_code == 200 and "AI was unavailable" in page.text and "Standard plan" in page.text


async def test_plan_without_ai_gets_template_and_upsell(client, db, fake_ai):
    provider = fake_ai(responses=["{}"])
    customer, _ = await _member(db, ai=False)
    await login_as(client, customer)
    page = await client.get("/me/coach")
    assert "Unlock AI coaching" in page.text and "Generate my plan" in page.text
    await _generate(client, db, customer)
    assert (await _plan_job(db, customer)).payload["ai"] is False  # no AI to admit: the template plan is free
    plan = (await db.execute(select(WorkoutPlan).where(WorkoutPlan.customer_id == customer.id))).scalar_one()
    assert plan.source == "template" and plan.plan["meta"]["ai_status"] == "not_included"
    assert provider.prompts == []  # AI never called without plan_access.ai
    page = await client.get("/me/coach")
    assert "Unlock AI coaching" in page.text and plan.title in page.text


async def test_unsafe_only_ai_plan_is_rejected_for_template(client, db, fake_ai):
    customer, _ = await _member(db, ai=True, conditions=["Surgery last month"])
    fake_ai(responses=[_ai_plan_json({"exercise": "Box jump", "sets": 3, "reps": 10}, {"exercise": "Burpee", "sets": 3, "reps": 10})])
    await login_as(client, customer)
    await _generate(client, db, customer)
    plan = (await db.execute(select(WorkoutPlan).where(WorkoutPlan.customer_id == customer.id))).scalar_one()
    assert plan.source == "template"
    assert {i["category"] for d in plan.plan["days"] for i in d["items"]} <= {"mobility", "breathing"}


async def test_suspended_customer_cannot_generate(client, db, fake_ai):
    provider = fake_ai(responses=[_ai_plan_json({"exercise": "Goblet squat", "sets": 3, "reps": 10})])
    customer, _ = await _member(db, ai=True)
    prof = await db.get(CustomerProfile, customer.id)
    prof.activity_suspended = True
    prof.suspension_reason = "Blood pressure reading 190/120"
    await db.commit()
    await login_as(client, customer)
    r = await post_form(client, "/me/coach/generate")
    assert r.status_code == 303
    assert await db.scalar(select(func.count(WorkoutPlan.id))) == 0 and provider.prompts == []
    assert await db.scalar(select(func.count(Job.id))) == 0  # nothing queued for a suspended member
    page = await client.get("/me/coach")
    assert page.status_code == 200 and "Coaching is paused" in page.text and "disabled" in page.text


async def test_minor_plan_notifies_guardian(client, db, ai_down):
    today = date.today()
    customer, _ = await _member(db, ai=True, date_of_birth=today.replace(year=today.year - 15))
    prof = await db.get(CustomerProfile, customer.id)
    prof.guardian_email = "guardian@example.com"
    await db.commit()
    await login_as(client, customer)
    await _generate(client, db, customer)
    plan = (await db.execute(select(WorkoutPlan))).scalar_one()
    assert "minor" in plan.plan["meta"]["tags"] and any("Under 18" in n for n in plan.safety_notes)
    mail = (await db.execute(select(EmailOutbox).where(EmailOutbox.user_id == customer.id))).scalar_one()
    assert mail.cc == ["guardian@example.com"]


# --------------------------------------------------------------------------- the click's AI charge + the job's events
GOOD_PLAN = _ai_plan_json({"exercise": "Goblet squat", "sets": 3, "reps": 10},
                          {"exercise": "Lat pulldown", "sets": 3, "reps": 10},
                          {"exercise": "Cat-cow", "sets": 2, "duration_s": 40}, days=3)


@pytest.fixture
def guard_on(monkeypatch):
    """The real guard with the shipped quotas: an AI member may start one coach plan (3 'gen' units) per minute."""
    monkeypatch.setattr(get_settings(), "ai_guard_enabled", True)
    monkeypatch.setattr(get_settings(), "ai_quota_scale", 1.0)


async def _gen_used(user_id: int) -> int:
    """'gen' units charged today (the day window: a minute boundary mid-test never resets it)."""
    return (await guard.status(user_id))["usage"]["gen"][2]


async def _click(client, db, customer) -> Job:
    r = await post_form(client, "/me/coach/generate")
    job = await _plan_job(db, customer)
    assert r.status_code == 303 and job is not None and r.headers["location"] == f"/me/coach?generating={job.id}"
    return job


async def _only_plan(db, customer) -> WorkoutPlan:
    stmt = select(WorkoutPlan).where(WorkoutPlan.customer_id == customer.id)
    return (await db.execute(stmt.execution_options(populate_existing=True))).scalar_one()


async def _wait_until(predicate: Callable[[], Any], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.01)


async def test_the_click_admission_rides_with_the_job_and_an_ai_plan_keeps_its_charge(client, db, fake_ai, guard_on):
    fake_ai(responses=[GOOD_PLAN])
    customer, _ = await _member(db, ai=True)
    await login_as(client, customer)
    job = await _click(client, db, customer)
    admission = job.payload["admission"]  # Admission.to_payload() of the click, settled by the job
    assert (admission["user_id"], admission["feature"], admission["group"], admission["units"], admission["settled"]) == (
        customer.id, "coach", "gen", 3, False)
    assert await _gen_used(customer.id) == 3
    assert await run_jobs() == 1
    assert (await _only_plan(db, customer)).source == "ai"
    assert await _gen_used(customer.id) == 3  # the AI answered: the charge stays ...
    assert await guard.settle_payload(dict(admission), False) is False  # ... for good: settled once, by the job
    assert await _gen_used(customer.id) == 3


@pytest.mark.parametrize("ai_state", ["offline", "busy"])
async def test_a_template_plan_gives_the_click_back(client, db, fake_ai, guard_on, monkeypatch, ai_state):
    customer, _ = await _member(db, ai=True)
    await login_as(client, customer)
    queue_full = [ai_state == "busy"]
    real_generate = coach_ai.generate_ai_plan

    async def generate(*args: Any, **kwargs: Any) -> Any:
        if queue_full[0]:
            raise AIRejected("busy", BUSY_MESSAGE, retry_after=5)
        return await real_generate(*args, **kwargs)

    monkeypatch.setattr(coach_ai, "generate_ai_plan", generate)
    if ai_state == "offline":
        ai.use_providers([FakeProvider("gateway", fail=True), FakeProvider("docker", fail=True)])
    await _click(client, db, customer)
    assert await _gen_used(customer.id) == 3
    assert await run_jobs() == 1
    plan = await _only_plan(db, customer)
    assert plan.source == "template" and plan.plan["meta"]["ai_status"] == "unavailable"
    assert await _gen_used(customer.id) == 0  # no AI plan came of the click: its 'coach' units are back ...

    queue_full[0] = False  # the AI is back
    fake_ai(responses=[GOOD_PLAN])
    await _click(client, db, customer)  # ... so asking again within the same minute is not refused
    assert await run_jobs() == 1 and await _gen_used(customer.id) == 3
    assert (await coach_service.active_plan(db, customer.id)).source == "ai"


async def test_an_unsafe_ai_plan_keeps_the_charge(client, db, fake_ai, guard_on):
    customer, _ = await _member(db, ai=True, conditions=["Surgery last month"])
    fake_ai(responses=[_ai_plan_json({"exercise": "Box jump", "sets": 3, "reps": 10},
                                     {"exercise": "Burpee", "sets": 3, "reps": 10})])
    await login_as(client, customer)
    await _click(client, db, customer)
    assert await run_jobs() == 1
    plan = await _only_plan(db, customer)
    assert plan.source == "template" and plan.plan["meta"]["ai_status"] == "rejected"
    assert await _gen_used(customer.id) == 3  # the AI did answer (its plan failed the safety filter)


@pytest.mark.parametrize("change", ["suspended", "no_ai_any_more", "membership_ended"])
async def test_no_ai_plan_by_the_time_the_job_runs_gives_the_click_back(client, db, fake_ai, guard_on, change):
    provider = fake_ai(responses=[GOOD_PLAN])
    customer, gym = await _member(db, ai=True)
    await login_as(client, customer)
    await _click(client, db, customer)
    assert await _gen_used(customer.id) == 3
    if change == "suspended":  # e.g. a red-flag check-in while the plan was queued
        await db.execute(update(CustomerProfile).where(CustomerProfile.user_id == customer.id)
                         .values(activity_suspended=True))
    elif change == "no_ai_any_more":  # switched to a plan without AI
        online = await db.scalar(select(Plan.id).where(Plan.gym_id == gym.id, Plan.code == "online"))
        await db.execute(update(Subscription).where(Subscription.customer_id == customer.id).values(plan_id=online))
    else:
        await db.execute(update(Subscription).where(Subscription.customer_id == customer.id)
                         .values(status=SubscriptionStatus.CANCELLED))
    await db.commit()
    assert await run_jobs() == 1
    assert (await _plan_job(db, customer)).status == JobStatus.DONE and provider.prompts == []
    plans = (await db.execute(select(WorkoutPlan).where(WorkoutPlan.customer_id == customer.id))).scalars().all()
    if change == "no_ai_any_more":
        assert [p.plan["meta"]["ai_status"] for p in plans] == ["not_included"]
    else:
        assert plans == []
    assert await _gen_used(customer.id) == 0


async def test_only_the_last_failed_attempt_gives_the_click_back(client, db, fake_ai, guard_on, monkeypatch):
    fake_ai(responses=[GOOD_PLAN])
    customer, _ = await _member(db, ai=True)

    async def explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("library table is corrupt")

    monkeypatch.setattr(coach_service, "generate_plan", explode)
    await login_as(client, customer)
    job = await _click(client, db, customer)
    assert await run_jobs() == 1  # attempt 1 of 2: the retry still owes the member the plan - the charge stays
    retry = await _plan_job(db, customer)
    assert (retry.status, retry.attempts) == (JobStatus.QUEUED, 1) and await _gen_used(customer.id) == 3
    await db.execute(update(Job).where(Job.id == job.id).values(run_after=utcnow()))
    await db.commit()
    assert await run_jobs() == 1  # the last attempt fails too: the click got nothing
    assert (await _plan_job(db, customer)).status == JobStatus.FAILED and await _gen_used(customer.id) == 0


async def test_a_plan_stopped_because_the_member_left_settles_only_when_it_finally_runs(client, db, fake_ai, guard_on,
                                                                                        monkeypatch):
    monkeypatch.setattr(coach_service, "PLAN_JOB_ATTEMPTS", 1)  # the stopped run is the last attempt
    release, started = asyncio.Event(), []

    async def slow_model(**kwargs: Any) -> str:
        started.append(1)
        await release.wait()
        return GOOD_PLAN

    fake_ai(handler=slow_model)
    customer, _ = await _member(db, ai=True)
    await login_as(client, customer)
    await _click(client, db, customer)
    runner = asyncio.create_task(run_jobs())
    await _wait_until(lambda: started)
    result = await presence.on_left(customer.id)
    assert result is not None and result["jobs"]["running_stopped"] == 1
    assert await asyncio.wait_for(runner, 10) == 1
    assert (await _plan_job(db, customer)).status == JobStatus.PAUSED
    assert await _gen_used(customer.id) == 3  # paused, not failed: the resumed run still owes the AI plan
    await client.get("/me/coach")  # back
    await presence.drain()
    release.set()
    assert await run_jobs() == 1
    assert (await _only_plan(db, customer)).source == "ai" and await _gen_used(customer.id) == 3


async def test_a_stop_that_surfaces_as_an_error_on_the_last_attempt_keeps_the_charge(client, db, guard_on,
                                                                                     monkeypatch):
    monkeypatch.setattr(coach_service, "PLAN_JOB_ATTEMPTS", 1)
    entered = asyncio.Event()

    async def interrupted(db: Any, payload: dict[str, Any], user_id: int) -> bool:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:  # e.g. a driver that reports the cancel as its own error
            raise RuntimeError("connection was closed in the middle of operation") from None
        return True

    monkeypatch.setattr(coach_service, "_build_plan", interrupted)
    customer, _ = await _member(db, ai=True)
    await login_as(client, customer)
    await _click(client, db, customer)
    runner = asyncio.create_task(run_jobs())
    await asyncio.wait_for(entered.wait(), 5)
    await jobs_service.apply_exit_policy(customer.id, reason="left")
    assert await asyncio.wait_for(runner, 10) == 1
    assert (await _plan_job(db, customer)).status == JobStatus.PAUSED
    assert await _gen_used(customer.id) == 3  # a stop is not the last attempt failing: no refund


async def test_the_plan_job_leaves_its_done_event_to_the_runner(client, db, fake_ai, guard_on):
    fake_ai(responses=[GOOD_PLAN])
    customer, _ = await _member(db, ai=True)
    await login_as(client, customer)
    job = await _click(client, db, customer)
    q = hub.subscribe(customer.id)
    try:
        assert await run_jobs() == 1
        for _ in range(30):
            await asyncio.sleep(0)
        events = []
        while not q.empty():
            evt = q.get_nowait()
            if isinstance(evt, dict) and evt.get("type") in ("task.update", "notification"):
                events.append(evt)
    finally:
        hub.unsubscribe(customer.id, q)
    # running -> the notification (after COMMIT) -> ONE 'done': the runner's, after the DONE write, with the job's
    # link and message, silent (the notification is the island note)
    assert [(e["type"], e.get("status")) for e in events] == [("task.update", "running"), ("notification", None),
                                                              ("task.update", "done")]
    done = events[-1]
    assert (done["task_id"], done["kind"], done["link"], done["message"], done["notify"]) == (
        job.id, "coach_plan", "/me/coach", coach_service.PLAN_READY, False)


async def test_coach_page_query_count_is_constant(client, db):
    customer, _ = await _member(db, ai=True)
    await login_as(client, customer)
    for i in range(4):
        db.add(Video(slug=f"v-{i}", title=f"V {i}", goal="general_fitness", description="d", duration_seconds=60, file_name=f"v{i}.mp4"))
    small, _, notes = template_plan("general_fitness", "beginner", [])
    small["days"] = small["days"][:1]
    small["days"][0]["items"] = small["days"][0]["items"][:2]
    small["days"][0]["items"][0]["video_slug"] = "v-0"
    await coach_service.save_plan(db, customer, goal="general_fitness", source="template", plan=small, removed=[], notes=notes,
                                  ai_provider=None, gym_id=None)
    await db.commit()
    await client.get("/me/coach")  # warm-up
    with QueryCounter() as q1:
        r = await client.get("/me/coach")
    assert r.status_code == 200
    big = {"title": "Big", "days": [{"name": f"Day {d}", "items": [dict(i) for i in small["days"][0]["items"] * 5]} for d in range(7)],
           "weekly_notes": []}
    for d in big["days"]:
        for n, item in enumerate(d["items"]):
            item["video_slug"] = f"v-{n % 4}"
    removed = [{"exercise": f"X{i}", "reason": "r", "replacement": None, "day": "Day 1"} for i in range(10)]
    await coach_service.save_plan(db, customer, goal="general_fitness", source="ai", plan=big, removed=removed, notes=notes,
                                  ai_provider="gateway", gym_id=None)
    await db.commit()
    with QueryCounter() as q2:
        r = await client.get("/me/coach")
    assert r.status_code == 200 and "Watch" in r.text
    assert q2.count == q1.count


# --------------------------------------------------------------------------- videos
async def _videos(db, n: int = 3) -> list[Video]:
    goals = ["calisthenics", "bulking", "weight_loss", "general_fitness", "recovery"]
    rows = []
    for i in range(n):
        v = Video(slug=f"session-{i}", title=f"Session {i}", goal=goals[i % len(goals)], level="beginner",
                  description=f"Guided session {i}", duration_seconds=300 + i, file_name=f"session-{i}.mp4",
                  poster_name=f"session-{i}.jpg", exercise_slugs=["goblet-squat"], sort_order=i)
        db.add(v)
        rows.append(v)
    await db.commit()
    return rows


async def test_videos_locked_without_subscription(client, db):
    customer = await make_customer(db)
    await _videos(db)
    await login_as(client, customer)
    r = await client.get("/me/videos")
    assert r.status_code == 200 and "Unlock the full video library" in r.text
    assert 'href="/me/videos/session-0"' not in r.text and "Session 0" in r.text  # teaser without links
    r = await client.get("/me/videos/session-0")
    assert r.status_code == 303 and r.headers["location"] == "/me/videos"


async def test_videos_any_subscription_and_ai_down(client, db, ai_down, tmp_path, monkeypatch):
    customer, _ = await _member(db, ai=False)
    await _seed_library(db)
    await _videos(db, 5)
    monkeypatch.setattr(coach_service, "VIDEO_DIR", tmp_path)
    (tmp_path / "session-0.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    (tmp_path / "session-0.jpg").write_bytes(b"\xff\xd8\xff")
    await login_as(client, customer)
    r = await client.get("/me/videos")
    assert r.status_code == 200 and "Unlock the full video library" not in r.text
    assert 'href="/me/videos/session-0"' in r.text and "Recovery &amp; mobility" in r.text
    r = await client.get("/me/videos?goal=calisthenics")
    assert "Session 0" in r.text and "Session 1" not in r.text
    r = await client.get("/me/videos/session-0")
    assert r.status_code == 200
    assert '<source src="/static/videos/session-0.mp4" type="video/mp4">' in r.text
    assert 'poster="/static/videos/session-0.jpg"' in r.text and "playsinline" in r.text
    assert "Goblet squat cue" in r.text  # cues of related exercises
    r = await client.get("/me/videos/session-1")
    assert r.status_code == 200 and "has not been generated yet" in r.text
    assert (await client.get("/me/videos/nope")).status_code == 404
    assert (await client.get("/me/videos/..%2f..%2fetc")).status_code in (403, 404)


async def test_inactive_video_is_hidden(client, db):
    customer, _ = await _member(db, ai=False)
    (v,) = await _videos(db, 1)
    v.is_active = False
    await db.commit()
    await login_as(client, customer)
    assert (await client.get("/me/videos/session-0")).status_code == 404
    assert "Session 0" not in (await client.get("/me/videos")).text


async def test_trainer_plan_with_irregular_json_renders(client, db):
    customer, gym = await _member(db, ai=False)
    db.add(WorkoutPlan(customer_id=customer.id, gym_id=gym.id, goal="general_fitness", source="trainer", title="Coach Asha's block",
                       plan={"days": [{"name": "Upper", "items": [{"exercise": "Bench press", "sets": "4", "reps": "8-12", "rest_s": "90"},
                                                                   {"exercise": "Rows", "reps": "AMRAP"}, "bad item", {"notes": "no name"}]},
                                      "not a day", {"name": "Empty", "items": []}],
                             "weekly_notes": "Single note string"},
                       safety_notes=[], removed_items=[], is_active=True))
    await db.commit()
    await login_as(client, customer)
    r = await client.get("/me/coach")
    assert r.status_code == 200
    assert "From your trainer" in r.text and "4 × 8-12" in r.text and "AMRAP" in r.text and "Empty" not in r.text
    assert "<span>Single note string</span>" in r.text


async def test_videos_page_query_count_is_constant(client, db):
    customer, _ = await _member(db, ai=False)
    await login_as(client, customer)
    await _videos(db, 2)
    await client.get("/me/videos")
    with QueryCounter() as small:
        assert (await client.get("/me/videos")).status_code == 200
    for i in range(20):
        db.add(Video(slug=f"extra-{i}", title=f"Extra {i}", goal="recovery", description="d", duration_seconds=90, file_name=f"x{i}.mp4"))
    await db.commit()
    with QueryCounter() as large:
        r = await client.get("/me/videos")
    assert r.status_code == 200 and "Extra 19" in r.text
    assert large.count == small.count
