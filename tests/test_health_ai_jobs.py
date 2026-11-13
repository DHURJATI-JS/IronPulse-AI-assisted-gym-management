"""Health and wellness AI under the per-user guard and the stop-when-you-leave rules (docs/ai-guard-and-queue.md):

* AC advice: a condition change stores the rule-based advice at once; the optional AI wording is the
  ``health_ac_note`` background job (one per member, judged by the guard at the click, paused while the member is
  away, silent in the browser, and never applied to advice that changed meanwhile).
* POST /me/health/scan and POST /me/wellness: a guard refusal of the photo check is explained (scan: on the result
  card next to the OCR read-out; wellness: in the flash and on the result page) - the photo is still never stored,
  the wellness questionnaire and its red-flag safety path are always recorded, and a red-flag check-in never waits for
  a selfie read-out at all. A check stopped because the page left shows nothing about it. 'busy' falls back silently,
  like an offline AI.
* The health_metrics safety review is never refused, even while the account's AI is paused.
* Plain HTML forms tie their AI work to the tab that posted them (``page_id`` field).
"""

from __future__ import annotations

import asyncio
import html
import json
from typing import Any

import pytest
from sqlalchemy import func, select, update

from app import db as dbmod
from app.ai import health as health_ai
from app.ai import wellness as wellness_ai
from app.ai.errors import AICancelled, AIRejected
from app.ai.guard import guard
from app.ai.owner import AIOwner, current_owner
from app.ai.scheduler import BUSY_MESSAGE
from app.config import get_settings
from app.core.utils import utcnow
from app.models import CustomerProfile, HealthAlert, HealthCondition, Job, MetricEntry, Notification, User, WellnessCheck
from app.models.enums import ConditionKind, JobStatus, StressLevel
from app.services import health as hs
from app.services.presence import presence
from app.services.realtime import hub
from tests.conftest import login_as, post_form, run_jobs
from tests.factories import make_customer, make_gym, make_owner, make_plans, make_subscription, png_bytes

QUEUED, DONE, PAUSED, FAILED = JobStatus.QUEUED, JobStatus.DONE, JobStatus.PAUSED, JobStatus.FAILED
WORDING = "Keep the room at 24 to 26 °C, no cold drafts, clean filters."
FACE_JSON = json.dumps({"observations": ["Eyes look a little red"], "fatigue_signs": "low", "stress_indicators": [],
                        "disclaimer": "Not a medical assessment."})
TAB = "tab-health-0001-forms"


@pytest.fixture(autouse=True)
def _guard_as_configured(monkeypatch, ai_down):
    """The real guard with the shipped quotas; AI offline unless a test installs ``fake_ai``."""
    monkeypatch.setattr(get_settings(), "ai_guard_enabled", True)
    monkeypatch.setattr(get_settings(), "ai_quota_scale", 1.0)
    yield


# ------------------------------------------------------------------ helpers
async def _ai_member(db, **kwargs: Any) -> User:
    owner = await make_owner(db)
    plans = {p.code: p for p in await make_plans(db, await make_gym(db, owner))}
    user = await make_customer(db, **kwargs)
    await make_subscription(db, user, plans["online_ai"])
    return user


async def _advice(user: User) -> dict[str, Any]:
    async with dbmod.get_sessionmaker()() as s:
        return (await s.get(CustomerProfile, user.id)).ac_recommendation


async def _profile(user: User) -> CustomerProfile:
    async with dbmod.get_sessionmaker()() as s:
        return await s.get(CustomerProfile, user.id)


async def _ac_job(user: User) -> Job | None:
    async with dbmod.get_sessionmaker()() as s:
        return await s.scalar(select(Job).where(Job.dedupe_key == f"health_ac_note:{user.id}"))


async def _notes_used(user_id: int) -> int:
    """Units of the AC wording's own quota group ('notes', app/ai/policy.py - it never touches the coach plans' 'gen')
    charged today: the day window, so a minute boundary mid-test never resets the count."""
    return (await guard.status(user_id))["usage"]["notes"][2]


async def _add_condition(client, name: str):
    return await post_form(client, "/me/health/profile/conditions", {"kind": "condition", "name": name})


