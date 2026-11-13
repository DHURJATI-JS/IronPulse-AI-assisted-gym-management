"""Hardening of the AI features (docs/ai-guard-and-queue.md): regression tests, each failing without its fix.

* Health notes: the friendly AI note of a normal reading (the ``health_ai_note`` job) is AI-written only for a plan
  with AI, judged by the AI guard at the click (its own non-safety feature ``hs.NOTE_FEATURE`` - background jobs are
  never judged), refunded when no AI note comes of it, and never runs as the 'health_metrics' safety review.
* Coach voice (``/api/tts``): members never share a cached line or a synthesis - one member can no longer learn which
  lines (first names, workout details) another member just heard, and a refusal reaches only the member it belongs
  to. An abandoned voice line (the coach page aborted its fetch) gives its place in the voice lane back at once.
* No pooled connection - nor a transaction the server kills after 60 s idle - is held while the AI or OCR works: the
  scale scan, the wellness selfie, the coach plan job and the health note job.
* Staff certificate re-check: a person-requested re-read is bounded per record and charged to the person who asked
  (``app.services.ocr.reread_documents``).
"""

from __future__ import annotations

import asyncio
import html
import json
from typing import Any

import pytest
from sqlalchemy import select

from app import db as dbmod
from app.ai import health as health_ai
from app.ai import wellness as wellness_ai
from app.ai.client import ai
from app.ai.errors import AIRejected
from app.ai.guard import guard
from app.ai.owner import AIOwner, owner_scope
from app.ai.policy import policy_for
from app.ai.scheduler import scheduler
from app.config import get_settings
from app.models import Job, MetricEntry, User
from app.models.enums import AnomalyLevel, JobStatus
from app.services import health as hs
from app.services import ocr
from app.services import tts as tts_mod
from app.services.tts import tts
from tests.ask_ai_helpers import pool_probe, steady_minute  # noqa: F401 - fixtures
from tests.conftest import api, login_as, make_client, post_form, run_jobs
from tests.droppable_client import droppable_client
from tests.factories import make_customer, make_gym, make_owner, make_plans, make_subscription, png_bytes
from tests.test_tts import MP3, FakeSpeech, page_owner, tts_lane, wait_until

NOTE = "Steady and healthy - keep it up."
NOTE_JSON = json.dumps({"assessment": NOTE, "consult_professional": False, "escalate_to_suspension": False,
                        "reason": ""})
TAB = "tab-hardening-0001-xyz"


@pytest.fixture(autouse=True)
def _guard_as_configured(monkeypatch, ai_down):
    """The real guard with the shipped quotas; AI offline unless a test installs ``fake_ai``."""
    monkeypatch.setattr(get_settings(), "ai_guard_enabled", True)
    monkeypatch.setattr(get_settings(), "ai_quota_scale", 1.0)
    yield


@pytest.fixture
def speech():
    installed: list[FakeSpeech] = []

    def install(**kwargs: Any) -> FakeSpeech:
        fake = FakeSpeech(**kwargs)
        tts.use_client(fake)
        installed.append(fake)
        return fake

    yield install
    for fake in installed:
        fake.release()  # nothing may stay blocked into the next test
    tts.use_client(None)


# ------------------------------------------------------------------ helpers
async def _ai_member(db, **kwargs: Any) -> User:
    owner = await make_owner(db)
    plans = {p.code: p for p in await make_plans(db, await make_gym(db, owner))}
    user = await make_customer(db, **kwargs)
    await make_subscription(db, user, plans["online_ai"])
    return user


async def _today(user: User) -> MetricEntry | None:
    async with dbmod.get_sessionmaker()() as s:
        stmt = select(MetricEntry).where(MetricEntry.customer_id == user.id).order_by(MetricEntry.recorded_on.desc())
        return (await s.execute(stmt.limit(1))).scalars().first()


async def _note_jobs(user: User) -> list[Job]:
    async with dbmod.get_sessionmaker()() as s:
        stmt = select(Job).where(Job.kind == "health_ai_note", Job.user_id == user.id)
        return list((await s.execute(stmt)).scalars().all())


async def _log(client, height: float, weight: float):
    r = await post_form(client, "/me/health/metrics", {"height_cm": str(height), "weight_kg": str(weight),
                                                       "page_id": TAB})
    assert r.status_code == 303, r.text[:300]
    return r


