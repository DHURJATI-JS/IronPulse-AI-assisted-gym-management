"""WP-H identity: account ID page, uploads, verification job, consensus decisions, authorised images, owner review."""

from __future__ import annotations

import io
import json
import os
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from app.ai.client import FakeProvider, ai
from app.db import QueryCounter
from app.models import AuditLog, Employee, IdentityDocument, Job, Notification, SecurityEvent, User
from app.models.enums import (
    EmploymentStatus,
    IdDocType,
    IdStatus,
    NotificationCategory,
    Role,
    SubscriptionStatus,
    ThreatCategory,
)
from app.services import id_verification as idv
from app.services import idparse
from app.services import ocr as ocr_mod
from app.services.jobs import run_job_inline
from app.services.ocr import MultiOcrResult, OcrEngineResult
from tests.conftest import api, login_as, post_form, run_jobs
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

TODAY = date(2026, 11, 10)
DOB = date(1995, 5, 17)


# ====================================================================== helpers / fixtures
@pytest.fixture(autouse=True)
def _ai_offline_by_default():
    """Never reach a real AI provider; tests that need AI install ``fake_ai``."""
    ai.use_providers([FakeProvider("gateway", fail=True)])
    yield
    ai.use_providers(None)


def aadhaar_no(prefix: str = "23456789012") -> str:
    return prefix + idparse.verhoeff_check_digit(prefix)


def aadhaar_text(name: str = "Rahul Mehta", dob: str = "17/05/1995", number: str | None = None) -> str:
    n = number or aadhaar_no()
    return f"GOVERNMENT OF INDIA\n{name}\nDOB: {dob}\nMALE\n{n[:4]} {n[4:8]} {n[8:]}"


def multi(**texts: str | None) -> MultiOcrResult:
    return MultiOcrResult([
        OcrEngineResult(name, text is not None, text or "", 0.91 if text else None, 150, None if text is not None else "Timed out after 45 s")
        for name, text in texts.items()
    ])


@pytest.fixture
def fake_ocr(monkeypatch):
    """``fake_ocr(front={"tesseract": "...", ...}, back={...})`` - front images are landscape, backs portrait."""
    texts: dict[str, dict[str, str | None]] = {"front": {}, "back": {}}
    calls: list[str] = []

    async def run(image: bytes, *, engines=None, purpose: str = "document") -> MultiOcrResult:
        from PIL import Image

        with Image.open(io.BytesIO(image)) as im:
            side = "front" if im.width >= im.height else "back"
        calls.append(side)
        return multi(**texts[side])

    monkeypatch.setattr(ocr_mod, "run_ocr", run)

    def configure(front: dict | None = None, back: dict | None = None) -> list[str]:
        texts["front"], texts["back"] = dict(front or {}), dict(back or {})
        return calls

    return configure


async def rahul(db, **kwargs) -> User:
    kwargs.setdefault("id_status", IdStatus.MISSING)
    return await make_customer(db, full_name="Rahul Mehta", date_of_birth=DOB, **kwargs)


async def submit(db, user: User | None, *, doc_type: str = "aadhaar", back: bool = False, uploaded_by: User | None = None,
                 employee: Employee | None = None) -> IdentityDocument:
    doc = await idv.submit_identity_document(
        db, user=user, employee=employee, doc_type=IdDocType(doc_type), country_code="IN",
        front=png_bytes((300, 200)), back=png_bytes((200, 300)) if back else None, uploaded_by=uploaded_by or user,
    )
    await db.commit()
    return doc


async def fresh(db, model, id_):
    return (await db.execute(select(model).where(model.id == id_).execution_options(populate_existing=True))).scalar_one()


async def process(doc_id: int) -> None:
    await run_job_inline("verify_identity", {"document_id": doc_id})


def verdict_for(texts: dict[str, str], *, name: str | None = "Rahul Mehta", dob: date | None = DOB, doc_type: str = "aadhaar",
                duplicate: bool = False, ai_assessment=None, min_engines: int = 2) -> idv.Verdict:
    results = {e: OcrEngineResult(e, True, t, 0.9, 100) for e, t in texts.items()}
    live = {e: t for e, t in texts.items() if t.strip()}
    per = {e: idparse.extract_fields(t, doc_type=doc_type, country="IN", today=TODAY) for e, t in live.items()}
    cons = idparse.build_consensus(per, doc_type=doc_type)
    return idv.decide(per_engine=per, results=results, consensus=cons, account_name=name, account_dob=dob, doc_type=doc_type,
                      min_engines=min_engines, today=TODAY, duplicate=duplicate, ai_assessment=ai_assessment,
                      ai_unavailable=ai_assessment is None, texts=live)


def checks_by_key(v: idv.Verdict) -> dict[str, dict]:
    return {c["key"]: c for c in v.checks}


# ====================================================================== pages
async def test_identity_page_renders_for_new_customer(client, db):
    user = await make_customer(db, id_status=IdStatus.MISSING)
    await login_as(client, user)
    r = await client.get("/account/identity")
    assert r.status_code == 200, r.text[:2000]
    assert "Verify your identity" in r.text
    assert 'name="id_front"' in r.text and 'enctype="multipart/form-data"' in r.text
    assert "No ID card yet" in r.text and 'data-component="id-upload"' in r.text
    assert "<script>" not in r.text and "onclick=" not in r.text and 'style="' not in r.text


async def test_identity_page_requires_login(client):
    r = await client.get("/account/identity")
    assert r.status_code == 303 and r.headers["location"].startswith("/auth/login")


