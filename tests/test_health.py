"""WP-E1 tests: Today dashboard, daily metrics + anomaly rules, confirmation flow, safety suspension and
reactivation, AI escalation (and its limits), AC advice, bedtime maths + reminders, scale OCR, profile, N+1."""

from __future__ import annotations

import asyncio
import html
import json
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.core.utils import bmi, local_today
from app.db import QueryCounter
from app.models import (
    AuditLog,
    CustomerProfile,
    EmailOutbox,
    HealthAlert,
    HealthCondition,
    Job,
    MetricEntry,
    Notification,
    SuspensionEvent,
    Task,
    TrainerClient,
    User,
    WellnessCheck,
)
from app.models.enums import (
    AnomalyLevel,
    NotificationCategory,
    Role,
    ShiftMode,
    StressLevel,
    SubscriptionStatus,
    TaskKind,
)
from app.services import health as hs
from app.services.realtime import hub
from tests.ask_ai_helpers import pool_probe  # noqa: F401 - a fixture
from tests.conftest import login_as, make_client, post_form, run_jobs
from tests.factories import (
    make_customer,
    make_employee,
    make_gym,
    make_owner,
    make_plans,
    make_subscription,
    make_user,
    png_bytes,
)

W, C, LT, NONE = AnomalyLevel.WARNING, AnomalyLevel.CRITICAL, AnomalyLevel.LIFE_THREATENING, AnomalyLevel.NONE
T = date(2026, 11, 10)


@pytest.fixture(autouse=True)
def _offline_ai_by_default(ai_down):
    """Tests never touch real AI providers; tests that need AI install ``fake_ai`` on top."""
    yield


# ------------------------------------------------------------------ helpers
def ago(days: int) -> date:
    return T - timedelta(days=days)


def R(days_ago: int, height: float, weight: float) -> hs.Reading:
    return hs.Reading(ago(days_ago), height, weight)


async def add_entries(db, user: User, series: list[tuple[int, float, float]]) -> None:
    today = local_today(user.timezone)
    for days_ago, h, w in series:
        db.add(MetricEntry(customer_id=user.id, recorded_on=today - timedelta(days=days_ago), height_cm=h, weight_kg=w,
                           bmi=bmi(h, w), confirmed=True, anomaly_level=AnomalyLevel.NONE, anomaly_reasons=[]))
    await db.commit()


def years_ago(n: int) -> date:
    today = date.today()
    return today.replace(year=today.year - n, day=min(today.day, 28))


async def make_minor(db, *, guardian: bool = True, age: int = 15) -> User:
    minor = await make_customer(db, date_of_birth=years_ago(age))
    if guardian:
        prof = await db.get(CustomerProfile, minor.id)
        prof.guardian_name, prof.guardian_email, prof.guardian_phone = "Neha Gupta", "guardian@example.com", "+91 98450 12345"
        await db.commit()
    return minor


async def with_ai_plan(db, user: User) -> User:
    """Give ``user`` a membership whose plan includes AI: only those get the AI-written note of a normal reading."""
    plans = await make_plans(db, await make_gym(db, await make_owner(db)))
    await make_subscription(db, user, plans[6])  # online + AI
    return user


async def today_entry(db, user: User) -> MetricEntry | None:
    stmt = select(MetricEntry).where(MetricEntry.customer_id == user.id, MetricEntry.recorded_on == local_today(user.timezone))
    return (await db.execute(stmt.execution_options(populate_existing=True))).scalar_one_or_none()


async def profile_of(db, user: User) -> CustomerProfile:
    stmt = select(CustomerProfile).where(CustomerProfile.user_id == user.id)
    return (await db.execute(stmt.execution_options(populate_existing=True))).scalar_one()


def assessment(text="Looks steady.", consult=False, escalate=False, reason="") -> str:
    return json.dumps({"assessment": text, "consult_professional": consult, "escalate_to_suspension": escalate,
                       "reason": reason})


async def log_reading(client, h, w, nxt="/me/health"):
    return await post_form(client, "/me/health/metrics", {"height_cm": str(h), "weight_kg": str(w), "next": nxt})


async def confirm_from(client, response):
    loc = response.headers["location"]
    assert loc.startswith("/me/health/metrics/confirm?"), loc
    qs = parse_qs(urlsplit(loc).query)
    page = await client.get(loc)
    assert page.status_code == 200
    return await post_form(client, "/me/health/metrics/confirm", {"token": qs["token"][0], "next": qs["next"][0]})


# ================================================================== rule engine (table-driven, pure)
ADULT_CASES = [
    # (label, age, history, height, weight, expected codes, expected level)
    ("first reading, healthy", 30, [], 175, 70, [], NONE),
    ("height +2.0 cm is fine", 30, [R(3, 175, 70)], 177.0, 70, [], NONE),
    ("height +2.1 cm re-measure", 30, [R(3, 175, 70)], 177.1, 70, ["height_change"], W),
    ("height -5.0 cm still a warning", 30, [R(3, 175, 70)], 170.0, 70, ["height_change"], W),
    ("height +5.1 cm invalid", 30, [R(3, 175, 70)], 180.1, 70, ["height_invalid"], W),
    ("age 21 uses adult height rules", 21, [R(3, 175, 70)], 177.5, 70, ["height_change"], W),
    ("day-over-day 3.0% ok", 30, [R(1, 175, 100)], 175, 103.0, [], NONE),
    ("day-over-day 3.1% warns", 30, [R(1, 175, 100)], 175, 103.1, ["weight_day_change"], W),
    ("two days ago is not day-over-day", 30, [R(2, 175, 100)], 175, 104, [], NONE),
    ("7-day loss 5% warns", 30, [R(6, 175, 100)], 175, 95.0, ["weight_week_change"], W),
    ("7-day loss 4.9% ok", 30, [R(6, 175, 100)], 175, 95.1, [], NONE),
    ("7-day gain 5% warns", 30, [R(7, 175, 100)], 175, 105.0, ["weight_week_change"], W),
    ("8 days ago is outside the week", 30, [R(8, 175, 100)], 175, 95.0, [], NONE),
    ("30-day loss 10% critical", 30, [R(20, 175, 100)], 175, 90.0, ["weight_loss_30d"], C),
    ("30-day loss 9.9% ok", 30, [R(20, 175, 100)], 175, 90.1, [], NONE),
    ("31 days ago is ignored", 30, [R(31, 175, 100)], 175, 85.0, [], NONE),
    ("3-day loss 5% critical", 30, [R(3, 175, 100)], 175, 95.0, ["weight_loss_3d", "weight_week_change"], C),
    ("2-day loss 4.9% ok", 30, [R(2, 175, 100)], 175, 95.1, [], NONE),
    ("4-day loss 5% only weekly", 30, [R(4, 175, 100)], 175, 95.0, ["weight_week_change"], W),
    ("BMI 16.0 ok", 30, [], 175, 49.0, [], NONE),
    ("BMI 15.9 critical", 30, [], 175, 48.7, ["bmi_low"], C),
    ("BMI 15.0 critical", 30, [], 175, 45.9, ["bmi_low"], C),
    ("BMI 14.9 life-threatening", 30, [], 175, 45.6, ["bmi_very_low"], LT),
    ("BMI 39.9 ok", 30, [], 175, 122.2, [], NONE),
    ("BMI 40.0 warning", 30, [], 175, 122.5, ["bmi_high"], W),
    ("BMI 49.9 warning", 30, [], 175, 152.9, ["bmi_high"], W),
    ("BMI 50.0 critical", 30, [], 175, 153.2, ["bmi_very_high"], C),
    ("unknown age = adult rules", None, [], 175, 45.6, ["bmi_very_low"], LT),
    ("adult 18 uses adult BMI", 18, [], 175, 45.6, ["bmi_very_low"], LT),
]