def _note_group() -> str:
    return policy_for(hs.NOTE_FEATURE).group


async def _note_units(user_id: int, window: int = 2) -> int:
    """Units of the note's quota group charged (day window by default: a minute boundary never resets it)."""
    return (await guard.status(user_id))["usage"][_note_group()][window]


def _features(monkeypatch) -> list[str]:
    """Every ``ai.complete`` feature, in call order (the real router still answers)."""
    seen: list[str] = []
    real = ai.complete

    async def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs.get("feature"))
        return await real(*args, **kwargs)

    monkeypatch.setattr(ai, "complete", spy)
    return seen


async def outcome(owner: AIOwner, text: str) -> Any:
    with owner_scope(owner):
        try:
            return await tts.synthesize_or_refuse(text)
        except AIRejected as exc:
            return exc


async def say_as(owner: AIOwner | None, text: str) -> bytes | None:
    with owner_scope(owner):
        return await tts.synthesize(text)


# ============================================================================================ health notes
async def test_normal_readings_of_a_member_without_ai_never_reach_the_ai(client, db, fake_ai, monkeypatch):
    """Before: every post queued (or recycled) the health_ai_note job, which asked the AI under a job owner the guard
    never judges - unlimited, uncounted AI calls for any customer, even without an AI plan."""
    provider = fake_ai(handler=lambda **k: NOTE_JSON)
    features = _features(monkeypatch)
    member = await make_customer(db)  # no plan with AI
    await login_as(client, member)
    for i in range(5):
        await _log(client, 175, 70 + i / 10)
        await run_jobs()
    assert provider.prompts == [] and features == []
    assert (await _today(member)).ai_note == hs.deterministic_note(AnomalyLevel.NONE, False)
    assert await _note_jobs(member) == []
    assert all(v == [0, 0, 0] for v in (await guard.status(member.id))["usage"].values())


async def test_an_ai_members_note_is_judged_at_the_click_and_bounded_by_its_quota(client, db, fake_ai, monkeypatch,
                                                                                  steady_minute):
    provider = fake_ai(handler=lambda **k: NOTE_JSON)
    features = _features(monkeypatch)
    member = await _ai_member(db)
    limit = guard._limits("member_ai", _note_group())[0]  # per minute
    assert not policy_for(hs.NOTE_FEATURE).safety  # a friendly note is never the safety review ...
    await login_as(client, member)
    for i in range(limit + 3):
        await _log(client, 175, 70 + i / 10)  # a new value each time
        await run_jobs()
    assert len(provider.prompts) == limit  # ... and only as many notes as the member's quota allows
    assert features == [hs.NOTE_FEATURE] * limit and "health_metrics" not in features
    assert await _note_units(member.id, window=0) == limit
    status = await guard.status(member.id)
    assert status["cooldown_until"] is None
    # a refused note keeps the rules' note and queues nothing: the last reading has no AI note
    assert (await _today(member)).ai_note == hs.deterministic_note(AnomalyLevel.NONE, False)


async def test_the_click_admission_rides_with_the_note_job_and_is_kept_when_the_ai_answers(client, db, fake_ai):
    fake_ai(handler=lambda **k: NOTE_JSON)
    member = await _ai_member(db)
    await login_as(client, member)
    await _log(client, 175, 70)
    await _log(client, 175, 70.2)  # re-logged while the note is queued: the same job, no second charge
    [job] = await _note_jobs(member)
    admission = job.payload["admission"]
    assert (admission["user_id"], admission["feature"], admission["units"], admission["settled"]) == (
        member.id, hs.NOTE_FEATURE, 1, False)
    assert job.status == JobStatus.QUEUED and await _note_units(member.id) == 1
    assert await run_jobs() == 1
    entry = await _today(member)
    assert entry.ai_note == NOTE and entry.weight_kg == 70.2  # the entry's newest values were reviewed
    assert await _note_units(member.id) == 1  # the AI answered: the charge stays ...
    assert await guard.settle_payload(dict(admission), False) is False  # ... settled once, by the job
    assert await _note_units(member.id) == 1


async def test_a_note_that_comes_out_without_the_ai_gives_the_click_back(client, db):
    member = await _ai_member(db)  # ai_down: every provider fails
    await login_as(client, member)
    await _log(client, 175, 70)
    assert await _note_units(member.id) == 1
    assert await run_jobs() == 1
    assert (await _today(member)).ai_note == hs.deterministic_note(AnomalyLevel.NONE, False)
    assert await _note_units(member.id) == 0  # refunded: no AI note came of the click