def _updates(q: asyncio.Queue, kind: str) -> list[dict[str, Any]]:
    out = []
    while not q.empty():
        evt = q.get_nowait()
        if isinstance(evt, dict) and evt.get("type") == "task.update" and evt.get("kind") == kind:
            out.append(evt)
    return out


async def _ticks(n: int = 30) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


def _upload_files() -> int:
    root = get_settings().upload_dir
    return sum(1 for p in root.rglob("*") if p.is_file()) if root.exists() else 0


def _fake_ocr(monkeypatch, texts: dict[str, str]) -> None:
    from app.services import ocr

    async def run_ocr(image, *, engines=None, purpose="document"):
        return ocr.MultiOcrResult(results=[ocr.OcrEngineResult(e, True, t) for e, t in texts.items()])

    monkeypatch.setattr(ocr, "run_ocr", run_ocr)


async def _scan(client):
    return await post_form(client, "/me/health/scan", {"page_id": TAB},
                           files={"photo": ("scale.png", png_bytes((200, 120)), "image/png")})


def _answers(q1, q2, q3, q4, symptoms=("none",)) -> dict[str, Any]:
    return {"q1": str(q1), "q2": str(q2), "q3": str(q3), "q4": str(q4), "symptoms": list(symptoms), "page_id": TAB}


async def _check_in(client, data: dict[str, Any]):
    return await post_form(client, "/me/wellness", data,
                           files={"photo": ("selfie.png", png_bytes((160, 160), "#d9b99b"), "image/png")})


async def _latest_check(user: User) -> WellnessCheck:
    async with dbmod.get_sessionmaker()() as s:
        stmt = select(WellnessCheck).where(WellnessCheck.customer_id == user.id).order_by(WellnessCheck.id.desc())
        return (await s.execute(stmt.limit(1))).scalars().first()


# ============================================================================================ AC wording job
async def test_a_condition_change_stores_the_rules_advice_now_and_a_job_adds_the_ai_wording(client, db, fake_ai):
    provider = fake_ai(handler=lambda **k: json.dumps({"summary": WORDING}))
    user = await _ai_member(db)
    await login_as(client, user)
    r = await _add_condition(client, "Asthma since childhood")
    assert r.status_code == 303 and r.headers["location"] == "/me/health/profile#conditions"
    advice = await _advice(user)
    assert advice["verdict"] == "caution" and advice["tags"] == ["asthma"] and advice["ai_summary"] is None
    assert provider.prompts == []  # the request never waits for the AI
    job = await _ac_job(user)
    assert (job.kind, job.status, job.user_id, job.exit_policy, job.payload) == (
        "health_ac_note", QUEUED, user.id, "pause", {"user_id": user.id})
    assert await _notes_used(user.id) == 1  # the click was judged and charged by the guard (jobs never are)

    q = hub.subscribe(user.id)
    try:
        assert await run_jobs() == 1
        await _ticks()
        events = _updates(q, "health_ac_note")
    finally:
        hub.unsubscribe(user.id, q)
    advice = await _advice(user)
    assert advice["ai_summary"] == WORDING and advice["ai_provider"] == "gateway" and advice["temp_min"] == 24
    assert len(provider.prompts) == 1 and "air-conditioning" in provider.prompts[0]["system"]
    # one 'done' - the runner's, after the DONE write - with the job's link, silent (a reworded sentence is no
    # island-worthy news); the job no longer sends one of its own first
    assert [e["status"] for e in events] == ["done"]
    assert events[0]["notify"] is False and events[0]["link"] == "/me/health/profile" and events[0]["task_id"] == job.id
    assert events[0]["message"] == "Your air-conditioning advice has friendlier AI wording."
    assert "Worded by AI" in (await client.get("/me/health/profile")).text
    assert await _notes_used(user.id) == 1


async def test_changes_while_the_wording_is_queued_share_one_job_and_one_charge(client, db, fake_ai):
    provider = fake_ai(handler=lambda **k: json.dumps({"summary": WORDING}))
    user = await _ai_member(db)
    await login_as(client, user)
    await _add_condition(client, "Asthma since childhood")
    await _add_condition(client, "Arthritis")
    assert await db.scalar(select(func.count(Job.id)).where(Job.kind == "health_ac_note")) == 1
    assert await _notes_used(user.id) == 1
    assert await run_jobs() == 1
    advice = await _advice(user)
    assert advice["tags"] == ["arthritis", "asthma"] and advice["ai_summary"] == WORDING  # the newest advice
    assert len(provider.prompts) == 1 and "Arthritis" in provider.prompts[0]["prompt"]


