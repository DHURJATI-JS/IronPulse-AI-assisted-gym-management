"""WP-E2 AI coach with safety guardrails, exercise library, pre-recorded videos.

Routes: ``GET /me/coach`` · ``POST /me/coach/generate`` · ``GET /me/coach/status`` · ``GET /me/videos`` ·
``GET /me/videos/{slug}``. Videos work with ANY active subscription and keep working when every AI provider is down.

Plans are built in the background (``coach_plan`` job, app/services/coach.py): the POST only queues one and
redirects to ``/me/coach?generating=<job id>``, whose progress card follows the job (WebSocket ``task.update``,
polling ``/me/coach/status`` as a fallback) and shows the new plan once it is saved.
"""

from __future__ import annotations

import math
import re
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import defer

from app.ai.errors import AIRejected
from app.ai.owner import clean_page_id, current_owner
from app.ai.safety import RULES, tag_label, to_int
from app.core.deps import DB, CustomerUser, NotFound
from app.core.templating import redirect, render
from app.models import Exercise, Video
from app.services import coach as coach_service
from app.services.subscriptions import plan_access

router = APIRouter()

VIDEO_GROUPS: tuple[tuple[str, str, str], ...] = (
    ("calisthenics", "Calisthenics", "Bodyweight strength, control and skills."),
    ("bulking", "Muscle building", "Hypertrophy sessions with dumbbells, barbells and machines."),
    ("weight_loss", "Fat loss & conditioning", "Low-impact circuits and steady cardio."),
    ("general_fitness", "General fitness", "Balanced full-body sessions for every level."),
    ("recovery", "Recovery & mobility", "Stretching, breathing and mobility flows."),
)
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")


def _enum_value(value: object, default: str) -> str:
    return str(getattr(value, "value", value) or default)


# ------------------------------------------------------------------ coach
def _display_item(item: dict) -> dict:
    out = dict(item)
    for key in ("sets", "duration_s", "rest_s"):
        out[key] = to_int(item.get(key))
    raw = item.get("reps")
    text = str(raw).strip() if isinstance(raw, str) else ""
    if text and not text.isdigit():  # "8-12", "AMRAP" ... from trainer plans
        out["reps"], out["reps_label"] = None, text[:20]
    else:
        out["reps"], out["reps_label"] = to_int(raw), None
    return out


def display_days(plan_json: object) -> list[dict]:
    """Defensive view of ``WorkoutPlan.plan`` (trainer plans from other modules may vary in shape)."""
    if not isinstance(plan_json, dict):
        return []
    days = []
    for i, day in enumerate(plan_json.get("days") or []):
        if not isinstance(day, dict):
            continue
        items = [_display_item(it) for it in (day.get("items") or []) if isinstance(it, dict) and it.get("exercise")]
        if not items:
            continue
        seconds = 0
        for it in items:
            work = it["duration_s"] or (it["reps"] or 10) * 4
            seconds += (it["sets"] or 1) * (work + (it["rest_s"] if it["rest_s"] is not None else 60))
        days.append({"name": str(day.get("name") or f"Day {i + 1}"), "focus": day.get("focus"), "items": items,
                     "minutes": max(1, math.ceil(seconds / 60))})
    return days


def weekly_notes(plan_json: object) -> list[str]:
    notes = plan_json.get("weekly_notes") if isinstance(plan_json, dict) else None
    if isinstance(notes, str):
        notes = [notes]
    return [str(n).strip()[:300] for n in (notes or []) if isinstance(n, (str, int, float)) and str(n).strip()][:8]


def _job_card(job: Any, generating: str | None) -> tuple[dict | None, dict | None]:
    """(progress card, problem notice) for the member's plan job: the card while it is queued / running / paused;
    a failed or stopped request only on the page its POST redirected to (``?generating=<id>``)."""
    if job is None:
        return None, None
    state = coach_service.job_state(job)
    if state["active"]:
        return state, None
    if state["status"] in ("failed", "cancelled") and (generating or "").strip() == str(job.id):
        return None, state
    return None, None