MINOR_CASES = [
    ("minor day-over-day 2.0% ok", 15, [R(1, 160, 50)], 160, 51.0, [], NONE),
    ("minor day-over-day 2.2% warns", 15, [R(1, 160, 50)], 160, 51.1, ["weight_day_change"], W),
    ("minor 30-day loss 7% critical", 15, [R(20, 160, 60)], 160, 55.8, ["weight_loss_30d"], C),
    ("minor 30-day loss 6.8% ok", 15, [R(20, 160, 60)], 160, 55.9, [], NONE),
    ("minor growth +3.0 cm in a week ok", 15, [R(5, 160, 50)], 163.0, 50, [], NONE),
    ("minor growth +3.1 cm in a week", 15, [R(5, 160, 50)], 163.1, 50, ["growth_jump"], W),
    ("minor growth 10 days apart ok", 15, [R(10, 160, 50)], 165.0, 50, [], NONE),
    ("minor height -2.0 cm ok", 15, [R(5, 160, 50)], 158.0, 50, [], NONE),
    ("minor height -2.1 cm warns", 15, [R(5, 160, 50)], 157.9, 50, ["height_loss"], W),
    ("age 20 uses growing height rules", 20, [R(5, 175, 70)], 178.0, 70, [], NONE),
    ("minor BMI 15.5 ok", 15, [], 160, 39.7, [], NONE),
    ("minor BMI 15.4 critical", 15, [], 160, 39.4, ["bmi_low"], C),
    ("minor BMI 14.0 critical", 15, [], 160, 35.8, ["bmi_low"], C),
    ("minor BMI 13.9 life-threatening", 15, [], 160, 35.6, ["bmi_very_low"], LT),
    ("age 12 uses teen BMI rules", 12, [], 150, 31.0, ["bmi_very_low"], LT),
    ("age 11 has no BMI rules", 11, [], 150, 31.0, [], NONE),
]


@pytest.mark.parametrize("label,age,history,height,weight,codes,level", ADULT_CASES + MINOR_CASES,
                         ids=[c[0] for c in ADULT_CASES + MINOR_CASES])
def test_anomaly_rules(label, age, history, height, weight, codes, level):
    ev = hs.evaluate_metrics(age, history, height, weight, today=T)
    assert sorted(ev.codes) == sorted(codes), label
    assert ev.level == level, label


def test_invalid_adult_height_is_ignored_for_bmi_and_needs_no_confirmation():
    ev = hs.evaluate_metrics(35, [R(2, 175, 70), R(5, 176, 70), R(9, 175, 70)], 150, 70, today=T)
    assert ev.height_invalid and ev.effective_height_cm == 175
    assert ev.bmi == bmi(175, 70)  # a typo in height can't trigger a dangerous-BMI suspension
    assert not ev.needs_confirmation


def test_reference_height_is_used_without_history_and_confirmation_rules():
    ev = hs.evaluate_metrics(30, [], 172.5, 70, today=T, reference_height=175)
    assert ev.codes == ["height_change"] and ev.needs_confirmation
    assert hs.evaluate_metrics(30, [R(1, 175, 100)], 175, 104, today=T).needs_confirmation  # day-over-day asks
    assert not hs.evaluate_metrics(30, [R(6, 175, 100)], 175, 95, today=T).needs_confirmation  # weekly warning only
    assert hs.evaluate_metrics(30, [], 175, 48.7, today=T).needs_confirmation  # CRITICAL always asks


@pytest.mark.parametrize("h,w,ok", [
    ("50", "20", True), ("250", "120", True), ("100", "20", True), ("250", "300", True), ("49.9", "70", False),
    ("250.1", "70", False), ("175", "19.9", False), ("175", "300.1", False), ("abc", "70", False), ("", "70", False),
    ("175", "", False), ("1e3", "70", False), ("nan", "70", False), ("172,5", "70", True), ("175", "72,5", True),
    ("240", "21", False),  # BMI 3.6: no living person
])
def test_metric_validation_ranges(h, w, ok):
    height, weight, errors = hs.validate_metrics(h, w)
    assert (not errors) is ok, errors
    if not ok and (h and h not in ("abc",)):
        assert any("doesn't look right" in e or "Enter your" in e for e in errors.values())


# ================================================================== pages, prefill and permissions
async def test_pages_render_and_forms_are_prefilled_with_last_values(client, db):
    user = await make_customer(db, full_name="Ishaan Verma")
    await add_entries(db, user, [(3, 176.0, 70.6), (2, 176.0, 70.4), (1, 176.0, 70.2)])
    await login_as(client, user)
    for url in ("/me", "/me/health"):
        r = await client.get(url)
        assert r.status_code == 200, url
        assert 'name="height_cm"' in r.text and 'value="176"' in r.text and 'value="70.2"' in r.text, url
        assert "csrf_token" in r.text
    dash = (await client.get("/me")).text
    assert "Ishaan" in dash and "Bedtime" in dash and "22:30" in dash and "Find your gym" in dash
    assert 'data-component="bedtime"' in dash and "/gyms" in dash
    health = (await client.get("/me/health")).text
    assert "Weight trend" in health and 'data-component="trend-chart"' in health
    assert (await client.get("/me/health/profile")).status_code == 200
    assert (await client.get("/me/wellness")).status_code == 200


async def test_profile_height_prefills_when_there_is_no_history(client, db):
    user = await make_customer(db)
    await login_as(client, user)
    r = await client.get("/me/health")
    assert 'value="175"' in r.text and 'value="72"' in r.text and "No readings yet" in r.text


async def test_dashboard_shows_subscription_tasks_and_alerts(client, db):
    owner = await make_owner(db)
    gym = await make_gym(db, owner, name="Forge Test Gym")
    plans = await make_plans(db, gym)
    user = await make_customer(db)
    await make_subscription(db, user, plans[7])  # online + trainer + AI
    db.add(Task(user_id=user.id, title="Mobility flow", kind=TaskKind.WORKOUT, due_at=datetime.now(UTC) + timedelta(hours=2)))
    db.add(HealthAlert(customer_id=user.id, level=W, title="Quick weight drop noticed", message="Check again.", source="rule"))
    await db.commit()
    await login_as(client, user)
    text = (await client.get("/me")).text
    assert "Forge Test Gym" in text and "AI coach" in text and "Trainer" in text and "Find your gym" not in text
    assert "Mobility flow" in text and "Quick weight drop noticed" in text


async def test_role_routing_and_auth(client, db, anon_client_factory):
    owner = await make_owner(db)
    await login_as(client, owner)
    r = await client.get("/me")
    assert r.status_code == 303 and r.headers["location"] == "/owner"
    assert (await client.get("/me/health")).status_code == 403
    assert (await client.get("/me/wellness")).status_code == 403
    anon = anon_client_factory("203.0.113.40")
    r = await anon.get("/me")
    assert r.status_code == 303 and r.headers["location"].startswith("/auth/login")


# ================================================================== logging metrics
async def test_normal_reading_saves_updates_profile_and_redirects(client, db):
    user = await make_customer(db)
    await add_entries(db, user, [(1, 175, 72)])
    await login_as(client, user)
    r = await log_reading(client, 175.2, 71.8, nxt="/me")
    assert r.status_code == 303 and r.headers["location"] == "/me"
    entry = await today_entry(db, user)
    assert entry.confirmed and entry.anomaly_level == NONE and entry.bmi == bmi(175.2, 71.8)
    assert entry.ai_note == hs.deterministic_note(NONE, False)  # AI offline -> deterministic note
    prof = await profile_of(db, user)
    assert prof.height_cm == 175.2 and prof.weight_kg == 71.8
    # logging again the same day updates the same row (one entry per customer + local date)
    r = await log_reading(client, 175.2, 71.6)
    assert r.status_code == 303
    assert await db.scalar(select(func.count(MetricEntry.id)).where(MetricEntry.customer_id == user.id)) == 2