async def test_identity_page_shows_verified_card_details(client, db, fake_ocr, fake_ai):
    user = await rahul(db)
    fake_ai(responses=['{"looks_like_government_id": true, "document_type_guess": "Indian Aadhaar card", '
                       '"tampering_signs": [], "confidence": 0.9}'])
    fake_ocr(front={"tesseract": aadhaar_text(), "apple_vision": aadhaar_text()})
    doc = await submit(db, user)
    await process(doc.id)
    await login_as(client, user)
    r = await client.get("/account/identity")
    assert r.status_code == 200
    html = r.text
    assert "Your identity is verified" in html and "••••••••" + aadhaar_no()[-4:] in html
    assert "Agrees" in html and "Engines agree" in html and "AI document check" in html
    assert "AI can make mistakes" in html  # disclaimer next to the AI summary
    assert f"/identity/{doc.id}/front" in html and "Replace your ID card" in html
    assert aadhaar_no() not in html  # the full number never reaches the page


async def test_needs_review_page_offers_recheck_and_status_api(client, db, fake_ocr):
    user = await rahul(db)
    fake_ocr(front={"tesseract": aadhaar_text(), "apple_vision": None})
    doc = await submit(db, user)
    await login_as(client, user)
    r = await api(client, "GET", "/api/identity/status")
    assert r.json() == {"id_status": "pending", "document_id": doc.id, "document_status": "pending",
                        "updated_at": r.json()["updated_at"], "done": False}
    assert "no-store" in r.headers["cache-control"]
    page = await client.get("/account/identity")
    assert 'data-component="idv-live"' in page.text and "Queued for verification" in page.text
    await process(doc.id)
    r = await api(client, "GET", "/api/identity/status")
    assert r.json()["document_status"] == "needs_review" and r.json()["done"] is True
    page = await client.get("/account/identity")
    assert "A person will double-check your card" in page.text and "Run the automatic check again" in page.text
    # re-run the automatic check (e.g. once more engines are online)
    r = await post_form(client, f"/account/identity/{doc.id}/recheck")
    assert r.status_code == 303
    doc = await fresh(db, IdentityDocument, doc.id)
    assert doc.status == IdStatus.PENDING
    assert (await db.scalar(select(Job.status).where(Job.dedupe_key == f"verify_identity:{doc.id}"))).value == "queued"
    # someone else's card -> 404
    other = await make_customer(db)
    await login_as(client, other)
    r = await post_form(client, f"/account/identity/{doc.id}/recheck")
    assert r.status_code == 404


async def test_recheck_limited_to_three_attempts(client, db, fake_ocr):
    user = await rahul(db)
    fake_ocr(front={"tesseract": aadhaar_text()})
    doc = await submit(db, user)
    await process(doc.id)
    await login_as(client, user)
    for _ in range(2):
        r = await post_form(client, f"/account/identity/{doc.id}/recheck")
        assert r.status_code == 303
        await process(doc.id)
    doc = await fresh(db, IdentityDocument, doc.id)
    assert doc.attempts == 3 and doc.status == IdStatus.NEEDS_REVIEW
    r = await post_form(client, f"/account/identity/{doc.id}/recheck")
    assert r.status_code == 303
    assert (await fresh(db, IdentityDocument, doc.id)).status == IdStatus.NEEDS_REVIEW


async def test_page_handles_legacy_seeded_card(client, db):
    from app.core.utils import utcnow

    user = await rahul(db, id_status=IdStatus.VERIFIED)
    db.add(IdentityDocument(user_id=user.id, doc_type=IdDocType.NATIONAL_ID, country_code="IN", front_key="private/identity/" + "a" * 32 + ".bin",
                            status=IdStatus.VERIFIED, id_number_masked="••••0042", name_on_id="Rahul Mehta", dob_on_id=DOB,
                            engines=["seed"], checks=[{"check": "Demo document", "passed": True, "detail": "Seeded specimen"}],
                            consensus_score=1.0, ai_summary="Demo identity document created by the seed script.",
                            verified_at=utcnow(), uploaded_by_id=user.id))
    await db.commit()
    await login_as(client, user)
    r = await client.get("/account/identity")
    assert r.status_code == 200
    assert "Demo data" in r.text and "Demo document" in r.text and "••••0042" in r.text
    assert "created by the seed script" not in r.text  # a plain note is never presented as AI output


# ====================================================================== upload route
async def test_upload_creates_pending_document_and_queues_verification(client, db):
    user = await rahul(db)
    await login_as(client, user)
    r = await post_form(client, "/account/identity", {"id_doc_type": "passport", "id_country": "in"},
                        files={"id_front": ("front.png", png_bytes((320, 200)), "image/png"),
                               "id_back": ("back.png", png_bytes((200, 320)), "image/png")})
    assert r.status_code == 303 and r.headers["location"] == "/account/identity"
    doc = await db.scalar(select(IdentityDocument).where(IdentityDocument.user_id == user.id))
    assert doc.status == IdStatus.PENDING and doc.doc_type == IdDocType.PASSPORT and doc.country_code == "IN"
    assert doc.back_key and doc.front_key.startswith("private/identity/") and doc.uploaded_by_id == user.id
    assert (await fresh(db, User, user.id)).id_status == IdStatus.PENDING
    assert await db.scalar(select(func.count(Job.id)).where(Job.kind == "verify_identity")) == 1
    note = await db.scalar(select(Notification).where(Notification.user_id == user.id))
    assert note.title == "ID card received" and note.category == NotificationCategory.VERIFICATION