async def test_a_membership_without_ai_by_the_time_the_note_runs_gets_no_ai_and_is_refunded(client, db, fake_ai):
    provider = fake_ai(handler=lambda **k: NOTE_JSON)
    member = await _ai_member(db)
    await login_as(client, member)
    await _log(client, 175, 70)
    assert await _note_units(member.id) == 1
    async with dbmod.get_sessionmaker()() as s:  # the membership ends before the job runs
        from app.models import Subscription
        from app.models.enums import SubscriptionStatus

        for sub in (await s.execute(select(Subscription).where(Subscription.customer_id == member.id))).scalars():
            sub.status = SubscriptionStatus.EXPIRED
        await s.commit()
    assert await run_jobs() == 1
    assert provider.prompts == [] and await _note_units(member.id) == 0


async def test_the_note_job_holds_no_transaction_while_the_ai_thinks(client, db, fake_ai, pool_probe):
    seen: list[int] = []

    async def model(**kwargs: Any) -> str:
        seen.append(pool_probe.checked_out)
        return NOTE_JSON

    fake_ai(handler=model)
    member = await _ai_member(db)
    await login_as(client, member)
    await _log(client, 175, 70)
    await db.close()  # the test's own session holds nothing
    assert await run_jobs() == 1
    assert seen == [0]  # before: 1 - the job's read transaction stayed open (and pooled) across the AI call
    assert (await _today(member)).ai_note == NOTE


async def test_a_note_for_values_logged_again_while_the_ai_was_busy_is_dropped(client, db, fake_ai):
    member = await _ai_member(db)
    await login_as(client, member)
    await _log(client, 175, 70)

    async def model(**kwargs: Any) -> str:  # the member logs a new value while the AI words the note
        async with dbmod.get_sessionmaker()() as s:
            entry = (await s.execute(select(MetricEntry).where(MetricEntry.customer_id == member.id))).scalar_one()
            entry.weight_kg = 71.5
            entry.ai_note = "rules note for 71.5"
            await s.commit()
        return NOTE_JSON

    fake_ai(handler=model)
    assert await run_jobs() == 1
    entry = await _today(member)
    assert entry.weight_kg == 71.5 and entry.ai_note == "rules note for 71.5"  # never a note about 70 kg
    assert await _note_units(member.id) == 0  # no AI note came of the click


# ============================================================================================ coach voice (TTS)
LINE = "Session ended. Nice work today, Rohan - see you next time!"


async def test_a_member_cannot_tell_which_lines_another_member_heard(db, speech, monkeypatch):
    """Before: cache hits (and syntheses under way) were served to everyone before the guard judged the request, even
    to a refused member - a 200 (someone heard this line) versus a 429 told a paused or over-quota member which
    lines, with first names, other members had just heard; GET /api/tts/status counted the cache too."""
    monkeypatch.setattr(get_settings(), "ai_quota_scale", 1 / 600)  # 1 new voice line per minute and hour
    fake = speech()
    victim, attacker = await make_customer(db, full_name="Rohan Iyer"), await make_customer(db, full_name="Mal Lory")
    async with make_client() as v:  # the victim's live coach says goodbye
        await login_as(v, victim)
        assert (await api(v, "POST", "/api/tts", json={"text": LINE})).status_code == 200
    async with make_client() as m:
        await login_as(m, attacker)
        assert (await api(m, "POST", "/api/tts", json={"text": "Warm up."})).status_code == 200  # uses the quota
        status = (await api(m, "GET", "/api/tts/status")).json()
        heard = await api(m, "POST", "/api/tts", json={"text": LINE})
        unheard = await api(m, "POST", "/api/tts", json={"text": LINE.replace("Rohan", "Priya")})
    assert "cached" not in status
    assert (heard.status_code, heard.json()["code"]) == (unheard.status_code, unheard.json()["code"]) == (429, "quota")
    assert "cached" not in heard.json()["tts"]
    assert [c["input"] for c in fake.calls] == [LINE, "Warm up."]  # the probes never reached Kokoro


