"""WP-C1 staff: add staff (invite), activation check job, staff page, documents, suspend/reactivate, profile
edits, trainer clients, staff portal access control and N+1 guards."""

from __future__ import annotations

import asyncio
import html
import re
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, update

from app.core.utils import utcnow
from app.db import QueryCounter
from app.models import (
    AuditLog,
    AuthToken,
    EmailOutbox,
    Employee,
    EmployeeDocument,
    HealthCondition,
    JobPosting,
    MetricEntry,
    Notification,
    Subscription,
    TrainerClient,
    User,
    VerificationRun,
    WorkoutPlan,
)
from app.models.enums import (
    ConditionKind,
    EmploymentStatus,
    EmployeeRole,
    Goal,
    IdStatus,
    Role,
    ShiftMode,
    SubscriptionStatus,
    TokenPurpose,
    VerificationStatus,
    WorkMode,
)
from app.security.crypto import token_hash
from app.services import staffing as S
from app.services import storage
from app.services.jobs import run_job_inline
from tests.conftest import login_as, post_form, run_jobs
from tests.factories import (
    make_branch,
    make_customer,
    make_employee,
    make_gym,
    make_owner,
    make_plans,
    make_shift,
    make_subscription,
    make_user,
    png_bytes,
)


# ====================================================================== helpers
def staff_form(**overrides: str) -> dict[str, str]:
    data = {
        "full_name": "Rahul Mehta", "invite_email": "rahul.coach@example.com", "phone": "+91 98450 11111",
        "role": "trainer", "role_title": "Head coach", "employment_type": "full_time", "work_mode": "offline",
        "country_code": "IN", "currency": "", "monthly_salary": "35000", "hire_date": date.today().isoformat(),
        "bio": "Strength coach.", "specialties": "Strength, Mobility, strength", "certifications": "ACE CPT",
        "years_experience": "6", "is_public": "1",
    }
    data.update(overrides)
    return data


def photo_file(name: str = "me.png") -> tuple[str, tuple[str, bytes, str]]:
    return ("photo", (name, png_bytes((120, 120), "teal"), "image/png"))


async def owner_gym(db, *, verified_branch: bool = True):
    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    branch = await make_branch(db, gym, verified=verified_branch)
    return owner, gym, branch


async def reload(db, model, ident):
    """Fresh row from the database (other identities in the session stay untouched)."""
    return await db.get(model, ident, populate_existing=True)


async def add_cert(db, emp: Employee, *, text: str = "CERTIFICATE") -> EmployeeDocument:
    stored, _ = await storage.save_private_file(png_bytes((200, 120), "white", text=text), kind="staff_docs",
                                                filename="cert.png")
    doc = EmployeeDocument(employee_id=emp.id, doc_type="certification", file_key=stored.key,
                           original_filename="cert.png", content_type=stored.content_type, sha256=stored.sha256,
                           size_bytes=stored.size)
    db.add(doc)
    await db.commit()
    return doc


def fake_ocr(monkeypatch, text: str = "", *, fail: bool = False) -> list[str]:
    from app.services import ocr
    from app.services.ocr import MultiOcrResult, OcrEngineResult

    calls: list[str] = []

    async def _ocr_document(data, content_type, *, max_pages=3, engines=None):  # noqa: ANN001
        calls.append(content_type)
        if fail:
            raise NotImplementedError("no OCR engines in this build")
        return MultiOcrResult([OcrEngineResult("tesseract", True, text, 0.9)])

    monkeypatch.setattr(ocr, "ocr_document", _ocr_document)
    return calls


async def check(emp_id: int) -> None:
    await run_job_inline("staff_activation_check", {"employee_id": emp_id})


async def count_get(client, url: str) -> int:
    with QueryCounter() as qc:
        r = await client.get(url)
    assert r.status_code == 200, (url, r.status_code, r.text[:1500])
    return qc.count


# ====================================================================== smoke: every page renders
async def test_smoke_owner_pages_render(client, db):
    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    branch = await make_branch(db, gym)
    emp = await make_employee(db, gym)
    await make_shift(db, emp, branch=branch, weekday=0, start="06:00", end="10:00")
    job = JobPosting(gym_id=gym.id, title="Coach", role=emp.role, employment_type=emp.employment_type,
                     location="Bengaluru", currency="INR", requirements="Certified coach with 2 years experience")
    db.add(job)
    await db.commit()
    await login_as(client, owner)
    for url in ("/owner/staff", f"/owner/gyms/{gym.id}/staff/new", f"/owner/staff/{emp.id}",
                *(f"/owner/staff/{emp.id}?tab={t}" for t in ("schedule", "clients", "documents", "profile", "activity")),
                "/owner/jobs", f"/owner/gyms/{gym.id}/jobs/new", f"/owner/jobs/{job.id}", f"/owner/jobs/{job.id}/enquiries"):
        r = await client.get(url)
        assert r.status_code == 200, (url, r.status_code, r.text[:2000])
        assert "<script>" not in r.text and " onclick=" not in r.text and ' style="' not in r.text


async def test_owner_pages_empty_states(client, db):
    owner = await make_owner(db)
    await login_as(client, owner)
    r = await client.get("/owner/staff")
    assert r.status_code == 200 and "Create a gym to start building your team" in r.text
    r = await client.get("/owner/staff/new")
    assert r.status_code == 303 and r.headers["location"] == "/owner/gyms/new"
    gym = await make_gym(db, owner)
    r = await client.get("/owner/staff")
    assert "No staff yet" in r.text
    r = await client.get("/owner/staff/new")
    assert r.status_code == 303 and r.headers["location"] == f"/owner/gyms/{gym.id}/staff/new"
    await make_gym(db, owner)
    r = await client.get("/owner/staff/new")
    assert r.status_code == 200 and "Choose the gym" in r.text
    r = await client.get("/owner/staff?q=nobody")
    assert "Nobody matches these filters" in r.text


# ====================================================================== add staff
async def test_add_staff_requires_photo(client, db):
    owner, gym, _ = await owner_gym(db)
    await login_as(client, owner)
    r = await post_form(client, f"/owner/gyms/{gym.id}/staff/new", staff_form())
    assert r.status_code == 400 and "A staff photo is required." in r.text
    assert await db.scalar(select(func.count(Employee.id))) == 0
    assert await db.scalar(select(func.count(AuthToken.id))) == 0


async def test_add_staff_creates_invite_token_and_queues_email(client, db):
    owner, gym, _ = await owner_gym(db)
    await login_as(client, owner)
    files = [photo_file(), ("certification_files", ("ace.png", png_bytes(text="ACE"), "image/png")),
             ("resume_file", ("cv.png", png_bytes(text="CV"), "image/png"))]
    r = await post_form(client, f"/owner/gyms/{gym.id}/staff/new", staff_form(invite_email="Rahul.Coach@Example.com"),
                        files=files)
    assert r.status_code == 303, r.text[:2000]
    emp = await db.scalar(select(Employee))
    assert r.headers["location"] == f"/owner/staff/{emp.id}"
    assert emp.status == EmploymentStatus.INVITED and emp.user_id is None
    assert emp.invite_email == "rahul.coach@example.com" and emp.is_trainer
    assert emp.photo_key.startswith("public/staff/") and storage.path_for(emp.photo_key).is_file()
    assert emp.currency == "INR" and emp.monthly_salary == Decimal("35000.00")
    assert emp.specialties == ["Strength", "Mobility"]  # de-duplicated, case-insensitive
    assert emp.verification_status == VerificationStatus.QUEUED

    token = await db.scalar(select(AuthToken).where(AuthToken.employee_id == emp.id))
    assert token.purpose == TokenPurpose.STAFF_INVITE and token.email == "rahul.coach@example.com"
    assert timedelta(days=6, hours=23) < token.expires_at - utcnow() <= timedelta(days=7)
    mail = await db.scalar(select(EmailOutbox).where(EmailOutbox.to_email == "rahul.coach@example.com"))
    assert mail is not None and gym.name in mail.subject
    m = re.search(r"http://testserver/auth/staff-invite\?token=([A-Za-z0-9_\-]+)", mail.text_body)
    assert m and token_hash(m.group(1)) == token.token_hash

    docs = (await db.execute(select(EmployeeDocument).where(EmployeeDocument.employee_id == emp.id))).scalars().all()
    assert sorted(d.doc_type for d in docs) == ["certification", "resume"]
    assert all(d.file_key.startswith("private/staff_docs/") for d in docs)
    assert await db.scalar(select(AuditLog).where(AuditLog.action == "staff.create", AuditLog.entity_id == emp.id))
    page = await client.get(r.headers["location"])
    assert "Invitation sent to rahul.coach@example.com" in page.text and "Activation checklist" in page.text