async def test_no_wording_job_without_an_ai_plan(client, db, fake_ai):
    provider = fake_ai(handler=lambda **k: json.dumps({"summary": WORDING}))
    user = await make_customer(db)
    await login_as(client, user)
    assert (await _add_condition(client, "Asthma since childhood")).status_code == 303
    assert await _ac_job(user) is None and await _notes_used(user.id) == 0
    assert (await _advice(user))["verdict"] == "caution" and provider.prompts == []


async def test_a_refused_wording_keeps_the_rules_advice_and_the_change(client, db, fake_ai, monkeypatch):
    monkeypatch.setattr(get_settings(), "ai_quota_scale", 0.5)  # 'notes' for an AI member: 6 a minute -> 3
    provider = fake_ai(handler=lambda **k: json.dumps({"summary": WORDING}))
    user = await _ai_member(db)  # 3 'notes' units a minute here; one AC wording costs 1
    await login_as(client, user)
    for name in ("Asthma since childhood", "Arthritis", "Diabetes type 2"):
        assert (await _add_condition(client, name)).status_code == 303
        assert await run_jobs() == 1
    assert await _notes_used(user.id) == 3 and len(provider.prompts) == 3
    r = await _add_condition(client, "Pregnancy")  # the 4th within a minute: the guard refuses the AI wording
    assert r.status_code == 303
    flash = html.unescape((await client.get(r.headers["location"].split("#")[0])).text)
    assert "Added “Pregnancy” to your health profile." in flash  # the change itself is never refused
    assert await db.scalar(select(func.count(HealthCondition.id)).where(HealthCondition.customer_id == user.id)) == 4
    advice = await _advice(user)
    assert "pregnancy" in advice["tags"] and advice["ai_summary"] is None  # the rule-based wording stays
    assert (await _ac_job(user)).status == DONE and await run_jobs() == 0 and len(provider.prompts) == 3


async def test_the_wording_job_with_the_ai_offline_keeps_the_rules_advice_quietly(client, db):
    user = await _ai_member(db)
    await login_as(client, user)
    await _add_condition(client, "Asthma since childhood")
    q = hub.subscribe(user.id)
    try:
        assert await run_jobs() == 1  # ai_down: every provider fails
        await _ticks()
        events = _updates(q, "health_ac_note")
    finally:
        hub.unsubscribe(user.id, q)
    assert (await _ac_job(user)).status == DONE and (await _advice(user))["ai_summary"] is None
    assert events[0]["status"] == "done" and events[0]["notify"] is False and events[0]["link"] is None


async def test_a_failing_wording_job_is_tracked_silently_and_only_its_last_failure_is_announced(client, db,
                                                                                                 monkeypatch):
    user = await _ai_member(db)
    await login_as(client, user)
    await _add_condition(client, "Asthma since childhood")
    job = await _ac_job(user)

    async def broken(db: Any, member: User) -> bool:
        raise RuntimeError("the profile row is locked")

    monkeypatch.setattr(hs, "_word_ac", broken)
    q = hub.subscribe(user.id)
    try:
        assert await run_jobs() == 1  # attempt 1 of 2 fails: a retry follows, which is no news
        await _ticks()
        assert _updates(q, "health_ac_note") == []
        async with dbmod.get_sessionmaker()() as s:
            await s.execute(update(Job).where(Job.id == job.id).values(run_after=utcnow()))
            await s.commit()
        assert await run_jobs() == 1  # the last attempt fails too
        await _ticks()
        events = _updates(q, "health_ac_note")
    finally:
        hub.unsubscribe(user.id, q)
    assert (await _ac_job(user)).status == FAILED and (await _advice(user))["ai_summary"] is None
    # the runner's last 'failed' (the job sends none of its own): tracked by the browser, never an island note
    assert [(e["task_id"], e["status"], e["notify"]) for e in events] == [(job.id, "failed", False)]