@pytest.mark.parametrize("data,files,expected", [
    ({"id_doc_type": "aadhaar", "id_country": "IN"}, None, "Upload a photo of the front"),
    ({"id_doc_type": "spaceship", "id_country": "IN"}, "png", "Choose the type of ID document"),
    ({"id_doc_type": "aadhaar", "id_country": "1N"}, "png", "2-letter code"),
    ({"id_doc_type": "aadhaar", "id_country": "IN"}, "text", "not a valid image"),
])
async def test_upload_validation_errors(client, db, data, files, expected):
    user = await rahul(db)
    await login_as(client, user)
    upload = None
    if files == "png":
        upload = {"id_front": ("front.png", png_bytes(), "image/png")}
    elif files == "text":
        upload = {"id_front": ("front.png", b"this is not an image at all", "image/png")}
    r = await post_form(client, "/account/identity", data, files=upload or {"id_front": ("", b"", "application/octet-stream")})
    assert r.status_code == 400
    assert expected in r.text
    assert await db.scalar(select(func.count(IdentityDocument.id))) == 0


async def test_upload_blocked_while_a_card_is_being_checked(client, db):
    user = await rahul(db)
    await submit(db, user)
    await login_as(client, user)
    r = await post_form(client, "/account/identity", {"id_doc_type": "aadhaar", "id_country": "IN"},
                        files={"id_front": ("front.png", png_bytes(), "image/png")})
    assert r.status_code == 400 and "being checked right now" in r.text
    assert await db.scalar(select(func.count(IdentityDocument.id))) == 1


# ====================================================================== decision rules (pure)
def test_two_engines_agree_verified():
    v = verdict_for({"tesseract": aadhaar_text(), "apple_vision": aadhaar_text()})
    assert v.status == IdStatus.VERIFIED and v.reason is None
    assert v.number == aadhaar_no() and v.name_on_id == "Rahul Mehta" and v.dob_on_id == DOB
    assert all(c["status"] in ("pass", "skip") for c in v.checks)
    assert [e["agrees"] for e in v.engine_results] == [True, True]
    assert v.engine_results[0]["number_masked"].endswith(aadhaar_no()[-4:])
    assert aadhaar_no() not in json.dumps(v.engine_results) + json.dumps(v.checks)


def test_one_engine_needs_review_even_with_valid_checksum():
    v = verdict_for({"tesseract": aadhaar_text(), "apple_vision": "smudge"})
    assert v.status == IdStatus.NEEDS_REVIEW
    c = checks_by_key(v)
    assert c["checksum"]["status"] == "pass" and c["consensus"]["status"] == "warn" and c["engines"]["status"] == "warn"
    assert "Only 1" in v.reason


def test_vision_llm_counts_as_an_engine():
    v = verdict_for({"tesseract": aadhaar_text(), "vision_llm": aadhaar_text()})
    assert v.status == IdStatus.VERIFIED


def test_disagreement_needs_review():
    pan = "INCOME TAX DEPARTMENT\nPermanent Account Number\n{n}\nName: RAHUL MEHTA\nDate of Birth: 17/05/1995"
    v = verdict_for({"tesseract": pan.format(n="ABCPM1234F"), "apple_vision": pan.format(n="XYZPM9876K")}, doc_type="pan")
    assert v.status == IdStatus.NEEDS_REVIEW
    assert checks_by_key(v)["number"]["status"] == "warn"
    v = verdict_for({"tesseract": pan.format(n="ABCPM1234F"), "apple_vision": pan.format(n="ABCPM1234F")}, doc_type="pan")
    assert v.status == IdStatus.VERIFIED and checks_by_key(v)["checksum"]["status"] == "skip"


def test_checksum_invalid_under_consensus_rejected():
    bad = aadhaar_no()[:11] + str((int(aadhaar_no()[11]) + 1) % 10)
    v = verdict_for({"tesseract": aadhaar_text(number=bad), "apple_vision": aadhaar_text(number=bad)})
    assert v.status == IdStatus.REJECTED and "checksum" in v.reason.lower()
    assert checks_by_key(v)["checksum"]["status"] == "fail"
    # one engine misreading a digit is OCR noise, not a forgery
    v = verdict_for({"tesseract": aadhaar_text(number=bad), "apple_vision": aadhaar_text()})
    assert v.status == IdStatus.VERIFIED


def test_name_mismatch_rejected_and_partial_name_reviewed():
    pan = "Permanent Account Number\nABCPM1234F\nName: PRIYA SHARMA\nDate of Birth: 17/05/1995"
    v = verdict_for({"tesseract": pan, "apple_vision": pan}, doc_type="pan")
    assert v.status == IdStatus.REJECTED and "name" in v.reason.lower()
    assert checks_by_key(v)["name"]["status"] == "fail"
    # a name the engines could not read is never a rejection
    v = verdict_for({"tesseract": "ABCPM1234F\n17/05/1995", "apple_vision": "ABCPM1234F\n17/05/1995"}, doc_type="pan")
    assert v.status == IdStatus.NEEDS_REVIEW and checks_by_key(v)["name"]["status"] == "warn"
    # middle names / initials still match
    v = verdict_for({"tesseract": aadhaar_text(name="Rahul Kumar Mehta"), "apple_vision": aadhaar_text(name="Rahul Kumar Mehta")})
    assert v.status == IdStatus.VERIFIED


def _passport(expiry: str, dob: str = "950517") -> str:
    cd = lambda s: str(idparse.mrz_check_digit(s))  # noqa: E731
    l1 = "P<INDMEHTA<<RAHUL".ljust(44, "<")
    num = "J8369854<"
    l2 = num + cd(num) + "IND" + dob + cd(dob) + "M" + expiry + cd(expiry) + "<" * 14 + "<"
    l2 += cd(l2[0:10] + l2[13:20] + l2[21:43])
    return f"REPUBLIC OF INDIA\nPASSPORT\n{l1}\n{l2}"