async def test_members_never_share_a_cached_line_or_a_synthesis(speech):
    fake = speech()
    a, b = page_owner(1), page_owner(2)
    assert await say_as(a, LINE) == MP3
    assert await say_as(b, LINE) == MP3  # another member: a line of their own, synthesised (and charged) for them
    assert fake.started(LINE) == 2 and tts.stats.hits == 0
    fake.hold("Last set!")
    first = asyncio.create_task(say_as(a, "Last set!"))
    await wait_until(lambda: fake.started("Last set!") == 1, what="a's synthesis")
    second = asyncio.create_task(say_as(b, "Last set!"))
    await wait_until(lambda: fake.started("Last set!") == 2, what="b's own synthesis")
    assert tts.stats.joined == 0
    fake.release("Last set!")
    assert await asyncio.gather(first, second) == [MP3, MP3]
    # the same person's other tab still uses their own cached line (free)
    other_tab = AIOwner(user_id=a.user_id, role="customer", session_hash="session-1", page_id="tts-page-other-tab")
    assert await say_as(other_tab, LINE) == MP3 and fake.started(LINE) == 2 and tts.stats.hits == 1


async def test_the_voice_status_shows_members_no_cache_count(client, db, speech):
    speech()
    member = await make_customer(db)
    await login_as(client, member)
    assert (await api(client, "POST", "/api/tts", json={"text": "Shoulders down."})).status_code == 200
    body = (await client.get("/api/tts/status")).json()
    assert "cached" not in body and body["state"] == "up"
    assert "cached" not in tts.status() and tts.status(admin=True)["cached"] == 1  # the admin panel keeps it


async def test_a_synthesis_never_hands_one_member_another_members_refusal(speech, monkeypatch):
    """Before: member B (AI paused, at the voice-request cap) joined member A's queued synthesis of the same line for
    free; A's ticket was then stopped (A signed out in another tab) and the synthesis moved to B's ticket, which the
    queue refused - and A, the creator, got B's pause (reason and length) as a 429."""
    monkeypatch.setattr(get_settings(), "ai_tts_concurrency", 1)
    monkeypatch.setattr(get_settings(), "ai_queue_max_per_user", 1)
    fake = speech()
    fake.hold("Busy phrase", "B's own other line")
    a, b, c = page_owner(1), page_owner(2), page_owner(9)
    busy = asyncio.create_task(outcome(c, "Busy phrase"))  # someone fills the only voice slot
    await wait_until(lambda: fake.started("Busy phrase") == 1, what="the lane to fill")
    b_other = asyncio.create_task(outcome(b, "B's own other line"))
    await wait_until(lambda: tts_lane()["queued"] == 1, what="B's ticket to wait")
    for _ in range(2):  # B's AI is paused (two spam refusals elsewhere)
        with pytest.raises(AIRejected):
            await guard.admit(b, "assistant", "!" * 60)
    a_req = asyncio.create_task(outcome(a, "Get ready for the plank."))
    await wait_until(lambda: tts_lane()["queued"] == 2, what="A's ticket to wait")
    b_req = asyncio.create_task(outcome(b, "Get ready for the plank."))
    await wait_until(lambda: b_req.done() or tts.stats.joined == 1, what="B's request")
    assert scheduler.cancel(page_id=a.page_id, user_id=a.user_id, kinds={"user"}, reason="logout") == 1
    a_result, b_result = await asyncio.wait_for(asyncio.gather(a_req, b_req), 5)
    fake.release()
    await asyncio.gather(busy, b_other)
    assert a_result is None  # A's tab is gone: the browser voice - never B's pause
    assert isinstance(b_result, AIRejected) and b_result.code == "cooldown"  # B's refusal is B's
    status = await guard.status(a.user_id)
    assert status["strikes"] == 0 and status["cooldown_until"] is None


async def test_a_shared_synthesis_refusal_reaches_only_the_member_it_belongs_to(speech, monkeypatch):
    """Belt and braces below the per-member keys: if one synthesis ever has waiters of two members, a refusal of one
    member's ticket goes to that member's requests only - never by who started the synthesis."""
    speech()
    a, b = page_owner(1), page_owner(2)
    joined = asyncio.Event()

    async def acquire(owner: AIOwner | None, feature: str, **kwargs: Any) -> Any:
        await joined.wait()
        if owner is a:  # the starter's ticket is stopped (page closed) - the synthesis moves to b's ticket
            from app.ai.errors import AICancelled

            raise AICancelled("page_closed")
        raise AIRejected("too_many", "You already have several AI requests in progress.", retry_after=5,
                         feature=feature)

    monkeypatch.setattr(scheduler, "acquire", acquire)
    starter = tts_mod._Waiter(a, creator=True)
    flight = tts._launch("shared-key", "One more rep!", "af_heart", 1.0, starter, None)
    joiner = tts_mod._Waiter(b)
    flight.waiters.append(joiner)
    joined.set()
    got_a, got_b = await asyncio.gather(tts._wait(flight, starter), tts._wait(flight, joiner),
                                        return_exceptions=True)
    assert got_a is None  # before: the starter got b's 'too_many'
    assert isinstance(got_b, AIRejected) and got_b.code == "too_many"  # before: b got the browser voice silently