async def test_the_wording_job_pauses_while_the_member_is_away(client, db, fake_ai, monkeypatch):
    monkeypatch.setattr(get_settings(), "presence_grace_seconds", 0.1)
    provider = fake_ai(handler=lambda **k: json.dumps({"summary": WORDING}))
    user = await _ai_member(db)
    await login_as(client, user)
    await _add_condition(client, "Asthma since childhood")
    await presence.on_left(user.id)
    job = await _ac_job(user)
    assert (job.status, job.cancel_reason) == (PAUSED, "left")
    assert await run_jobs() == 0 and provider.prompts == []
    await client.get("/me/health/profile")  # back (load_user -> presence.touch)
    await presence.drain()
    assert (await _ac_job(user)).status == QUEUED
    await _add_condition(client, "Arthritis")  # one more change: the same job, no new charge
    assert await _notes_used(user.id) == 1
    assert await run_jobs() == 1
    advice = await _advice(user)
    assert advice["ai_summary"] == WORDING and advice["tags"] == ["arthritis", "asthma"]


async def test_a_wording_that_raced_a_newer_change_is_redone_for_the_newest_advice(db, fake_ai):
    user = await _ai_member(db, conditions=["Asthma since childhood"])
    async with dbmod.get_sessionmaker()() as s:
        member = await s.get(User, user.id)
        await hs.refresh_ac(s, member, await s.get(CustomerProfile, user.id), ["asthma"])
        await s.commit()
    answers = ["Stale wording for 24 to 26 °C.", "Keep it at 24 to 26 °C and keep your joints warm."]

    async def model(**kwargs: Any) -> str:
        if len(answers) == 2:  # while the AI is busy with the first wording, the member adds a condition
            async with dbmod.get_sessionmaker()() as s:
                s.add(HealthCondition(customer_id=user.id, kind=ConditionKind.CONDITION, name="Arthritis",
                                      tag="arthritis"))
                await hs.refresh_ac(s, await s.get(User, user.id), await s.get(CustomerProfile, user.id),
                                    ["arthritis", "asthma"])
                await s.commit()
        return json.dumps({"summary": answers.pop(0)})

    provider = fake_ai(handler=model)
    async with dbmod.get_sessionmaker()() as s:
        await hs.ac_note_job(s, {"user_id": user.id})
    advice = await _advice(user)
    assert advice["tags"] == ["arthritis", "asthma"] and advice["ai_summary"] == "Keep it at 24 to 26 °C and keep your joints warm."
    assert len(provider.prompts) == 2 and "Arthritis" in provider.prompts[1]["prompt"]
    async with dbmod.get_sessionmaker()() as s:
        await hs.ac_note_job(s, {"user_id": 987654})  # unknown member -> no-op
        await hs.ac_note_job(s, {"user_id": "x"})  # malformed payload -> no-op
    assert len(provider.prompts) == 2


# ============================================================================================ scale scan
async def test_a_refused_ai_scan_is_explained_and_the_ocr_reading_still_prefills(client, db, monkeypatch, fake_ai):
    _fake_ocr(monkeypatch, {"tesseract": "WEIGHT 72.4 kg", "apple_vision": "72.4 kg"})
    provider = fake_ai(responses=[json.dumps({"weight": 72.4, "weight_unit": "kg"})])
    user = await make_customer(db)  # no AI plan: 3 'vision' units a minute, one photo check costs 2
    await login_as(client, user)
    before = _upload_files()
    first = await _scan(client)
    assert first.status_code == 200 and "AI vision check skipped" not in first.text and len(provider.prompts) == 1
    second = await _scan(client)
    assert second.status_code == 200 and len(provider.prompts) == 1  # refused before any compute
    text = html.unescape(second.text)
    assert "AI vision check skipped." in text and "You've used your AI photo checks for this minute." in text
    assert "You can try again in" in text and "The values below come from the OCR engines only." in text
    assert 'data-scan-ai-refusal="quota"' in text and 'value="72.4"' in text and "2 engines agree" in text
    assert _upload_files() == before and await db.scalar(select(func.count(MetricEntry.id))) == 0


async def test_a_refused_scan_without_an_ocr_reading_still_explains_why(client, db, monkeypatch, fake_ai):
    _fake_ocr(monkeypatch, {"tesseract": "no numbers here"})
    fake_ai(responses=[json.dumps({"weight": None})])

    async def refused(image: bytes) -> Any:
        raise AIRejected("cooldown", "AI is paused on your account for 2 min because of messages that looked like "
                                     "spam.", retry_after=120)

    monkeypatch.setattr(health_ai, "read_measurement_photo", refused)
    user = await make_customer(db)
    await login_as(client, user)
    text = html.unescape((await _scan(client)).text)
    assert "We couldn't read the numbers" in text and "AI is paused on your account for 2 min" in text
    assert "You can type the values in meanwhile." in text