async def test_add_staff_offline_needs_a_verified_branch(client, db):
    owner, gym, _ = await owner_gym(db, verified_branch=False)
    await login_as(client, owner)
    for mode in ("offline", "both"):
        r = await post_form(client, f"/owner/gyms/{gym.id}/staff/new", staff_form(work_mode=mode), files=[photo_file()])
        assert r.status_code == 400 and "VERIFIED branch" in r.text
    form = await client.get(f"/owner/gyms/{gym.id}/staff/new")
    assert "can only work online" in form.text
    r = await post_form(client, f"/owner/gyms/{gym.id}/staff/new", staff_form(work_mode="online"), files=[photo_file()])
    assert r.status_code == 303
    emp = await db.scalar(select(Employee))
    assert emp.work_mode == WorkMode.ONLINE


async def test_add_staff_validation_and_email_rules(client, db):
    owner, gym, _ = await owner_gym(db)
    await make_employee(db, gym, status=EmploymentStatus.INVITED)
    existing_invite = (await db.scalar(select(Employee))).invite_email
    gone = await make_user(db, role=Role.CUSTOMER, email="gone@example.com")
    gone.deleted_at, gone.purge_after = utcnow(), utcnow() + timedelta(days=30)
    await db.commit()
    await login_as(client, owner)
    url = f"/owner/gyms/{gym.id}/staff/new"
    cases = [
        (staff_form(invite_email=""), "An invite e-mail is required"),
        (staff_form(invite_email="not-an-email"), "Enter a valid e-mail address."),
        (staff_form(monthly_salary="0"), "must be greater than zero"),
        (staff_form(monthly_salary="-5"), "plain number"),
        (staff_form(role="other", role_title=""), "Describe the role"),
        (staff_form(country_code="XX"), "Choose the payroll country"),
        (staff_form(currency="ABCD"), "3-letter ISO currency"),
        (staff_form(hire_date="2001-02-30"), "Enter the hire date"),
        (staff_form(years_experience="200"), "between 0 and 70"),
        (staff_form(phone="12"), "valid phone number"),
        (staff_form(invite_email=existing_invite), "already has a staff record"),
        (staff_form(invite_email=owner.email), "gym admin account"),
        (staff_form(invite_email="gone@example.com"), "deleted recently"),
    ]
    for data, message in cases:
        r = await post_form(client, url, data, files=[photo_file()])
        assert r.status_code == 400, message
        assert message in r.text.replace("&#39;", "'"), message
    assert await db.scalar(select(func.count(Employee.id))) == 1


def test_validate_staff_form_defaults_currency_from_country():
    data, errors, values = S.validate_staff_form(staff_form(country_code="AE", currency="", work_mode="online"),
                                                 verified_branches=0)
    assert errors == {} and data is not None and data.currency == "AED" and values["currency"] == "AED"
    data, errors, _ = S.validate_staff_form(staff_form(country_code="GR", currency=""), verified_branches=1)
    assert data is not None and data.currency == "EUR" and data.work_mode == WorkMode.OFFLINE
    data, errors, _ = S.validate_staff_form(staff_form(monthly_salary="1,20,000.50"), verified_branches=1)
    assert data is not None and data.monthly_salary == Decimal("120000.50")


async def test_add_staff_permissions(client, db, anon_client_factory):
    owner, gym, _ = await owner_gym(db)
    intruder = await make_owner(db)
    await login_as(client, intruder)
    assert (await client.get(f"/owner/gyms/{gym.id}/staff/new")).status_code == 404
    r = await post_form(client, f"/owner/gyms/{gym.id}/staff/new", staff_form(), files=[photo_file()])
    assert r.status_code == 404
    emp = await make_employee(db, gym)
    assert (await client.get(f"/owner/staff/{emp.id}")).status_code == 404
    listing = await client.get("/owner/staff")
    assert listing.status_code == 200 and emp.full_name not in listing.text
    staff_client = anon_client_factory("203.0.113.21")
    await login_as(staff_client, await make_user(db, role=Role.STAFF))
    assert (await staff_client.get("/owner/staff")).status_code == 403
    anon = anon_client_factory("203.0.113.22")
    r = await anon.get("/owner/staff")
    assert r.status_code == 303 and r.headers["location"].startswith("/auth/login")


async def test_resend_invite_invalidates_old_links(client, db):
    owner, gym, _ = await owner_gym(db)
    await login_as(client, owner)
    await post_form(client, f"/owner/gyms/{gym.id}/staff/new", staff_form(), files=[photo_file()])
    emp = await db.scalar(select(Employee))
    first = await db.scalar(select(AuthToken.token_hash).where(AuthToken.employee_id == emp.id))
    r = await post_form(client, f"/owner/staff/{emp.id}/resend-invite")
    assert r.status_code == 303
    tokens = (await db.execute(select(AuthToken.token_hash).where(AuthToken.employee_id == emp.id))).scalars().all()
    assert len(tokens) == 1 and tokens[0] != first
    assert await db.scalar(select(func.count(EmailOutbox.id)).where(EmailOutbox.to_email == emp.invite_email)) == 2
    reminder = (await db.execute(select(EmailOutbox.subject).order_by(EmailOutbox.id.desc()))).scalars().first()
    assert reminder.startswith("IronPulse: Reminder")
    # once linked, no new invitation is sent
    emp.user_id = (await make_user(db, role=Role.STAFF)).id
    await db.commit()
    r = await post_form(client, f"/owner/staff/{emp.id}/resend-invite")
    assert r.status_code == 303
    assert await db.scalar(select(func.count(AuthToken.id)).where(AuthToken.employee_id == emp.id)) == 1


async def test_staff_invite_resends_are_serialised_throttled_and_capped(client, db):
    """F13 (staff side): 'Re-send invitation' had no throttle at all - eight parallel clicks e-mailed eight
    invitations to the address at once, and a script could send any number a day. Re-sends of one staff record now
    run one after the other (row lock): at most one per INVITE_RESEND_SECONDS and STAFF_INVITE_RESENDS_PER_DAY in any
    24 hours, each one superseding the earlier link."""
    from app.routers import owner_staff as OS
    from tests.test_superadmin import flash_text

    owner, gym, _ = await owner_gym(db)
    await login_as(client, owner)
    await post_form(client, f"/owner/gyms/{gym.id}/staff/new", staff_form(), files=[photo_file()])
    emp = await db.scalar(select(Employee))

    async def outbox() -> int:
        return int(await db.scalar(select(func.count(EmailOutbox.id)).where(EmailOutbox.to_email == emp.invite_email)))

    rs = await asyncio.gather(*[post_form(client, f"/owner/staff/{emp.id}/resend-invite") for _ in range(8)])
    assert {r.status_code for r in rs} == {303}
    flashes = [flash_text(r) for r in rs]
    assert sum("A fresh invitation was sent" in f for f in flashes) == 1
    assert sum("a moment ago" in f for f in flashes) == 7, flashes
    assert await outbox() == 2  # the invitation + one re-send
    assert await db.scalar(select(func.count(AuthToken.id)).where(AuthToken.employee_id == emp.id,
                                                                  AuthToken.used_at.is_(None))) == 1

    async def age(minutes: int) -> None:
        await db.execute(update(AuditLog).where(AuditLog.action == "staff.invite_resent")
                         .values(created_at=utcnow() - timedelta(minutes=minutes)))
        await db.commit()

    for _ in range(OS.STAFF_INVITE_RESENDS_PER_DAY - 1):
        await age(3)
        r = await post_form(client, f"/owner/staff/{emp.id}/resend-invite")
        assert "A fresh invitation was sent" in flash_text(r)
    await age(3)
    r = await post_form(client, f"/owner/staff/{emp.id}/resend-invite")
    assert r.status_code == 303 and f"{OS.STAFF_INVITE_RESENDS_PER_DAY} times in the last 24 hours" in flash_text(r)
    assert await outbox() == 1 + OS.STAFF_INVITE_RESENDS_PER_DAY
    await age(25 * 60)  # a rolling window: a day later it can be sent again
    assert "A fresh invitation was sent" in flash_text(await post_form(client, f"/owner/staff/{emp.id}/resend-invite"))


async def test_existing_staff_account_gets_in_app_heads_up(client, db):
    owner, gym, _ = await owner_gym(db)
    staff = await make_user(db, role=Role.STAFF, email="already@example.com")
    await login_as(client, owner)
    r = await post_form(client, f"/owner/gyms/{gym.id}/staff/new", staff_form(invite_email="already@example.com"),
                        files=[photo_file()])
    assert r.status_code == 303
    note = await db.scalar(select(Notification).where(Notification.user_id == staff.id))
    assert note is not None and "invitation" in note.title.lower()