def test_expired_passport_rejected_and_dob_mismatch_reviewed():
    v = verdict_for({"tesseract": _passport("240101"), "apple_vision": _passport("240101")}, doc_type="passport")
    assert v.status == IdStatus.REJECTED and "expired" in v.reason.lower() and v.expires_on == date(2024, 1, 1)
    v = verdict_for({"tesseract": _passport("320516"), "apple_vision": _passport("320516")}, doc_type="passport")
    assert v.status == IdStatus.VERIFIED and v.number == "J8369854" and v.expires_on == date(2032, 5, 16)
    v = verdict_for({"tesseract": _passport("320516", dob="960517"), "apple_vision": _passport("320516", dob="960517")},
                    doc_type="passport")
    assert v.status == IdStatus.NEEDS_REVIEW and checks_by_key(v)["dob"]["status"] == "fail"


def test_expiry_needs_confirmation_before_rejecting():
    dl = "DRIVING LICENCE\nDL No: MH12 20110012345\nName: RAHUL MEHTA\nDOB: 17-05-1995\nValid Till: {d}"
    # one engine reading a past date (a misread year?) only asks for a review ...
    v = verdict_for({"tesseract": dl.format(d="16-05-2020"), "apple_vision": dl.format(d="16-05-2030")}, doc_type="driving_license")
    assert v.status == IdStatus.NEEDS_REVIEW and checks_by_key(v)["expiry"]["status"] == "warn"
    # ... two engines reading the same past date reject the card
    v = verdict_for({"tesseract": dl.format(d="16-05-2020"), "apple_vision": dl.format(d="16-05-2020")}, doc_type="driving_license")
    assert v.status == IdStatus.REJECTED and "expired on 16 May 2020" in v.reason
    v = verdict_for({"tesseract": dl.format(d="16-05-2035"), "apple_vision": dl.format(d="16-05-2035")}, doc_type="driving_license")
    assert v.status == IdStatus.VERIFIED and v.number == "MH1220110012345"


def test_duplicate_rejected_and_ai_only_downgrades():
    v = verdict_for({"tesseract": aadhaar_text(), "apple_vision": aadhaar_text()}, duplicate=True)
    assert v.status == IdStatus.REJECTED and "another account" in v.reason
    from app.ai.idcard import IdCardAssessment

    worried = IdCardAssessment(looks_like_government_id=False, document_type_guess="printout", tampering_signs=["edited digits"],
                               confidence=0.9)
    v = verdict_for({"tesseract": aadhaar_text(), "apple_vision": aadhaar_text()}, ai_assessment=worried)
    assert v.status == IdStatus.NEEDS_REVIEW and checks_by_key(v)["ai"]["status"] == "warn"
    # the AI alone can never reject: an unreadable card with a worried AI is still only a review
    v = verdict_for({"tesseract": "x", "apple_vision": ""}, ai_assessment=worried)
    assert v.status == IdStatus.NEEDS_REVIEW
    unsure = IdCardAssessment(looks_like_government_id=False, confidence=0.3)
    v = verdict_for({"tesseract": aadhaar_text(), "apple_vision": aadhaar_text()}, ai_assessment=unsure)
    assert v.status == IdStatus.VERIFIED


def test_illegible_card_needs_review():
    v = verdict_for({"tesseract": "", "apple_vision": "?"})
    assert v.status == IdStatus.NEEDS_REVIEW
    assert checks_by_key(v)["legible"]["status"] == "fail" and checks_by_key(v)["number"]["status"] == "fail"


# ====================================================================== verification job
async def test_job_verifies_customer_and_staff_records(db, fake_ocr):
    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    staff = await make_user(db, role=Role.STAFF, full_name="Rahul Mehta", date_of_birth=DOB, id_status=IdStatus.MISSING)
    emp = await make_employee(db, gym, user=staff, status=EmploymentStatus.PENDING_VERIFICATION)
    fake_ocr(front={"tesseract": aadhaar_text(), "apple_vision": aadhaar_text(), "vision_llm": None})
    doc = await submit(db, staff)
    await process(doc.id)
    doc = await fresh(db, IdentityDocument, doc.id)
    assert doc.status == IdStatus.VERIFIED and doc.verified_at is not None and doc.attempts == 1
    assert doc.consensus_score == pytest.approx(1.0)
    assert doc.id_number_masked == "••••••••" + aadhaar_no()[-4:]
    from app.security.crypto import pepper_hash

    assert doc.id_number_hash == pepper_hash(aadhaar_no())
    assert doc.name_on_id == "Rahul Mehta" and doc.dob_on_id == DOB and doc.engines == ["tesseract", "apple_vision", "vision_llm"]
    stored = json.dumps(doc.engine_results) + json.dumps(doc.checks)
    assert aadhaar_no() not in stored and "GOVERNMENT OF INDIA" not in stored  # never the raw OCR text
    llm = next(e for e in doc.engine_results if e["engine"] == "vision_llm")
    assert llm["ok"] is False and llm["error"] == "Took too long and was skipped."
    assert (await fresh(db, User, staff.id)).id_status == IdStatus.VERIFIED
    assert (await fresh(db, Employee, emp.id)).id_status == IdStatus.VERIFIED
    job = await db.scalar(select(Job).where(Job.kind == "staff_activation_check"))
    assert job is not None and job.payload == {"employee_id": emp.id}
    titles = (await db.execute(select(Notification.title).where(Notification.user_id == staff.id))).scalars().all()
    assert "Your ID card is verified" in titles
    # running the job again is a no-op
    await process(doc.id)
    assert (await fresh(db, IdentityDocument, doc.id)).attempts == 1