@pytest.mark.parametrize("h,w,nxt,template_marker", [
    ("49", "70", "/me/health", "Weight trend"), ("175", "301", "/me", "Up next"), ("abc", "70", "/me", "Up next"),
])
async def test_out_of_range_readings_rerender_with_errors(client, db, h, w, nxt, template_marker):
    user = await make_customer(db)
    await login_as(client, user)
    r = await post_form(client, "/me/health/metrics", {"height_cm": h, "weight_kg": w, "next": nxt})
    assert r.status_code == 400
    text = html.unescape(r.text)
    assert template_marker in text and 'aria-invalid="true"' in text
    assert "This doesn't look right" in text or "Enter your" in text
    assert await db.scalar(select(func.count(MetricEntry.id))) == 0


async def test_warning_reading_needs_confirmation_and_is_logged_meanwhile(client, db):
    user = await make_customer(db)
    await add_entries(db, user, [(1, 175, 70)])
    await login_as(client, user)
    r = await log_reading(client, 175, 72.5)  # +3.6 % since yesterday
    assert r.status_code == 303
    entry = await today_entry(db, user)
    assert entry is not None and entry.confirmed is False and entry.anomaly_level == W
    assert "weight_day_change" in hs.reason_codes(entry.anomaly_reasons)
    alert = (await db.execute(select(HealthAlert).where(HealthAlert.metric_entry_id == entry.id))).scalar_one()
    assert alert.level == W and alert.source == "rule"
    prof = await profile_of(db, user)
    assert prof.weight_kg == 72.0  # unconfirmed values don't overwrite the profile yet
    page = await client.get(r.headers["location"])
    assert "double-check" in html.unescape(page.text) and "Big change since yesterday" in page.text
    done = await confirm_from(client, r)
    assert done.status_code == 303 and done.headers["location"] == "/me/health"
    entry = await today_entry(db, user)
    assert entry.confirmed is True
    assert (await profile_of(db, user)).weight_kg == 72.5
    # a second confirmation with the same token is a no-op
    token = parse_qs(urlsplit(r.headers["location"]).query)["token"][0]
    again = await post_form(client, "/me/health/metrics/confirm", {"token": token})
    assert again.status_code == 303
    assert (await client.get(r.headers["location"])).status_code == 303  # page redirects once confirmed


async def test_confirmation_token_is_bound_to_user_and_values(client, db):
    user = await make_customer(db)
    await add_entries(db, user, [(1, 175, 70)])
    await login_as(client, user)
    r = await log_reading(client, 175, 75)
    token = parse_qs(urlsplit(r.headers["location"]).query)["token"][0]
    other = await make_customer(db)
    async with make_client() as c2:
        await login_as(c2, other)
        bad = await post_form(c2, "/me/health/metrics/confirm", {"token": token})
        assert bad.status_code == 303 and bad.headers["location"] == "/me/health"
        assert (await c2.get(r.headers["location"])).status_code == 303
    forged = await post_form(client, "/me/health/metrics/confirm", {"token": token[:-3] + "abc"})
    assert forged.status_code == 303 and forged.headers["location"] == "/me/health"
    # values changed after the token was issued -> the old token no longer matches
    await log_reading(client, 175, 76)
    stale = await post_form(client, "/me/health/metrics/confirm", {"token": token})
    assert stale.status_code == 303
    assert (await today_entry(db, user)).confirmed is False


def test_confirmation_token_expires():
    class E:
        id, recorded_on, height_cm, weight_kg = 5, T, 175.0, 70.0

    tok = hs.make_confirm_token(9, E())
    assert hs.read_confirm_token(tok, 9) is not None
    assert hs.read_confirm_token(tok, 10) is None
    assert hs.read_confirm_token(tok, 9, max_age=-1) is None


async def test_critical_reading_alerts_and_notifies_minor_guardian(client, db):
    minor = await make_minor(db)
    await add_entries(db, minor, [(20, 160, 60)])
    await login_as(client, minor)
    r = await log_reading(client, 160, 55.5)  # 7.5 % loss within 30 days -> CRITICAL for a minor
    done = await confirm_from(client, r)
    assert done.status_code == 303
    entry = await today_entry(db, minor)
    assert entry.anomaly_level == C and entry.confirmed
    assert not (await profile_of(db, minor)).activity_suspended  # CRITICAL never suspends on its own
    notes = (await db.execute(select(Notification).where(Notification.user_id == minor.id))).scalars().all()
    assert any(n.category == NotificationCategory.HEALTH and "weight loss" in n.title.lower() for n in notes)
    mails = (await db.execute(select(EmailOutbox))).scalars().all()
    assert any(m.to_email == "guardian@example.com" for m in mails)  # explicit guardian e-mail
    assert any("guardian@example.com" in (m.cc or []) for m in mails)  # + CC of the minor's notification
    alert = (await db.execute(select(HealthAlert).where(HealthAlert.metric_entry_id == entry.id))).scalar_one()
    assert alert.guardian_notified


# ================================================================== life-threatening -> suspension
async def _gym_with_member(db, member: User):
    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    plans = await make_plans(db, gym)
    sub = await make_subscription(db, member, plans[5])  # online + trainer
    emp = await make_employee(db, gym)
    db.add(TrainerClient(employee_id=emp.id, customer_id=member.id, subscription_id=sub.id, mode=ShiftMode.ONLINE))
    await db.commit()
    return owner, gym, emp


async def test_confirmed_life_threatening_reading_suspends_and_notifies_everyone(client, db):
    minor = await make_minor(db)
    owner, gym, trainer = await _gym_with_member(db, minor)
    await login_as(client, minor)
    r = await log_reading(client, 160, 35.0)  # BMI 13.7 for a 15-year-old
    assert (await today_entry(db, minor)).anomaly_level == LT
    assert not (await profile_of(db, minor)).activity_suspended  # nothing happens before confirmation
    done = await confirm_from(client, r)
    assert done.status_code == 303
    prof = await profile_of(db, minor)
    assert prof.activity_suspended and prof.suspended_by == "rule" and prof.suspension_reason
    event = (await db.execute(select(SuspensionEvent).where(SuspensionEvent.customer_id == minor.id))).scalar_one()
    assert event.action == "suspend" and event.actor == "rule" and event.health_alert_id is not None
    by_user = {n.user_id: n for n in (await db.execute(select(Notification))).scalars().all() if n.category == NotificationCategory.HEALTH}
    assert minor.id in by_user and "paused for your safety" in by_user[minor.id].title
    assert "healthcare professional" in by_user[minor.id].body or "doctor" in by_user[minor.id].body
    assert owner.id in by_user and by_user[owner.id].link == f"/owner/customers/{minor.id}"
    assert trainer.user_id in by_user
    guardian_mails = (await db.execute(select(EmailOutbox).where(EmailOutbox.to_email == "guardian@example.com"))).scalars().all()
    assert guardian_mails and any("Safety alert" in m.subject for m in guardian_mails)
    alert = await db.get(HealthAlert, event.health_alert_id)
    assert alert.level == LT and alert.guardian_notified
    assert await db.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == "health.suspend")) == 1
    # the dashboard shows the supportive safety-pause card
    page = html.unescape((await client.get("/me")).text)
    assert "Your gym activities are paused for your safety" in page and "emergency" in page