# ====================================================================== activation check
async def test_activation_each_missing_requirement_keeps_pending(db):
    owner, gym, _ = await owner_gym(db)
    emp = await make_employee(db, gym, role=EmployeeRole.RECEPTIONIST, status=EmploymentStatus.INVITED)
    emp.verification_status = VerificationStatus.DRAFT
    await db.commit()
    await check(emp.id)
    emp = await reload(db, Employee, emp.id)
    assert emp.status == EmploymentStatus.INVITED and "Own IronPulse account linked" in emp.verification_summary

    user = await make_user(db, role=Role.STAFF, photo_key="", id_status=IdStatus.PENDING)
    emp.user_id = user.id  # WP-A2 links the account, then enqueues the check
    await db.commit()
    await check(emp.id)
    emp = await reload(db, Employee, emp.id)
    assert emp.status == EmploymentStatus.PENDING_VERIFICATION
    assert "Profile photo" in emp.verification_summary and "Government ID verified" in emp.verification_summary
    note = await db.scalar(select(Notification).where(Notification.user_id == user.id))
    assert note is not None and "Almost there" in note.title

    user = await reload(db, User, user.id)
    user.photo_key = "public/avatars/" + "b" * 32 + ".jpg"
    await db.commit()
    await check(emp.id)
    emp = await reload(db, Employee, emp.id)
    assert emp.status == EmploymentStatus.PENDING_VERIFICATION and "Profile photo" not in emp.verification_summary

    user = await reload(db, User, user.id)
    user.id_status = IdStatus.VERIFIED
    await db.commit()
    await check(emp.id)
    emp = await reload(db, Employee, emp.id)
    assert emp.status == EmploymentStatus.ACTIVE and emp.id_status == IdStatus.VERIFIED
    assert emp.verification_status == VerificationStatus.VERIFIED
    titles = (await db.execute(select(Notification.title, Notification.user_id))).all()
    assert any(uid == user.id and "approved" in t for t, uid in titles)
    assert any(uid == owner.id and "is now active" in t for t, uid in titles)
    assert await db.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == "staff.status")) == 2


async def test_trainer_without_certification_stays_pending(db):
    owner, gym, _ = await owner_gym(db)
    emp = await make_employee(db, gym, status=EmploymentStatus.PENDING_VERIFICATION,
                              user=await make_user(db, role=Role.STAFF))
    emp.verification_status = VerificationStatus.DRAFT
    await db.commit()
    await check(emp.id)
    emp = await reload(db, Employee, emp.id)
    assert emp.status == EmploymentStatus.PENDING_VERIFICATION
    assert "Trainer certification uploaded" in emp.verification_summary


async def _trainer_with_cert(db, *, name: str = "Rahul Mehta"):
    owner, gym, _ = await owner_gym(db)
    user = await make_user(db, role=Role.STAFF, full_name=name)
    emp = await make_employee(db, gym, user=user, status=EmploymentStatus.PENDING_VERIFICATION)
    emp.verification_status = VerificationStatus.QUEUED
    await db.commit()
    doc = await add_cert(db, emp)
    return owner, gym, user, emp, doc


async def test_certification_ocr_name_match_activates_trainer(db, monkeypatch, ai_down):
    owner, gym, user, emp, doc = await _trainer_with_cert(db)
    calls = fake_ocr(monkeypatch, "CERTIFIED PERSONAL TRAINER\nawarded to MR. RAHUL MEHTA\n2024")
    await check(emp.id)
    emp = await reload(db, Employee, emp.id)
    assert emp.verification_status == VerificationStatus.VERIFIED and emp.status == EmploymentStatus.ACTIVE
    doc = await reload(db, EmployeeDocument, doc.id)
    assert "RAHUL MEHTA" in doc.ocr_text and doc.assessment["name_match"] is True and doc.assessment["method"] == "ocr"
    run = await db.scalar(select(VerificationRun).where(VerificationRun.employee_id == emp.id))
    assert run.outcome == VerificationStatus.VERIFIED
    # OCR text is cached: a second check does not OCR again
    await check(emp.id)
    assert calls == ["image/jpeg"]


async def test_certification_ai_fallback_confirms_name(db, monkeypatch, fake_ai):
    owner, gym, user, emp, doc = await _trainer_with_cert(db)
    fake_ocr(monkeypatch, "ISSA certificate of completion")
    provider = fake_ai(responses=['{"is_certificate": true, "name_on_document": "Rahul  Mehta", "name_matches": true, '
                                  '"issuer": "ISSA", "confidence": 0.92}'])
    await check(emp.id)
    emp = await reload(db, Employee, emp.id)
    assert emp.verification_status == VerificationStatus.VERIFIED and emp.status == EmploymentStatus.ACTIVE
    doc = await reload(db, EmployeeDocument, doc.id)
    assert doc.assessment["method"] == "ai" and doc.assessment["ai"]["revalidated"] is True
    assert "user-provided" in provider.prompts[0]["prompt"] and "Rahul Mehta" in provider.prompts[0]["prompt"]


async def test_certification_ai_output_is_revalidated(db, monkeypatch, fake_ai):
    owner, gym, user, emp, doc = await _trainer_with_cert(db)
    fake_ocr(monkeypatch, "Certificate of achievement")
    fake_ai(responses=['{"is_certificate": true, "name_on_document": "Priya Sharma", "name_matches": true, '
                       '"confidence": 0.99}'])
    await check(emp.id)
    emp = await reload(db, Employee, emp.id)
    assert emp.verification_status == VerificationStatus.NEEDS_REVIEW
    assert emp.status == EmploymentStatus.PENDING_VERIFICATION
    note = await db.scalar(select(Notification).where(Notification.user_id == owner.id,
                                                      Notification.title.like("Review%certification")))
    assert note is not None and note.link == f"/owner/staff/{emp.id}?tab=documents"


async def test_certification_check_ai_unavailable_needs_review(db, monkeypatch, ai_down):
    owner, gym, user, emp, doc = await _trainer_with_cert(db)
    fake_ocr(monkeypatch, "illegible scan")
    await check(emp.id)
    emp = await reload(db, Employee, emp.id)
    assert emp.verification_status == VerificationStatus.NEEDS_REVIEW
    assert emp.status == EmploymentStatus.PENDING_VERIFICATION
    run = await db.scalar(select(VerificationRun).where(VerificationRun.employee_id == emp.id))
    assert run.outcome == VerificationStatus.NEEDS_REVIEW and "AI unavailable" in run.summary


async def test_certification_check_survives_ocr_failure(db, monkeypatch, ai_down):
    owner, gym, user, emp, doc = await _trainer_with_cert(db)
    fake_ocr(monkeypatch, fail=True)
    await check(emp.id)
    emp = await reload(db, Employee, emp.id)
    doc = await reload(db, EmployeeDocument, doc.id)
    assert emp.verification_status == VerificationStatus.NEEDS_REVIEW and doc.ocr_text is None
    assert "OCR unavailable" in doc.assessment["ocr_error"]


async def test_owner_manual_certification_approval_activates(client, db, monkeypatch, ai_down):
    owner, gym, user, emp, doc = await _trainer_with_cert(db)
    fake_ocr(monkeypatch, "blurred")
    await check(emp.id)
    await login_as(client, owner)
    page = await client.get(f"/owner/staff/{emp.id}?tab=documents")
    assert "Certification needs your review" in page.text
    r = await post_form(client, f"/owner/staff/{emp.id}/verify-documents")
    assert r.status_code == 303
    emp = await reload(db, Employee, emp.id)
    assert emp.verification_status == VerificationStatus.VERIFIED and emp.status == EmploymentStatus.ACTIVE
    run = await db.scalar(select(VerificationRun).where(VerificationRun.employee_id == emp.id,
                                                        VerificationRun.manual.is_(True)))
    assert run is not None and run.reviewer_id == owner.id
    assert await db.scalar(select(AuditLog).where(AuditLog.action == "staff.documents_approved"))


async def test_recheck_button_enqueues_job_and_worker_runs_it(client, db, monkeypatch, ai_down):
    owner, gym, user, emp, doc = await _trainer_with_cert(db)
    fake_ocr(monkeypatch, "Trainer certificate - Rahul Mehta")
    await login_as(client, owner)
    r = await post_form(client, f"/owner/staff/{emp.id}/recheck")
    assert r.status_code == 303
    assert await run_jobs() >= 1
    emp = await reload(db, Employee, emp.id)
    assert emp.status == EmploymentStatus.ACTIVE


