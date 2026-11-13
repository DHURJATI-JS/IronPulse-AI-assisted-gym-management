"""WP-E1 wellness / stress check-in: PSS-4 scoring, symptoms + red flags (suspension), optional selfie analysed
by the vision model in memory only (never stored), AI-unavailable fallback, history and ownership."""

from __future__ import annotations

import html
import json
from datetime import date

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.db import QueryCounter
from app.models import CustomerProfile, HealthAlert, Notification, SuspensionEvent, WellnessCheck
from app.models.enums import AnomalyLevel, NotificationCategory, StressLevel
from app.services import health as hs
from tests.conftest import login_as, make_client, post_form
from tests.factories import make_customer, make_gym, make_owner, make_plans, make_subscription, png_bytes

LOW, MOD, HIGH = StressLevel.LOW, StressLevel.MODERATE, StressLevel.HIGH


@pytest.fixture(autouse=True)
def _offline_ai_by_default(ai_down):
    """Tests never touch real AI providers; tests that need AI install ``fake_ai`` on top."""
    yield


def answers(q1, q2, q3, q4, symptoms=("none",)) -> dict:
    return {"q1": str(q1), "q2": str(q2), "q3": str(q3), "q4": str(q4), "symptoms": list(symptoms)}


def _upload_files() -> int:
    root = get_settings().upload_dir
    return sum(1 for p in root.rglob("*") if p.is_file()) if root.exists() else 0


async def latest_check(db, user) -> WellnessCheck:
    stmt = select(WellnessCheck).where(WellnessCheck.customer_id == user.id).order_by(WellnessCheck.id.desc()).limit(1)
    return (await db.execute(stmt.execution_options(populate_existing=True))).scalars().first()


async def profile_of(db, user) -> CustomerProfile:
    stmt = select(CustomerProfile).where(CustomerProfile.user_id == user.id)
    return (await db.execute(stmt.execution_options(populate_existing=True))).scalar_one()


FACE_JSON = json.dumps({"observations": ["Eyes look a little red", "Slight drooping eyelids"], "fatigue_signs": "High",
                        "stress_indicators": ["tense brow"], "disclaimer": "Not a medical assessment."})


# ================================================================== PSS-4 scoring (pure)
@pytest.mark.parametrize("ans,score,level", [
    ([0, 4, 4, 0], 0, LOW),     # items 2 and 3 are reverse-scored
    ([4, 0, 0, 4], 16, HIGH),
    ([2, 2, 2, 2], 8, MOD),
    ([1, 3, 2, 1], 5, LOW),     # boundary: 5 is still low
    ([1, 2, 2, 1], 6, MOD),     # boundary: 6 is moderate
    ([2, 1, 2, 2], 9, HIGH),    # boundary: 9 is high
    ([3, 1, 1, 1], 10, HIGH),
    ([0, 2, 2, 0], 4, LOW),
])
def test_pss4_scoring(ans, score, level):
    assert hs.pss_score(ans) == score
    assert hs.pss_level(score) == level


@pytest.mark.parametrize("bad", [[1, 2, 3], [1, 2, 3, 4, 0], [5, 0, 0, 0], [-1, 0, 0, 0]])
def test_pss4_rejects_invalid_answers(bad):
    with pytest.raises(ValueError):
        hs.pss_score(bad)


@pytest.mark.parametrize("level,face,expected", [
    (LOW, None, LOW),
    (LOW, {"status": "ok", "fatigue_signs": "high"}, MOD),
    (MOD, {"status": "ok", "fatigue_signs": "high"}, HIGH),
    (HIGH, {"status": "ok", "fatigue_signs": "high"}, HIGH),
    (LOW, {"status": "ok", "fatigue_signs": "moderate"}, LOW),  # only strong fatigue nudges the result
    (MOD, {"status": "unavailable"}, MOD),
])
def test_face_analysis_can_raise_one_level_at_most(level, face, expected):
    assert hs.combine_stress(level, face) == expected


def test_recommendations_always_link_the_breathing_video():
    for level in (LOW, MOD, HIGH):
        recs = hs.wellness_recommendations(level, [], minor=False)
        assert any(hs.BREATHING_VIDEO in r for r in recs)
    red = hs.wellness_recommendations(LOW, ["chest_pain"], minor=False)
    assert red[0].startswith("Seek emergency care now")
    teen = " ".join(hs.wellness_recommendations(HIGH, [], minor=True))
    assert "guardian" in teen


