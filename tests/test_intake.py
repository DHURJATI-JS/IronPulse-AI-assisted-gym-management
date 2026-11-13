"""WP-F AI intake: free text -> validated registration prefill (AI via fake_ai, deterministic fallback via ai_down),
and the AI guard / AI queue (docs/ai-guard-and-queue.md): refusals answer with the contract JSON (never the regex
fallback), a full AI queue keeps the regex fallback, the owner's note reaches the guard, an owner who leaves stops it,
and no pooled DB connection is held while the owner waits for the AI."""

from __future__ import annotations

import asyncio
import json
from datetime import date

import pytest

from app.ai import policy
from app.ai.client import AI_DISCLAIMER
from app.ai.errors import AIRejected
from app.ai.guard import guard
from app.ai.intake import BUSY_NOTE, FALLBACK_NOTE, IntakeExtraction, postprocess, regex_extract
from app.config import get_settings
from app.services import intake as svc
from tests.ask_ai_helpers import (  # noqa: F401 - pool_probe and steady_minute are fixtures
    connections_while_the_ai_works,
    pool_probe,
    steady_minute,
)
from tests.conftest import api, login_as, post_form
from tests.droppable_client import droppable_client
from tests.factories import make_customer, make_gym, make_owner, png_bytes

SAMPLE = ("this customer's name is xyz and they suffer from allergies asthama and here is their contact number "
          "00000000")
RICH = ("New member: Riya Kapoor, born 12 March 2010, email riya.k@example.com, phone +91 98765 43210. "
        "She is allergic to peanuts and has asthma. Mother Priya Kapoor 98111 22233, priya@example.com. "
        "Wants to lose weight. Lives in Bengaluru.")


async def owner_client(client, db):
    owner = await make_owner(db)
    await make_gym(db, owner)
    await login_as(client, owner)
    return owner


# ====================================================================== AI available (fake provider)
async def test_ai_extraction_is_validated_and_returned(client, db, fake_ai):
    provider = fake_ai(responses=[json.dumps({
        "full_name": "Riya Kapoor", "email": "riya.k@example.com", "phone": "+91 98765 43210",
        "date_of_birth": "2010-03-12", "age": None, "gender": "female", "city": "Bengaluru", "goal": "weight_loss",
        "conditions": ["asthma"], "allergies": ["peanuts"], "medications": [], "injuries": [],
        "guardian": {"name": "Priya Kapoor", "phone": "98111 22233", "email": "priya@example.com", "relation": "mother"},
        "emergency_contact": None, "notes": None, "missing_fields": [],
    })])
    await owner_client(client, db)
    r = await api(client, "POST", "/owner/customers/intake", json={"text": RICH})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["source"] == "ai" and data["ai_available"] is True and data["provider"] == "Fake (gateway)"
    assert data["disclaimer"] == AI_DISCLAIMER
    f = data["fields"]
    assert f["full_name"] == "Riya Kapoor" and f["email"] == "riya.k@example.com" and f["phone"] == "+91 98765 43210"
    assert f["date_of_birth"] == "2010-03-12" and f["goal"] == "weight_loss"
    # the note never states a gender ("she" is not used to guess one) -> the model's value is dropped
    assert "gender" not in f and [d["field"] for d in data["dropped"]] == ["gender"]
    assert f["city"] == "Bengaluru" and f["conditions"] == "Asthma" and f["allergies"] == "Peanuts"
    assert f["guardian_name"] == "Priya Kapoor" and f["guardian_relation"] == "Mother"
    assert f["guardian_email"] == "priya@example.com"
    assert data["minor"] is True and data["invalid"] == {}
    assert data["missing"] == ["id_front"]  # the ID card always has to be uploaded
    # the note is passed as data, inside tags, with a prompt-injection warning
    sent = provider.prompts[0]
    assert "<note>" in sent["prompt"] and RICH in sent["prompt"] and "untrusted" in sent["system"]
    assert sent["max_tokens"] <= 1500