async def test_recheck_runs_inline_for_non_trainers(client, db):
    owner, gym, _ = await owner_gym(db)
    emp = await make_employee(db, gym, role=EmployeeRole.ACCOUNTANT, status=EmploymentStatus.PENDING_VERIFICATION,
                              user=await make_user(db, role=Role.STAFF))
    await login_as(client, owner)
    r = await post_form(client, f"/owner/staff/{emp.id}/recheck")
    assert r.status_code == 303
    emp = await reload(db, Employee, emp.id)
    assert emp.status == EmploymentStatus.ACTIVE


async def test_activation_check_skips_suspended_and_unknown(db):
    owner, gym, _ = await owner_gym(db)
    emp = await make_employee(db, gym, role=EmployeeRole.CLEANER, status=EmploymentStatus.SUSPENDED)
    assert await S.run_activation_check(db, emp.id) is None
    await db.commit()
    assert (await reload(db, Employee, emp.id)).status == EmploymentStatus.SUSPENDED
    await check(999999)  # unknown id: silently ignored
    await run_job_inline("staff_activation_check", {"employee_id": "garbage"})


async def test_active_staff_losing_a_requirement_is_paused(db):
    owner, gym, _ = await owner_gym(db)
    emp = await make_employee(db, gym, role=EmployeeRole.MANAGER)
    user = await db.get(User, emp.user_id)
    user.id_status = IdStatus.REJECTED
    await db.commit()
    await check(emp.id)
    emp = await reload(db, Employee, emp.id)
    assert emp.status == EmploymentStatus.PENDING_VERIFICATION
    note = await db.scalar(select(Notification).where(Notification.user_id == user.id))
    assert "paused" in note.title


def test_name_matching_is_ocr_tolerant_but_strict():
    assert S.name_matches_text(["Rahul Mehta"], "This certifies that RAHUL MEHTA has completed")
    assert S.name_matches_text(["Rahul Mehta"], "awarded to Rahu1 Mehta")  # OCR noise
    assert S.name_matches_text(["Dr. Priya K. Sharma"], "PRIYA SHARMA - Yoga Alliance RYT 200")
    assert not S.name_matches_text(["Rahul Mehta"], "awarded to Rahul Verma")
    assert not S.name_matches_text(["Rahul Mehta"], "")
    assert not S.name_matches_text([""], "anything")


# ====================================================================== staff page actions
async def test_suspend_and_reactivate(client, db):
    owner, gym, _ = await owner_gym(db)
    emp = await make_employee(db, gym, role=EmployeeRole.RECEPTIONIST)
    await login_as(client, owner)
    r = await post_form(client, f"/owner/staff/{emp.id}/suspend", {"reason": "Member complaint under review"})
    assert r.status_code == 303
    emp = await reload(db, Employee, emp.id)
    assert emp.status == EmploymentStatus.SUSPENDED
    note = await db.scalar(select(Notification).where(Notification.user_id == emp.user_id))
    assert "suspended" in note.title and "Member complaint" in note.body
    assert await db.scalar(select(AuditLog).where(AuditLog.action == "staff.suspend"))
    r = await post_form(client, f"/owner/staff/{emp.id}/suspend")  # already suspended
    assert r.status_code == 303 and (await reload(db, Employee, emp.id)).status == EmploymentStatus.SUSPENDED
    r = await post_form(client, f"/owner/staff/{emp.id}/reactivate")
    assert r.status_code == 303
    assert (await reload(db, Employee, emp.id)).status == EmploymentStatus.ACTIVE
    # reactivating with a missing requirement -> pending verification
    await post_form(client, f"/owner/staff/{emp.id}/suspend")
    user = await reload(db, User, emp.user_id)
    user.id_status = IdStatus.PENDING
    await db.commit()
    await post_form(client, f"/owner/staff/{emp.id}/reactivate")
    assert (await reload(db, Employee, emp.id)).status == EmploymentStatus.PENDING_VERIFICATION
    page = await client.get(f"/owner/staff/{emp.id}?tab=activity")
    assert "Suspended" in page.text and "Reactivated" in page.text