# ================================================================== pages and the happy path
async def test_wellness_page_and_questionnaire_only_result(client, db):
    user = await make_customer(db)
    await login_as(client, user)
    page = await client.get("/me/wellness")
    assert page.status_code == 200
    text = html.unescape(page.text)
    assert "How often have you felt confident about your ability to handle your personal problems?" in text
    assert 'capture="user"' in text and 'accept="image/*"' in text and 'enctype="multipart/form-data"' in text
    assert "Your check-ins will appear here" in text
    r = await post_form(client, "/me/wellness", answers(2, 2, 2, 2))
    assert r.status_code == 303
    check = await latest_check(db, user)
    assert r.headers["location"] == f"/me/wellness/{check.id}"
    assert check.pss_score == 8 and check.stress_level == MOD and check.symptoms == [] and check.face_analysis is None
    assert check.pss_answers == [2, 2, 2, 2] and hs.BREATHING_VIDEO in check.recommendations
    result = await client.get(r.headers["location"])
    assert result.status_code == 200
    text = html.unescape(result.text)
    assert "Moderate stress" in text and "/me/videos/box-breathing" in text and "No selfie this time" in text
    assert "Seek emergency care now" not in text
    # history lists it on the form page and the dashboard shows the latest check-in
    assert f"/me/wellness/{check.id}" in (await client.get("/me/wellness")).text
    assert "8/16" in (await client.get("/me")).text


async def test_high_stress_notifies_supportively(client, db):
    user = await make_customer(db)
    await login_as(client, user)
    await post_form(client, "/me/wellness", answers(4, 0, 1, 3))
    check = await latest_check(db, user)
    assert check.stress_level == HIGH
    note = (await db.execute(select(Notification).where(Notification.user_id == user.id))).scalar_one()
    assert note.category == NotificationCategory.HEALTH and note.link == f"/me/wellness/{check.id}"
    assert not (await profile_of(db, user)).activity_suspended


@pytest.mark.parametrize("data,field_msg", [
    ({"q1": "1", "q2": "2", "q3": "2", "symptoms": ["none"]}, "question 4"),
    ({"q1": "7", "q2": "2", "q3": "2", "q4": "1", "symptoms": ["none"]}, "question 1"),
    ({"q1": "1", "q2": "2", "q3": "2", "q4": "1"}, "None of these"),
    ({"q1": "1", "q2": "2", "q3": "2", "q4": "1", "symptoms": ["none", "chest_pain"]}, "not both"),
    ({"q1": "1", "q2": "2", "q3": "2", "q4": "1", "symptoms": ["headache"]}, "from the list"),
])
async def test_wellness_validation_rerenders_with_errors(client, db, data, field_msg):
    user = await make_customer(db)
    await login_as(client, user)
    r = await post_form(client, "/me/wellness", data)
    assert r.status_code == 400
    assert field_msg in html.unescape(r.text)
    assert await db.scalar(select(func.count(WellnessCheck.id))) == 0
    assert not (await profile_of(db, user)).activity_suspended


# ================================================================== red flags -> suspension
@pytest.mark.parametrize("flag", ["chest_pain", "fainting", "shortness_of_breath"])
async def test_red_flag_symptoms_suspend_and_point_to_emergency_care(client, db, flag):
    owner = await make_owner(db)
    plans = await make_plans(db, await make_gym(db, owner))
    user = await make_customer(db)
    await make_subscription(db, user, plans[0])
    await login_as(client, user)
    r = await post_form(client, "/me/wellness", answers(1, 3, 3, 1, symptoms=[flag, "palpitations"]))
    assert r.status_code == 303
    check = await latest_check(db, user)
    assert check.symptoms == [flag, "palpitations"]
    prof = await profile_of(db, user)
    assert prof.activity_suspended and prof.suspended_by == "rule"
    event = (await db.execute(select(SuspensionEvent))).scalar_one()
    assert event.actor == "rule" and event.action == "suspend"
    alert = await db.get(HealthAlert, event.health_alert_id)
    assert alert.level == AnomalyLevel.LIFE_THREATENING and alert.wellness_check_id == check.id
    assert "emergency" in alert.recommendation.lower()
    owner_note = (await db.execute(select(Notification).where(Notification.user_id == owner.id))).scalar_one()
    assert "Safety pause" in owner_note.title
    page = html.unescape((await client.get(r.headers["location"])).text)
    assert "Seek emergency care now" in page and "112" in page


async def test_red_flag_when_already_suspended_still_sends_emergency_guidance(client, db):
    user = await make_customer(db)
    await hs.suspend_customer(db, user, "Earlier reading", "rule")
    await db.commit()
    await login_as(client, user)
    r = await post_form(client, "/me/wellness", answers(1, 3, 3, 1, symptoms=["fainting"]))
    assert r.status_code == 303
    assert await db.scalar(select(func.count(SuspensionEvent.id))) == 1
    titles = [n.title for n in (await db.execute(select(Notification).where(Notification.user_id == user.id))).scalars()]
    assert "Seek emergency care now" in titles


async def test_advisory_symptoms_warn_without_suspension(client, db):
    user = await make_customer(db)
    await login_as(client, user)
    await post_form(client, "/me/wellness", answers(1, 3, 3, 1, symptoms=["severe_dizziness"]))
    assert not (await profile_of(db, user)).activity_suspended
    alert = (await db.execute(select(HealthAlert).where(HealthAlert.customer_id == user.id))).scalar_one()
    assert alert.level == AnomalyLevel.WARNING and "healthcare professional" in alert.recommendation