@router.get("/me/coach")
async def coach_page(request: Request, db: DB, user: CustomerUser, generating: str | None = None):
    access = await plan_access(db, user.id)
    job_card, job_problem = _job_card(await coach_service.plan_job_of(db, user.id) if access.has_any else None,
                                      generating)
    plan = await coach_service.active_plan(db, user.id) if access.has_any else None
    conditions = await coach_service.conditions_of(db, user.id)
    history = await coach_service.recent_plans(db, user.id, limit=6) if access.has_any else []
    days = display_days(plan.plan if plan is not None else None)
    slugs = {str(item["video_slug"]) for day in days for item in day["items"] if item.get("video_slug")}
    videos = await coach_service.videos_by_slug(db, slugs) if access.videos else {}
    profile = user.customer_profile
    tags = coach_service.tags_of(conditions, user.age())
    meta = (plan.plan or {}).get("meta", {}) if plan is not None and isinstance(plan.plan, dict) else {}
    return render(request, "customer/coach/index.html", {
        "ai_feature": True,
        "access": access,
        "plan": plan,
        "days": days,
        "weekly": weekly_notes(plan.plan if plan is not None else None),
        "n_items": sum(len(d["items"]) for d in days),
        "plan_meta": meta if isinstance(meta, dict) else {},
        "history": [h for h in history if plan is None or h.id != plan.id][:5],
        "videos": videos,
        "conditions": conditions,
        "tags": [(t, tag_label(t), RULES[t].avoid) for t in tags],
        "profile": profile,
        "suspended": bool(profile and profile.activity_suspended),
        "goal_label": coach_service.GOAL_LABELS.get(_enum_value(getattr(profile, "goal", None), "general_fitness"), "General fitness"),
        "level_label": coach_service.LEVEL_LABELS.get(_enum_value(getattr(profile, "fitness_level", None), "beginner"), "Beginner"),
        "ac": profile.ac_recommendation if profile is not None and isinstance(profile.ac_recommendation, dict) else None,
        "gear": _gear_for(conditions, user.age()),
        "job_card": job_card,
        "job_problem": job_problem,
    })


def _gear_for(conditions: Any, age: int | None) -> Any:
    """Protective-gear advice (back strap, gloves ...) from the member's recorded conditions."""
    from app.services.gear import recommend_gear

    names = [str(getattr(c, "name", c)) for c in (conditions or [])]
    return recommend_gear(names, age=age)


@router.post("/me/coach/generate")
async def coach_generate(request: Request, db: DB, user: CustomerUser):
    """Queue the plan (never waits for the AI): the AI guard judges the click first - a refusal comes back as a
    flash with the reason and the wait - then the ``coach_plan`` job builds, saves and announces the plan."""
    form = await request.form()
    owner = current_owner()
    if owner is not None:  # plain HTML form: the tab that asked (00-core.js adds page_id to every POST form)
        owner.page_id = clean_page_id(form.get("page_id")) or owner.page_id
    profile = user.customer_profile
    if profile is not None and profile.activity_suspended:
        return redirect("/me/coach", "Coaching is paused while your gym activity is suspended for safety. "
                                     "A gym admin has to approve your reactivation first.", "error")
    access = await plan_access(db, user.id)
    if not access.has_any:
        return redirect("/me/coach", "You need an active membership to get a training plan. Choose a gym to get started.", "warning")
    try:
        queued = await coach_service.queue_plan(db, user, access=access, owner=owner)
    except AIRejected as exc:
        return redirect("/me/coach", f"We couldn't start a new plan. {exc.message}",
                        "error" if exc.code == "cooldown" else "warning")
    except coach_service.PlanLimitError as exc:
        return redirect("/me/coach", exc.message, "warning")
    target = f"/me/coach?generating={queued.job.id}"
    if not queued.created:
        return redirect(target, "Your plan is already being built - it appears here as soon as it's ready.", "info")
    return redirect(target)


