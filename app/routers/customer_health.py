"""WP-E1 customer dashboard, daily metrics + anomaly detection, safety suspension, wellness/stress, AC advice, bedtime.

GET  /me                                    "Today" dashboard (other roles are sent to their own home)
GET  /me/health                             metrics form (pre-filled), trend chart, history, alerts, AC advice
POST /me/health/metrics                     validate + rules -> save, or ask to confirm a severe reading
GET  /me/health/metrics/confirm?token=      confirmation page (signed, user-bound, 30-minute token)
POST /me/health/metrics/confirm             confirm -> AI layer (may only escalate), notifications, suspension
POST /me/health/scan                        scale display / medical report photo -> multi-engine OCR -> pre-fill
POST /me/health/alerts/{id}/ack             mark a health alert as read
GET  /me/health/alerts?page=                every health alert, newest first (20 per page)
GET  /me/health/profile, POST               goal, level, wake time, reminders, guardian, emergency contact, notes
POST /me/health/profile/conditions          add a condition / allergy / medication / injury
POST /me/health/profile/conditions/{id}/delete
GET  /me/health/profile/fitness, POST       training preferences (fitness profile: exercise likes / avoids, goals,
                                            equipment, schedule, notes - every list and text capped)
GET  /me/health/profile/ai                  "What the AI knows about you": everything the AI features use, each item
                                            with Edit / Forget, and exactly what a prompt is given
POST /me/health/profile/ai/forget           forget one item everywhere at once (app/ai/memory.py)
GET  /me/wellness, POST                     PSS-4 + symptoms + optional selfie (processed in memory, never stored)
GET  /me/wellness/{id}                      check-in result

Importing ``app.services.health`` here registers the ``bedtime_reminders`` periodic task.

Every list is bounded in SQL: the metric history and the alert history are paginated (``app.core.pagination``), a
member lists at most ``MAX_CONDITIONS`` conditions themselves and every conditions reader stops at
``CONDITIONS_SHOWN`` rows.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from sqlalchemy import delete, func, select

from app.ai import memory as memory_ai
from app.ai import wellness as wellness_ai
from app.ai.client import AIUnavailableError
from app.ai.embeddings import embed
from app.ai.errors import AICancelled, AIRejected
from app.ai.health import PROFILE_PROMPT_CHARS
from app.ai.member_context import load_member_context
from app.ai.owner import clean_page_id, current_owner
from app.core import caps
from app.core.deps import DB, AuthUser, CustomerUser, NotFound
from app.core.pagination import page_url, paginate, parse_page
from app.core.templating import redirect, render
from app.core.utils import local_now, normalize_phone, parse_hhmm, utcnow
from app.models import CustomerProfile, HealthAlert, HealthCondition, MetricEntry, Task, User, WellnessCheck
from app.models.enums import AnomalyLevel, ConditionKind, FitnessLevel, Goal, NotificationCategory, Role, Severity
from app.services import fitness_profile as fp
from app.services import gym_limits
from app.services import health as hs
from app.services import storage
from app.services.conditions import TAG_LABELS
from app.services.notify import email_address, notify
from app.services.subscriptions import plan_access

log = logging.getLogger("ironpulse.health.web")
router = APIRouter()

HOME_BY_ROLE = {Role.OWNER: "/owner", Role.STAFF: "/staff", Role.SUPERADMIN: "/superadmin"}
NEXT_TARGETS = ("/me", "/me/health")
GOALS = [(Goal.GENERAL_FITNESS.value, "General fitness"), (Goal.WEIGHT_LOSS.value, "Lose weight"),
         (Goal.BULKING.value, "Build muscle (bulking)"), (Goal.CALISTHENICS.value, "Calisthenics")]
LEVELS = [(FitnessLevel.BEGINNER.value, "Beginner"), (FitnessLevel.INTERMEDIATE.value, "Intermediate"),
          (FitnessLevel.ADVANCED.value, "Advanced")]
KINDS = [(ConditionKind.CONDITION.value, "Condition / illness"), (ConditionKind.ALLERGY.value, "Allergy"),
         (ConditionKind.MEDICATION.value, "Medication"), (ConditionKind.INJURY.value, "Injury")]
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
ALERTS_NEXT_RE = re.compile(r"^/me/health/alerts(\?page=[1-9][0-9]{0,5})?$")

# list bounds (all enforced in SQL)
MAX_CONDITIONS = hs.MAX_CONDITIONS  # a member's profile holds at most 50 items in total (race-safe, hs.add_condition)
CONDITIONS_SHOWN = 100  # every conditions reader stops here (owner intake allows 4 kinds x 20 = 80, so nothing real is cut)
CONDITIONS_TEXT_MAX = 4000  # characters of the conditions list that are embedded (feature hashing, off the event loop)
HISTORY_PER_PAGE = 30  # metric readings per history page (one reading a day)
ALERTS_SHOWN = 6  # latest alerts on /me/health ("All alerts" links to the paginated history)
ALERTS_PER_PAGE = 20


# ============================================================================ small helpers
def _s(form: Any, name: str, limit: int | None = None) -> str:
    value = form.get(name)
    if not isinstance(value, str):
        return ""
    value = value.strip()
    return value[:limit] if limit else value


def _next(form: Any, default: str = "/me/health") -> str:
    value = _s(form, "next")
    return value if value in NEXT_TARGETS else default


def _ack_next(form: Any) -> str:
    """Where "Mark as read" returns to: /me, /me/health or a page of the alert history."""
    value = _s(form, "next", 40)
    return value if ALERTS_NEXT_RE.match(value) else _next(form)


def _take_page_id(form: Any) -> None:
    """Plain HTML forms carry their tab's id (00-core.js adds ``page_id`` to every POST form): AI work this request
    starts belongs to that tab, so closing it stops the work nobody would see (docs/ai-guard-and-queue.md section 1).
    Safety work (health_metrics, exit policy 'keep') is never stopped by it."""
    owner = current_owner()
    if owner is not None:
        owner.page_id = clean_page_id(form.get("page_id")) or owner.page_id


def _is_minor(user: User) -> bool:
    age = user.age()
    return age is not None and age < 18


async def _entries(db: DB, user_id: int, limit: int) -> list[MetricEntry]:
    stmt = select(MetricEntry).where(MetricEntry.customer_id == user_id).order_by(MetricEntry.recorded_on.desc()).limit(limit)
    return list((await db.execute(stmt)).scalars().all())


async def _ac(db: DB, profile: CustomerProfile | None, tags: list[str], age: int | None) -> dict[str, Any]:
    """AC advice from the cache; recomputed (deterministically) and cached when conditions/age changed."""
    ac, changed = hs.ac_for_display(profile, tags, age)
    if changed and profile is not None:
        profile.ac_recommendation = ac
        await db.commit()
    return ac


def _provider_label(name: str | None) -> str | None:
    """Display label of the AI provider that produced a stored result (e.g. "gateway" -> "Gemma (gateway)")."""
    if not name:
        return None
    from app.ai.client import ai

    for p in ai.providers:
        if p.name == name:
            return p.label
    return name


def _saved_flash(outcome: hs.FinalizeOutcome) -> tuple[str, str]:
    if outcome.suspended:
        return ("Reading saved. Your gym activities are paused for your safety - please consult a healthcare "
                "professional, and call your local emergency number if you feel unwell.", "warning")
    if outcome.level == AnomalyLevel.CRITICAL:
        return "Reading saved. Please consult a healthcare professional soon - the details are in your health alerts.", "warning"
    if outcome.level == AnomalyLevel.WARNING:
        return "Reading saved with a note - see the alert on your health page.", "info"
    return "Reading saved. Nice consistency!", "success"


# ============================================================================ context builders
async def _dashboard_context(db: DB, user: User, *, form: dict | None = None, errors: dict | None = None) -> dict[str, Any]:
    now_local = local_now(user.timezone)
    today = now_local.date()
    age = user.age(today)
    minor = age is not None and age < 18
    profile = await db.get(CustomerProfile, user.id)
    entries = await _entries(db, user.id, 30)
    tasks = list((await db.execute(
        select(Task).where(Task.user_id == user.id, Task.completed_at.is_(None))
        .order_by(func.coalesce(Task.due_at, Task.remind_at).asc().nulls_last(), Task.id).limit(5)
    )).scalars().all())
    access = await plan_access(db, user.id)
    alerts = list((await db.execute(
        select(HealthAlert).where(HealthAlert.customer_id == user.id, HealthAlert.acknowledged_at.is_(None))
        .order_by(HealthAlert.created_at.desc()).limit(3)
    )).scalars().all())
    tags = await hs.condition_tags(db, user.id)
    last_check = (await db.execute(
        select(WellnessCheck).where(WellnessCheck.customer_id == user.id).order_by(WellnessCheck.created_at.desc()).limit(1)
    )).scalars().first()
    ac = await _ac(db, profile, tags, age)

    plan = hs.bedtime_for(age, profile.wake_time if profile else None)
    confirmed = [e for e in entries if e.confirmed]
    latest = confirmed[0] if confirmed else None
    values = form or hs.prefill_values(entries, profile)
    logged_today = any(e.recorded_on == today for e in entries)
    suspended = bool(profile and profile.activity_suspended)
    if suspended:
        tagline = "Your safety comes first today - rest, and check in with a healthcare professional."
    elif logged_today:
        tagline = "Today's reading is in. Here's your day at a glance."
    else:
        tagline = "Here's your day at a glance - log today's reading to keep your trend honest."
    return {
        "ai_feature": True,
        "greeting": hs.greeting_for(now_local), "first_name": hs.first_name(user),
        "today": today, "today_label": now_local.strftime("%A, %d %B").replace(" 0", " "), "tagline": tagline,
        "profile": profile, "minor": minor, "plan": plan, "sleep": hs.sleep_window(plan, user.timezone),
        "reminders_on": profile.sleep_reminders_enabled if profile else True,
        "latest": latest, "delta": hs.change_over(entries, 30, today),
        "category": hs.bmi_category(latest.bmi, age) if latest else None,
        "gauge": hs.bmi_gauge(latest.bmi, age) if latest else None,
        "spark": hs.sparkline(list(reversed(confirmed[:14]))),
        "streak": hs.logging_streak((e.recorded_on for e in confirmed), today),
        "form": values, "errors": errors or {}, "hint": hs.bmi_hint(values, age), "logged_today": logged_today,
        "tasks": tasks, "subs": access.subscriptions, "access": access, "alerts": alerts,
        "last_check": last_check, "ac": ac, "fmt": hs.fmt_num,
        # "Online gyms N/3 · In-person gym N/1" (app/services/gym_limits.py: one indexed statement)
        "gym_slots": await gym_limits.gym_slots(db, user.id),
    }


def _history_url(page: int) -> str:
    return page_url("/me/health", None, page) + "#history"


async def _health_context(db: DB, user: User, *, form: dict | None = None, errors: dict | None = None,
                          scan: hs.ScanResult | None = None, scan_error: str | None = None,
                          source: str = "manual", page: int = 1) -> dict[str, Any]:
    today = local_now(user.timezone).date()
    age = user.age(today)
    profile = await db.get(CustomerProfile, user.id)
    entries = await _entries(db, user.id, 60)
    # the History table: one page of readings (count(*) OVER () + LIMIT/OFFSET - a single statement)
    history = await paginate(
        db, select(MetricEntry).where(MetricEntry.customer_id == user.id)
        .order_by(MetricEntry.recorded_on.desc(), MetricEntry.id.desc()),
        page=page, per_page=HISTORY_PER_PAGE, max_per_page=HISTORY_PER_PAGE, window_count=True,
    )
    alerts = list((await db.execute(
        select(HealthAlert).where(HealthAlert.customer_id == user.id)
        .order_by(HealthAlert.created_at.desc(), HealthAlert.id.desc()).limit(ALERTS_SHOWN + 1)
    )).scalars().all())
    tags = await hs.condition_tags(db, user.id)
    ac = await _ac(db, profile, tags, age)
    confirmed = [e for e in entries if e.confirmed]
    latest = confirmed[0] if confirmed else None
    values = form or hs.prefill_values(entries, profile)
    from app.services.gear import recommend_gear

    condition_names = list((await db.execute(
        select(HealthCondition.name).where(HealthCondition.customer_id == user.id)
        .order_by(HealthCondition.id).limit(CONDITIONS_SHOWN)
    )).scalars().all())
    return {
        "gear": recommend_gear(condition_names, age=age),
        "ai_feature": True, "profile": profile, "minor": age is not None and age < 18, "today": today,
        "entries": entries[:HISTORY_PER_PAGE], "history": history, "history_url": _history_url,
        "alerts": alerts[:ALERTS_SHOWN], "alerts_more": len(alerts) > ALERTS_SHOWN, "ac": ac, "latest": latest,
        "category": hs.bmi_category(latest.bmi, age) if latest else None,
        "change": hs.change_over(entries, 30, today), "chart": hs.trend_chart(list(reversed(confirmed[:30]))),
        "form": values, "errors": errors or {}, "hint": hs.bmi_hint(values, age), "source": source,
        "logged_today": any(e.recorded_on == today for e in entries),
        "scan": scan, "scan_error": scan_error, "fmt": hs.fmt_num,
    }


async def _profile_context(db: DB, user: User, *, form: dict | None = None, errors: dict | None = None,
                           cond_form: dict | None = None, cond_errors: dict | None = None) -> dict[str, Any]:
    age = user.age()
    profile = await db.get(CustomerProfile, user.id)
    conditions = list((await db.execute(
        select(HealthCondition).where(HealthCondition.customer_id == user.id)
        .order_by(HealthCondition.kind, func.lower(HealthCondition.name), HealthCondition.id).limit(CONDITIONS_SHOWN + 1)
    )).scalars().all())
    conditions_more = len(conditions) > CONDITIONS_SHOWN
    conditions = conditions[:CONDITIONS_SHOWN]
    ac = await _ac(db, profile, sorted({c.tag for c in conditions if c.tag}), age)
    if form is None:
        p = profile
        form = {
            "goal": (p.goal.value if p else Goal.GENERAL_FITNESS.value),
            "fitness_level": (p.fitness_level.value if p else FitnessLevel.BEGINNER.value),
            "wake_time": (p.wake_time.strftime("%H:%M") if p and p.wake_time else "06:30"),
            "sleep_reminders": (p.sleep_reminders_enabled if p else True),
            "guardian_name": (p.guardian_name if p else "") or "", "guardian_relation": (p.guardian_relation if p else "") or "",
            "guardian_email": (p.guardian_email if p else "") or "", "guardian_phone": (p.guardian_phone if p else "") or "",
            "emergency_contact_name": (p.emergency_contact_name if p else "") or "",
            "emergency_contact_phone": (p.emergency_contact_phone if p else "") or "",
            "health_notes": (p.health_notes if p else "") or "",
        }
    try:
        wake = parse_hhmm(form.get("wake_time")) or (profile.wake_time if profile else None)
    except ValueError:
        wake = profile.wake_time if profile else None
    return {
        "ai_feature": True, "profile": profile, "minor": age is not None and age < 18, "form": form,
        "errors": errors or {}, "conditions": conditions, "conditions_more": conditions_more,
        "max_conditions": MAX_CONDITIONS, "cond_form": cond_form or {"kind": "condition"},
        "cond_errors": cond_errors or {}, "goals": GOALS, "levels": LEVELS, "kinds": KINDS, "ac": ac,
        "plan": hs.bedtime_for(age, wake), "tag_labels": TAG_LABELS,
    }


async def _wellness_context(db: DB, user: User, *, form: dict | None = None, errors: dict | None = None) -> dict[str, Any]:
    history = list((await db.execute(
        select(WellnessCheck).where(WellnessCheck.customer_id == user.id).order_by(WellnessCheck.created_at.desc()).limit(8)
    )).scalars().all())
    return {
        "ai_feature": True, "questions": list(enumerate(hs.PSS_QUESTIONS, 1)), "options": hs.PSS_OPTIONS,
        "symptoms": hs.SYMPTOMS, "red_flags": sorted(hs.RED_FLAG_SYMPTOMS), "history": history,
        "form": form or {}, "errors": errors or {}, "minor": _is_minor(user),
    }


# ============================================================================ dashboard
@router.get("/me")
async def dashboard(request: Request, db: DB, user: AuthUser):
    if user.role != Role.CUSTOMER:
        return redirect(HOME_BY_ROLE.get(user.role, "/"))
    return render(request, "customer/dashboard.html", await _dashboard_context(db, user))


# ============================================================================ daily metrics
@router.get("/me/health")
async def health_page(request: Request, db: DB, user: CustomerUser):
    page = parse_page(request, HISTORY_PER_PAGE, HISTORY_PER_PAGE).page
    return render(request, "customer/health/index.html", await _health_context(db, user, page=page))


@router.post("/me/health/metrics")
async def log_metrics(request: Request, db: DB, user: CustomerUser):
    form = await request.form()
    _take_page_id(form)  # the AI safety review of an abnormal reading is never refused or stopped (health_metrics)
    nxt = _next(form)
    values = {"height_cm": _s(form, "height_cm", 12), "weight_kg": _s(form, "weight_kg", 12)}
    height, weight, errors = hs.validate_metrics(values["height_cm"], values["weight_kg"])
    source = "ocr" if _s(form, "source") == "ocr" else "manual"
    if errors or height is None or weight is None:
        if nxt == "/me":
            ctx = await _dashboard_context(db, user, form=values, errors=errors)
            return render(request, "customer/dashboard.html", ctx, status_code=400)
        ctx = await _health_context(db, user, form=values, errors=errors, source=source)
        return render(request, "customer/health/index.html", ctx, status_code=400)
    logged = await hs.record_metrics(db, user, height, weight, source=source)
    if logged.evaluation.needs_confirmation:
        token = hs.make_confirm_token(user.id, logged.entry)
        await db.commit()
        return redirect("/me/health/metrics/confirm?" + urlencode({"token": token, "next": nxt}))
    outcome = await hs.finalize_metrics(db, user, logged.entry, alert=logged.alert)
    await db.commit()
    return redirect(nxt, *_saved_flash(outcome))


async def _entry_for_token(db: DB, user: User, token: str, *, lock: bool = False) -> MetricEntry | None:
    """The reading a confirmation token names (the member's own, values unchanged). ``lock``: under the member's
    readings lock and a row lock, freshly read - so a reading is confirmed (and AI-reviewed) once, however many
    confirmations of it arrive at the same moment (two tabs, a double click, a script)."""
    data = hs.read_confirm_token(token, user.id)
    if data is None or not isinstance(data.get("e"), int):
        return None
    if lock:
        await hs.lock_metrics(db, user.id)
        entry = (await db.execute(
            select(MetricEntry).where(MetricEntry.id == data["e"], MetricEntry.customer_id == user.id)
            .with_for_update().execution_options(populate_existing=True)
        )).scalar_one_or_none()
    else:
        entry = await db.get(MetricEntry, data["e"])
    if entry is None or entry.customer_id != user.id or not hs.token_matches(data, entry):
        return None
    return entry


@router.get("/me/health/metrics/confirm")
async def confirm_page(request: Request, db: DB, user: CustomerUser, token: str = "", next: str = "/me/health"):
    entry = await _entry_for_token(db, user, token)
    if entry is None:
        return redirect("/me/health", "This confirmation has expired. Please log your reading again.", "error")
    if entry.confirmed:
        return redirect("/me/health", "This reading is already confirmed.", "info")
    level = hs.as_level(entry.anomaly_level)
    return render(request, "customer/health/confirm.html", {
        "ai_feature": True, "entry": entry, "level": level, "level_rank": hs.level_rank(level),
        "findings": [f for f in (entry.anomaly_reasons or []) if isinstance(f, dict)],
        "token": token, "next": next if next in NEXT_TARGETS else "/me/health", "minor": _is_minor(user),
        "fmt": hs.fmt_num,
    })


@router.post("/me/health/metrics/confirm")
async def confirm_metrics(request: Request, db: DB, user: CustomerUser):
    form = await request.form()
    _take_page_id(form)
    nxt = _next(form)
    entry = await _entry_for_token(db, user, _s(form, "token", 2000), lock=True)
    if entry is None:
        await db.commit()  # releases the readings lock
        return redirect("/me/health", "This confirmation has expired. Please log your reading again.", "error")
    if entry.confirmed:
        await db.commit()
        return redirect(nxt, "This reading is already confirmed.", "info")
    alert = (await db.execute(
        select(HealthAlert).where(HealthAlert.metric_entry_id == entry.id, HealthAlert.source == "rule")
        .order_by(HealthAlert.id.desc()).limit(1)
    )).scalars().first()
    outcome = await hs.finalize_metrics(db, user, entry, alert=alert)
    await db.commit()
    return redirect(nxt, *_saved_flash(outcome))


@router.post("/me/health/scan")
async def scan_photo(request: Request, db: DB, user: CustomerUser):
    """OCR engines + the AI vision model read the photo (in memory only). A refused AI check (quota, cooldown) is
    explained on the result card next to the OCR read-out; a stopped one (the page left) simply leaves the OCR
    read-out - the photo is never stored either way."""
    form = await request.form()
    _take_page_id(form)
    upload = form.get("photo")
    try:
        if upload is None or isinstance(upload, str) or not getattr(upload, "filename", None):
            raise storage.UploadError("Choose a photo of the scale display or the medical report.")
        image = await storage.run_image_decode(hs.normalize_photo, await storage.read_upload(upload), 2000)
    except storage.UploadError as exc:
        ctx = await _health_context(db, user, scan_error=str(exc))
        return render(request, "customer/health/index.html", ctx, status_code=400)
    # OCR (up to 90 s) and the AI vision check (up to 60 s) must not run inside the request's transaction: past the
    # idle-in-transaction limit (60 s) the server kills the connection and the page fails. Nothing is pending here.
    await db.commit()
    result = await hs.scan_measurements(image)
    del image  # processed in memory only - never stored
    prefill = None
    if result.found:
        base = hs.prefill_values(await _entries(db, user.id, 10), await db.get(CustomerProfile, user.id))
        prefill = {
            "height_cm": hs.fmt_num(result.height_cm) if result.height_cm is not None else base["height_cm"],
            "weight_kg": hs.fmt_num(result.weight_kg) if result.weight_kg is not None else base["weight_kg"],
        }
    ctx = await _health_context(db, user, form=prefill, scan=result, source="ocr" if result.found else "manual")
    return render(request, "customer/health/index.html", ctx)


@router.post("/me/health/alerts/{alert_id}/ack")
async def acknowledge_alert(alert_id: int, request: Request, db: DB, user: CustomerUser):
    form = await request.form()
    alert = (await db.execute(
        select(HealthAlert).where(HealthAlert.id == alert_id, HealthAlert.customer_id == user.id)
    )).scalars().first()
    if alert is None:
        raise NotFound("Alert not found.")
    if alert.acknowledged_at is None:
        alert.acknowledged_at = utcnow()
        await db.commit()
    return redirect(_ack_next(form), "Alert marked as read.", "success")


@router.get("/me/health/alerts")
async def alerts_page(request: Request, db: DB, user: CustomerUser):
    """Every health alert, newest first - one page at a time (COUNT + one LIMIT/OFFSET query)."""
    page = await paginate(
        db, select(HealthAlert).where(HealthAlert.customer_id == user.id)
        .order_by(HealthAlert.created_at.desc(), HealthAlert.id.desc()),
        page=parse_page(request, ALERTS_PER_PAGE, ALERTS_PER_PAGE).page, per_page=ALERTS_PER_PAGE,
        max_per_page=ALERTS_PER_PAGE,
    )
    here = page_url("/me/health/alerts", None, page.page)
    return render(request, "customer/health/alerts.html", {"page": page, "here": here})


# ============================================================================ health profile
@router.get("/me/health/profile")
async def profile_page(request: Request, db: DB, user: CustomerUser):
    return render(request, "customer/health/profile.html", await _profile_context(db, user))


@router.post("/me/health/profile")
async def profile_save(request: Request, db: DB, user: CustomerUser):
    form = await request.form()
    minor = _is_minor(user)
    values: dict[str, Any] = {
        "goal": _s(form, "goal", 40), "fitness_level": _s(form, "fitness_level", 40),
        "wake_time": _s(form, "wake_time", 8), "sleep_reminders": _s(form, "sleep_reminders") == "1",
        "guardian_name": _s(form, "guardian_name", 200), "guardian_relation": _s(form, "guardian_relation", 80),
        "guardian_email": _s(form, "guardian_email", 300).lower(), "guardian_phone": _s(form, "guardian_phone", 40),
        "emergency_contact_name": _s(form, "emergency_contact_name", 200),
        "emergency_contact_phone": _s(form, "emergency_contact_phone", 40),
        "health_notes": _s(form, "health_notes", 1200),
    }
    errors: dict[str, str] = {}
    if values["goal"] not in {g.value for g in Goal}:
        errors["goal"] = "Choose a goal."
    if values["fitness_level"] not in {lv.value for lv in FitnessLevel}:
        errors["fitness_level"] = "Choose your fitness level."
    wake = None
    try:
        wake = parse_hhmm(values["wake_time"])
        if wake is None:
            errors["wake_time"] = "Enter your usual wake-up time (HH:MM)."
    except ValueError:
        errors["wake_time"] = "Use a 24-hour time like 06:30."
    if len(values["health_notes"]) > 1000:
        errors["health_notes"] = "Keep notes under 1,000 characters."
    country = user.country_code or "IN"

    def check_phone(key: str, required: bool, label: str) -> None:
        raw = values[key]
        if not raw:
            if required:
                errors[key] = f"Enter the {label} phone number."
            return
        if len(raw) > 32:
            errors[key] = "That phone number is too long."
            return
        try:
            normalize_phone(raw, country)
        except ValueError as exc:
            errors[key] = str(exc)

    if minor:
        if not 2 <= len(values["guardian_name"]) <= 120:
            errors["guardian_name"] = "Enter your parent's or guardian's full name."
        if len(values["guardian_relation"]) > 40:
            errors["guardian_relation"] = "Keep it under 40 characters."
        email = values["guardian_email"]
        if not email or len(email) > 254 or not EMAIL_RE.match(email):
            errors["guardian_email"] = "Enter a valid e-mail address for your parent or guardian."
        elif user.email and email == user.email.lower():
            errors["guardian_email"] = "Use your parent's or guardian's e-mail, not your own."
        check_phone("guardian_phone", True, "guardian's")
    if len(values["emergency_contact_name"]) > 120:
        errors["emergency_contact_name"] = "Keep the name under 120 characters."
    check_phone("emergency_contact_phone", bool(values["emergency_contact_name"]), "emergency contact's")
    if values["emergency_contact_phone"] and not values["emergency_contact_name"]:
        errors.setdefault("emergency_contact_name", "Add the emergency contact's name.")
    if errors:
        ctx = await _profile_context(db, user, form=values, errors=errors)
        return render(request, "customer/health/profile.html", ctx, status_code=400)

    profile = await hs.ensure_profile(db, user)
    changes: list[str] = []

    def apply(attr: str, new: Any, label: str) -> None:
        if getattr(profile, attr) != new:
            setattr(profile, attr, new)
            changes.append(label)

    old_guardian_email = (profile.guardian_email or "").lower()
    apply("goal", Goal(values["goal"]), "goal")
    apply("fitness_level", FitnessLevel(values["fitness_level"]), "fitness level")
    apply("wake_time", wake, "wake-up time")
    apply("sleep_reminders_enabled", values["sleep_reminders"], "bedtime reminders")
    apply("health_notes", values["health_notes"] or None, "notes for your coach")
    if minor:
        apply("guardian_name", values["guardian_name"], "guardian name")
        apply("guardian_relation", values["guardian_relation"] or None, "guardian relationship")
        apply("guardian_email", values["guardian_email"], "guardian e-mail")
        apply("guardian_phone", values["guardian_phone"], "guardian phone")
    apply("emergency_contact_name", values["emergency_contact_name"] or None, "emergency contact")
    apply("emergency_contact_phone", values["emergency_contact_phone"] or None, "emergency contact phone")
    if not changes:
        return redirect("/me/health/profile", "No changes to save.", "info")
    await notify(
        db, user, "Health profile updated",
        f"Your health profile was updated ({', '.join(changes)}). If this wasn't you, contact your gym admin.",
        category=NotificationCategory.ACCOUNT, severity=Severity.INFO, link="/me/health/profile",
    )
    if minor and values["guardian_email"] and values["guardian_email"] != old_guardian_email:
        await email_address(
            db, values["guardian_email"], "You're listed as a guardian on IronPulse",
            f"{user.full_name} added you as their parent or guardian on IronPulse. You'll receive copies of their "
            "safety alerts and account messages. If this is a mistake, please contact the gym.",
            recipient=values["guardian_name"],
        )
    await db.commit()
    return redirect("/me/health/profile", "Health profile saved.", "success")


async def _after_conditions_change(db: DB, user: User, change: str) -> bool:
    """Refresh the conditions embedding + the rule-based AC advice and notify (``hs.after_conditions_change``: a
    bounded, safety-first read; the text hashed in a worker thread). Returns whether the plan includes AI: then the
    caller queues the friendlier AI wording (``hs.queue_ac_note``) once this change is committed - the request never
    waits for the AI."""
    return await hs.after_conditions_change(db, user, change, embed_fn=embed)


@router.post("/me/health/profile/conditions")
async def add_condition(request: Request, db: DB, user: CustomerUser):
    form = await request.form()
    values = {"kind": _s(form, "kind", 20), "name": " ".join(_s(form, "name", 400).split()), "notes": _s(form, "notes", 400)}
    errors: dict[str, str] = {}
    if values["kind"] not in {k.value for k in ConditionKind}:
        errors["kind"] = "Choose a type."
    if not 2 <= len(values["name"]) <= 120:
        errors["name"] = "Enter a name between 2 and 120 characters."
    if len(values["notes"]) > 255:
        errors["notes"] = "Keep notes under 255 characters."
    if not errors:
        # duplicate check, cap check and insert behind the member's lock (hs.add_condition): requests that arrive at
        # the same moment can never pass MAX_CONDITIONS
        added = await hs.add_condition(db, user.id, kind=ConditionKind(values["kind"]), name=values["name"],
                                       notes=values["notes"] or None, source="self")
        if added.status == "known":
            errors["name"] = "That's already on your list."
        elif added.status == "full":
            errors["name"] = (f"Your profile already lists {MAX_CONDITIONS} items - the most it can hold. "
                              "Remove one you no longer need, then add this one.")
    if errors:
        await db.commit()  # nothing was written: this ends the transaction and so releases the member's cap lock
        ctx = await _profile_context(db, user, cond_form=values, cond_errors=errors)
        return render(request, "customer/health/profile.html", ctx, status_code=400)
    kind = ConditionKind(values["kind"])
    fp.unforget(await fp.lock_row(db, user.id), values["name"])  # typed back in: the AI may use it again
    ai_wording = await _after_conditions_change(db, user, f"Added {dict(KINDS)[kind.value].lower()}: {values['name']}")
    await db.commit()
    if ai_wording:
        await hs.queue_ac_note(db, user)
    return redirect("/me/health/profile#conditions", f"Added “{values['name']}” to your health profile.", "success")


@router.post("/me/health/profile/conditions/{condition_id}/delete")
async def remove_condition(condition_id: int, request: Request, db: DB, user: CustomerUser):
    cond = (await db.execute(
        select(HealthCondition).where(HealthCondition.id == condition_id, HealthCondition.customer_id == user.id)
    )).scalars().first()
    if cond is None:
        raise NotFound("Condition not found.")
    name, tag = cond.name, cond.tag
    await hs.lock_conditions(db, user.id)
    await db.execute(delete(HealthCondition).where(HealthCondition.id == cond.id, HealthCondition.customer_id == user.id))
    db.expunge(cond)
    # removed everywhere at once: every AI prompt reads the list afresh, and the assistant's view of past chats hides it
    fp.note_forgotten(await fp.lock_row(db, user.id), name, tag)
    ai_wording = await _after_conditions_change(db, user, f"Removed: {name}")
    await db.commit()
    if ai_wording:
        await hs.queue_ac_note(db, user)
    return redirect("/me/health/profile#conditions", f"Removed “{name}”.", "success")


# ============================================================================ training preferences (fitness profile)
FITNESS_PAGE = "/me/health/profile/fitness"
MEMORY_PAGE = "/me/health/profile/ai"
FORGET_KINDS = ("health", "list", "scalar", "fitness_notes", "health_notes", "level")


def _vocab_options(vocab: dict[str, tuple[str, tuple[str, ...]]]) -> list[tuple[str, str]]:
    return [(slug, label) for slug, (label, _phrases) in vocab.items()]


async def _fitness_context(db: DB, user: User, *, values: dict[str, Any] | None = None,
                           errors: dict[str, str] | None = None) -> dict[str, Any]:
    row = await fp.load(db, user.id)
    data = fp.FitnessData.from_row(row)
    profile = await db.get(CustomerProfile, user.id)
    level = (profile.fitness_level.value if profile is not None else FitnessLevel.BEGINNER.value)
    if values is None:
        def own(name: str) -> str:  # the member's own labels go back into the "Other" box
            return ", ".join(v for v in getattr(data, name) if v not in fp.VOCAB[name])

        values = {
            "likes": data.likes, "avoids": data.avoids, "goals": data.goals, "equipment": data.equipment,
            "likes_other": own("likes"), "avoids_other": own("avoids"), "goals_other": own("goals"),
            "equipment_other": own("equipment"),
            "sessions_per_week": str(data.sessions_per_week or ""), "session_minutes": str(data.session_minutes or ""),
            "preferred_time": data.preferred_time or "", "notes": data.notes or "", "fitness_level": level,
        }
    return {
        "ai_feature": True, "form": values, "errors": errors or {}, "levels": LEVELS,
        "exercise_types": _vocab_options(fp.EXERCISE_TYPES), "goal_options": _vocab_options(fp.GOALS),
        "equipment_options": _vocab_options(fp.EQUIPMENT), "times": fp.PREFERRED_TIMES, "caps": fp.LIST_CAPS,
        "item_max": fp.ITEM_MAX, "notes_max": fp.NOTES_MAX, "sessions_range": fp.SESSIONS_RANGE,
        "minutes_range": fp.MINUTES_RANGE, "memory_page": MEMORY_PAGE,
    }


@router.get("/me/health/profile/fitness")
async def fitness_page(request: Request, db: DB, user: CustomerUser):
    return render(request, "customer/health/fitness.html", await _fitness_context(db, user))


@router.post("/me/health/profile/fitness")
async def fitness_save(request: Request, db: DB, user: CustomerUser):
    """Training preferences: vocabulary checkboxes + "other" text, schedule, notes - every list capped (20 / 20 / 10 /
    30), every text length-capped; friendly errors re-render the form with what was typed. Saved behind the row lock
    (``fp.save_form``), so two tabs saving at once can neither pass a cap nor lose each other's change."""
    form = await request.form()
    values, errors, data = fp.parse_form(form)
    level = _s(form, "fitness_level", 40)
    values["fitness_level"] = level
    if level and level not in {lv.value for lv in FitnessLevel}:
        errors["fitness_level"] = "Choose your experience level."
    if errors:
        ctx = await _fitness_context(db, user, values=values, errors=errors)
        return render(request, "customer/health/fitness.html", ctx, status_code=400)
    changed = await fp.save_form(db, user.id, data)
    if level:
        profile = await hs.ensure_profile(db, user)
        if profile.fitness_level != FitnessLevel(level):
            profile.fitness_level = FitnessLevel(level)
            changed.append("experience level")
    if not changed:
        await db.rollback()
        return redirect(FITNESS_PAGE, "No changes to save.", "info")
    await notify(db, user, "Training preferences updated",
                 f"Your training preferences were updated ({', '.join(changed[:6])}). Your coach and the AI use them now.",
                 category=NotificationCategory.ACCOUNT, severity=Severity.INFO, link=FITNESS_PAGE, email=False)
    await db.commit()
    return redirect(FITNESS_PAGE, "Training preferences saved - your coach and the AI use them from now on.", "success")


# ============================================================================ what the AI knows about you
@router.get("/me/health/profile/ai")
async def ai_memory_page(request: Request, db: DB, user: CustomerUser):
    """Everything the AI features use about the member (the ONE member context - the same read every prompt makes),
    each item with "Edit" and "Forget", plus exactly what a prompt is given."""
    member = await load_member_context(db, user)
    held = await hs.conditions_held(db, user.id, limit=hs.MAX_CONDITIONS + 1)
    return render(request, "customer/health/ai_memory.html", {
        "ai_feature": True, "member": member, "held": held, "max_conditions": hs.MAX_CONDITIONS,
        "kind_labels": {"condition": "Condition", "injury": "Injury", "medication": "Medication", "allergy": "Allergy"},
        "tag_labels": TAG_LABELS, "fp": fp, "prompt_preview": member.block(), "fitness_page": FITNESS_PAGE,
        "goal_labels": dict(GOALS), "level_labels": dict(LEVELS),
        "from_chat": ("assistant", "live_coach"),
    })


@router.post("/me/health/profile/ai/forget")
async def ai_memory_forget(request: Request, db: DB, user: CustomerUser):
    """"Forget" on the memory page: that one item is removed everywhere at once (no cache to wait for) and hidden
    from the assistant's view of past chats. Re-adding it later makes it visible to the AI again."""
    form = await request.form()
    kind = _s(form, "kind", 20)
    if kind not in FORGET_KINDS:
        return redirect(MEMORY_PAGE, "Choose something to forget.", "error")
    result = await memory_ai.forget_item(db, user, kind=kind, field_name=_s(form, "field", 30),
                                         value=_s(form, "value", 200))
    if not result["ok"]:
        await db.rollback()
        return redirect(MEMORY_PAGE, result["detail"], "info")
    await db.commit()
    await memory_ai.after_commit(db, user, result)
    return redirect(MEMORY_PAGE, result["detail"], "success")


# ============================================================================ wellness / stress check-in
@router.get("/me/wellness")
async def wellness_page(request: Request, db: DB, user: CustomerUser):
    return render(request, "customer/wellness/index.html", await _wellness_context(db, user))


@router.post("/me/wellness")
async def wellness_submit(request: Request, db: DB, user: CustomerUser):
    """PSS-4 + symptoms + an optional selfie (analysed in memory, never stored). The questionnaire and the red-flag
    safety path are ALWAYS recorded, whatever happens to the selfie read-out: refused by the AI guard (the flash says
    why and how long to wait), stopped because the page that asked is gone, or AI offline."""
    form = await request.form()
    _take_page_id(form)
    errors: dict[str, str] = {}
    answers: list[int] = []
    values: dict[str, Any] = {}
    for i in range(1, 5):
        raw = _s(form, f"q{i}", 2)
        values[f"q{i}"] = raw
        if raw not in ("0", "1", "2", "3", "4"):
            errors[f"q{i}"] = f"Choose an answer for question {i}."
        else:
            answers.append(int(raw))
    allowed = dict(hs.SYMPTOMS)
    symptoms = list(dict.fromkeys(s for s in form.getlist("symptoms") if isinstance(s, str)))[:10]
    values["symptoms"] = [s for s in symptoms if s in allowed]
    if any(s not in allowed for s in symptoms):
        errors["symptoms"] = "Choose symptoms from the list."
    elif not symptoms:
        errors["symptoms"] = "Choose any symptoms you have - or “None of these”."
    elif "none" in symptoms and len(symptoms) > 1:
        errors["symptoms"] = "Choose “None of these” or specific symptoms - not both."
    # an emergency comes first: with a red-flag symptom the suspension and emergency guidance never wait for a selfie
    # read-out (up to a minute of AI time that could only nudge the stress level) - nor fail on a bad photo
    red_flag = "symptoms" not in errors and any(s in hs.RED_FLAG_SYMPTOMS for s in symptoms)
    photo: bytes | None = None
    upload = form.get("photo")
    selfie = upload is not None and not isinstance(upload, str) and bool(getattr(upload, "filename", None))
    if selfie and not red_flag:
        try:
            photo = await storage.run_image_decode(hs.normalize_photo, await storage.read_upload(upload), 1024)
        except storage.UploadError as exc:
            errors["photo"] = str(exc)
    if errors:
        ctx = await _wellness_context(db, user, form=values, errors=errors)
        return render(request, "customer/wellness/index.html", ctx, status_code=400)
    if not red_flag and (full := await hs.wellness_room(db, user.id)):  # the day's check-ins: before any AI time
        return redirect("/me/wellness", full, "warning")

    face: dict[str, Any] | None = None
    provider: str | None = None
    refusal: AIRejected | None = None
    if selfie and red_flag:
        face = {"status": "skipped"}  # the photo was never read
    elif photo is not None:
        # what the member told the app (conditions, medications) - the ONE member context, read before the commit
        member = await load_member_context(db, user)
        extra = {} if member.empty else {"profile": member.block(max_chars=PROFILE_PROMPT_CHARS, include_basics=False)}
        await db.commit()  # no transaction (nor pooled connection) is held while the selfie is read (up to a minute)
        try:
            analysis, provider = await wellness_ai.analyse_face(photo, minor=_is_minor(user), **extra)
            face = {"status": "ok", **analysis.model_dump(), "provider": provider}
        except AICancelled:
            face = {"status": "cancelled"}  # the page that asked is gone: nobody waits for the read-out
        except AIRejected as exc:
            if exc.code == "busy":  # the queue was full: the same questionnaire-only fallback as an offline AI
                face = {"status": "unavailable"}
            else:
                face, refusal = {"status": "refused", "code": exc.code}, exc
        except AIUnavailableError:
            face = {"status": "unavailable"}
        finally:
            photo = None  # analysed in memory only - never written anywhere
    # safety first: the check-in, red flags (suspension + emergency guidance) and alerts are recorded in every case
    try:
        outcome = await hs.record_wellness_check(db, user, answers=answers, symptoms=symptoms, face=face,
                                                 provider=provider)
    except caps.CapReached as exc:  # another tab saved the day's last check-in meanwhile (never with a red flag)
        await db.rollback()
        return redirect("/me/wellness", str(exc), "warning")
    await db.commit()
    if outcome.red_flags:
        message, category = "Seek emergency care now. Your gym activities are paused for your safety.", "error"
    elif refusal is not None:
        message, category = f"Check-in saved without the selfie read-out. {refusal.message}", "warning"
    elif face and face.get("status") == "unavailable":
        message, category = "Check-in saved. AI is unavailable, so your result is based on the questionnaire only.", "info"
    else:
        message, category = "Check-in saved.", "success"
    return redirect(f"/me/wellness/{outcome.check.id}", message, category)


@router.get("/me/wellness/{check_id}")
async def wellness_result(check_id: int, request: Request, db: DB, user: CustomerUser):
    check = (await db.execute(
        select(WellnessCheck).where(WellnessCheck.id == check_id, WellnessCheck.customer_id == user.id)
    )).scalars().first()
    if check is None:
        raise NotFound("Check-in not found.")
    history = list((await db.execute(
        select(WellnessCheck).where(WellnessCheck.customer_id == user.id).order_by(WellnessCheck.created_at.desc()).limit(6)
    )).scalars().all())
    options = dict(hs.PSS_OPTIONS)
    short_q = ("Felt unable to control important things", "Felt confident handling personal problems",
               "Felt things were going your way", "Felt difficulties were piling up")
    answers = [(short_q[i], options.get(int(a), str(a))) for i, a in enumerate((check.pss_answers or [])[:4])]
    symptom_labels = [hs.SYMPTOM_LABELS.get(s, s) for s in (check.symptoms or [])]
    red = [hs.SYMPTOM_LABELS[s].lower() for s in (check.symptoms or []) if s in hs.RED_FLAG_SYMPTOMS]
    recs = [r.replace(f": {hs.BREATHING_VIDEO}", "").strip() for r in (check.recommendations or "").split("\n") if r.strip()]
    score = check.pss_score or 0
    return render(request, "customer/wellness/result.html", {
        "ai_feature": True, "check": check, "history": history, "answers": answers, "symptom_labels": symptom_labels,
        "red_flags": red, "recommendations": recs, "breathing_video": hs.BREATHING_VIDEO, "minor": _is_minor(user),
        "base_level": hs.pss_level(score).value, "ring_dash": round(min(16, max(0, score)) / 16 * 326.73, 2),
        "provider_label": _provider_label((check.face_analysis or {}).get("provider")) if (check.face_analysis or {}).get("status") == "ok" else None,
    })