async def test_invented_values_are_dropped(client, db, fake_ai):
    fake_ai(responses=[json.dumps({
        "full_name": "Xyz Sharma", "email": "xyz@@invalid", "phone": "+91 99999 12345", "date_of_birth": "1990-01-01",
        "city": "Mumbai", "goal": "bulking", "conditions": ["asthma", "diabetes"], "allergies": ["allergies"],
        "guardian": {"name": "Ravi", "phone": None, "email": "ravi@example.com", "relation": "father"},
    })])
    await owner_client(client, db)
    r = await api(client, "POST", "/owner/customers/intake", json={"text": SAMPLE})
    data = r.json()
    dropped = {d["field"] for d in data["dropped"]}
    assert {"full_name", "email", "phone", "date_of_birth", "city", "goal", "guardian_name", "guardian_email"} <= dropped
    assert "email" not in data["fields"] and "Mumbai" not in json.dumps(data["fields"])
    # deterministic findings fill what the model got wrong
    assert data["fields"]["full_name"] == "Xyz"
    assert data["lists"]["conditions"] == ["Asthma"]  # "diabetes" was never mentioned
    assert {d["value"] for d in data["dropped"] if d["field"] == "conditions"} == {"Diabetes"}
    assert data["fields"]["phone"] == "00000000" and "phone" in data["invalid"]


async def test_invalid_email_in_note_is_flagged_not_filled_silently(client, db, fake_ai):
    note = "Name is Kabir Rao, email kabir@@mail, 30 years old, no health issues."
    fake_ai(responses=[json.dumps({"full_name": "Kabir Rao", "email": "kabir@@mail", "age": 30})])
    await owner_client(client, db)
    data = (await api(client, "POST", "/owner/customers/intake", json={"text": note})).json()
    assert "email" in data["invalid"] and "email" in data["missing"] and "phone" in data["missing"]
    assert data["age"] == 30 and data["minor"] is False and "date_of_birth" in data["missing"]
    assert data["fields"]["full_name"] == "Kabir Rao"


# ====================================================================== AI unavailable -> deterministic fallback
async def test_ai_down_fallback_on_exact_sample_sentence(client, db, ai_down):
    await owner_client(client, db)
    r = await api(client, "POST", "/owner/customers/intake", json={"text": SAMPLE})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["source"] == "fallback" and data["ai_available"] is False and data["provider"] is None
    assert data["disclaimer"] == AI_DISCLAIMER
    assert data["fields"]["full_name"].lower() == "xyz"
    assert data["lists"]["conditions"] == ["Asthma"]  # "asthama" corrected
    assert data["lists"]["allergies"] and "allerg" in data["lists"]["allergies"][0].lower()
    assert "phone" in data["invalid"]  # 00000000 is not a usable phone number
    assert "email" in data["missing"] and "phone" in data["missing"] and "date_of_birth" in data["missing"]
    assert "id_front" in data["missing"]
    assert any("AI is unavailable" in n for n in data["notes"])
    assert data["notes"][0] == FALLBACK_NOTE and data["busy"] is False  # offline - not a full AI queue


async def test_ai_down_rich_note(client, db, ai_down):
    await owner_client(client, db)
    data = (await api(client, "POST", "/owner/customers/intake", json={"text": RICH})).json()
    f = data["fields"]
    assert f["full_name"] == "Riya Kapoor" and f["email"] == "riya.k@example.com" and f["phone"] == "+91 98765 43210"
    assert f["date_of_birth"] == "2010-03-12" and f["goal"] == "weight_loss" and f["city"] == "Bengaluru"
    assert data["lists"]["allergies"] == ["Peanuts"] and data["lists"]["conditions"] == ["Asthma"]
    assert f["guardian_name"] == "Priya Kapoor" and f["guardian_relation"] == "Mother"
    assert f["guardian_phone"] == "98111 22233" and f["guardian_email"] == "priya@example.com"
    assert data["minor"] is True and data["missing"] == ["id_front"]


# ====================================================================== input validation / auth / rate limit
async def test_intake_validation_and_permissions(client, db, ai_down):
    await owner_client(client, db)
    for payload in ({"text": "short"}, {"text": "x" * 4001}, {"text": 12}, {"note": SAMPLE}, [SAMPLE]):
        r = await api(client, "POST", "/owner/customers/intake", json=payload)
        assert r.status_code == 400, payload
    r = await client.post("/owner/customers/intake", content=b"{not json",
                          headers={"X-CSRF-Token": (await api(client, "GET", "/healthz")).request.headers["X-CSRF-Token"],
                                   "Content-Type": "application/json"})
    assert r.status_code == 400
    customer = await make_customer(db)
    from tests.conftest import make_client

    async with make_client() as c:
        await login_as(c, customer)
        assert (await api(c, "POST", "/owner/customers/intake", json={"text": SAMPLE})).status_code == 403
    async with make_client() as anon:
        r = await api(anon, "POST", "/owner/customers/intake", json={"text": SAMPLE})
        assert r.status_code == 401