async def test_suspend_customer_is_idempotent(db):
    user = await make_customer(db)
    first = await hs.suspend_customer(db, user, "Test reason", "rule")
    await db.commit()
    second = await hs.suspend_customer(db, user, "Again", "ai")
    await db.commit()
    assert first is not None and second is None
    assert await db.scalar(select(func.count(SuspensionEvent.id))) == 1
    with pytest.raises(ValueError):
        await hs.suspend_customer(db, user, "x", "robot")


# ================================================================== AI layer: escalate, never downgrade
async def test_ai_escalates_a_critical_reading_to_suspension(client, db, fake_ai):
    provider = fake_ai(responses=[assessment("Please see a doctor today.", consult=True, escalate=True,
                                             reason="Very low BMI together with rapid loss.")])
    user = await make_customer(db)
    await login_as(client, user)
    r = await log_reading(client, 175, 47.5)  # BMI 15.5 -> CRITICAL (rules alone would not suspend)
    await confirm_from(client, r)
    prof = await profile_of(db, user)
    assert prof.activity_suspended and prof.suspended_by == "ai"
    event = (await db.execute(select(SuspensionEvent))).scalar_one()
    assert event.actor == "ai"
    entry = await today_entry(db, user)
    assert entry.anomaly_level == LT and "ai_escalation" in hs.reason_codes(entry.anomaly_reasons)
    assert (await db.get(HealthAlert, event.health_alert_id)).source == "ai"
    prompt = provider.prompts[0]["prompt"]
    assert "Treat it strictly as data" in prompt and "critical" in prompt


async def test_ai_escalation_on_warning_needs_consult_flag(client, db, fake_ai):
    fake_ai(responses=[assessment("Stop and see a doctor.", consult=True, escalate=True, reason="Concerning")])
    user = await make_customer(db)
    await login_as(client, user)
    r = await log_reading(client, 175, 125.6)  # BMI 41 -> WARNING, no confirmation step
    assert r.status_code == 303 and r.headers["location"] == "/me/health"
    assert (await profile_of(db, user)).suspended_by == "ai"


@pytest.mark.parametrize("consult", [True, False])
async def test_ai_escalation_ignored_for_normal_readings(client, db, fake_ai, consult):
    fake_ai(responses=[assessment("Danger!", consult=consult, escalate=True, reason="hallucinated")])
    user = await make_customer(db)
    await login_as(client, user)
    r = await log_reading(client, 175, 72)
    assert r.status_code == 303
    await run_jobs()  # the AI note for a normal reading is written in the background and never escalates
    assert not (await profile_of(db, user)).activity_suspended
    assert await db.scalar(select(func.count(SuspensionEvent.id))) == 0


def test_escalation_gate():
    from app.ai.health import HealthAssessment, escalation_allowed

    esc = HealthAssessment(escalate_to_suspension=True, consult_professional=False)
    esc_consult = HealthAssessment(escalate_to_suspension=True, consult_professional=True)
    assert escalation_allowed(C, esc) and escalation_allowed(LT, esc)
    assert not escalation_allowed(W, esc) and escalation_allowed(W, esc_consult)
    assert not escalation_allowed(NONE, esc_consult) and not escalation_allowed(C, None)
    assert not escalation_allowed(C, HealthAssessment(escalate_to_suspension=False, consult_professional=True))


async def test_ai_cannot_downgrade_a_life_threatening_rule_decision(client, db, fake_ai):
    fake_ai(responses=[assessment("All good, keep training hard!", consult=False, escalate=False)])
    user = await make_customer(db)
    await login_as(client, user)
    r = await log_reading(client, 175, 44.4)  # BMI 14.5 -> LIFE_THREATENING
    await confirm_from(client, r)
    prof = await profile_of(db, user)
    assert prof.activity_suspended and prof.suspended_by == "rule"
    entry = await today_entry(db, user)
    assert entry.anomaly_level == LT
    assert "All good" not in (entry.ai_note or "") and "healthcare professional" in entry.ai_note


async def test_ai_note_is_stored_when_available(client, db, fake_ai):
    provider = fake_ai(responses=[assessment("Nice and steady - same time tomorrow?")])
    user = await with_ai_plan(db, await make_customer(db))  # AI-written notes come with a plan that includes AI
    await add_entries(db, user, [(1, 175, 72)])
    await login_as(client, user)
    r = await log_reading(client, 175, 72.1)
    assert r.status_code == 303 and provider.prompts == []  # saving never waits for the AI on a normal reading
    assert (await today_entry(db, user)).ai_note == hs.deterministic_note(NONE, False)
    assert await run_jobs() == 1
    assert (await today_entry(db, user)).ai_note == "Nice and steady - same time tomorrow?"
    assert provider.prompts and "72.1" in provider.prompts[0]["prompt"]
    assert "Nice and steady" in (await client.get("/me/health")).text


async def test_the_background_ai_note_is_announced_without_an_island_note(client, db, fake_ai):
    fake_ai(responses=[assessment("Nice and steady - same time tomorrow?")])
    user = await with_ai_plan(db, await make_customer(db))
    await add_entries(db, user, [(1, 175, 72)])
    await login_as(client, user)
    await log_reading(client, 175, 72.1)
    job = (await db.execute(select(Job).where(Job.kind == "health_ai_note"))).scalar_one()
    q = hub.subscribe(user.id)
    try:
        assert await run_jobs() == 1
        for _ in range(30):
            await asyncio.sleep(0)
        events = []
        while not q.empty():
            evt = q.get_nowait()
            if isinstance(evt, dict) and evt.get("type") == "task.update":
                events.append(evt)
    finally:
        hub.unsubscribe(user.id, q)
    # the note shows on the reading itself: the browser tracks the job, but no empty "Your health note - done" note
    assert [(e["task_id"], e["kind"], e["status"], e["notify"]) for e in events] == [(job.id, "health_ai_note", "done",
                                                                                      False)]
    assert (await today_entry(db, user)).ai_note == "Nice and steady - same time tomorrow?"


async def test_minor_prompt_asks_for_age_appropriate_language(client, db, fake_ai):
    provider = fake_ai(responses=[assessment("Great job logging!")])
    minor = await with_ai_plan(db, await make_minor(db))
    await add_entries(db, minor, [(1, 160, 50)])
    await login_as(client, minor)
    await log_reading(client, 160, 50)
    await run_jobs()
    assert "minor" in provider.prompts[0]["system"].lower() and "guardian" in provider.prompts[0]["system"].lower()