@pytest.mark.parametrize("stop", [AICancelled("client_left"), AIRejected("busy", BUSY_MESSAGE, retry_after=5)],
                         ids=["page-left", "busy"])
async def test_a_stopped_or_busy_ai_scan_shows_the_ocr_reading_without_a_message(client, db, monkeypatch, fake_ai, stop):
    _fake_ocr(monkeypatch, {"tesseract": "WEIGHT 72.4 kg"})
    fake_ai(responses=["{}"])

    async def interrupted(image: bytes) -> Any:
        raise stop

    monkeypatch.setattr(health_ai, "read_measurement_photo", interrupted)
    user = await make_customer(db)
    await login_as(client, user)
    r = await _scan(client)
    assert r.status_code == 200 and 'value="72.4"' in r.text
    assert "AI vision check skipped" not in r.text and "data-scan-ai-refusal" not in r.text


# ============================================================================================ wellness check-in
async def test_a_refused_selfie_is_flashed_and_the_check_in_is_still_saved(client, db, fake_ai):
    provider = fake_ai(responses=[FACE_JSON])
    user = await make_customer(db)  # 3 'vision' units a minute, one selfie read-out costs 2
    await login_as(client, user)
    before = _upload_files()
    assert (await _check_in(client, _answers(1, 3, 3, 1))).status_code == 303
    r = await _check_in(client, _answers(2, 2, 2, 2))
    assert r.status_code == 303 and len(provider.prompts) == 1  # the second selfie was refused before any compute
    check = await _latest_check(user)
    assert check.face_analysis == {"status": "refused", "code": "quota"} and check.ai_provider is None
    assert check.pss_score == 8 and check.stress_level == StressLevel.MODERATE  # the questionnaire decides
    page = html.unescape((await client.get(r.headers["location"])).text)
    assert "Check-in saved without the selfie read-out. You've used your AI photo checks for this minute." in page
    assert "You can try again in" in page
    assert "Selfie not analysed" in page and "your AI photo checks for now were used up" in page
    assert _upload_files() == before  # the photo is never stored, refused or not


async def test_a_red_flag_check_in_never_waits_for_the_selfie(client, db, monkeypatch):
    calls: list[int] = []

    async def read_out(image: bytes, *, minor: bool = False) -> Any:
        calls.append(len(image))
        raise AIRejected("cooldown", "AI is paused on your account for 15 min.", retry_after=900)

    monkeypatch.setattr(wellness_ai, "analyse_face", read_out)
    user = await _ai_member(db)
    await login_as(client, user)
    before = _upload_files()
    r = await _check_in(client, _answers(1, 3, 3, 1, symptoms=["chest_pain"]))
    assert r.status_code == 303 and calls == []  # the emergency path never waits for (or spends) a selfie read-out
    prof = await _profile(user)
    assert prof.activity_suspended and prof.suspended_by == "rule"  # red flags: suspension + emergency guidance
    check = await _latest_check(user)
    assert check.symptoms == ["chest_pain"] and check.face_analysis == {"status": "skipped"} and check.ai_provider is None
    page = html.unescape((await client.get(r.headers["location"])).text)
    assert "Seek emergency care now" in page and "the symptoms you reported come first" in page
    assert _upload_files() == before


async def test_a_red_flag_check_in_with_a_broken_photo_is_still_recorded(client, db):
    user = await make_customer(db)
    await login_as(client, user)
    r = await post_form(client, "/me/wellness", _answers(1, 3, 3, 1, symptoms=["shortness_of_breath"]),
                        files={"photo": ("x.png", b"definitely not a png", "image/png")})
    assert r.status_code == 303  # no "not a valid photo" form error standing between them and the emergency path
    assert (await _profile(user)).activity_suspended
    assert (await _latest_check(user)).face_analysis == {"status": "skipped"}