async def test_documents_upload_download_delete(client, db, anon_client_factory):
    owner, gym, _ = await owner_gym(db)
    emp = await make_employee(db, gym, role=EmployeeRole.ACCOUNTANT)
    await login_as(client, owner)
    r = await post_form(client, f"/owner/staff/{emp.id}/documents", {"doc_type": "contract"},
                        files={"document": ("contract.png", png_bytes(text="CONTRACT"), "image/png")})
    assert r.status_code == 303
    doc = await db.scalar(select(EmployeeDocument).where(EmployeeDocument.employee_id == emp.id))
    assert doc.doc_type == "contract"
    r = await client.get(f"/owner/staff/{emp.id}/documents/{doc.id}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert "no-store" in r.headers["cache-control"] and r.headers["content-disposition"].startswith("inline")
    # validation errors re-render the page with 400
    r = await post_form(client, f"/owner/staff/{emp.id}/documents", {"doc_type": "contract"})
    assert r.status_code == 400 and "Choose a file to upload." in r.text
    r = await post_form(client, f"/owner/staff/{emp.id}/documents", {"doc_type": "contract"},
                        files={"document": ("evil.png", b"<?php echo 1; ?>", "image/png")})
    assert r.status_code == 400 and "not a valid image" in r.text
    r = await post_form(client, f"/owner/staff/{emp.id}/documents", {"doc_type": "passport"},
                        files={"document": ("x.png", png_bytes(), "image/png")})
    assert r.status_code == 400 and "Choose the document type." in r.text
    other = anon_client_factory("203.0.113.30")
    await login_as(other, await make_owner(db))
    assert (await other.get(f"/owner/staff/{emp.id}/documents/{doc.id}")).status_code == 404
    assert (await post_form(other, f"/owner/staff/{emp.id}/documents/{doc.id}/delete")).status_code == 404
    r = await post_form(client, f"/owner/staff/{emp.id}/documents/{doc.id}/delete")
    assert r.status_code == 303
    assert await db.scalar(select(func.count(EmployeeDocument.id))) == 0
    assert not storage.path_for(doc.file_key).exists()


async def test_profile_edit_notifies_and_audits_salary_change(client, db):
    owner, gym, branch = await owner_gym(db)
    emp = await make_employee(db, gym, role=EmployeeRole.RECEPTIONIST)
    await make_shift(db, emp, branch=branch, weekday=1, start="08:00", end="12:00")
    await login_as(client, owner)
    values = S.staff_values_from_employee(emp)
    values.update(monthly_salary="42000", is_public="1")
    r = await post_form(client, f"/owner/staff/{emp.id}", values)
    assert r.status_code == 303
    emp = await reload(db, Employee, emp.id)
    assert emp.monthly_salary == Decimal("42000.00")
    note = await db.scalar(select(Notification).where(Notification.user_id == emp.user_id))
    assert "monthly salary" in note.body and "42,000.00" in note.body
    log = await db.scalar(select(AuditLog).where(AuditLog.action == "staff.update"))
    assert log.detail["salary"]["to"] == "42000.00"
    # switching to online-only while branch shifts exist is refused
    values.update(work_mode="online")
    r = await post_form(client, f"/owner/staff/{emp.id}", values)
    assert r.status_code == 400 and "Remove their branch shifts first" in r.text
    values.update(work_mode="offline", monthly_salary="abc")
    r = await post_form(client, f"/owner/staff/{emp.id}", values)
    assert r.status_code == 400 and "plain number" in r.text
    # the linked account's e-mail cannot be changed from here
    values.update(monthly_salary="42000", invite_email="someone@else.com")
    r = await post_form(client, f"/owner/staff/{emp.id}", values)
    assert (await reload(db, Employee, emp.id)).invite_email != "someone@else.com"


# ====================================================================== trainer clients
async def _gym_with_trainer(db, *, work_mode: WorkMode = WorkMode.BOTH, status=EmploymentStatus.ACTIVE):
    owner, gym, branch = await owner_gym(db)
    trainer = await make_employee(db, gym, work_mode=work_mode, status=status,
                                  user=await make_user(db, role=Role.STAFF))
    plans = {p.code: p for p in await make_plans(db, gym)}
    return owner, gym, branch, trainer, plans


async def test_assign_client_with_trainer_plan(client, db):
    owner, gym, branch, trainer, plans = await _gym_with_trainer(db)
    cust = await make_customer(db)
    sub = await make_subscription(db, cust, plans["offline_trainer"], branch=branch)
    no_trainer = await make_subscription(db, await make_customer(db), plans["offline"], branch=branch)
    await login_as(client, owner)
    page = await client.get(f"/owner/staff/{trainer.id}?tab=clients")
    assert cust.full_name in page.text and f'value="{no_trainer.id}"' not in page.text
    r = await post_form(client, f"/owner/staff/{trainer.id}/clients", {"subscription_id": str(no_trainer.id)})
    assert r.status_code == 400 and "trainer-included plan" in r.text
    r = await post_form(client, f"/owner/staff/{trainer.id}/clients", {"subscription_id": str(sub.id), "mode": "online"})
    assert r.status_code == 400 and "only be coached at branch" in r.text
    r = await post_form(client, f"/owner/staff/{trainer.id}/clients", {"subscription_id": str(sub.id)})
    assert r.status_code == 303
    link = await db.scalar(select(TrainerClient))
    assert link.employee_id == trainer.id and link.mode == ShiftMode.OFFLINE and link.active
    assert (await reload(db, Subscription, sub.id)).trainer_id == trainer.id
    to_customer = await db.scalar(select(Notification).where(Notification.user_id == cust.id))
    to_trainer = await db.scalar(select(Notification).where(Notification.user_id == trainer.user_id))
    assert "Meet your trainer" in to_customer.title and cust.full_name in to_trainer.title

    # reassigning the same subscription to another trainer deactivates the first link
    second = await make_employee(db, gym, user=await make_user(db, role=Role.STAFF))
    r = await post_form(client, f"/owner/staff/{second.id}/clients", {"subscription_id": str(sub.id)})
    assert r.status_code == 303
    links = {lk.employee_id: lk.active for lk in (await db.execute(
        select(TrainerClient).execution_options(populate_existing=True))).scalars().all()}
    assert links == {trainer.id: False, second.id: True}
    note = (await db.execute(select(Notification).where(Notification.user_id == trainer.user_id)
                             .order_by(Notification.id.desc()))).scalars().first()
    assert "reassigned" in note.title

    # unassign
    link2 = await db.scalar(select(TrainerClient).where(TrainerClient.employee_id == second.id))
    r = await post_form(client, f"/owner/staff/{second.id}/clients/{link2.id}/remove")
    assert r.status_code == 303
    assert (await reload(db, TrainerClient, link2.id)).active is False
    assert (await reload(db, Subscription, sub.id)).trainer_id is None


async def test_assign_client_rules(client, db):
    owner, gym, branch, trainer, plans = await _gym_with_trainer(db, work_mode=WorkMode.ONLINE)
    offline_sub = await make_subscription(db, await make_customer(db), plans["offline_trainer"], branch=branch)
    online_sub = await make_subscription(db, await make_customer(db), plans["online_trainer"])
    other_owner, other_gym, other_branch = await owner_gym(db)
    other_plans = {p.code: p for p in await make_plans(db, other_gym)}
    foreign_sub = await make_subscription(db, await make_customer(db), other_plans["combo_trainer"], branch=other_branch)
    await login_as(client, owner)
    r = await post_form(client, f"/owner/staff/{trainer.id}/clients", {"subscription_id": str(offline_sub.id)})
    assert r.status_code == 400 and "doesn&#39;t match this trainer" in r.text
    r = await post_form(client, f"/owner/staff/{trainer.id}/clients", {"subscription_id": str(foreign_sub.id)})
    assert r.status_code == 400 and "trainer-included plan" in r.text
    r = await post_form(client, f"/owner/staff/{trainer.id}/clients", {"subscription_id": str(online_sub.id)})
    assert r.status_code == 303
    pending = await make_employee(db, gym, status=EmploymentStatus.PENDING_VERIFICATION,
                                  user=await make_user(db, role=Role.STAFF))
    r = await post_form(client, f"/owner/staff/{pending.id}/clients", {"subscription_id": str(online_sub.id)})
    assert r.status_code == 400 and "must be approved" in r.text
    receptionist = await make_employee(db, gym, role=EmployeeRole.RECEPTIONIST)
    r = await post_form(client, f"/owner/staff/{receptionist.id}/clients", {"subscription_id": str(online_sub.id)})
    assert r.status_code == 400 and "Only trainers" in r.text


# ====================================================================== staff portal
async def _portal_world(db):
    owner, gym, branch, trainer, plans = await _gym_with_trainer(db)
    staff_user = await db.get(User, trainer.user_id)
    mine = await make_customer(db, full_name="Meera Nair", conditions=["Asthma"])
    sub = await make_subscription(db, mine, plans["offline_trainer"], branch=branch)
    db.add(TrainerClient(employee_id=trainer.id, customer_id=mine.id, subscription_id=sub.id, mode=ShiftMode.OFFLINE))
    db.add(HealthCondition(customer_id=mine.id, kind=ConditionKind.ALLERGY, name="Peanut allergy", tag="food_allergy"))
    db.add(MetricEntry(customer_id=mine.id, recorded_on=date.today(), height_cm=160, weight_kg=64, bmi=25.0))
    db.add(WorkoutPlan(customer_id=mine.id, gym_id=gym.id, goal=Goal.WEIGHT_LOSS, source="trainer", title="Fat loss base",
                       plan={"days": [{"name": "Day A", "items": [{"exercise": "Goblet squat", "sets": 3, "reps": 10}]}]}))
    # another trainer's client at the same gym
    colleague = await make_employee(db, gym, user=await make_user(db, role=Role.STAFF))
    theirs = await make_customer(db, full_name="Arjun Das")
    sub2 = await make_subscription(db, theirs, plans["combo_trainer"], branch=branch)
    db.add(TrainerClient(employee_id=colleague.id, customer_id=theirs.id, subscription_id=sub2.id, mode=ShiftMode.ONLINE))
    # a client of another gym
    o2, g2, b2 = await owner_gym(db)
    p2 = {p.code: p for p in await make_plans(db, g2)}
    t2 = await make_employee(db, g2, user=await make_user(db, role=Role.STAFF))
    stranger = await make_customer(db, full_name="Other Gym Member")
    sub3 = await make_subscription(db, stranger, p2["offline_trainer"], branch=b2)
    db.add(TrainerClient(employee_id=t2.id, customer_id=stranger.id, subscription_id=sub3.id, mode=ShiftMode.OFFLINE))
    await db.commit()
    return owner, gym, trainer, staff_user, mine, sub, theirs, stranger


async def test_staff_portal_client_access_control(client, db, anon_client_factory):
    owner, gym, trainer, staff_user, mine, sub, theirs, stranger = await _portal_world(db)
    await login_as(client, staff_user)
    r = await client.get("/staff/clients")
    assert r.status_code == 200 and "Meera Nair" in r.text and "Arjun Das" not in r.text and "Other Gym Member" not in r.text
    r = await client.get(f"/staff/clients/{mine.id}")
    assert r.status_code == 200
    for text in ("Asthma", "Peanut allergy", "Fat loss base", "Goblet squat", "25.0"):
        assert text in r.text, text
    assert (await client.get(f"/staff/clients/{theirs.id}")).status_code == 404
    assert (await client.get(f"/staff/clients/{stranger.id}")).status_code == 404
    assert (await client.get("/staff/clients/987654")).status_code == 404

    # expired subscription -> no access
    s = await reload(db, Subscription, sub.id)
    s.status = SubscriptionStatus.EXPIRED
    await db.commit()
    assert (await client.get(f"/staff/clients/{mine.id}")).status_code == 404
    s.status = SubscriptionStatus.ACTIVE
    await db.commit()
    assert (await client.get(f"/staff/clients/{mine.id}")).status_code == 200

    # suspended employment -> no access
    t = await reload(db, Employee, trainer.id)
    t.status = EmploymentStatus.SUSPENDED
    await db.commit()
    assert (await client.get(f"/staff/clients/{mine.id}")).status_code == 404
    assert "Meera Nair" not in (await client.get("/staff/clients")).text
    t.status = EmploymentStatus.ACTIVE
    await db.commit()

    # unassigned -> no access
    link = await db.scalar(select(TrainerClient).where(TrainerClient.customer_id == mine.id))
    link.active = False
    await db.commit()
    assert (await client.get(f"/staff/clients/{mine.id}")).status_code == 404

    # other roles cannot use the staff portal
    owner_client = anon_client_factory("203.0.113.51")
    await login_as(owner_client, owner)
    assert (await owner_client.get("/staff")).status_code == 403
    cust_client = anon_client_factory("203.0.113.52")
    await login_as(cust_client, mine)
    assert (await cust_client.get(f"/staff/clients/{mine.id}")).status_code == 403


async def test_staff_portal_dashboard_requirements_and_upload(client, db):
    owner, gym, _ = await owner_gym(db)
    user = await make_user(db, role=Role.STAFF, full_name="Priya Sharma", id_status=IdStatus.MISSING)
    emp = await make_employee(db, gym, user=user, status=EmploymentStatus.PENDING_VERIFICATION)
    emp.verification_status = VerificationStatus.DRAFT
    other = await make_employee(db, gym, user=await make_user(db, role=Role.STAFF))
    await db.commit()
    await login_as(client, user)
    r = await client.get("/staff")
    assert r.status_code == 200
    assert f"Finish joining {gym.name}" in r.text and 'href="/account/identity"' in r.text
    assert "Upload a trainer certification" in r.text
    r = await post_form(client, f"/staff/employments/{emp.id}/documents", {"doc_type": "certification"},
                        files={"document": ("cert.png", png_bytes(text="CERT"), "image/png")})
    assert r.status_code == 303
    doc = await db.scalar(select(EmployeeDocument).where(EmployeeDocument.employee_id == emp.id))
    assert doc is not None and doc.doc_type == "certification"
    assert (await reload(db, Employee, emp.id)).verification_status == VerificationStatus.QUEUED
    owner_note = await db.scalar(select(Notification).where(Notification.user_id == owner.id))
    assert "uploaded a certification" in owner_note.title
    r = await post_form(client, f"/staff/employments/{other.id}/documents", {"doc_type": "certification"},
                        files={"document": ("cert.png", png_bytes(), "image/png")})
    assert r.status_code == 404


async def test_staff_portal_empty_state_and_schedule(client, db):
    lonely = await make_user(db, role=Role.STAFF)
    await login_as(client, lonely)
    r = await client.get("/staff")
    assert r.status_code == 200 and "not linked to a gym yet" in r.text
    r = await client.get("/staff/schedule")
    assert r.status_code == 200 and "No schedule yet" in r.text
    owner, gym, branch = await owner_gym(db)
    emp = await make_employee(db, gym, user=lonely)
    await make_shift(db, emp, branch=branch, weekday=2, start="07:00", end="11:30")
    r = await client.get("/staff/schedule")
    assert r.status_code == 200 and "07:00–11:30" in r.text and "4.5 h" in r.text


# ====================================================================== N+1 guards
async def test_owner_staff_list_constant_queries(client, db):
    owner, gym, branch = await owner_gym(db)
    plans = {p.code: p for p in await make_plans(db, gym)}

    async def add_staff(n: int) -> None:
        for i in range(n):
            emp = await make_employee(db, gym)
            await make_shift(db, emp, branch=branch, weekday=i % 7, start="06:00", end="09:00")
            cust = await make_customer(db)
            sub = await make_subscription(db, cust, plans["offline_trainer"], branch=branch)
            db.add(TrainerClient(employee_id=emp.id, customer_id=cust.id, subscription_id=sub.id, mode=ShiftMode.OFFLINE))
        await db.commit()

    await add_staff(1)
    await login_as(client, owner)
    await client.get("/owner/staff")
    few = await count_get(client, "/owner/staff")
    await add_staff(5)
    many = await count_get(client, "/owner/staff")
    assert few == many


async def test_owner_staff_detail_constant_queries(client, db):
    owner, gym, branch = await owner_gym(db)
    plans = {p.code: p for p in await make_plans(db, gym)}
    emp = await make_employee(db, gym)
    await login_as(client, owner)

    async def grow(day: int) -> None:
        await make_shift(db, emp, branch=branch, weekday=day, start="06:00", end="09:00")
        await add_cert(db, emp)
        cust = await make_customer(db)
        sub = await make_subscription(db, cust, plans["offline_trainer"], branch=branch)
        db.add(TrainerClient(employee_id=emp.id, customer_id=cust.id, subscription_id=sub.id, mode=ShiftMode.OFFLINE))
        await make_subscription(db, await make_customer(db), plans["combo_trainer"], branch=branch)
        await db.commit()

    await grow(0)
    url = f"/owner/staff/{emp.id}?tab=clients"
    await client.get(url)
    few = await count_get(client, url)
    for day in range(1, 6):
        await grow(day)
    many = await count_get(client, url)
    assert few == many


async def test_staff_portal_constant_queries(client, db):
    owner, gym, branch = await owner_gym(db)
    plans = {p.code: p for p in await make_plans(db, gym)}
    user = await make_user(db, role=Role.STAFF)
    emp = await make_employee(db, gym, user=user)

    async def grow(n: int) -> None:
        for i in range(n):
            cust = await make_customer(db, conditions=["Asthma", "Knee pain"])
            sub = await make_subscription(db, cust, plans["offline_trainer"], branch=branch)
            db.add(TrainerClient(employee_id=emp.id, customer_id=cust.id, subscription_id=sub.id, mode=ShiftMode.OFFLINE))
            other_owner, other_gym, other_branch = await owner_gym(db)
            extra = await make_employee(db, other_gym, user=user)
            await make_shift(db, extra, branch=other_branch, weekday=i % 7, start="06:00", end="07:00")
        await db.commit()

    await grow(1)
    await login_as(client, user)
    await client.get("/staff")
    counts_few = [await count_get(client, u) for u in ("/staff", "/staff/clients", "/staff/schedule")]
    await grow(3)
    counts_many = [await count_get(client, u) for u in ("/staff", "/staff/clients", "/staff/schedule")]
    assert counts_few == counts_many


@pytest.mark.parametrize("extra_shifts", [0, 5])
async def test_person_shift_slots_single_query(db, extra_shifts):
    owner, gym, branch = await owner_gym(db)
    emp = await make_employee(db, gym)
    for day in range(extra_shifts):
        await make_shift(db, emp, branch=branch, weekday=day, start="06:00", end="07:00")
    with QueryCounter() as qc:
        slots = await S.person_shift_slots(db, emp)
    assert len(slots) == extra_shifts and qc.count == 1


async def test_activation_sweep_and_user_helper(db):
    from app.models import Job

    owner, gym, _ = await owner_gym(db)
    ready_user = await make_user(db, role=Role.STAFF)
    ready = await make_employee(db, gym, user=ready_user, role=EmployeeRole.RECEPTIONIST, status=EmploymentStatus.INVITED)
    waiting_user = await make_user(db, role=Role.STAFF, id_status=IdStatus.PENDING)
    waiting = await make_employee(db, gym, user=waiting_user, role=EmployeeRole.CLEANER,
                                  status=EmploymentStatus.PENDING_VERIFICATION)
    unlinked = await make_employee(db, gym, role=EmployeeRole.CLEANER, status=EmploymentStatus.INVITED)
    assert await S.staff_activation_sweep(db) == 1  # only the linked-but-never-checked employment
    await db.commit()
    assert await run_jobs() == 1
    assert (await reload(db, Employee, ready.id)).status == EmploymentStatus.ACTIVE
    assert (await reload(db, Employee, waiting.id)).status == EmploymentStatus.PENDING_VERIFICATION
    assert (await reload(db, Employee, unlinked.id)).status == EmploymentStatus.INVITED
    # other packages queue checks for every current employment of a person (e.g. after ID verification)
    assert await S.queue_activation_checks_for_user(db, waiting_user.id) == 1
    await db.commit()
    job = await db.scalar(select(Job).where(Job.dedupe_key == f"staff_activation_check:{waiting.id}"))
    assert job is not None and job.payload == {"employee_id": waiting.id}


async def test_staff_portal_pages_are_csp_clean(client, db):
    owner, gym, trainer, staff_user, mine, sub, theirs, stranger = await _portal_world(db)
    branch = await db.scalar(select(Subscription.branch_id).where(Subscription.id == sub.id))
    from app.models import Branch

    await make_shift(db, trainer, branch=await db.get(Branch, branch), weekday=date.today().weekday(),
                     start="06:00", end="10:00")
    await make_shift(db, trainer, branch=None, weekday=(date.today().weekday() + 1) % 7, start="18:00", end="20:00")
    await login_as(client, staff_user)
    for url in ("/staff", "/staff/schedule", "/staff/clients", f"/staff/clients/{mine.id}"):
        r = await client.get(url)
        assert r.status_code == 200, url
        assert "<script>" not in r.text and " onclick=" not in r.text and ' style="' not in r.text, url
    r = await client.get("/staff")
    assert "06:00–10:00" in r.text and "Meera Nair" not in r.text  # dashboard shows today's shift, not client data


async def test_detail_page_states_render(client, db):
    owner, gym, branch = await owner_gym(db)
    suspended = await make_employee(db, gym, role=EmployeeRole.CLEANER, status=EmploymentStatus.SUSPENDED)
    former_user = await make_user(db, role=Role.STAFF)
    former = await make_employee(db, gym, user=former_user, role=EmployeeRole.MANAGER,
                                 status=EmploymentStatus.TERMINATED)
    former.termination_date = date.today() - timedelta(days=3)
    deleted = await make_employee(db, gym, role=EmployeeRole.SECURITY, status=EmploymentStatus.TERMINATED)
    deleted.deleted_at, deleted.purge_after = utcnow(), utcnow() + timedelta(days=30)
    await db.commit()
    await login_as(client, owner)
    r = await client.get(f"/owner/staff/{suspended.id}")
    assert r.status_code == 200 and "Reactivate" in r.text and "Suspended staff keep their record" in r.text
    r = await client.get(f"/owner/staff/{former.id}?tab=schedule")
    assert r.status_code == 200 and "This employment has ended on" in r.text
    assert "Shifts can&#39;t be added to a former staff member." in r.text
    r = await client.get(f"/owner/staff/{deleted.id}")
    assert r.status_code == 200 and "Deleted staff member" in r.text and "deleted their IronPulse account" in r.text
    r = await post_form(client, f"/owner/staff/{former.id}", S.staff_values_from_employee(former))
    assert r.status_code == 303 and (await reload(db, Employee, former.id)).status == EmploymentStatus.TERMINATED
    listing = await client.get("/owner/staff?status=former")
    assert "Deleted staff member" in listing.text and former.full_name in listing.text
    assert former.full_name not in (await client.get("/owner/staff")).text  # current view hides former staff
    # the former employee's portal shows the empty state with past employments
    await login_as(client, former_user)
    r = await client.get("/staff")
    assert r.status_code == 200 and "not linked to a gym yet" in r.text and gym.name in r.text


async def test_profile_photo_replacement_and_role_change_guard(client, db):
    owner, gym, branch, trainer, plans = await _gym_with_trainer(db)
    old_photo = await storage.save_public_image(png_bytes((80, 80), "red"), kind="staff")
    trainer.photo_key = old_photo.key
    sub = await make_subscription(db, await make_customer(db), plans["offline_trainer"], branch=branch)
    db.add(TrainerClient(employee_id=trainer.id, customer_id=sub.customer_id, subscription_id=sub.id,
                         mode=ShiftMode.OFFLINE))
    await db.commit()
    await login_as(client, owner)
    values = S.staff_values_from_employee(trainer)
    values["is_public"] = "1"
    r = await post_form(client, f"/owner/staff/{trainer.id}", values,
                        files=[("photo", ("new.png", png_bytes((90, 90), "blue"), "image/png"))])
    assert r.status_code == 303
    fresh = await reload(db, Employee, trainer.id)
    assert fresh.photo_key != old_photo.key and storage.path_for(fresh.photo_key).is_file()
    assert not storage.path_for(old_photo.key).exists()
    note = await db.scalar(select(Notification).where(Notification.user_id == trainer.user_id))
    assert "photo" in note.body
    values["role"] = "receptionist"  # a position is never changed in place (tests/test_trust_safety.py)
    r = await post_form(client, f"/owner/staff/{trainer.id}", values)
    assert r.status_code == 400 and S.POSITION_LOCKED.split(".")[0] in html.unescape(r.text)
    assert f"/owner/staff/{trainer.id}/change-position" in r.text
    assert (await reload(db, Employee, trainer.id)).role == EmployeeRole.TRAINER


async def test_changing_invite_email_reinvites(client, db):
    owner, gym, _ = await owner_gym(db)
    await login_as(client, owner)
    await post_form(client, f"/owner/gyms/{gym.id}/staff/new", staff_form(work_mode="online"), files=[photo_file()])
    emp = await db.scalar(select(Employee))
    values = S.staff_values_from_employee(emp)
    values.update(invite_email="new.address@example.com", is_public="1")
    r = await post_form(client, f"/owner/staff/{emp.id}", values)
    assert r.status_code == 303
    tokens = (await db.execute(select(AuthToken).where(AuthToken.employee_id == emp.id))).scalars().all()
    assert len(tokens) == 1 and tokens[0].email == "new.address@example.com"
    assert await db.scalar(select(func.count(EmailOutbox.id)).where(EmailOutbox.to_email == "new.address@example.com")) == 1


# ====================================================================== certificate checks: no transaction across OCR / AI
CERT_TEXT = "PARTICIPATION AWARD"
NO_MATCH = '{"is_certificate": false, "name_on_document": null, "name_matches": false, "confidence": 0.1}'


async def _trainer_with_certificates(db, n: int) -> tuple[User, Employee]:
    """An invited trainer record (no linked account) with ``n`` stored certificates whose holder name was not found
    (their earlier AI verdicts recorded no names - as rows checked before names were recorded)."""
    owner, gym, _ = await owner_gym(db)
    emp = await make_employee(db, gym, status=EmploymentStatus.INVITED)
    emp.verification_status = VerificationStatus.NEEDS_REVIEW
    for i in range(n):
        stored, _plain = await storage.save_private_file(png_bytes((200, 120), "white", text="X"), kind="staff_docs",
                                                         filename=f"cert{i}.png")
        db.add(EmployeeDocument(employee_id=emp.id, doc_type="certification", file_key=stored.key,
                                original_filename=f"cert{i}.png", content_type=stored.content_type, sha256=stored.sha256,
                                size_bytes=stored.size, ocr_text=CERT_TEXT,
                                assessment={"name_match": False, "method": "ai", "ai": {"ai": "ok"}}))
    await db.commit()
    return owner, emp


async def _reread_certificates(employee_id: int) -> list[int]:
    """What the owner's re-check does first: read the unmatched certificates again (bounded batch), then commit."""
    from app import db as dbmod
    from app.services import ocr

    async with dbmod.get_sessionmaker()() as s:
        docs = list((await s.execute(select(EmployeeDocument).where(EmployeeDocument.employee_id == employee_id)
                                     .order_by(EmployeeDocument.id))).scalars().all())
        result = await ocr.reread_documents([d for d in docs if not (d.assessment or {}).get("name_match")],
                                            noun="certificate")
        await s.commit()
        return [d.id for d in result.documents]


async def test_the_activation_check_keeps_the_ai_verdict_of_a_text_it_already_judged(db, monkeypatch, fake_ai):
    """app/services/staffing.py verify_certifications: before, every run of the activation check (a re-check, an
    upload, a name change) asked the AI again about every unmatched certificate - up to 40 vision calls per run, the
    same answers. A verdict is now kept for the text and names it judged; a certificate read again is judged again.
    (cross_owner_tests.patch, second test - moved here from tests/test_ai_hardening_features.py, which this owner
    does not edit.)"""
    from app import db as dbmod
    from app.services import ocr

    calls = fake_ocr(monkeypatch, CERT_TEXT)
    provider = fake_ai(responses=[NO_MATCH])
    _owner, emp = await _trainer_with_certificates(db, 10)

    async def run() -> int:
        before = len(provider.prompts)
        async with dbmod.get_sessionmaker()() as s:
            await S.run_activation_check(s, emp.id)
            await s.commit()
        return len(provider.prompts) - before

    assert await run() == 10  # judged once (these verdicts had no names recorded yet)
    assert await run() == 0 and calls == []  # nothing new to judge: no AI call, no OCR
    reread = await _reread_certificates(emp.id)
    assert len(reread) == ocr.REREAD_BATCH
    assert await run() == ocr.REREAD_BATCH and len(calls) == ocr.REREAD_BATCH  # only what was read again


async def test_the_certificate_check_holds_no_transaction_while_ocr_and_ai_run(db, monkeypatch, fake_ai):
    """Before: the job read the employee and its documents and kept that transaction - a pooled connection - open
    through the OCR engines and the AI vision call."""
    from app.services import ocr
    from app.services.ocr import MultiOcrResult, OcrEngineResult
    from tests.test_finance_scale import idle_transactions

    owner, gym, user, emp, doc = await _trainer_with_cert(db)
    seen: list[tuple[str, list[str]]] = []

    async def read(data, content_type, *, max_pages=3, engines=None):  # noqa: ANN001
        seen.append(("ocr", await idle_transactions()))
        return MultiOcrResult([OcrEngineResult("tesseract", True, "ISSA certificate of completion", 0.9)])

    async def judge(**kwargs):  # noqa: ANN003
        seen.append(("ai", await idle_transactions()))
        return '{"is_certificate": true, "name_on_document": "Rahul Mehta", "name_matches": true, "confidence": 0.92}'

    monkeypatch.setattr(ocr, "ocr_document", read)
    fake_ai(handler=judge)
    await db.close()
    await check(emp.id)
    assert seen == [("ocr", []), ("ai", [])]
    emp = await reload(db, Employee, emp.id)
    assert emp.verification_status == VerificationStatus.VERIFIED and emp.status == EmploymentStatus.ACTIVE
    doc = await reload(db, EmployeeDocument, doc.id)
    assert "ISSA certificate" in doc.ocr_text and doc.assessment["method"] == "ai" and doc.assessment["checked_names"]


async def test_an_owner_approval_made_while_the_ai_judges_stands(db, monkeypatch, fake_ai):
    """The check writes on a fresh, locked read: an approval committed while the AI was judging is not overwritten by
    the AI's "needs review" (before, the job's stale copy won)."""
    from app import db as dbmod

    owner, gym, user, emp, doc = await _trainer_with_cert(db)
    fake_ocr(monkeypatch, "Certificate of achievement")

    async def judge(**kwargs):  # noqa: ANN003
        async with dbmod.get_sessionmaker()() as s:  # the owner approves the certificate by hand meanwhile
            row = await s.get(Employee, emp.id)
            row.verification_status = VerificationStatus.VERIFIED
            row.verified_at = utcnow()
            await s.commit()
        return NO_MATCH

    fake_ai(handler=judge)
    await db.close()
    await check(emp.id)
    emp = await reload(db, Employee, emp.id)
    assert emp.verification_status == VerificationStatus.VERIFIED and emp.status == EmploymentStatus.ACTIVE
    outcomes = (await db.execute(select(VerificationRun.outcome).where(VerificationRun.employee_id == emp.id))).scalars()
    assert VerificationStatus.NEEDS_REVIEW not in list(outcomes)
    assert not await db.scalar(select(func.count(Notification.id)).where(Notification.title.like("Review%certification")))


async def test_a_trainer_made_staff_while_the_ai_judges_is_checked_as_staff(db, monkeypatch, fake_ai):
    """The owner takes the trainer role away while the AI judges the certificate: the check carries on with the employee
    as they are now - certificates no longer decide anything - instead of dropping the whole activation check."""
    from app import db as dbmod

    owner, gym, user, emp, doc = await _trainer_with_cert(db)
    fake_ocr(monkeypatch, "Certificate of achievement")

    async def judge(**kwargs):  # noqa: ANN003
        async with dbmod.get_sessionmaker()() as s:  # the owner makes them a receptionist meanwhile
            row = await s.get(Employee, emp.id)
            row.is_trainer = False
            row.role = EmployeeRole.RECEPTIONIST
            await s.commit()
        return NO_MATCH

    fake_ai(handler=judge)
    await db.close()
    await check(emp.id)
    emp = await reload(db, Employee, emp.id)
    assert emp.status == EmploymentStatus.ACTIVE and emp.verification_status == VerificationStatus.VERIFIED
    assert not await db.scalar(select(func.count(Notification.id)).where(Notification.title.like("Review%certification")))


async def test_a_certificate_uploaded_while_others_are_read_is_checked_in_the_same_run(db, monkeypatch, fake_ai):
    """Documents that change while the files are read are not written over: the check decides again on the fresh
    state - the new certificate (whose own queued check is this one) is read and matched in the same run."""
    from app import db as dbmod
    from app.services import ocr
    from app.services.ocr import MultiOcrResult, OcrEngineResult

    owner, gym, user, emp, doc = await _trainer_with_cert(db)
    texts = iter(["blurred scan", "CERTIFIED TRAINER awarded to RAHUL MEHTA"])

    async def read(data, content_type, *, max_pages=3, engines=None):  # noqa: ANN001
        text = next(texts)
        if text == "blurred scan":  # a clearer certificate is uploaded while the first one is read
            async with dbmod.get_sessionmaker()() as s:
                stored, _ = await storage.save_private_file(png_bytes((200, 120), "white", text="Y"), kind="staff_docs",
                                                            filename="clear.png")
                s.add(EmployeeDocument(employee_id=emp.id, doc_type="certification", file_key=stored.key,
                                       original_filename="clear.png", content_type=stored.content_type,
                                       sha256=stored.sha256, size_bytes=stored.size))
                await s.commit()
        return MultiOcrResult([OcrEngineResult("tesseract", True, text, 0.9)])

    monkeypatch.setattr(ocr, "ocr_document", read)
    fake_ai(responses=[NO_MATCH])
    await db.close()
    await check(emp.id)
    emp = await reload(db, Employee, emp.id)
    assert emp.verification_status == VerificationStatus.VERIFIED and emp.status == EmploymentStatus.ACTIVE
    docs = (await db.execute(select(EmployeeDocument).where(EmployeeDocument.employee_id == emp.id)
                             .order_by(EmployeeDocument.id))).scalars().all()
    assert len(docs) == 2 and "blurred scan" in docs[0].ocr_text and "RAHUL MEHTA" in docs[1].ocr_text
    assert docs[0].assessment["name_match"] is False and docs[1].assessment["name_match"] is True


async def test_a_certificate_read_again_while_the_ai_judges_is_judged_on_its_new_text(db, monkeypatch, fake_ai):
    """The owner's re-check clears a certificate's text while the AI judges the old one: the stale verdict is not
    written over the re-read document (its re-read stamp survives) - the new text is read and judged in the same run."""
    calls = fake_ocr(monkeypatch, CERT_TEXT)
    judged: list[int] = []
    _owner, emp = await _trainer_with_certificates(db, 1)

    async def judge(**kwargs):  # noqa: ANN003
        judged.append(1)
        if len(judged) == 1:
            assert len(await _reread_certificates(emp.id)) == 1
        return NO_MATCH

    fake_ai(handler=judge)
    await db.close()
    await check(emp.id)
    assert len(judged) == 2 and len(calls) == 1  # judged again after the re-read, which OCR read once
    doc = (await db.execute(select(EmployeeDocument).where(EmployeeDocument.employee_id == emp.id))).scalar_one()
    assert doc.assessment["rereads"] == 1 and doc.assessment["checked_names"] and CERT_TEXT in doc.ocr_text
    assert (await reload(db, Employee, emp.id)).verification_status == VerificationStatus.NEEDS_REVIEW


# ====================================================================== staff portal: client health entries
async def test_client_page_reads_at_most_100_health_entries_safety_first(client, db, monkeypatch):
    """The trainer's client page: at most CONDITIONS_PER_KIND per kind and CONDITIONS_SHOWN in all, safety-relevant
    first (before: 25 per kind by name - a tagged knee injury named last was cut off behind 25 harmless sprains)."""
    from sqlalchemy import insert

    from app.routers import staff_portal
    from tests.test_finance_scale import Sent
    from tests.test_owner_customers import _padded_profile

    monkeypatch.setattr(staff_portal, "CONDITIONS_SHOWN", 30)
    owner, gym, trainer, staff_user, mine, sub, theirs, stranger = await _portal_world(db)
    await db.execute(insert(HealthCondition), _padded_profile(mine.id))
    await db.commit()
    await login_as(client, staff_user)
    with Sent() as sent:
        r = await client.get(f"/staff/clients/{mine.id}")
    assert r.status_code == 200
    notes = r.text.split("Safety notes", 1)[1]
    names = re.findall(r'<p class="text-sm font-medium">([^<]+)</p>', notes)
    assert len(names) == 30 and "Zz knee injury" in names  # the safety tag first, whatever its name
    assert "Asthma" in names and "Peanut allergy" in names  # every kind keeps its first entry
    assert "more recorded - ask the gym admin" in notes
    (sql, params), = [(s, p) for s, p in sent.items if "FROM health_conditions" in s and "row_number()" in s]
    assert "LIMIT" in sql and 30 in list(params) and staff_portal.CONDITIONS_PER_KIND in list(params)


async def test_client_page_shows_every_safety_tag_before_the_repeats_of_one(client, db):
    """A client padded with one safety tag over and over: another tag still makes the page (before: tagged entries by
    id - the back injury, recorded last, was cut behind 25 ankle sprains)."""
    from sqlalchemy import insert

    from tests.test_owner_customers import _one_tag_over_and_over

    owner, gym, trainer, staff_user, mine, sub, theirs, stranger = await _portal_world(db)
    await db.execute(insert(HealthCondition), _one_tag_over_and_over(mine.id, 40))
    await db.commit()
    await login_as(client, staff_user)
    r = await client.get(f"/staff/clients/{mine.id}")
    assert r.status_code == 200
    notes = r.text.split("Safety notes", 1)[1]
    names = re.findall(r'<p class="text-sm font-medium">([^<]+)</p>', notes)
    injuries = [n for n in names if n.startswith("Ankle sprain") or n == "Back pain"]
    assert len(injuries) == 25 and "Back pain" in injuries  # 25 of the 41 injuries, the other safety tag among them