async def test_an_abandoned_voice_line_gives_its_queue_place_back_at_once(db, speech, monkeypatch):
    """The coach page interrupts a line (a newer one replaces it, or the page moves on) by aborting its fetch. Before,
    the server kept the line's ticket in the voice lane until it was synthesised for nobody (or waited out its 8 s):
    later lines were refused as 'too_many' and earned strikes - a member talking to the coach got their AI paused."""
    monkeypatch.setattr(get_settings(), "ai_tts_concurrency", 1)
    monkeypatch.setattr(get_settings(), "ai_queue_max_per_user", 1)
    fake = speech()
    fake.hold("Busy phrase")
    busy = asyncio.create_task(say_as(page_owner(9), "Busy phrase"))  # another member fills the only voice slot
    await wait_until(lambda: fake.started("Busy phrase") == 1, what="the lane to fill")
    member = await make_customer(db)
    gone = asyncio.Event()
    async with droppable_client(gone) as c:
        await login_as(c, member)
        line = asyncio.create_task(api(c, "POST", "/api/tts", json={"text": "Okay, skipping this one."}))
        await wait_until(lambda: tts_lane()["queued"] == 1, what="the line to wait for a slot")
        gone.set()  # the coach page aborts the fetch
        await wait_until(lambda: tts_lane()["queued"] == 0, timeout=3, what="the abandoned line's ticket to go")
        assert (await asyncio.wait_for(line, 5)).status_code == 503  # nobody reads it
    async with make_client() as c2:  # the next line of the same member is not 'too_many'
        await login_as(c2, member)
        nxt = asyncio.create_task(api(c2, "POST", "/api/tts", json={"text": "Next up: lunges."}))
        await wait_until(lambda: tts_lane()["queued"] == 1 or nxt.done(), what="the next line to wait")
        fake.release("Busy phrase")
        r = await asyncio.wait_for(nxt, 5)
    assert r.status_code == 200 and r.content == MP3
    assert await busy == MP3
    await wait_until(lambda: not tts_mod._background, what="the refunds")
    status = await guard.status(member.id)
    assert status["strikes"] == 0 and status["usage"]["voice"][2] == 1  # only the line that was spoken is charged
    assert fake.started("Okay, skipping this one.") == 0


async def test_an_abandoned_voice_line_stops_kokoro_and_earns_no_strike(db, speech):
    """Kokoro stops at once and nothing counts against the member's standing (interrupting a line is ordinary coach
    behaviour). The line is charged, though: Kokoro was already working on it - the rule of ``ai.complete`` (hardening
    round 2, docs section 12), so aborting lines mid-synthesis is no way around the voice quota."""
    fake = speech()
    fake.hold("Hold it there, three more seconds.")
    member = await make_customer(db)
    gone = asyncio.Event()
    async with droppable_client(gone) as c:
        await login_as(c, member)
        line = asyncio.create_task(api(c, "POST", "/api/tts", json={"text": "Hold it there, three more seconds."}))
        await wait_until(lambda: fake.started("Hold it there, three more seconds.") == 1, what="Kokoro to start")
        gone.set()
        await wait_until(lambda: fake.cancelled == ["Hold it there, three more seconds."], timeout=3,
                         what="the synthesis to stop")
        await asyncio.wait_for(line, 5)
    await wait_until(lambda: tts_lane()["running"] == 0 and not tts_mod._background, what="the ticket and settle")
    status = await guard.status(member.id)
    assert status["usage"]["voice"][2] == 1 and status["strikes"] == 0  # Kokoro worked on it: charged, never struck
    assert not tts.is_down and tts.stats.failures == 0  # stopping is not an engine failure