async def test_job_rejects_duplicate_id_with_fraud_event(db, fake_ocr):
    first = await rahul(db)
    fake_ocr(front={"tesseract": aadhaar_text(), "apple_vision": aadhaar_text()})
    d1 = await submit(db, first)
    await process(d1.id)
    assert (await fresh(db, IdentityDocument, d1.id)).status == IdStatus.VERIFIED
    # the same person re-uploading their own card is fine
    d2 = await submit(db, first)
    await process(d2.id)
    assert (await fresh(db, IdentityDocument, d2.id)).status == IdStatus.VERIFIED
    impostor = await make_customer(db, full_name="Rahul Mehta", date_of_birth=DOB, id_status=IdStatus.MISSING)
    d3 = await submit(db, impostor)
    await process(d3.id)
    d3 = await fresh(db, IdentityDocument, d3.id)
    assert d3.status == IdStatus.REJECTED and "another account" in d3.rejection_reason
    assert (await fresh(db, User, impostor.id)).id_status == IdStatus.REJECTED
    ev = await db.scalar(select(SecurityEvent).where(SecurityEvent.category == ThreatCategory.FRAUD))
    assert ev is not None and ev.severity == 3 and ev.user_id == impostor.id and f"document {d3.id}" in ev.detail
    assert await db.scalar(select(AuditLog).where(AuditLog.action == "identity.duplicate_rejected")) is not None
    note = await db.scalar(select(Notification).where(Notification.user_id == impostor.id,
                                                      Notification.title == "We couldn't verify your ID card"))
    assert note is not None


async def test_job_with_ai_available_stores_summary(db, fake_ocr, fake_ai):
    provider = fake_ai(responses=['{"looks_like_government_id": true, "document_type_guess": "Indian Aadhaar card", '
                                  '"tampering_signs": [], "confidence": 0.88}'])
    user = await rahul(db)
    fake_ocr(front={"tesseract": aadhaar_text(), "apple_vision": aadhaar_text()},
             back={"tesseract": "Address: 12 MG Road, Bengaluru", "apple_vision": "Address: 12 MG Road"})
    doc = await submit(db, user, back=True)
    await process(doc.id)
    doc = await fresh(db, IdentityDocument, doc.id)
    assert doc.status == IdStatus.VERIFIED
    assert "genuine Indian Aadhaar card" in doc.ai_summary and "88%" in doc.ai_summary
    sent = provider.prompts[-1]
    assert len(sent["images"]) == 2 and sent["max_tokens"] <= 1500


async def test_job_ai_unavailable_does_not_crash(db, fake_ocr, ai_down):
    user = await rahul(db)
    fake_ocr(front={"tesseract": aadhaar_text(), "apple_vision": aadhaar_text()})
    doc = await submit(db, user)
    await process(doc.id)
    doc = await fresh(db, IdentityDocument, doc.id)
    assert doc.status == IdStatus.VERIFIED and doc.ai_summary is None
    assert {c["key"]: c["status"] for c in doc.checks}["ai"] == "skip"


async def test_job_with_unreadable_stored_image_needs_review(db, fake_ocr):
    from app.services import storage

    user = await rahul(db)
    doc = await submit(db, user)
    await storage.delete_key(doc.front_key)
    await process(doc.id)
    doc = await fresh(db, IdentityDocument, doc.id)
    assert doc.status == IdStatus.NEEDS_REVIEW and "upload the card again" in doc.rejection_reason


async def test_newer_card_wins_subject_status(db, fake_ocr):
    user = await rahul(db)
    fake_ocr(front={"tesseract": aadhaar_text(), "apple_vision": aadhaar_text()})
    old = await submit(db, user)
    new = await submit(db, user)
    fake_ocr(front={"tesseract": "", "apple_vision": ""})
    await process(new.id)
    fake_ocr(front={"tesseract": aadhaar_text(), "apple_vision": aadhaar_text()})
    await process(old.id)
    assert (await fresh(db, IdentityDocument, old.id)).status == IdStatus.VERIFIED
    assert (await fresh(db, User, user.id)).id_status == IdStatus.NEEDS_REVIEW  # follows the latest card


async def test_queue_runner_processes_uploaded_card(client, db, fake_ocr):
    user = await rahul(db)
    fake_ocr(front={"tesseract": aadhaar_text(), "apple_vision": aadhaar_text()})
    await login_as(client, user)
    r = await post_form(client, "/account/identity", {"id_doc_type": "aadhaar", "id_country": "IN"},
                        files={"id_front": ("front.png", png_bytes((300, 200)), "image/png")})
    assert r.status_code == 303
    assert await run_jobs() >= 1
    assert (await fresh(db, User, user.id)).id_status == IdStatus.VERIFIED


async def test_staff_upload_marks_employments_pending(client, db):
    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    staff = await make_user(db, role=Role.STAFF, full_name="Asha Rao", id_status=IdStatus.VERIFIED)
    emp = await make_employee(db, gym, user=staff)
    await login_as(client, staff)
    r = await post_form(client, "/account/identity", {"id_doc_type": "pan", "id_country": "IN"},
                        files={"id_front": ("pan.png", png_bytes((300, 200)), "image/png")})
    assert r.status_code == 303
    assert (await fresh(db, Employee, emp.id)).id_status == IdStatus.PENDING
    assert (await fresh(db, User, staff.id)).id_status == IdStatus.PENDING