# ================================================================== reactivation
async def test_reactivation_permissions_and_effects(db):
    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    plans = await make_plans(db, gym)
    member = await make_customer(db)
    sub = await make_subscription(db, member, plans[0])
    other_owner = await make_owner(db)
    await make_gym(db, other_owner)
    staff = await make_user(db, role=Role.STAFF)
    admin = await make_user(db, role=Role.SUPERADMIN, with_profile=False)
    await hs.suspend_customer(db, member, "BMI in a dangerous range", "rule")
    await db.commit()

    for actor in (other_owner, staff, member, None):
        with pytest.raises(PermissionError):
            await hs.reactivate_customer(db, member, actor, "Doctor cleared")
    with pytest.raises(ValueError):
        await hs.reactivate_customer(db, member, owner, "  ")
    assert await hs.can_reactivate(db, member, owner) and not await hs.can_reactivate(db, member, other_owner)

    event = await hs.reactivate_customer(db, member, owner, "Doctor's clearance letter received")
    await db.commit()
    prof = await profile_of(db, member)
    assert not prof.activity_suspended and prof.suspension_reason is None
    assert event.action == "reactivate" and event.actor == "owner" and event.actor_user_id == owner.id
    assert await db.scalar(select(func.count(Notification.id)).where(
        Notification.user_id == member.id, Notification.title.like("%active again%"))) == 1
    log_row = (await db.execute(select(AuditLog).where(AuditLog.action == "health.reactivate"))).scalar_one()
    assert log_row.gym_id == gym.id and log_row.actor_id == owner.id
    with pytest.raises(ValueError):  # not suspended any more
        await hs.reactivate_customer(db, member, owner, "Doctor cleared again")

    # a past (expired) membership still lets that gym's admin reactivate; the super-admin always can
    sub.status = SubscriptionStatus.EXPIRED
    await db.commit()
    await hs.suspend_customer(db, member, "Second episode", "ai")
    await db.commit()
    assert await hs.can_reactivate(db, member, owner)
    await hs.reactivate_customer(db, member, admin, "Reviewed by the platform team")
    await db.commit()
    assert not (await profile_of(db, member)).activity_suspended
    # a pending (never paid) subscription is not enough
    pending_owner = await make_owner(db)
    pending_gym = await make_gym(db, pending_owner)
    pplans = await make_plans(db, pending_gym)
    await make_subscription(db, member, pplans[0], status=SubscriptionStatus.PENDING_PAYMENT)
    assert not await hs.can_reactivate(db, member, pending_owner)


# ================================================================== AC advice
@pytest.mark.parametrize("tags,age,verdict,lo,hi", [
    ([], 30, "recommended", 23, 26),
    (["hypertension"], 30, "recommended", 22, 26),
    (["cardiac"], 30, "recommended", 22, 26),
    (["heat_intolerance"], 30, "recommended", 22, 26),
    (["obesity"], 30, "recommended", 22, 26),
    (["pregnancy"], 30, "recommended", 22, 26),
    (["diabetes"], 30, "recommended", 22, 26),
    (["multiple_sclerosis"], 30, "recommended", 22, 26),
    ([], 60, "recommended", 22, 26),
    ([], 59, "recommended", 23, 26),
    (["asthma"], 30, "caution", 24, 26),
    (["copd"], 30, "caution", 24, 26),
    (["cold_sensitivity"], 30, "not_recommended", None, None),
    (["respiratory_allergy"], 30, "recommended", 23, 26),
    (["arthritis"], 30, "caution", 24, 27),
    (["hypertension", "asthma"], 30, "caution", 24, 26),
    (["arthritis", "diabetes"], 30, "caution", 24, 26),
    (["cold_sensitivity", "hypertension"], 70, "not_recommended", None, None),
    (["knee_injury"], 30, "recommended", 23, 26),
])
def test_ac_rules(tags, age, verdict, lo, hi):
    rec = hs.ac_recommendation(tags, age)
    assert (rec["verdict"], rec["temp_min"], rec["temp_max"]) == (verdict, lo, hi)
    assert rec["summary"] and rec["reasons"] and json.dumps(rec)  # JSON-safe for the JSONB cache


def test_ac_tips_for_respiratory_conditions():
    tips = " ".join(hs.ac_recommendation(["asthma"], 30)["tips"]).lower()
    assert "cold drafts" in tips and "filters" in tips and "40% and 60%" in tips
    assert "filters" in " ".join(hs.ac_recommendation(["respiratory_allergy"], 30)["tips"]).lower()
    assert "fan" in hs.ac_recommendation(["cold_sensitivity"], 30)["summary"]


async def test_ac_is_cached_and_recomputed_when_conditions_change(client, db, fake_ai):
    owner = await make_owner(db)
    plans = await make_plans(db, await make_gym(db, owner))
    user = await make_customer(db)
    await make_subscription(db, user, plans[6])  # online + AI
    provider = fake_ai(handler=lambda **k: json.dumps({"summary": "Keep the room at 24 to 26 °C, no cold drafts, clean filters."}))
    await login_as(client, user)
    await client.get("/me/health")
    cached = (await profile_of(db, user)).ac_recommendation
    assert cached["verdict"] == "recommended" and cached["tags"] == []
    r = await post_form(client, "/me/health/profile/conditions", {"kind": "condition", "name": "Asthma since childhood"})
    assert r.status_code == 303
    cond = (await db.execute(select(HealthCondition).where(HealthCondition.customer_id == user.id))).scalar_one()
    assert cond.tag == "asthma"
    prof = await profile_of(db, user)
    assert prof.ac_recommendation["verdict"] == "caution" and prof.ac_recommendation["tags"] == ["asthma"]
    # the rule-based advice is stored at once; the AI wording is the health_ac_note job - no request waits for it
    assert prof.ac_recommendation["ai_summary"] is None and provider.prompts == []
    assert await run_jobs() == 1
    prof = await profile_of(db, user)
    assert prof.ac_recommendation["verdict"] == "caution" and prof.ac_recommendation["tags"] == ["asthma"]
    assert prof.ac_recommendation["ai_summary"].startswith("Keep the room at 24 to 26")
    assert prof.health_embedding is not None and len(prof.health_embedding) == 384
    assert any("air-conditioning" in p["system"] for p in provider.prompts)
    text = (await client.get("/me/health/profile")).text
    assert "Asthma since childhood" in text and "Worded by AI" in text


async def test_ac_ai_wording_rejected_when_numbers_change(db, fake_ai):
    provider = fake_ai(handler=lambda **k: json.dumps({"summary": "Set it to a chilly 18 °C."}))
    owner = await make_owner(db)
    plans = await make_plans(db, await make_gym(db, owner))
    user = await make_customer(db, conditions=["Asthma since childhood"])
    await make_subscription(db, user, plans[6])  # online + AI
    prof = await db.get(CustomerProfile, user.id)
    rec = await hs.refresh_ac(db, user, prof, ["asthma"])  # rules only, stored at once - never an inline AI call
    await db.commit()
    assert rec["ai_summary"] is None and rec["temp_min"] == 24 and provider.prompts == []
    await hs.ac_note_job(db, {"user_id": user.id})  # the background wording: the model changed the numbers
    stored = (await profile_of(db, user)).ac_recommendation
    assert len(provider.prompts) == 1 and stored["ai_summary"] is None and stored["temp_min"] == 24


# ================================================================== bedtime
@pytest.mark.parametrize("age,wake,bed", [
    (30, "06:30", "22:30"), (8, "07:00", "20:30"), (12, "07:00", "20:30"), (13, "07:00", "22:00"), (17, "06:00", "21:00"),
    (18, "06:00", "22:00"), (64, "06:00", "22:00"), (65, "06:00", "22:30"), (80, "05:00", "21:30"),
    (None, "06:30", "22:30"), (30, "08:00", "00:00"), (30, "05:00", "21:00"), (30, "09:15", "01:15"), (4, "07:00", "19:30"),
])
def test_bedtime_for(age, wake, bed):
    plan = hs.bedtime_for(age, time.fromisoformat(wake))
    assert plan.bedtime.strftime("%H:%M") == bed
    reminder = (datetime.combine(T, plan.bedtime) - timedelta(minutes=30)).strftime("%H:%M")
    assert plan.reminder_at.strftime("%H:%M") == reminder