# ===================================================================================== no transaction while AI works
def _fake_ocr(monkeypatch, probe: Any, seen: dict[str, int], text: str = "WEIGHT 72.4 kg") -> None:
    async def run_ocr(image: bytes, *, engines: Any = None, purpose: str = "document") -> Any:
        seen["ocr"] = probe.checked_out
        return ocr.MultiOcrResult(results=[ocr.OcrEngineResult("tesseract", True, text)])

    monkeypatch.setattr(ocr, "run_ocr", run_ocr)


async def test_the_scale_scan_holds_no_transaction_while_ocr_and_the_vision_check_run(client, db, monkeypatch,
                                                                                    fake_ai, pool_probe):
    """OCR (up to 90 s) and the AI vision check (up to 60 s) used to run inside the request's transaction: past the
    60 s idle-in-transaction limit the server killed the connection and the page failed (500)."""
    fake_ai(responses=["{}"])
    seen: dict[str, int] = {}
    _fake_ocr(monkeypatch, pool_probe, seen)

    async def vision(image: bytes) -> Any:
        seen["vision"] = pool_probe.checked_out
        return health_ai.MeasurementReading(weight=72.4, weight_unit="kg"), "gateway"

    monkeypatch.setattr(health_ai, "read_measurement_photo", vision)
    member = await make_customer(db)
    await login_as(client, member)
    await db.close()
    r = await post_form(client, "/me/health/scan", {"page_id": TAB},
                        files={"photo": ("scale.png", png_bytes((200, 120)), "image/png")})
    assert r.status_code == 200 and 'value="72.4"' in r.text
    assert seen == {"ocr": 0, "vision": 0}


async def test_the_wellness_selfie_read_out_holds_no_transaction(client, db, monkeypatch, pool_probe):
    seen: list[int] = []

    async def analyse(image: bytes, *, minor: bool = False) -> Any:
        seen.append(pool_probe.checked_out)
        return wellness_ai.FaceAnalysis(observations=["Eyes look a little red"]), "gateway"

    monkeypatch.setattr(wellness_ai, "analyse_face", analyse)
    member = await make_customer(db)
    await login_as(client, member)
    await db.close()
    r = await post_form(client, "/me/wellness", {"q1": "1", "q2": "3", "q3": "3", "q4": "1", "symptoms": ["none"],
                                                 "page_id": TAB},
                        files={"photo": ("selfie.png", png_bytes((160, 160), "#d9b99b"), "image/png")})
    assert r.status_code == 303
    assert seen == [0]
    assert "Check-in saved." in html.unescape((await client.get(r.headers["location"])).text)


async def test_the_coach_plan_job_holds_no_transaction_while_the_ai_thinks(client, db, fake_ai, pool_probe):
    from tests.test_coach import GOOD_PLAN

    seen: list[int] = []

    async def model(**kwargs: Any) -> str:
        seen.append(pool_probe.checked_out)
        return GOOD_PLAN

    fake_ai(handler=model)
    member = await _ai_member(db)
    await login_as(client, member)
    r = await post_form(client, "/me/coach/generate")
    assert r.status_code == 303
    await db.close()
    assert await run_jobs() == 1
    assert seen == [0]  # before: 1 - plan_job's read transaction stayed open (and pooled) across the AI call
    async with dbmod.get_sessionmaker()() as s:
        from app.models import WorkoutPlan

        plan = (await s.execute(select(WorkoutPlan).where(WorkoutPlan.customer_id == member.id))).scalar_one()
    assert plan.source == "ai"


# ===================================================================================== staff certificate re-check
CERT_TEXT = "PARTICIPATION AWARD"


async def _trainer_with_certificates(db, n: int, *, files: bool = False) -> tuple[User, Any]:
    """An invited trainer record (no linked account) with ``n`` certificates whose holder name was not found."""
    from app.models import EmployeeDocument
    from app.models.enums import EmploymentStatus, VerificationStatus
    from app.services import storage
    from tests.factories import make_branch, make_employee

    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    await make_branch(db, gym)
    emp = await make_employee(db, gym, status=EmploymentStatus.INVITED)
    emp.verification_status = VerificationStatus.NEEDS_REVIEW
    for i in range(n):
        key, ctype, sha, size = f"staff_docs/missing-{i}.png", "image/png", "0" * 64, 10
        if files:
            stored, _plain = await storage.save_private_file(png_bytes((200, 120), "white", text="X"),
                                                             kind="staff_docs", filename=f"cert{i}.png")
            key, ctype, sha, size = stored.key, stored.content_type, stored.sha256, stored.size
        db.add(EmployeeDocument(employee_id=emp.id, doc_type="certification", file_key=key,
                                original_filename=f"cert{i}.png", content_type=ctype, sha256=sha, size_bytes=size,
                                ocr_text=CERT_TEXT,
                                assessment={"name_match": False, "method": "ai", "ai": {"ai": "ok"}}))
    await db.commit()
    return owner, emp