async def test_sweeper_requeues_stuck_cards_and_gives_up_after_five_runs(db):
    from sqlalchemy import update

    from app.core.utils import utcnow
    from app.models.enums import JobStatus

    user = await rahul(db)
    stuck = await submit(db, user)
    fresh_card = await submit(db, await make_customer(db))  # a recent card is left alone
    exhausted_user = await rahul(db)
    exhausted = await submit(db, exhausted_user)
    old = utcnow() - timedelta(hours=1)
    # the jobs failed (e.g. queued before the verifier was deployed) and the cards are old
    await db.execute(update(Job).where(Job.dedupe_key.in_([f"verify_identity:{stuck.id}", f"verify_identity:{exhausted.id}"]))
                     .values(status=JobStatus.FAILED, attempts=5))
    await db.execute(update(IdentityDocument).where(IdentityDocument.id == stuck.id).values(updated_at=old))
    await db.execute(update(IdentityDocument).where(IdentityDocument.id == exhausted.id).values(updated_at=old, attempts=5))
    await db.commit()
    assert await idv.identity_sweep(db) == 1
    await db.commit()
    job = await db.scalar(select(Job).where(Job.dedupe_key == f"verify_identity:{stuck.id}").execution_options(populate_existing=True))
    assert job.status == JobStatus.QUEUED and job.attempts == 0
    fresh_job = await db.scalar(select(Job).where(Job.dedupe_key == f"verify_identity:{fresh_card.id}"))
    assert fresh_job.status == JobStatus.QUEUED  # untouched (still queued from the upload)
    gave_up = await fresh(db, IdentityDocument, exhausted.id)
    assert gave_up.status == IdStatus.NEEDS_REVIEW and "couldn't finish" in gave_up.rejection_reason
    assert (await fresh(db, User, exhausted_user.id)).id_status == IdStatus.NEEDS_REVIEW
    assert await idv.identity_sweep(db) == 0  # nothing left to do


# ====================================================================== authorised image route
async def test_image_route_authorisation(client, db, anon_client_factory):
    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    plans = await make_plans(db, gym)
    customer = await rahul(db)
    doc = await submit(db, customer, back=True, uploaded_by=owner)

    async def get(user, side="front"):
        await login_as(client, user)
        return await client.get(f"/identity/{doc.id}/{side}")

    r = await get(customer)
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert "no-store" in r.headers["cache-control"] and r.headers["content-disposition"].startswith("inline")
    assert r.content[:2] == b"\xff\xd8"
    assert (await get(customer, "back")).status_code == 200
    assert (await get(customer, "left")).status_code == 404
    assert await db.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == "identity.image_viewed")) == 0

    assert (await get(await make_customer(db))).status_code == 404  # another customer
    assert (await get(await make_owner(db))).status_code == 404  # unrelated owner
    assert (await get(owner)).status_code == 200  # the owner who uploaded it at the desk
    assert (await get(await make_user(db, role=Role.SUPERADMIN))).status_code == 200

    # an owner whose gym the customer had a subscription with (even an expired one)
    other_owner = await make_owner(db)
    other_gym = await make_gym(db, other_owner)
    other_plans = await make_plans(db, other_gym)
    assert (await get(other_owner)).status_code == 404
    await make_subscription(db, customer, other_plans[0], status=SubscriptionStatus.EXPIRED)
    assert (await get(other_owner)).status_code == 200
    assert plans  # noqa: the first owner's plans are irrelevant to their access (they uploaded the card)

    views = (await db.execute(select(AuditLog).where(AuditLog.action == "identity.image_viewed"))).scalars().all()
    assert len(views) == 3 and all(v.entity_id == doc.id for v in views)

    anon = anon_client_factory()
    r = await anon.get(f"/identity/{doc.id}/front")
    assert r.status_code in (303, 401) and not r.content.startswith(b"\xff\xd8")
    await login_as(client, customer)
    assert (await client.get(f"/identity/{doc.id + 999}/front")).status_code == 404
    assert (await client.get(f"/identity/{2**70}/front")).status_code == 404  # beyond BIGINT: 404, not a 500
    assert (await client.get(f"/owner/identity/{2**70}")).status_code == 404


async def test_image_route_staff_card_visible_to_employer(client, db):
    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    staff = await make_user(db, role=Role.STAFF, full_name="Asha Rao", id_status=IdStatus.MISSING)
    await make_employee(db, gym, user=staff)
    doc = await submit(db, staff)
    await login_as(client, owner)
    assert (await client.get(f"/identity/{doc.id}/front")).status_code == 200
    stranger = await make_owner(db)
    await login_as(client, stranger)
    assert (await client.get(f"/identity/{doc.id}/front")).status_code == 404
    assert (await client.get(f"/identity/{doc.id}/back")).status_code == 404
    # a card the owner uploaded for the staff record before the account was linked (no user_id on the card)
    emp2 = await make_employee(db, gym, user=None, status=EmploymentStatus.INVITED)
    staff_doc = await submit(db, None, employee=emp2, uploaded_by=owner)
    await login_as(client, staff)
    assert (await client.get(f"/identity/{staff_doc.id}/front")).status_code == 404  # not their record
    emp2 = await fresh(db, Employee, emp2.id)
    emp2.user_id = staff.id
    await db.commit()
    before = await db.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == "identity.image_viewed"))
    assert (await client.get(f"/identity/{staff_doc.id}/front")).status_code == 200  # now linked: their own card
    after = await db.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == "identity.image_viewed"))
    assert after == before  # viewing your own card is not audited
    r = await client.get("/account/identity")
    assert f"/identity/{staff_doc.id}/front" in r.text  # the linked card shows up on their own ID page