def test_reminder_window_crossing_midnight_fires_once():
    plan = hs.bedtime_for(30, time(8, 15))  # bedtime 00:15, reminder 23:45
    tz = "Asia/Kolkata"

    def at(day: date, hh: int, mm: int) -> datetime:
        from zoneinfo import ZoneInfo

        return datetime.combine(day, time(hh, mm), tzinfo=ZoneInfo(tz))

    key = hs.reminder_key(plan, tz, at(T, 23, 50), None)
    assert key == T
    assert hs.reminder_key(plan, tz, at(T + timedelta(days=1), 0, 5), key) is None  # same window, already sent
    assert hs.reminder_key(plan, tz, at(T + timedelta(days=1), 0, 5), None) == T  # catch-up inside the window
    assert hs.reminder_key(plan, tz, at(T + timedelta(days=1), 0, 16), None) is None  # window over
    assert hs.reminder_key(plan, tz, at(T, 23, 40), None) is None  # too early


async def test_bedtime_reminders_send_once_per_local_day(db):
    user = await make_customer(db)  # Asia/Kolkata, wake 06:30 -> bedtime 22:30, reminder 22:00 IST (16:30 UTC)
    quiet = await make_customer(db)
    prof = await db.get(CustomerProfile, quiet.id)
    prof.sleep_reminders_enabled = False
    await db.commit()
    t0 = datetime(2026, 11, 10, 16, 35, tzinfo=UTC)  # 22:05 IST
    assert await hs.bedtime_reminders(db, now=t0) == 1
    await db.commit()
    assert await hs.bedtime_reminders(db, now=t0 + timedelta(minutes=10)) == 0  # same evening
    await db.commit()
    assert await hs.bedtime_reminders(db, now=t0 + timedelta(days=1) - timedelta(minutes=20)) == 0  # 21:45 next day
    assert await hs.bedtime_reminders(db, now=t0 + timedelta(days=1, minutes=20)) == 1  # 22:25 next day (catch-up)
    await db.commit()
    notes = (await db.execute(select(Notification).where(Notification.user_id == user.id))).scalars().all()
    assert len(notes) == 2 and all(n.category == NotificationCategory.REMINDER for n in notes)
    assert "shut this down and sleep, recovery matters" in notes[0].body
    assert (await profile_of(db, user)).last_bedtime_reminder_on == date(2026, 11, 11)
    assert await db.scalar(select(func.count(Notification.id)).where(Notification.user_id == quiet.id)) == 0


async def test_bedtime_candidates_loaded_with_one_query(db):
    for _ in range(4):
        await make_customer(db)
    outside = datetime(2026, 11, 10, 6, 0, tzinfo=UTC)  # 11:30 IST: nobody is due
    with QueryCounter() as qc:
        assert await hs.bedtime_reminders(db, now=outside) == 0
    assert qc.count == 1


def test_sleep_window_states():
    plan = hs.bedtime_for(30, time(6, 30))
    tz = "UTC"
    awake = hs.sleep_window(plan, tz, datetime(2026, 11, 10, 18, 0, tzinfo=UTC))
    assert awake["state"] == "awake" and awake["label"] == "in 4 h 30 min" and 0 < awake["progress"] < 100
    assert hs.sleep_window(plan, tz, datetime(2026, 11, 10, 22, 10, tzinfo=UTC))["state"] == "wind-down"
    night = hs.sleep_window(plan, tz, datetime(2026, 11, 11, 2, 0, tzinfo=UTC))
    assert night["state"] == "sleep" and night["wake_at"] == datetime(2026, 11, 11, 6, 30, tzinfo=UTC)


# ================================================================== scale / report OCR
@pytest.mark.parametrize("text,weight,height", [
    ("72.4 kg", 72.4, None), ("72,4kg", 72.4, None), ("WEIGHT: 159.6 lb", 72.4, None), ("180 lbs", 81.6, None),
    ("Weight (lbs): 180", 81.6, None), ("Height: 5'9\"", None, 175.3), ("5 ft 9 in", None, 175.3),
    ("Ht 175 cm", None, 175.0), ("1.75 m", None, 175.0), ("69 in", None, 175.3),
    ("Height 175 cm Weight 72.4 kg BMI 23.6", 72.4, 175.0), ("Ht (cm): 168.5\nWt (kg): 61.2", 61.2, 168.5),
    ("BMI 23.6\n72.4", 72.4, None), ("Body fat 18.5%\n80.2", 80.2, None), ("hello world", None, None),
    ("Weight 5 kg", None, None), ("Weight 400 kg", None, None), ("Height: 6'2\" Weight: 200 lb", 90.7, 188.0),
])
def test_parse_measurements(text, weight, height):
    m = hs.parse_measurements(text)
    assert (m.weight_kg, m.height_cm) == (weight, height)


def test_cross_check_prefers_agreement():
    per = {"tesseract": hs.Measurement(weight_kg=78.1), "apple_vision": hs.Measurement(weight_kg=72.4),
           "rapidocr": hs.Measurement(weight_kg=72.5), "vision_model": hs.Measurement(height_cm=175.0)}
    res = hs.cross_check(per)
    assert res.weight_kg == 72.5 and res.weight_engines == ["apple_vision", "rapidocr"]
    assert res.height_cm == 175.0 and res.height_engines == ["vision_model"] and res.vision_used
    tie = hs.cross_check({"tesseract": hs.Measurement(weight_kg=70.0), "apple_vision": hs.Measurement(weight_kg=75.0)})
    assert tie.weight_kg == 75.0  # tie -> the more trusted engine


def _upload_files() -> int:
    root = get_settings().upload_dir
    return sum(1 for p in root.rglob("*") if p.is_file()) if root.exists() else 0


def _fake_ocr(monkeypatch, texts: dict[str, str]):
    from app.services import ocr

    seen = []

    async def run_ocr(image, *, engines=None, purpose="document"):
        seen.append((len(image), purpose))
        return ocr.MultiOcrResult(results=[ocr.OcrEngineResult(e, True, t) for e, t in texts.items()]
                                  + [ocr.OcrEngineResult("windows_ocr", False, "", error="not available")])

    monkeypatch.setattr(ocr, "run_ocr", run_ocr)
    return seen


async def test_scan_prefills_form_with_cross_checked_values(client, db, monkeypatch):
    seen = _fake_ocr(monkeypatch, {"tesseract": "WEIGHT 159.6 lb", "apple_vision": "159.6 lbs\nHeight 5'9\""})
    user = await make_customer(db)
    await login_as(client, user)
    before = _upload_files()
    r = await post_form(client, "/me/health/scan", {}, files={"photo": ("scale.png", png_bytes((200, 120)), "image/png")})
    assert r.status_code == 200 and seen and seen[0][1] == "measurement"
    text = r.text
    assert 'value="72.4"' in text and 'value="175.3"' in text and 'name="source" value="ocr"' in text
    assert "2 engines agree" in text and "Tesseract" in text and "Apple Vision" in text
    assert await db.scalar(select(func.count(MetricEntry.id))) == 0  # never auto-saved
    assert _upload_files() == before  # the photo is never stored
    saved = await post_form(client, "/me/health/metrics", {"height_cm": "175.3", "weight_kg": "72.4", "source": "ocr"})
    assert saved.status_code in (303,)
    entry = await today_entry(db, user)
    assert entry.source == "ocr"


async def test_scan_with_vision_model_and_disagreement(client, db, monkeypatch, fake_ai):
    _fake_ocr(monkeypatch, {"tesseract": "78.1 kg"})
    fake_ai(responses=[json.dumps({"weight": 72.4, "weight_unit": "kg", "height": None})])
    user = await make_customer(db)
    await login_as(client, user)
    r = await post_form(client, "/me/health/scan", {}, files={"photo": ("scale.jpg", png_bytes(), "image/png")})
    assert r.status_code == 200
    assert 'value="72.4"' in r.text and "1 engine - please check" in r.text and "AI vision" in r.text