async def _certificates(s, employee_id: int) -> list[Any]:
    from app.models import EmployeeDocument

    stmt = select(EmployeeDocument).where(EmployeeDocument.employee_id == employee_id).order_by(EmployeeDocument.id)
    return list((await s.execute(stmt.execution_options(populate_existing=True))).scalars().all())


async def _reread(employee_id: int, **kwargs: Any) -> tuple[ocr.Reread, list[Any]]:
    """One "read the certificates again" request, as the owner's re-check route makes it (then commits)."""
    async with dbmod.get_sessionmaker()() as s:
        docs = await _certificates(s, employee_id)
        unmatched = [d for d in docs if not (d.assessment or {}).get("name_match")]
        result = await ocr.reread_documents(unmatched, noun="certificate", **kwargs)
        await s.commit()
        return result, await _certificates(s, employee_id)


async def test_a_certificate_reread_is_bounded_per_record_and_per_certificate(db):
    """Before: every re-check click cleared the OCR text of every unmatched certificate (up to 40 per record) and
    queued the check again - OCR plus an AI vision call per certificate per click, without any limit."""
    from datetime import timedelta

    from app.core.utils import utcnow

    _owner, emp = await _trainer_with_certificates(db, 5)
    t0 = utcnow()
    first, docs = await _reread(emp.id, now=t0)
    newest = [d.id for d in docs][-ocr.REREAD_BATCH:]
    assert first.refusal is None and sorted(d.id for d in first.documents) == newest  # a batch, the newest first
    for d in docs:
        if d.id in newest:
            assert d.ocr_text is None and d.assessment["rereads"] == 1 and "ai" not in d.assessment
            assert d.assessment["reread_at"] == t0.isoformat() and d.assessment["name_match"] is False
        else:
            assert d.ocr_text == CERT_TEXT and "rereads" not in d.assessment
    # asking again at once: refused - nothing changes
    again, after = await _reread(emp.id, now=t0 + timedelta(minutes=3))
    assert again.code == "cooldown" and again.documents == [] and again.retry_after == 7 * 60
    assert again.refusal == "These certificates were read again 3 min ago. You can ask again in 7 min."
    assert [(d.ocr_text, d.assessment) for d in after] == [(d.ocr_text, d.assessment) for d in docs]
    # every 10 minutes one more batch - until each certificate was read again REREAD_LIMIT times
    reread_ids: list[list[int]] = []
    now = t0
    for _round in range(3 * ocr.REREAD_LIMIT):  # bounded: without the limits it would go on for ever
        async with dbmod.get_sessionmaker()() as s:  # the check read them meanwhile (their text is back)
            for d in await _certificates(s, emp.id):
                d.ocr_text = d.ocr_text or CERT_TEXT
            await s.commit()
        now += timedelta(minutes=11)
        result, _docs = await _reread(emp.id, now=now)
        if result.refusal is not None:
            break
        reread_ids.append(sorted(d.id for d in result.documents))
    ids = [d.id for d in docs]
    assert reread_ids == [ids[2:]] * 2 + [ids[:2]] * 3  # the newest three once more, then the older two
    assert result.code == "limit" and result.retry_after == 0
    assert "can be read again at most 3 times" in result.refusal and "approve it yourself" in result.refusal
    async with dbmod.get_sessionmaker()() as s:
        assert {d.assessment["rereads"] for d in await _certificates(s, emp.id)} == {ocr.REREAD_LIMIT}


async def test_a_certificate_reread_is_charged_to_the_person_who_asks(db, steady_minute):
    owner, emp = await _trainer_with_certificates(db, 5)
    who = AIOwner(user_id=owner.id, role="owner")
    first, _docs = await _reread(emp.id, owner=who)
    assert first.refusal is None and len(first.documents) == ocr.REREAD_BATCH
    usage = (await guard.status(owner.id))["usage"][policy_for(ocr.REREAD_FEATURE).group]
    assert usage[0] == ocr.REREAD_BATCH  # one 'vision' unit per certificate read again (OCR + AI vision check)