# ====================================================================== owner / super-admin review
async def _desk_card(db, fake_ocr):
    """An owner registers a customer at the desk; only one engine can read the card -> NEEDS_REVIEW."""
    owner = await make_owner(db)
    await make_gym(db, owner)
    customer = await rahul(db)
    fake_ocr(front={"tesseract": aadhaar_text(), "apple_vision": "?"})
    doc = await submit(db, customer, uploaded_by=owner)
    await process(doc.id)
    return owner, customer, await fresh(db, IdentityDocument, doc.id)


async def test_owner_reviews_card_of_customer_they_registered(client, db, fake_ocr):
    owner, customer, doc = await _desk_card(db, fake_ocr)
    assert doc.status == IdStatus.NEEDS_REVIEW
    owner_notes = (await db.execute(select(Notification).where(Notification.user_id == owner.id))).scalars().all()
    assert any(n.link == f"/owner/identity/{doc.id}" and "needs your review" in n.title for n in owner_notes)

    await login_as(client, owner)
    page = await client.get(f"/owner/identity/{doc.id}")
    assert page.status_code == 200 and "Your decision" in page.text and "Rahul Mehta" in page.text
    r = await post_form(client, f"/owner/identity/{doc.id}/review", {"decision": "approve"})
    assert r.status_code == 400 and "checked the original card" in r.text
    r = await post_form(client, f"/owner/identity/{doc.id}/review", {"decision": "reject", "note": "no"})
    assert r.status_code == 400 and "at least 5 characters" in r.text
    r = await post_form(client, f"/owner/identity/{doc.id}/review", {"decision": "maybe"})
    assert r.status_code == 400
    r = await post_form(client, f"/owner/identity/{doc.id}/review",
                        {"decision": "approve", "seen_original": "1", "note": "Checked at the desk", "next": "https://evil.example"})
    assert r.status_code == 303 and r.headers["location"] == f"/owner/identity/{doc.id}"
    doc = await fresh(db, IdentityDocument, doc.id)
    assert doc.status == IdStatus.VERIFIED and doc.reviewed_by_id == owner.id and doc.reviewed_at and doc.verified_at
    assert {c["key"]: c["status"] for c in doc.checks}["review"] == "pass"
    assert (await fresh(db, User, customer.id)).id_status == IdStatus.VERIFIED
    assert await db.scalar(select(AuditLog).where(AuditLog.action == "identity.approved", AuditLog.actor_id == owner.id))
    assert await db.scalar(select(Notification).where(Notification.user_id == customer.id,
                                                      Notification.title == "Your ID card is verified"))
    # already decided -> nothing to review any more
    r = await post_form(client, f"/owner/identity/{doc.id}/review", {"decision": "reject", "note": "Changed my mind"})
    assert r.status_code == 303
    assert (await fresh(db, IdentityDocument, doc.id)).status == IdStatus.VERIFIED


async def test_owner_reject_and_permission_boundaries(client, db, fake_ocr):
    owner, customer, doc = await _desk_card(db, fake_ocr)
    # another owner, the customer themselves and staff cannot review (404 - no probing)
    for intruder in (await make_owner(db), customer, await make_user(db, role=Role.STAFF)):
        await login_as(client, intruder)
        assert (await client.get(f"/owner/identity/{doc.id}")).status_code == 404
        r = await post_form(client, f"/owner/identity/{doc.id}/review", {"decision": "approve", "seen_original": "1"})
        assert r.status_code == 404
    await login_as(client, owner)
    r = await post_form(client, f"/owner/identity/{doc.id}/review", {"decision": "reject", "note": "Photo of a photo - not the original card."})
    assert r.status_code == 303
    doc = await fresh(db, IdentityDocument, doc.id)
    assert doc.status == IdStatus.REJECTED and doc.rejection_reason == "Photo of a photo - not the original card."
    assert (await fresh(db, User, customer.id)).id_status == IdStatus.REJECTED
    assert await db.scalar(select(AuditLog).where(AuditLog.action == "identity.rejected"))


async def test_owner_cannot_review_self_uploaded_customer_card_but_superadmin_can(client, db, fake_ocr):
    owner = await make_owner(db)
    customer = await rahul(db)
    fake_ocr(front={"tesseract": aadhaar_text()})
    doc = await submit(db, customer)  # uploaded by the customer, not at the owner's desk
    await process(doc.id)
    await login_as(client, owner)
    assert (await client.get(f"/owner/identity/{doc.id}")).status_code == 404
    admin = await make_user(db, role=Role.SUPERADMIN)
    await login_as(client, admin)
    assert (await client.get(f"/owner/identity/{doc.id}")).status_code == 200
    r = await post_form(client, f"/owner/identity/{doc.id}/review", {"decision": "approve", "next": "/superadmin/reviews"})
    assert r.status_code == 303 and r.headers["location"] == "/superadmin/reviews"
    assert (await fresh(db, IdentityDocument, doc.id)).status == IdStatus.VERIFIED


async def test_review_refuses_to_approve_a_duplicate_number(client, db, fake_ocr):
    owner, customer, doc = await _desk_card(db, fake_ocr)
    other = await make_customer(db)
    from app.security.crypto import pepper_hash

    db.add(IdentityDocument(user_id=other.id, doc_type=IdDocType.AADHAAR, country_code="IN", front_key=doc.front_key,
                            status=IdStatus.VERIFIED, id_number_hash=pepper_hash(aadhaar_no())))
    d = await fresh(db, IdentityDocument, doc.id)
    d.id_number_hash = pepper_hash(aadhaar_no())
    await db.commit()
    await login_as(client, owner)
    r = await post_form(client, f"/owner/identity/{doc.id}/review", {"decision": "approve", "seen_original": "1"})
    assert r.status_code == 400 and "already verified on another account" in r.text
    assert (await fresh(db, IdentityDocument, doc.id)).status == IdStatus.NEEDS_REVIEW