async def test_scan_rejects_non_images_and_handles_unreadable_photos(client, db, monkeypatch):
    _fake_ocr(monkeypatch, {"tesseract": "no numbers here"})
    user = await make_customer(db)
    await login_as(client, user)
    bad = await post_form(client, "/me/health/scan", {}, files={"photo": ("x.png", b"not an image", "image/png")})
    assert bad.status_code == 400 and "not a valid photo" in bad.text
    missing = await post_form(client, "/me/health/scan", {})
    assert missing.status_code == 400
    nothing = await post_form(client, "/me/health/scan", {}, files={"photo": ("s.png", png_bytes(), "image/png")})
    assert nothing.status_code == 200 and "couldn't read the numbers" in html.unescape(nothing.text)


# ================================================================== health profile
async def test_profile_update_validates_and_notifies(client, db):
    user = await make_customer(db)
    await login_as(client, user)
    good = {"goal": "weight_loss", "fitness_level": "intermediate", "wake_time": "07:15", "sleep_reminders": "1",
            "emergency_contact_name": "Asha Verma", "emergency_contact_phone": "+91 98450 12345", "health_notes": "Mornings"}
    for field, bad_value, msg in (("wake_time", "25:00", "24-hour"), ("goal", "flying", "Choose a goal"),
                                  ("emergency_contact_phone", "12", "valid phone"), ("health_notes", "x" * 1001, "1,000")):
        r = await post_form(client, "/me/health/profile", {**good, field: bad_value})
        assert r.status_code == 400 and msg in html.unescape(r.text), field
    r = await post_form(client, "/me/health/profile", good)
    assert r.status_code == 303
    prof = await profile_of(db, user)
    assert prof.goal.value == "weight_loss" and prof.wake_time == time(7, 15) and prof.emergency_contact_name == "Asha Verma"
    note = (await db.execute(select(Notification).where(Notification.user_id == user.id))).scalar_one()
    assert note.title == "Health profile updated" and "wake-up time" in note.body
    again = await post_form(client, "/me/health/profile", good)
    assert again.status_code == 303 and await db.scalar(select(func.count(Notification.id))) == 1  # no-op, no spam
    # the bedtime moves with the wake time (07:15 - 8 h)
    assert "23:15" in (await client.get("/me")).text


async def test_minor_must_provide_a_guardian(client, db):
    minor = await make_minor(db, guardian=False)
    await login_as(client, minor)
    base = {"goal": "general_fitness", "fitness_level": "beginner", "wake_time": "06:30"}
    r = await post_form(client, "/me/health/profile", base)
    assert r.status_code == 400
    text = html.unescape(r.text)
    assert "guardian" in text.lower() and 'name="guardian_email"' in text
    same = await post_form(client, "/me/health/profile", {**base, "guardian_name": "Neha Gupta", "guardian_email": minor.email,
                                                          "guardian_phone": "+91 98450 12345"})
    assert same.status_code == 400
    ok = await post_form(client, "/me/health/profile", {**base, "guardian_name": "Neha Gupta", "guardian_relation": "Mother",
                                                        "guardian_email": "neha@example.com", "guardian_phone": "+91 98450 12345"})
    assert ok.status_code == 303
    prof = await profile_of(db, minor)
    assert prof.guardian_email == "neha@example.com"
    mails = (await db.execute(select(EmailOutbox))).scalars().all()
    assert any(m.to_email == "neha@example.com" and "guardian" in m.subject.lower() for m in mails)


async def test_conditions_add_remove_and_ownership(client, db):
    user = await make_customer(db)
    other = await make_customer(db, conditions=["Peanut allergy"])
    await login_as(client, user)
    r = await post_form(client, "/me/health/profile/conditions", {"kind": "injury", "name": "ACL tear, left knee"})
    assert r.status_code == 303
    dup = await post_form(client, "/me/health/profile/conditions", {"kind": "injury", "name": "acl tear, LEFT knee"})
    assert dup.status_code == 400 and "already on your list" in html.unescape(dup.text)
    for bad in ({"kind": "spell", "name": "Asthma"}, {"kind": "condition", "name": "A"}, {"kind": "condition", "name": "x" * 121}):
        assert (await post_form(client, "/me/health/profile/conditions", bad)).status_code == 400
    mine = (await db.execute(select(HealthCondition).where(HealthCondition.customer_id == user.id))).scalar_one()
    assert mine.tag == "knee_injury" and mine.source == "self"
    theirs = (await db.execute(select(HealthCondition).where(HealthCondition.customer_id == other.id))).scalar_one()
    assert (await post_form(client, f"/me/health/profile/conditions/{theirs.id}/delete")).status_code == 404
    r = await post_form(client, f"/me/health/profile/conditions/{mine.id}/delete")
    assert r.status_code == 303
    assert await db.scalar(select(func.count(HealthCondition.id)).where(HealthCondition.customer_id == user.id)) == 0
    prof = await profile_of(db, user)
    assert prof.health_embedding is None and prof.ac_recommendation["tags"] == []
    titles = [n.title for n in (await db.execute(select(Notification).where(Notification.user_id == user.id))).scalars()]
    assert titles.count("Health profile updated") == 2


async def test_alert_acknowledgement_is_scoped_to_owner(client, db):
    user = await make_customer(db)
    other = await make_customer(db)
    mine = HealthAlert(customer_id=user.id, level=W, title="Mine", message="m", source="rule")
    theirs = HealthAlert(customer_id=other.id, level=W, title="Theirs", message="t", source="rule")
    db.add_all([mine, theirs])
    await db.commit()
    await login_as(client, user)
    assert (await post_form(client, f"/me/health/alerts/{theirs.id}/ack", {"next": "/me"})).status_code == 404
    r = await post_form(client, f"/me/health/alerts/{mine.id}/ack", {"next": "/me"})
    assert r.status_code == 303 and r.headers["location"] == "/me"
    assert (await db.get(HealthAlert, mine.id, populate_existing=True)).acknowledged_at is not None
    assert (await db.get(HealthAlert, theirs.id, populate_existing=True)).acknowledged_at is None