async def test_minor_red_flag_emails_guardian(client, db):
    today = date.today()
    minor = await make_customer(db, date_of_birth=today.replace(year=today.year - 14, day=min(today.day, 28)))
    prof = await db.get(CustomerProfile, minor.id)
    prof.guardian_email, prof.guardian_name = "parent@example.com", "Parent"
    await db.commit()
    await login_as(client, minor)
    await post_form(client, "/me/wellness", answers(1, 3, 3, 1, symptoms=["chest_pain"]))
    from app.models import EmailOutbox

    mails = (await db.execute(select(EmailOutbox).where(EmailOutbox.to_email == "parent@example.com"))).scalars().all()
    assert mails and "Safety alert" in mails[0].subject


# ================================================================== selfie: vision AI, in memory only
async def test_face_photo_is_analysed_in_memory_and_never_stored(client, db, fake_ai):
    provider = fake_ai(responses=[FACE_JSON])
    user = await make_customer(db)
    await login_as(client, user)
    before = _upload_files()
    r = await post_form(client, "/me/wellness", answers(1, 3, 3, 1),
                        files={"photo": ("selfie.png", png_bytes((160, 160), "#d9b99b"), "image/png")})
    assert r.status_code == 303
    assert _upload_files() == before  # nothing written under the upload directory
    assert len(provider.prompts) == 1 and len(provider.prompts[0]["images"]) == 1
    assert "Never identify the person" in provider.prompts[0]["system"]
    check = await latest_check(db, user)
    face = check.face_analysis
    assert face["status"] == "ok" and face["fatigue_signs"] == "high" and face["observations"][0] == "Eyes look a little red"
    assert check.pss_score == 4 and check.stress_level == MOD  # low questionnaire, raised one level by strong fatigue
    assert check.ai_provider == "gateway"
    # only the model's read-out is kept - no image bytes or derived pixels end up in the database
    assert set(face) <= {"status", "observations", "fatigue_signs", "stress_indicators", "disclaimer", "provider"}
    assert len(json.dumps(face)) < 2000
    page = html.unescape((await client.get(r.headers["location"])).text)
    assert "Eyes look a little red" in page and "tense brow" in page and "Raised from low" in page
    assert "not stored" in page


async def test_face_photo_with_ai_unavailable_falls_back_to_questionnaire(client, db):
    user = await make_customer(db)
    await login_as(client, user)
    before = _upload_files()
    r = await post_form(client, "/me/wellness", answers(2, 2, 2, 2),
                        files={"photo": ("selfie.jpg", png_bytes((120, 120)), "image/png")})
    assert r.status_code == 303
    assert _upload_files() == before
    check = await latest_check(db, user)
    assert check.face_analysis == {"status": "unavailable"} and check.stress_level == MOD and check.ai_provider is None
    page = html.unescape((await client.get(r.headers["location"])).text)
    assert "AI unavailable" in page and "questionnaire only" in page


async def test_invalid_face_photo_is_a_form_error(client, db, fake_ai):
    provider = fake_ai(responses=[FACE_JSON])
    user = await make_customer(db)
    await login_as(client, user)
    r = await post_form(client, "/me/wellness", answers(2, 2, 2, 2), files={"photo": ("x.png", b"definitely not a png", "image/png")})
    assert r.status_code == 400 and "not a valid photo" in r.text
    assert provider.prompts == [] and await db.scalar(select(func.count(WellnessCheck.id))) == 0


# ================================================================== ownership + N+1
async def test_results_are_private(client, db):
    user = await make_customer(db)
    other = await make_customer(db)
    db.add(WellnessCheck(customer_id=other.id, pss_answers=[1, 1, 1, 1], pss_score=4, symptoms=[],
                         stress_level=LOW, recommendations="Rest"))
    await db.commit()
    theirs = (await db.execute(select(WellnessCheck))).scalar_one()
    await login_as(client, user)
    assert (await client.get(f"/me/wellness/{theirs.id}")).status_code == 404
    assert (await client.get("/me/wellness/999999")).status_code == 404


async def test_result_page_query_count_is_constant(db):
    counts = []
    for n in (1, 15):
        user = await make_customer(db)
        for i in range(n):
            db.add(WellnessCheck(customer_id=user.id, pss_answers=[1, 2, 2, 1], pss_score=6, symptoms=["palpitations"],
                                 stress_level=MOD, recommendations="Rest\nBreathe"))
        await db.commit()
        latest = (await db.execute(select(WellnessCheck).where(WellnessCheck.customer_id == user.id)
                                   .order_by(WellnessCheck.id.desc()).limit(1))).scalar_one()
        async with make_client() as c:
            await login_as(c, user)
            with QueryCounter() as qc:
                r = await c.get(f"/me/wellness/{latest.id}")
            assert r.status_code == 200
            counts.append(qc.count)
    assert counts[0] == counts[1] and counts[0] <= 6, counts