# ====================================================================== N+1 guard
async def test_identity_page_query_count_is_constant(client, db, fake_ocr):
    user = await rahul(db)
    await login_as(client, user)
    fake_ocr(front={"tesseract": aadhaar_text(), "apple_vision": aadhaar_text()})

    async def add_docs(n: int) -> None:
        for _ in range(n):
            doc = await submit(db, user, uploaded_by=user)
            await process(doc.id)

    async def measure() -> int:
        with QueryCounter() as qc:
            r = await client.get("/account/identity")
        assert r.status_code == 200
        return qc.count

    await add_docs(2)
    few = await measure()
    await add_docs(6)
    many = await measure()
    assert few == many and few > 0


# ====================================================================== real engines, end to end
def _font(paths: tuple[str, ...], size: int):
    from PIL import ImageFont

    for path in paths:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return None


def synthetic_passport(surname: str = "MEHTA", given: str = "RAHUL", number: str = "J8369854",
                       dob: str = "950517", expiry: str = "320516") -> bytes:
    """A large, clear passport-style page with a valid ICAO 9303 TD3 MRZ (specimen - not a real document)."""
    from PIL import Image, ImageDraw

    cd = lambda s: str(idparse.mrz_check_digit(s))  # noqa: E731
    l1 = ("P<IND" + surname + "<<" + given.replace(" ", "<")).ljust(44, "<")[:44]
    num = number.ljust(9, "<")
    l2 = num + cd(num) + "IND" + dob + cd(dob) + "M" + expiry + cd(expiry) + "<" * 14 + "<"
    l2 += cd(l2[0:10] + l2[13:20] + l2[21:43])
    sans = ("/System/Library/Fonts/Supplemental/Arial Bold.ttf", "C:/Windows/Fonts/arialbd.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
    mono = ("/System/Library/Fonts/Supplemental/Courier New Bold.ttf", "C:/Windows/Fonts/courbd.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf")
    big, mid, mrz = _font(sans, 46), _font(sans, 34), _font(mono, 50)
    im = Image.new("RGB", (1500, 1000), (246, 244, 236))
    d = ImageDraw.Draw(im)
    d.text((60, 40), "REPUBLIC OF INDIA - PASSPORT", fill=(20, 40, 90), font=big)
    d.rectangle((60, 130, 360, 520), fill=(190, 200, 215))
    y = 140
    for label, value in (("Surname", surname), ("Given Name(s)", given), ("Date of Birth", f"{dob[4:]}/{dob[2:4]}/19{dob[:2]}"),
                         ("Passport No.", number)):
        d.text((420, y), label, fill=(90, 90, 90), font=mid)
        d.text((420, y + 44), value, fill=(10, 10, 10), font=big)
        y += 110
    d.text((60, 800), l1, fill=(0, 0, 0), font=mrz)
    d.text((60, 880), l2, fill=(0, 0, 0), font=mrz)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


LOCAL_ENGINES = [e for e in ocr_mod.available_engines() if e in ocr_mod.LOCAL_ENGINES]


@pytest.mark.skipif(len(LOCAL_ENGINES) < 2, reason=f"needs >= 2 local OCR engines (found: {LOCAL_ENGINES or 'none'})")
async def test_integration_synthetic_passport_verified_by_real_engines(client, db, ai_down):
    user = await rahul(db)
    await login_as(client, user)
    r = await post_form(client, "/account/identity", {"id_doc_type": "passport", "id_country": "IN"},
                        files={"id_front": ("passport.png", synthetic_passport(), "image/png")})
    assert r.status_code == 303
    doc = await db.scalar(select(IdentityDocument).where(IdentityDocument.user_id == user.id))
    await process(doc.id)
    doc = await fresh(db, IdentityDocument, doc.id)
    agreeing = [e["engine"] for e in doc.engine_results if e["agrees"]]
    assert doc.status == IdStatus.VERIFIED, (doc.checks, doc.engine_results)
    assert len(agreeing) >= 2 and set(agreeing) <= set(LOCAL_ENGINES)
    assert doc.id_number_masked.endswith("9854") and doc.name_on_id == "Rahul Mehta" and doc.dob_on_id == DOB
    assert doc.expires_on == date(2032, 5, 16)
    assert "J8369854" not in json.dumps(doc.engine_results) + json.dumps(doc.checks)
    assert (await fresh(db, User, user.id)).id_status == IdStatus.VERIFIED
    page = await client.get("/account/identity")
    assert "Your identity is verified" in page.text and "Agrees" in page.text


@pytest.mark.live_ai
@pytest.mark.skipif(os.environ.get("IRONPULSE_LIVE_AI") != "1", reason="set IRONPULSE_LIVE_AI=1 to use the real Gemma gateway")
async def test_live_gateway_vision_ocr_and_ai_check(client, db):
    """Opt-in: the real provider chain (Gemma gateway first) transcribes the card and runs the AI document check."""
    ai.use_providers(None)
    user = await rahul(db)
    await login_as(client, user)
    r = await post_form(client, "/account/identity", {"id_doc_type": "passport", "id_country": "IN"},
                        files={"id_front": ("passport.png", synthetic_passport(), "image/png")})
    assert r.status_code == 303
    doc = await db.scalar(select(IdentityDocument).where(IdentityDocument.user_id == user.id))
    await process(doc.id)
    doc = await fresh(db, IdentityDocument, doc.id)
    llm = next(e for e in doc.engine_results if e["engine"] == "vision_llm")
    assert llm["ok"] and llm["agrees"], doc.engine_results
    assert doc.ai_summary and doc.status == IdStatus.VERIFIED