# ================================================================== N+1 guard
async def _seed_member(db, n: int) -> User:
    owner = await make_owner(db)
    user = await make_customer(db, conditions=["Asthma", "High blood pressure", "Dust allergy", "Knee pain"][: max(1, n // 6)])
    await add_entries(db, user, [(i + 1, 175, 72 + (i % 3) * 0.2) for i in range(n)])
    for i in range(n):
        db.add(Task(user_id=user.id, title=f"Task {i}", kind=TaskKind.TASK, due_at=datetime.now(UTC) + timedelta(hours=i + 1)))
        db.add(HealthAlert(customer_id=user.id, level=W, title=f"Alert {i}", message="m", source="rule"))
        db.add(WellnessCheck(customer_id=user.id, pss_answers=[1, 2, 2, 1], pss_score=6, symptoms=[],
                             stress_level=StressLevel.MODERATE, recommendations="Rest"))
    await db.commit()
    for k in range(max(1, n // 8)):
        gym = await make_gym(db, owner)
        plans = await make_plans(db, gym)
        await make_subscription(db, user, plans[k % len(plans)])
    return user


async def _queries(client, url: str) -> int:
    with QueryCounter() as qc:
        r = await client.get(url)
    assert r.status_code == 200, url
    return qc.count


@pytest.mark.parametrize("url", ["/me", "/me/health", "/me/health/profile", "/me/wellness"])
async def test_pages_run_a_constant_number_of_queries(db, url):
    counts = []
    for n in (2, 24):
        user = await _seed_member(db, n)
        async with make_client() as c:
            await login_as(c, user)
            await c.get(url)  # warm-up (caches the AC advice once)
            counts.append(await _queries(c, url))
    assert counts[0] == counts[1], counts
    assert counts[0] <= 12, counts


async def test_background_ai_note_skips_stale_or_abnormal_entries(client, db, fake_ai):
    provider = fake_ai(responses=[assessment("Lovely consistency.")])
    user = await with_ai_plan(db, await make_customer(db))
    await add_entries(db, user, [(1, 175, 72)])
    await login_as(client, user)
    await log_reading(client, 175, 72.2)
    await log_reading(client, 175, 72.3)  # re-logged: the job is recycled for the newer values
    assert await run_jobs() == 1
    assert (await today_entry(db, user)).ai_note == "Lovely consistency." and len(provider.prompts) == 1
    # a reading that became abnormal (re-logged with a big jump) is left to the synchronous safety path
    await log_reading(client, 175, 76)
    entry = await today_entry(db, user)
    assert entry.anomaly_level == W and entry.confirmed is False
    entry.confirmed = True
    await db.commit()
    await hs.ai_note_job(db, {"entry_id": entry.id})
    await hs.ai_note_job(db, {"entry_id": 987654})  # unknown entry -> no-op
    await hs.ai_note_job(db, {"entry_id": "x"})  # malformed payload -> no-op
    assert len(provider.prompts) == 1


async def test_customer_without_profile_row_can_use_every_page(client, db):
    user = await make_user(db, role=Role.CUSTOMER, with_profile=False)
    await login_as(client, user)
    for url in ("/me", "/me/health", "/me/health/profile", "/me/wellness"):
        assert (await client.get(url)).status_code == 200, url
    r = await log_reading(client, 172, 68)
    assert r.status_code == 303
    prof = await profile_of(db, user)  # created on first write
    assert prof.height_cm == 172 and prof.weight_kg == 68 and prof.wake_time == time(6, 30)


# ================================================================== bounds: one alert per reading, AI reviews per day
async def _alerts(db, user: User) -> list[HealthAlert]:
    stmt = select(HealthAlert).where(HealthAlert.customer_id == user.id).order_by(HealthAlert.id)
    return list((await db.execute(stmt.execution_options(populate_existing=True))).scalars().all())


async def test_relogging_an_abnormal_reading_keeps_one_alert_per_reading(client, db):
    """F08: every post of the day's abnormal reading added another health alert (90 rows on one reading, also when posts
    arrived at the same moment). The reading keeps ONE alert, updated with the newest values and unread again."""
    user = await make_customer(db)
    await login_as(client, user)
    for i in range(6):
        assert (await log_reading(client, 176, 130 + i % 2)).status_code == 303  # BMI 42: WARNING, saved at once
    results = await asyncio.gather(*(log_reading(client, 176, 130 + i % 2) for i in range(8)))
    assert {r.status_code for r in results} == {303}
    entry = await today_entry(db, user)
    alerts = await _alerts(db, user)
    assert len(alerts) == 1 and alerts[0].metric_entry_id == entry.id and alerts[0].level == W
    assert await db.scalar(select(func.count(MetricEntry.id)).where(MetricEntry.customer_id == user.id)) == 1
    # read, then logged again with other values: the same alert comes back unread with the new numbers
    assert (await post_form(client, f"/me/health/alerts/{alerts[0].id}/ack", {"next": "/me/health"})).status_code == 303
    assert (await log_reading(client, 176, 127)).status_code == 303
    alerts = await _alerts(db, user)
    assert len(alerts) == 1 and alerts[0].acknowledged_at is None and "127 kg" in alerts[0].message


async def test_new_health_alerts_are_capped_per_member_and_day(db, monkeypatch):
    """Past ``alerts_per_day`` the newest alert of the same kind is updated (highest level kept) and unread again - a
    safety signal is never dropped, and a kind of alert not seen that day is still stored."""
    monkeypatch.setattr(hs, "alerts_per_day", lambda: 3)
    user = await make_customer(db)

    def alert(title: str, level: AnomalyLevel = W) -> HealthAlert:
        return HealthAlert(customer_id=user.id, level=level, title=title, message=f"about {title}", source="rule")

    stored = [await hs.add_alert(db, alert(f"Discomfort: {part}")) for part in ("knee", "back", "wrist")]
    stored[1].acknowledged_at = hs.utcnow()
    again = await hs.add_alert(db, alert("Discomfort: back", C))
    other = await hs.add_alert(db, alert("Discomfort: neck"))
    await db.commit()
    assert again.id == stored[1].id and other.id not in {a.id for a in stored}
    rows = await _alerts(db, user)
    assert len(rows) == 4
    back = next(a for a in rows if a.id == stored[1].id)
    assert back.level == C and back.acknowledged_at is None


async def test_ai_safety_reviews_are_bounded_per_member_and_day(client, db, fake_ai, monkeypatch):
    """F09: logging an abnormal reading and confirming it, over and over, ran one 'health_metrics' AI review per round
    (never refused by the AI guard). Past the daily allowance the rules decide alone and the note says so."""
    monkeypatch.setattr(hs, "reviews_per_day", lambda: 2)
    provider = fake_ai(handler=lambda **kwargs: assessment("Please see a doctor soon.", consult=True))
    user = await make_customer(db)
    await login_as(client, user)
    for i in range(5):
        r = await log_reading(client, 170, 150 + i % 2)  # BMI 52: CRITICAL, confirmation first
        assert (await confirm_from(client, r)).status_code == 303
    assert len(provider.prompts) == 2
    entry = await today_entry(db, user)
    assert entry.confirmed and entry.anomaly_level == C
    assert "not repeated" in entry.ai_note and "healthcare professional" in entry.ai_note
    notes = (await db.execute(select(Notification).where(Notification.user_id == user.id))).scalars().all()
    assert len([n for n in notes if n.category == NotificationCategory.HEALTH]) == 5  # the rules' notice every time
    assert len(await _alerts(db, user)) == 1


async def test_a_reading_confirmed_many_times_at_once_is_reviewed_once(client, db, fake_ai):
    """F09: 20 confirmations of ONE token at the same moment ran 10 AI reviews - the confirmed flag was read unlocked."""
    provider = fake_ai(handler=lambda **kwargs: assessment("Please see a doctor soon.", consult=True))
    user = await make_customer(db)
    await login_as(client, user)
    r = await log_reading(client, 175, 47.5)  # BMI 15.5: CRITICAL, confirmation first
    token = parse_qs(urlsplit(r.headers["location"]).query)["token"][0]
    results = await asyncio.gather(*(post_form(client, "/me/health/metrics/confirm", {"token": token, "next": "/me/health"})
                                     for _ in range(8)))
    assert {x.status_code for x in results} == {303}
    assert len(provider.prompts) == 1
    assert (await today_entry(db, user)).ai_note == "Please see a doctor soon."


async def test_the_ai_safety_review_holds_no_transaction_while_the_model_thinks(client, db, fake_ai, pool_probe):
    """F09: the review ran inside the request's transaction - a slow model held a pooled connection per confirmation,
    and a burst of them starved every other page (QueuePool limit reached, 503)."""
    seen: list[int] = []

    async def model(**kwargs: Any) -> str:
        seen.append(pool_probe.checked_out)
        return assessment("Please see a doctor soon.", consult=True)

    fake_ai(handler=model)
    user = await make_customer(db)
    await login_as(client, user)
    r = await log_reading(client, 175, 47.5)
    await db.close()
    assert (await confirm_from(client, r)).status_code == 303
    assert seen == [0]
    assert (await today_entry(db, user)).ai_note == "Please see a doctor soon."