async def test_intake_uses_ai_rate_limit_bucket(anon_client_factory, db, ai_down, monkeypatch):
    from app.config import get_settings
    from app.security.ratelimit import bucket_for

    assert bucket_for("POST", "/owner/customers/intake", "application/json") == "ai"
    monkeypatch.setattr(get_settings(), "rate_limit_ai_per_minute", 2)
    owner = await make_owner(db)
    await make_gym(db, owner)
    c = anon_client_factory("203.0.113.77")
    await login_as(c, owner)
    codes = [(await api(c, "POST", "/owner/customers/intake", json={"text": SAMPLE})).status_code for _ in range(8)]
    assert codes[0] == 200 and 429 in codes


# ====================================================================== deterministic helpers
def test_regex_extract_handles_misspellings_ages_and_guardians():
    ex = regex_extract("Name: Arjun Mehta, 15 yrs old, has alergies to dust and diabeties; dad: Raj Mehta 99887 76655")
    assert ex.full_name == "Arjun Mehta" and ex.age == 15
    assert ex.allergies == ["Dust"] and ex.conditions == ["Diabetes"]
    assert ex.guardian is not None and ex.guardian.name == "Raj Mehta" and ex.guardian.relation == "Father"
    assert ex.guardian.phone == "99887 76655"
    p = postprocess(ex, "Name: Arjun Mehta, 15 yrs old, has alergies to dust and diabeties; dad: Raj Mehta 99887 76655")
    assert p.minor is True and "guardian_name" not in p.missing and "date_of_birth" in p.missing


def test_postprocess_never_trusts_unstated_values():
    note = "Register Sana Iqbal, knee injury, phone 98450 12345"
    ex = IntakeExtraction(full_name="Sana Iqbal", phone="98450 12345", email="sana@gmail.com", age=40,
                          gender="female", injuries=["knee injury", "shoulder injury"], conditions=["hypertension"])
    p = postprocess(ex, note, today=date(2026, 11, 10))
    assert p.fields["full_name"] == "Sana Iqbal" and p.fields["phone"] == "98450 12345"
    assert "email" not in p.fields and p.age is None and "gender" not in p.fields
    assert p.lists["injuries"] == ["Knee injury"] and p.lists["conditions"] == []
    assert {d["field"] for d in p.dropped} >= {"email", "age", "gender", "injuries", "conditions"}


def test_spelling_and_list_helpers():
    assert svc.fix_spelling("asthama and alergies") == "asthma and allergies"
    assert svc.split_list("asthma, high BP and dust allergy; none") == ["asthma", "high BP", "dust allergy"]
    items, err = svc.clean_items("asthama, Asthma, diabeties", svc.LIST_FIELDS["conditions"])
    assert items == ["Asthma", "Diabetes"] and err is None
    assert svc.validate_phone("00000000", "IN")[2] is not None
    assert svc.validate_phone("98765 43210", "IN")[1] == "+919876543210"
    assert svc.map_goal("I want to lose weight") == "weight_loss" and svc.map_relation("mom") == "Mother"


# ====================================================================== end-to-end: intake -> review -> save
async def test_intake_prefill_can_be_saved(client, db, ai_down):
    from sqlalchemy import select

    from app.models import CustomerProfile, HealthCondition, User

    owner = await owner_client(client, db)
    data = (await api(client, "POST", "/owner/customers/intake", json={"text": RICH})).json()
    form = {k: v for k, v in data["fields"].items() if v}
    form.update({"intake_source": "owner_ai", "country_code": "IN", "id_doc_type": "aadhaar", "id_country": "IN",
                 "consent": "1"})
    r = await post_form(client, "/owner/customers/new", form,
                        files={"id_front": ("id.png", png_bytes(text="ID"), "image/png")})
    assert r.status_code == 303, r.text[-2000:]
    user = (await db.execute(select(User).where(User.email == "riya.k@example.com"))).scalar_one()
    prof = await db.get(CustomerProfile, user.id)
    assert prof.intake_source == "owner_ai" and prof.registered_by_id == owner.id and prof.guardian_name == "Priya Kapoor"
    rows = (await db.execute(select(HealthCondition).where(HealthCondition.customer_id == user.id))).scalars().all()
    assert {(r_.name, r_.tag, r_.source) for r_ in rows} == {("Asthma", "asthma", "ai_intake"),
                                                           ("Peanuts", "food_allergy", "ai_intake")}