async def test_a_selfie_stopped_because_the_page_left_still_records_the_whole_check_in(client, db, monkeypatch):
    async def stopped(image: bytes, *, minor: bool = False) -> Any:
        raise AICancelled("page_closed")

    monkeypatch.setattr(wellness_ai, "analyse_face", stopped)
    user = await make_customer(db)
    await login_as(client, user)
    r = await _check_in(client, _answers(4, 0, 1, 3, symptoms=["severe_dizziness"]))  # high stress + advisory symptom
    assert r.status_code == 303
    check = await _latest_check(user)
    assert check.face_analysis == {"status": "cancelled"} and check.symptoms == ["severe_dizziness"]
    assert check.stress_level == StressLevel.HIGH and check.ai_provider is None
    async with dbmod.get_sessionmaker()() as s:  # the advisory alert and the supportive note are still there
        alerts = (await s.execute(select(HealthAlert).where(HealthAlert.customer_id == user.id))).scalars().all()
        notes = (await s.execute(select(Notification).where(Notification.user_id == user.id))).scalars().all()
    assert [a.title for a in alerts] == ["Symptoms worth checking"] and [n.title for n in notes] == ["Your stress check-in"]
    page = html.unescape((await client.get(r.headers["location"])).text)
    assert "Selfie read-out stopped" in page and "Selfie not analysed" not in page


async def test_a_busy_queue_falls_back_to_the_questionnaire_like_an_offline_ai(client, db, monkeypatch):
    async def busy(image: bytes, *, minor: bool = False) -> Any:
        raise AIRejected("busy", BUSY_MESSAGE, retry_after=5)

    monkeypatch.setattr(wellness_ai, "analyse_face", busy)
    user = await make_customer(db)
    await login_as(client, user)
    r = await _check_in(client, _answers(2, 2, 2, 2))
    assert (await _latest_check(user)).face_analysis == {"status": "unavailable"}
    assert "AI is unavailable, so your result is based on the questionnaire only." in html.unescape(
        (await client.get(r.headers["location"])).text)


# ============================================================================================ safety + tab identity
async def test_the_health_metrics_safety_review_is_never_refused_even_while_ai_is_paused(client, db, fake_ai):
    provider = fake_ai(responses=[json.dumps({"assessment": "Stop and see a doctor.", "consult_professional": True,
                                              "escalate_to_suspension": True, "reason": "Concerning"})])
    user = await make_customer(db)
    member = AIOwner(user_id=user.id, role="customer")
    for _ in range(2):  # two spam refusals (3 strike points each) pause the account's AI
        with pytest.raises(AIRejected):
            await guard.admit(member, "assistant", "a" * 60)
    assert (await guard.status(user.id))["cooldown_until"] is not None
    with pytest.raises(AIRejected) as err:
        await guard.admit(member, "assistant", "how should I warm up?")
    assert err.value.code == "cooldown"
    await login_as(client, user)
    r = await post_form(client, "/me/health/metrics", {"height_cm": "175", "weight_kg": "125.6", "next": "/me/health",
                                                       "page_id": TAB})  # BMI 41 -> WARNING, reviewed inline
    assert r.status_code == 303 and r.headers["location"] == "/me/health"
    assert len(provider.prompts) == 1  # the safety review ran despite the pause ...
    assert (await _profile(user)).suspended_by == "ai"  # ... and its escalation was applied


async def test_plain_forms_tie_their_ai_work_to_the_tab_that_posted_them(client, db, monkeypatch, fake_ai):
    # two photo checks (2 'vision' units each) within a minute: more than a member's quota - not what this is about
    monkeypatch.setattr(get_settings(), "ai_quota_scale", 2.0)
    _fake_ocr(monkeypatch, {"tesseract": "WEIGHT 72.4 kg"})
    seen: list[str | None] = []

    def model(**kwargs: Any) -> str:
        owner = current_owner()
        seen.append(owner.page_id if owner is not None else None)
        return FACE_JSON if "selfie" in str(kwargs.get("system", "")).lower() else json.dumps({"weight": 72.4})

    fake_ai(handler=model)
    user = await make_customer(db)
    await login_as(client, user)
    assert (await _scan(client)).status_code == 200
    assert (await _check_in(client, _answers(1, 1, 1, 1))).status_code == 303
    assert seen == [TAB, TAB]  # the scan's vision check and the selfie read-out both belong to the posting tab