@router.get("/me/coach/status")
async def coach_status(db: DB, user: CustomerUser, job: str = ""):
    """JSON progress of one of the member's own plan requests (the coach page polls it when the WebSocket is quiet).
    Read-only: never queues, charges or starts anything. Another member's job is a 404, like a missing one."""
    raw = job.strip()
    job_id = int(raw) if raw.isascii() and raw.isdigit() and len(raw) <= 12 else 0
    row = await coach_service.plan_job_by_id(db, user.id, job_id) if job_id > 0 else None
    if row is None:
        raise NotFound("That plan request was not found.")
    return JSONResponse(coach_service.job_state(row), headers={"Cache-Control": "no-store"})


# ------------------------------------------------------------------ videos
def _video_card(v: Video) -> dict:
    return {
        "video": v,
        "available": coach_service.media_exists(v.file_name),
        "poster": v.poster_name if coach_service.media_exists(v.poster_name) else None,
    }


@router.get("/me/videos")
async def videos_page(request: Request, db: DB, user: CustomerUser, goal: str | None = None):
    access = await plan_access(db, user.id)
    known = {g for g, _, _ in VIDEO_GROUPS}
    goal = goal if goal in known else None
    stmt = select(Video).where(Video.is_active.is_(True)).order_by(Video.goal, Video.sort_order, Video.title)
    if goal:
        stmt = stmt.where(Video.goal == goal)
    rows = (await db.execute(stmt)).scalars().all()
    by_goal: dict[str, list[dict]] = {}
    for v in rows:
        by_goal.setdefault(v.goal, []).append(_video_card(v))
    groups = [(g, label, blurb, by_goal.get(g, [])) for g, label, blurb in VIDEO_GROUPS if not goal or g == goal]
    others = [c for g, cards in by_goal.items() if g not in known for c in cards]
    if others:
        groups.append(("other", "More sessions", "Extra sessions from your gym.", others))
    return render(request, "customer/videos/index.html", {
        "locked": not access.videos,
        "groups": groups,
        "goal": goal,
        "goal_tabs": VIDEO_GROUPS,
        "total": len(rows),
        "minutes": sum(v.duration_seconds or 0 for v in rows) // 60,
    })


@router.get("/me/videos/{slug}")
async def video_detail(request: Request, slug: str, db: DB, user: CustomerUser):
    if not _SLUG.match(slug):
        raise NotFound("Video not found.")
    video = (await db.execute(select(Video).where(Video.slug == slug, Video.is_active.is_(True)))).scalar_one_or_none()
    if video is None:
        raise NotFound("Video not found.")
    access = await plan_access(db, user.id)
    if not access.videos:
        return redirect("/me/videos", "Videos are included with every membership - choose a plan to unlock the library.", "warning")
    ex_slugs = [str(s) for s in (video.exercise_slugs or [])][:20]
    exercises = []
    if ex_slugs:
        rows = (await db.execute(select(Exercise).options(defer(Exercise.embedding)).where(Exercise.slug.in_(ex_slugs)))).scalars().all()
        order = {s: i for i, s in enumerate(ex_slugs)}
        exercises = sorted(rows, key=lambda e: order.get(e.slug, 99))
    related_rows = (
        await db.execute(
            select(Video)
            .where(Video.goal == video.goal, Video.slug != video.slug, Video.is_active.is_(True))
            .order_by(Video.sort_order, Video.title)
            .limit(4)
        )
    ).scalars().all()
    group_label = next((label for g, label, _ in VIDEO_GROUPS if g == video.goal), video.goal.replace("_", " ").title())
    return render(request, "customer/videos/detail.html", {
        "video": video,
        "available": coach_service.media_exists(video.file_name),
        "poster": video.poster_name if coach_service.media_exists(video.poster_name) else None,
        "exercises": exercises,
        "related": [_video_card(v) for v in related_rows],
        "group_label": group_label,
    })