# ====================================================================== AI guard + AI queue (docs/ai-guard-and-queue.md)
INTAKE = "/owner/customers/intake"
PAGE = "intake-tab-00001"
REFUSAL_KEYS = {"error", "detail", "code", "retry_after", "ai_available"}
NOTES = (  # distinct notes: the duplicate check must not be what refuses them
    "Name is Kabir Rao, 30 years old, phone 98450 12345, has asthma.",
    "Register Sana Iqbal, knee injury, phone 98450 54321, wants to lose weight.",
    "New member: Arjun Mehta, 15 yrs old, allergic to dust; dad Raj Mehta 99887 76655.",
    "Customer name is Leela Nair, email leela@example.com, high blood pressure.",
)
EXTRACTION = json.dumps({"full_name": "Kabir Rao", "age": 30})


@pytest.fixture
def guard_on(monkeypatch):
    """The guard as configured by default, whatever the local .env says."""
    monkeypatch.setattr(get_settings(), "ai_guard_enabled", True)
    monkeypatch.setattr(get_settings(), "ai_quota_scale", 1.0)


def assert_refusal(resp, status: int, code: str) -> dict:
    """The contract's refusal JSON (docs section 7) + a Retry-After header matching retry_after when there is a wait."""
    assert resp.status_code == status, resp.text
    assert resp.headers["content-type"].startswith("application/json")
    body = resp.json()
    assert set(body) == REFUSAL_KEYS, body
    assert body["code"] == code and body["ai_available"] is True and body["error"] == AIRejected.TITLES[code]
    assert isinstance(body["detail"], str) and body["detail"]
    if body["retry_after"]:
        assert resp.headers["Retry-After"] == str(body["retry_after"])
    else:
        assert "Retry-After" not in resp.headers
    assert resp.headers["Cache-Control"] == "no-store"
    return body


async def test_intake_quota_refusal_is_contract_json_and_escalates_to_a_cooldown(client, db, fake_ai, guard_on,
                                                                                  monkeypatch, steady_minute):
    monkeypatch.setitem(policy.QUOTAS["owner"], "gen", (2, 60, 300))  # small per-minute limit for the owner tier
    provider = fake_ai(responses=[EXTRACTION])
    owner = await owner_client(client, db)
    for note in NOTES[:2]:
        r = await api(client, "POST", INTAKE, json={"text": note})
        assert r.status_code == 200 and r.json()["source"] == "ai"
    r = await api(client, "POST", INTAKE, json={"text": NOTES[2]})
    body = assert_refusal(r, 429, "quota")
    assert 0 < body["retry_after"] <= 60 and "AI generations" in body["detail"] and "minute" in body["detail"]
    # never the regex fallback instead: no prefill at all
    assert "fields" not in body and "source" not in body
    # always JSON - even for a client that would rather have HTML
    r = await api(client, "POST", INTAKE, json={"text": NOTES[3]}, headers={"Accept": "text/html"})
    assert_refusal(r, 429, "quota")
    # an owner who keeps hammering pauses their own AI
    codes = []
    for i in range(10):
        r = await api(client, "POST", INTAKE, json={"text": f"{NOTES[0]} Attempt number {i}."})
        codes.append(r.json()["code"])
        if codes[-1] == "cooldown":
            break
    assert codes[-1] == "cooldown" and set(codes[:-1]) <= {"quota"}, codes
    body = assert_refusal(r, 429, "cooldown")
    assert body["retry_after"] > 60 and "paused" in body["detail"]
    assert len(provider.prompts) == 2  # none of the refused notes reached the AI
    status = await guard.status(owner.id)
    assert status["usage"]["gen"][0] == 2 and status["cooldown_until"]


async def test_intake_duplicate_and_spam_are_refused_before_any_ai_work(client, db, fake_ai, guard_on):
    provider = fake_ai(responses=[EXTRACTION])
    owner = await owner_client(client, db)
    assert (await api(client, "POST", INTAKE, json={"text": RICH})).status_code == 200
    # the same note again (case and surrounding spaces do not matter) within the feature's dedupe window
    again = await api(client, "POST", INTAKE, json={"text": "  " + RICH.upper() + "  "})
    body = assert_refusal(again, 409, "duplicate")
    assert 0 < body["retry_after"] <= policy.policy_for("intake").dedupe_seconds
    spam = await api(client, "POST", INTAKE, json={"text": "Name is Spam Bot, phone " + "9" * 40})
    body = assert_refusal(spam, 422, "spam")
    assert body["retry_after"] == 0 and "spam" in body["detail"]
    assert len(provider.prompts) == 1
    status = await guard.status(owner.id)
    assert status["usage"]["gen"][0] == 1 and status["strikes"] == policy.STRIKE_POINTS["spam"]  # duplicates: free