async def test_a_reread_the_ai_guard_refuses_changes_nothing_and_costs_nothing(db, monkeypatch, steady_minute):
    monkeypatch.setattr(get_settings(), "ai_quota_scale", 2 / 6)  # an owner's 'vision' quota: 2 units a minute
    owner, emp = await _trainer_with_certificates(db, 5)
    who = AIOwner(user_id=owner.id, role="owner")
    async with dbmod.get_sessionmaker()() as s:
        before = [(d.ocr_text, d.assessment) for d in await _certificates(s, emp.id)]
    refused, after = await _reread(emp.id, owner=who)
    assert refused.code == "quota" and refused.documents == [] and 0 < refused.retry_after <= 60
    assert refused.refusal.startswith("You've used your AI photo checks for this minute.")
    assert [(d.ocr_text, d.assessment) for d in after] == before
    group = policy_for(ocr.REREAD_FEATURE).group
    assert (await guard.status(owner.id))["usage"][group] == [0, 0, 0]  # the two units it took were given back


async def test_repeated_rechecks_read_the_certificates_again_only_within_the_bounds(db, monkeypatch, fake_ai):
    """The re-check flow end to end (the route's call, the background activation check with its OCR): six clicks in a
    row read the newest certificates again once - the other five are refused before anything is cleared or queued."""
    from app.services.jobs import enqueue

    ocr_calls: list[str] = []

    async def ocr_document(data: bytes, content_type: str, *, max_pages: int = 3, engines: Any = None) -> Any:
        ocr_calls.append(content_type)
        return ocr.MultiOcrResult([ocr.OcrEngineResult("tesseract", True, CERT_TEXT, 0.9)])

    monkeypatch.setattr(ocr, "ocr_document", ocr_document)
    fake_ai(responses=[json.dumps({"is_certificate": False, "name_on_document": None, "name_matches": False,
                                   "confidence": 0.1})])
    owner, emp = await _trainer_with_certificates(db, 10, files=True)
    who = AIOwner(user_id=owner.id, role="owner")
    queued = 0
    for _click in range(6):
        result, _docs = await _reread(emp.id, owner=who)
        if result.refusal is None:
            async with dbmod.get_sessionmaker()() as s:
                await enqueue(s, "staff_activation_check", {"employee_id": emp.id},
                              dedupe_key=f"staff_activation_check:{emp.id}")
                await s.commit()
            queued += 1
        await run_jobs()
    assert queued == 1 and len(ocr_calls) == ocr.REREAD_BATCH


async def test_the_owner_recheck_route_reads_certificates_again_only_within_the_bounds(client, db, monkeypatch,
                                                                                     fake_ai, steady_minute):
    """POST /owner/staff/{id}/recheck (the call site of ``reread_documents``). Before: each click cleared the OCR text
    of every unmatched certificate and queued the check - six clicks on ten certificates read them 60 times (OCR plus
    an AI vision call each), never judged, never counted."""
    ocr_calls: list[str] = []

    async def ocr_document(data: bytes, content_type: str, *, max_pages: int = 3, engines: Any = None) -> Any:
        ocr_calls.append(content_type)
        return ocr.MultiOcrResult([ocr.OcrEngineResult("tesseract", True, CERT_TEXT, 0.9)])

    monkeypatch.setattr(ocr, "ocr_document", ocr_document)
    fake_ai(responses=[json.dumps({"is_certificate": False, "name_on_document": None, "name_matches": False,
                                   "confidence": 0.1})])
    owner, emp = await _trainer_with_certificates(db, 10, files=True)
    await login_as(client, owner)
    for _click in range(6):
        r = await post_form(client, f"/owner/staff/{emp.id}/recheck")
        assert r.status_code in (302, 303) and r.headers["location"] == f"/owner/staff/{emp.id}"
        await run_jobs()
    assert len(ocr_calls) == ocr.REREAD_BATCH  # one bounded re-read; the other five clicks were refused
    page = html.unescape((await client.get(f"/owner/staff/{emp.id}")).text)
    assert "These certificates were read again" in page and "You can ask again in" in page
    usage = (await guard.status(owner.id))["usage"][policy_for(ocr.REREAD_FEATURE).group]
    assert usage[2] == ocr.REREAD_BATCH  # charged to the owner who asked: one 'vision' unit per certificate