async def test_intake_falls_back_to_the_parser_when_the_ai_queue_is_full(client, db, fake_ai, guard_on, monkeypatch):
    """'busy' is not the owner's fault: the deterministic prefill, labelled "AI is busy", at no quota cost."""
    monkeypatch.setattr(get_settings(), "ai_queue_max_total", 0)  # every interactive request finds the queue full
    provider = fake_ai(responses=[json.dumps({"full_name": "Someone Else"})])
    owner = await owner_client(client, db)
    r = await api(client, "POST", INTAKE, json={"text": RICH})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["source"] == "fallback" and data["busy"] is True and data["ai_available"] is False
    assert data["provider"] is None and data["notes"][0] == BUSY_NOTE and data["disclaimer"] == AI_DISCLAIMER
    f = data["fields"]
    assert f["full_name"] == "Riya Kapoor" and f["email"] == "riya.k@example.com" and f["date_of_birth"] == "2010-03-12"
    assert data["missing"] == ["id_front"]
    assert provider.prompts == []
    status = await guard.status(owner.id)
    assert status["usage"]["gen"][0] == 0 and status["strikes"] == 0


async def test_intake_passes_the_owners_note_to_the_guard(client, db, fake_ai, guard_on, monkeypatch):
    calls: list[tuple] = []
    real = guard.admit

    async def spy(owner, feature, text=None):
        calls.append((owner, feature, text))
        return await real(owner, feature, text)

    monkeypatch.setattr(guard, "admit", spy)
    provider = fake_ai(responses=[EXTRACTION])
    owner = await owner_client(client, db)
    r = await api(client, "POST", INTAKE, json={"text": "  " + RICH + "\u0000 "}, headers={"X-Page-Id": PAGE})
    assert r.status_code == 200 and r.json()["source"] == "ai" and r.json()["busy"] is False
    [(who, feature, text)] = calls
    assert feature == "intake" and text == RICH  # the note itself (cleaned), not the prompt around it
    assert (who.user_id, who.kind, who.page_id) == (owner.id, "user", PAGE)
    assert "<note>" in provider.prompts[0]["prompt"] and "<note>" not in text


async def test_intake_stops_when_the_owner_leaves(db, fake_ai, guard_on):
    started = asyncio.Event()
    stopped: list[bool] = []

    async def slow(**kwargs):
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            stopped.append(True)
            raise
        return EXTRACTION

    fake_ai(handler=slow)
    owner = await make_owner(db)
    await make_gym(db, owner)
    gone = asyncio.Event()
    async with droppable_client(gone) as c:
        await login_as(c, owner)
        request = asyncio.create_task(api(c, "POST", INTAKE, json={"text": RICH}, headers={"X-Page-Id": PAGE}))
        await asyncio.wait_for(started.wait(), 5)
        gone.set()  # the owner closed the tab
        r = await asyncio.wait_for(request, 5)
    assert r.status_code == 503 and r.json()["code"] == "cancelled" and r.headers["Cache-Control"] == "no-store"
    assert stopped == [True]
    # charged: the provider was already reading the note (only work that never reached a provider is free)
    assert (await guard.status(owner.id))["usage"]["gen"][2] == 1


async def test_no_pooled_db_connection_is_held_while_the_ai_reads_the_note(client, db, pool_probe, monkeypatch):
    """The route reads the owner's gyms, closes the request's session and only then waits for the AI - in the queue and
    while the provider reads the note - so owners waiting for prefills never drain the DB pool (nothing is saved)."""
    await owner_client(client, db)
    extraction = json.dumps({"full_name": "Riya Kapoor", "email": "riya.k@example.com"})
    r, queued, answering = await connections_while_the_ai_works(
        lambda: api(client, "POST", INTAKE, json={"text": RICH}), pool_probe, lambda **kwargs: extraction, monkeypatch)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["source"] == "ai" and data["busy"] is False and data["fields"]["email"] == "riya.k@example.com"
    assert (queued, answering) == (0, [0])  # no connection checked out while queued, none while the provider answered
