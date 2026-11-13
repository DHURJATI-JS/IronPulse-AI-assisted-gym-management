"""The integrate step's cross-owner requests: fixes the bug-hunt owners asked for in files outside their own area.

* auth: the security flush and the AI call-log flush commit separately; AI call logs survive a NUL byte and an
  uncommitted flush; a violation withdraws the person's other pending records at the same owner's gyms; a record is
  never activated while the owner may not add the person (F17 / F37 / F49, F45).
* shop: the gym sees only members with a current, consented membership (F01); "CKD", dialysis and nephrotic get the
  kidney tag (F05); the desk explains the member's consent (F01).
* member-ai: the owner's health form carries its snapshot, a refused save is shown inline and what was kept is said
  (F34 - F36); the health bounds live in the caps table (F08 / F09).
* social: one open misconduct report per member and staff member in the database, duplicates of the old race closed
  before the index is built (F12); hot yoga / saunas warn for pregnancy (F28); the assistant sends a red flag for an
  unavailable staff member to the gym team (F30); names never keep bidi controls (F33).
* owner-staff: disabled (banned) staff accounts are never listed publicly nor alerted about a member's health (F46);
  ids out of the 64-bit range answer 404, never 500 (F42).
* platform: health photos, OCR images and AI vision images are decoded through the bounded image decoder; PDF pages
  are rendered under the PDFium lock at a bounded scale (F48, F41)."""

from __future__ import annotations

import html
import io
import re
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app import db as dbmod
from app.ai.client import CallLog, call_log_queue, flush_ai_call_logs
from app.core import caps
from app.core.utils import utcnow
from app.models import (
    AICallLog,
    AuditLog,
    AuthToken,
    ChatThread,
    Employee,
    HealthCondition,
    MisconductReport,
    Notification,
    SecurityEvent,
    StaffViolation,
    User,
)
from app.models.enums import (
    ConditionKind,
    EmployeeRole,
    EmploymentStatus,
    PurchaseChannel,
    ReportStatus,
    Role,
    SubscriptionStatus,
    ThreatCategory,
    TokenPurpose,
)
from app.security.bans import security_events
from app.services import intake, storage
from app.services import staffing as S
from app.services import trust_safety as TS
from tests.conftest import api, login_as, post_form
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
)
from tests.test_platform_hardening import png_bomb
from tests.test_trust_safety import flash_text, staff_form

BIG_ID = 2**63  # one past the largest bigint


# ====================================================================== auth: the background flushes
async def test_the_security_flush_and_the_ai_call_log_flush_commit_separately(monkeypatch):
    from app.ai import client as ai_client
    from app.security import bans
    from app.services.worker import BackgroundRunner

    security_events.record("203.0.113.71", ThreatCategory.PROBE, 3, "probe for /.env", method="GET", path="/.env")
    call_log_queue.append(CallLog("assistant", "gateway", "gemma", True, 100, None))

    async def broken(db):  # noqa: ANN001
        raise RuntimeError("the table is unavailable")

    monkeypatch.setattr(ai_client, "flush_ai_call_logs", broken)
    with pytest.raises(RuntimeError):
        await BackgroundRunner._security()
    async with dbmod.get_sessionmaker()() as s:  # committed although the call-log flush failed after it
        assert await s.scalar(select(func.count(SecurityEvent.id))) == 1
    monkeypatch.undo()

    monkeypatch.setattr(bans, "flush_security_queue", broken)
    with pytest.raises(RuntimeError):
        await BackgroundRunner._security()
    async with dbmod.get_sessionmaker()() as s:  # ... and the call logs are written although the security flush failed
        assert await s.scalar(select(func.count(AICallLog.id))) == 1
    assert not call_log_queue


async def test_ai_call_logs_survive_a_nul_byte_and_an_uncommitted_flush(db):
    call_log_queue.append(CallLog("assistant", "gate\x00way", "gemma", False, 120, "upstream said \x00 boom",
                                  owner_kind="us\x00er"))
    call_log_queue.append(CallLog("ocr", "docker", "gemma3", True, 900, None))
    async with dbmod.get_sessionmaker()() as s:  # rolled back: the batch goes back to the front of the queue
        assert await flush_ai_call_logs(s) == 2
        await s.rollback()
    assert [c.feature for c in call_log_queue] == ["assistant", "ocr"]
    async with dbmod.get_sessionmaker()() as s:  # closed without a COMMIT: the same
        assert await flush_ai_call_logs(s) == 2
    assert [c.feature for c in call_log_queue] == ["assistant", "ocr"]
    assert await flush_ai_call_logs(db) == 2
    await db.commit()
    assert not call_log_queue
    rows = (await db.execute(select(AICallLog).order_by(AICallLog.id))).scalars().all()
    assert [r.feature for r in rows] == ["assistant", "ocr"]  # each written once, oldest first
    assert (rows[0].provider, rows[0].error, rows[0].owner_kind) == ("gate\\x00way", "upstream said \\x00 boom",
                                                                     "us\\x00er")
    assert rows[1].error is None and rows[1].owner_kind is None


# ====================================================================== auth: pending records after a violation
async def test_a_violation_withdraws_the_persons_other_pending_records_at_the_same_owner(client, db):
    owner = await make_owner(db)
    gym_a, gym_b, gym_c = await make_gym(db, owner), await make_gym(db, owner), await make_gym(db, owner)
    elsewhere_gym = await make_gym(db, await make_owner(db))
    person = await make_user(db, role=Role.STAFF, email="asha.rao@example.com", full_name="Asha Rao")
    staff = EmployeeRole.RECEPTIONIST
    active = await make_employee(db, gym_a, user=person, role=staff)
    invited = await make_employee(db, gym_b, status=EmploymentStatus.INVITED, role=staff)
    pending = await make_employee(db, gym_c, user=person, status=EmploymentStatus.PENDING_VERIFICATION, role=staff)
    neighbour = await make_employee(db, gym_b, status=EmploymentStatus.INVITED, role=staff)
    elsewhere = await make_employee(db, elsewhere_gym, status=EmploymentStatus.INVITED, role=staff)
    invited.invite_email = "Asha.Rao+frontdesk@Example.com"  # the same address, once normalised
    neighbour.invite_email = "ravi.k@example.com"  # the same domain, another person
    elsewhere.invite_email = "asha.rao@example.com"  # another owner's gym: not this owner's decision
    db.add(AuthToken(purpose=TokenPurpose.STAFF_INVITE, token_hash="a" * 64, employee_id=invited.id,
                     email="asha.rao@example.com", expires_at=utcnow() + timedelta(days=7)))
    await db.commit()
    await login_as(client, owner)
    r = await post_form(client, f"/owner/staff/{active.id}/remove",
                        {"reason": "policy_violation", "note": "Shared members' phone numbers with a competitor.",
                         "confirm": "1"})
    assert r.status_code == 303
    states = {e.id: (e.status, e.removal_reason) for e in (await db.execute(
        select(Employee).execution_options(populate_existing=True))).scalars()}
    ended = (EmploymentStatus.TERMINATED, "policy_violation")
    assert states[active.id] == states[invited.id] == states[pending.id] == ended
    assert states[neighbour.id] == states[elsewhere.id] == (EmploymentStatus.INVITED, None)
    assert await db.scalar(select(func.count(AuthToken.id)).where(AuthToken.employee_id == invited.id)) == 0
    withdrawn = (await db.execute(select(AuditLog.entity_id).where(
        AuditLog.action == "staff.removed", AuditLog.detail["withdrawn_with"].astext == str(active.id)))).scalars().all()
    assert sorted(withdrawn) == sorted([invited.id, pending.id])
    assert await db.scalar(select(func.count(StaffViolation.id))) == 1  # one violation record, not one per job


async def test_a_benign_removal_withdraws_nothing(client, db):
    owner = await make_owner(db)
    gym_a, gym_b = await make_gym(db, owner), await make_gym(db, owner)
    invited = await make_employee(db, gym_a, status=EmploymentStatus.INVITED, role=EmployeeRole.RECEPTIONIST)
    other = await make_employee(db, gym_b, status=EmploymentStatus.INVITED, role=EmployeeRole.RECEPTIONIST)
    other.invite_email = invited.invite_email
    await db.commit()
    await login_as(client, owner)
    r = await post_form(client, f"/owner/staff/{invited.id}/remove", {"reason": "restructuring", "confirm": "1"})
    assert r.status_code == 303
    assert (await db.get(Employee, other.id, populate_existing=True)).status == EmploymentStatus.INVITED


async def test_a_record_is_never_activated_while_the_owner_may_not_add_the_person(db):
    owner = await make_owner(db)
    gym_a, gym_b = await make_gym(db, owner), await make_gym(db, owner)
    person = await make_user(db, role=Role.STAFF, email="vik.shah@example.com", full_name="Vik Shah")
    emp = await make_employee(db, gym_b, user=person, status=EmploymentStatus.PENDING_VERIFICATION,
                              role=EmployeeRole.RECEPTIONIST)
    db.add(StaffViolation(gym_id=gym_a.id, owner_id=owner.id, actor_id=owner.id, subject_user_id=person.id,
                          subject_hashes=[TS.email_hash(person.email)], gym_name=gym_a.name, person_name="Vik Shah",
                          reason="policy_violation", category=TS.VIOLATION,
                          note="Removed at the other branch after a review."))
    await db.commit()
    for _ in range(2):  # the activation job and the sweep's re-check
        result = await S.run_activation_check(db, emp.id, use_ai=False)
        await db.commit()
        assert result is not None and result.new_status == EmploymentStatus.PENDING_VERIFICATION
    fresh = await db.get(Employee, emp.id, populate_existing=True)
    assert fresh.status == EmploymentStatus.PENDING_VERIFICATION and fresh.verification_summary == S.ACTIVATION_REFUSED
    titles = (await db.execute(select(Notification.title).where(Notification.user_id == owner.id))).scalars().all()
    assert titles == ["Vik Shah can't be activated"]  # told once, never why
    assert "violation" not in S.ACTIVATION_REFUSED.lower()

    # another trust decision about the person is running: not now (and no notice) - the next check decides
    ready_person = await make_user(db, role=Role.STAFF, email="ria.d@example.com")
    ready = await make_employee(db, gym_b, user=ready_person, status=EmploymentStatus.PENDING_VERIFICATION,
                                role=EmployeeRole.RECEPTIONIST)
    async with dbmod.get_sessionmaker()() as holder:
        await TS.lock_person(holder, await TS.person_of_employee(holder, await holder.get(Employee, ready.id)))
        result = await S.run_activation_check(db, ready.id, use_ai=False)
        await db.commit()
        assert result is not None and result.new_status == EmploymentStatus.PENDING_VERIFICATION
        await holder.rollback()
    result = await S.run_activation_check(db, ready.id, use_ai=False)
    await db.commit()
    assert result is not None and result.new_status == EmploymentStatus.ACTIVE


# ====================================================================== shop: who the gym may see
async def test_the_gym_sees_only_members_who_agreed_to_a_current_membership(client, db):
    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    plan = (await make_plans(db, gym))[4]

    async def member(status: SubscriptionStatus, channel: PurchaseChannel = PurchaseChannel.ONLINE_CHECKOUT,
                     ended_days_ago: int | None = None) -> int:
        m = await make_customer(db, conditions=["Asthma"])
        sub = await make_subscription(db, m, plan, status=status)
        sub.channel = channel
        if ended_days_ago is not None:
            sub.ends_on = date.today() - timedelta(days=ended_days_ago)
            sub.starts_on = sub.ends_on - timedelta(days=30)
        await db.commit()
        return m.id

    visible = [await member(SubscriptionStatus.ACTIVE), await member(SubscriptionStatus.SUSPENDED),
               await member(SubscriptionStatus.PENDING_PAYMENT), await member(SubscriptionStatus.EXPIRED, ended_days_ago=30)]
    hidden = [await member(SubscriptionStatus.CANCELLED, PurchaseChannel.OWNER_MANUAL),  # F01: cancelled the desk's
              await member(SubscriptionStatus.PENDING_PAYMENT, PurchaseChannel.OWNER_MANUAL),
              await member(SubscriptionStatus.EXPIRED, ended_days_ago=intake.FORMER_MEMBER_DAYS + 10),
              await member(SubscriptionStatus.FAILED)]
    await login_as(client, owner)
    for mid in visible:
        assert (await client.get(f"/owner/customers/{mid}")).status_code == 200, mid
        assert (await client.get(f"/profile/{mid}")).status_code == 200, mid
        assert await intake.is_related(db, owner.id, mid)
    for mid in hidden:
        assert (await client.get(f"/owner/customers/{mid}")).status_code == 404, mid
        assert (await client.get(f"/profile/{mid}")).status_code == 404, mid
        assert not await intake.is_related(db, owner.id, mid)
    listed = {int(x) for x in re.findall(r'<a href="/owner/customers/(\d+)" class="group',
                                         (await client.get("/owner/customers")).text)}
    assert listed == set(visible)


def test_kidney_disease_abbreviations_get_the_kidney_tag():
    from app.services.conditions import normalize_condition

    for name in ("CKD stage 3", "Stage 3 CKD", "on dialysis", "Nephrotic syndrome", "IgA nephritis",
                 "glomerulonephritis", "Chronic kidney disease"):
        assert normalize_condition(name) == "kidney", name
    assert normalize_condition("backdrop painting") is None


async def test_the_desk_form_explains_the_members_consent(client, db):
    owner = await make_owner(db)
    await login_as(client, owner)
    await make_plans(db, await make_gym(db, owner))  # the cash form is shown once the gym has plans
    r = await client.get("/owner/desk")
    assert r.status_code == 200 and "that's their consent" in html.unescape(r.text)


# ====================================================================== member-ai: the owner's health form
async def test_the_owner_health_form_carries_its_snapshot_and_says_what_it_kept(client, db):
    from tests.test_owner_health_edit import owner_form

    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    member = await make_customer(db, full_name="Neha Kapoor")
    await make_subscription(db, member, (await make_plans(db, gym))[4])
    db.add_all([HealthCondition(customer_id=member.id, kind=ConditionKind.CONDITION, name="Asthma, exercise-induced",
                                notes="Inhaler in the bag", source="self"),
                HealthCondition(customer_id=member.id, kind=ConditionKind.ALLERGY, name="Peanuts", source="self")])
    await db.commit()
    await login_as(client, owner)
    form = await owner_form(client, member.id)
    assert intake.read_snapshot(form["health_seen"], member.id)  # a genuine snapshot of the entries shown
    assert form["conditions"] == "Asthma, exercise-induced"  # one entry per line: the comma stays inside it

    async def entries() -> dict[str, str | None]:
        rows = (await db.execute(select(HealthCondition).where(HealthCondition.customer_id == member.id)
                                 .execution_options(populate_existing=True))).scalars().all()
        return {c.name: c.notes for c in rows}

    # the owner deletes the allergy the form showed: removed - the snapshot proves the owner saw it
    r = await post_form(client, f"/owner/customers/{member.id}/health", {**form, "allergies": ""})
    assert r.status_code == 303
    assert await entries() == {"Asthma, exercise-induced": "Inhaler in the bag"}

    # 21 new allergies at once: refused on the page itself (400), nothing written, the snapshot kept for the retry
    form = await owner_form(client, member.id)
    r = await post_form(client, f"/owner/customers/{member.id}/health",
                        {**form, "allergies": "\n".join(f"Allergen {i}" for i in range(21))})
    assert r.status_code == 400
    assert "Add at most 20 new allergies at a time" in html.unescape(r.text) and 'name="health_seen"' in r.text
    assert await entries() == {"Asthma, exercise-induced": "Inhaler in the bag"}

    # a form without the snapshot never removes the member's own entries - and the owner is told
    stale = {k: v for k, v in form.items() if k != "health_seen"}
    r = await post_form(client, f"/owner/customers/{member.id}/health",
                        {**stale, "conditions": "", "health_notes": "Prefers morning sessions."})
    assert r.status_code == 303 and "Kept 1 entry the member added themselves" in flash_text(r)
    assert "Asthma, exercise-induced" in await entries()


def test_the_health_bounds_are_in_the_caps_table_and_tunable(monkeypatch):
    from app.config import get_settings
    from app.services import health as hs

    assert caps.limit("health_alerts_per_member_day") == hs.ALERTS_PER_DAY == 20
    assert caps.limit("health_reviews_per_member_day") == hs.REVIEWS_PER_DAY == 10
    monkeypatch.setattr(get_settings(), "caps_overrides", "health_alerts_per_member_day=5,health_reviews_per_member_day=3")
    assert (hs.alerts_per_day(), hs.reviews_per_day()) == (5, 3)


# ====================================================================== social
async def _report_world(db) -> tuple[int, int, int, int]:  # ids: a rollback expires the loaded rows
    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    member, emp, other = await make_customer(db), await make_employee(db, gym), await make_employee(db, gym)
    return gym.id, member.id, emp.id, other.id


def _report(gym_id: int, member_id: int, emp_id: int, **kw) -> MisconductReport:
    return MisconductReport(gym_id=gym_id, employee_id=emp_id, reporter_id=member_id, category="unprofessional",
                            description="Shouted at me in front of the class.", **kw)


async def test_one_open_report_per_member_and_staff_member_is_enforced_by_the_database(db):
    gym, member, emp, _other = await _report_world(db)
    db.add(_report(gym, member, emp))
    await db.commit()
    db.add(_report(gym, member, emp))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()
    db.add(_report(gym, member, emp, status=ReportStatus.DECLINED,
                   admin_response="We spoke to the trainer and reviewed the class."))  # answered reports don't count
    await db.commit()


async def test_the_schema_sync_closes_duplicate_open_reports_before_building_the_index(db):
    gym, member, emp, other = await _report_world(db)
    async with dbmod.get_engine().begin() as conn:
        await conn.execute(text("DROP INDEX uq_misconduct_reports_one_open"))
    db.add_all([_report(gym, member, emp), _report(gym, member, emp), _report(gym, member, emp),
                _report(gym, member, other)])
    await db.commit()
    async with dbmod.get_engine().begin() as conn:
        assert "index uq_misconduct_reports_one_open" in await dbmod.sync_schema(conn)
    rows = (await db.execute(select(MisconductReport).order_by(MisconductReport.id)
                             .execution_options(populate_existing=True))).scalars().all()
    first = rows[0]
    assert [r.status for r in rows] == [ReportStatus.OPEN, ReportStatus.DECLINED, ReportStatus.DECLINED,
                                        ReportStatus.OPEN]
    assert all(f"duplicate of your report #{first.id}" in r.admin_response for r in rows[1:3])
    async with dbmod.get_engine().begin() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM pg_indexes WHERE indexname = 'uq_misconduct_reports_one_open'"))
        assert await dbmod.sync_schema(conn) == []  # idempotent


def test_heat_exposure_warns_for_pregnancy():
    from app.ai.safety import RULES
    from app.services.trainer_safety import check_text

    assert "overheating" in RULES["pregnancy"].avoid
    for message in ("hot yoga on sunday", "try the sauna after leg day", "Bikram class at 7"):
        assert [f.tag for f in check_text(message, ["pregnancy"])] == ["pregnancy"], message
    assert check_text("no sauna for you this month", ["pregnancy"]) == []


async def test_the_assistant_sends_a_red_flag_for_an_unavailable_staff_member_to_the_gym_team(db, fake_ai):
    from tests.test_assistant import _member_with_staff, _reply

    customer, gym, _trainer = await _member_with_staff(db)
    away = await make_employee(db, gym, status=EmploymentStatus.SUSPENDED, user=await make_user(db, role=Role.STAFF))
    fake_ai(responses=[_reply(
        "Sending.",
        {"type": "message_staff", "gym_id": gym.id, "employee_id": away.id, "body": "Can we move Friday's session?"},
        {"type": "message_staff", "gym_id": gym.id, "employee_id": away.id,
         "body": "I have chest pain and feel faint after the run"},
    )])
    from tests.conftest import make_client

    async with make_client() as c:
        await login_as(c, customer)
        data = (await api(c, "POST", "/api/assistant/chat", json={"message": "tell my old trainer"})).json()
    ordinary, red = data["actions"]
    assert ordinary["ok"] is False and "isn't available" in ordinary["detail"]
    assert red["ok"] is True and "112" in red["detail"] and "your message went to the" in red["detail"]
    threads = (await db.execute(select(ChatThread.employee_id).where(ChatThread.customer_id == customer.id))).all()
    assert threads == [(None,)]  # the gym team's conversation - never the unavailable staff member's


def test_names_never_keep_bidi_controls():
    from app.services import auth_flows as af

    assert af.validate_name("Riya ‮Sharma") == ("Riya Sharma", None)
    assert intake.validate_name("Dev⁦ Patel⁩") == ("Dev Patel", None)
    data, errors, _values = S.validate_staff_form(staff_form(full_name="Rahul ‭Mehta"), verified_branches=0)
    assert "full_name" not in errors and data is not None and data.full_name == "Rahul Mehta"
    assert af.validate_name("‏نور‎")[0] == "‏نور‎"  # the marks (LRM / RLM) stay


# ====================================================================== owner-staff
async def test_a_disabled_staff_account_is_never_listed_publicly_nor_alerted(db):
    from app.routers import public as P
    from app.services import trainer_safety as T
    from tests.test_assistant import _member_with_staff

    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    branch = await make_branch(db, gym)
    shown, hidden = await make_employee(db, gym), await make_employee(db, gym)
    shown.is_public = hidden.is_public = True
    await db.commit()
    for e in (shown, hidden):
        await make_shift(db, e, branch=branch)
    (await db.get(User, hidden.user_id)).is_active = False  # a platform ban disables the account
    await db.commit()
    assert [m.employee.id for m in await P.public_staff(db, gym.id)] == [shown.id]
    assert [m.employee.id for m in await P.branch_team(db, branch)] == [shown.id]
    assert [m.employee.id for m in await P.featured_trainers(db, limit=10)] == [shown.id]
    assert dict(await P._count_site_stats(db))["trainers"] == 1

    customer, member_gym, trainer = await _member_with_staff(db)
    assert await T._trainer_users(db, customer.id, member_gym.id) == [trainer.user_id]
    (await db.get(User, trainer.user_id)).is_active = False
    await db.commit()
    assert await T._trainer_users(db, customer.id, member_gym.id) == []


async def test_ids_out_of_the_64_bit_range_are_not_found_never_a_server_error(client, db):
    member = await make_customer(db)
    await login_as(client, member)
    for path in (f"/profile/{BIG_ID}", f"/chat/{BIG_ID}", f"/me/wellness/{BIG_ID}", f"/profile/-{BIG_ID + 1}"):
        r = await client.get(path)
        assert r.status_code == 404, (path, r.status_code)
    r = await client.get(f"/api/live/sessions/{BIG_ID}", headers={"Accept": "application/json"})
    assert r.status_code == 404 and r.json()["detail"] == "Not found."
    from app.core.errors import id_out_of_range

    assert id_out_of_range(OverflowError("Python int too large to convert to C long"))
    assert not id_out_of_range(OverflowError("math range error"))  # any other overflow stays a logged 500


# ====================================================================== platform: images and PDFs
def test_health_photos_ocr_images_and_ai_vision_images_refuse_a_decompression_bomb():
    from app.ai.client import prepare_image
    from app.services import health as hs
    from app.services import ocr

    bomb = png_bomb(6000, 5000, color_type=0)  # 30 megapixels in a few KB
    for decode in (lambda: hs.normalize_photo(bomb, 2000), lambda: ocr._normalise_image(bomb),
                   lambda: prepare_image(bomb, 1024)):
        with pytest.raises(storage.UploadError, match="too large"):
            decode()
    with pytest.raises(storage.UploadError, match="not a valid photo"):  # the health form's own words stay
        hs.normalize_photo(b"definitely not an image")


async def test_a_bomb_reaching_ocr_is_answered_as_undecodable():
    from app.services import ocr

    result = await ocr.run_ocr(png_bomb(6000, 5000, color_type=0), engines=["tesseract"])
    assert all(not r.ok for r in result.results)


def test_pdf_pages_render_under_the_pdfium_lock_at_a_bounded_scale(monkeypatch):
    import pypdfium2 as pdfium
    from PIL import Image

    from app.ai import verification
    from app.services import ocr

    doc = pdfium.PdfDocument.new()
    doc.new_page(14400, 14400)  # 200 x 200 inches: a 3 GB bitmap at scale 2
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    real, seen = storage.pdf_render_scale, []

    def spy(width: float, height: float, wanted: float) -> float:
        scale = real(width, height, wanted)
        seen.append((storage.PDFIUM_LOCK.locked(), scale))
        return scale

    monkeypatch.setattr(storage, "pdf_render_scale", spy)
    monkeypatch.setattr(storage, "MAX_IMAGE_PIXELS", 250_000)  # keeps the test's bitmap small
    pages = ocr._render_pdf_pages(buf.getvalue(), 1)
    first = verification.first_page_image(buf.getvalue(), "application/pdf")
    assert len(pages) == 1 and first is not None
    assert [locked for locked, _ in seen] == [True, True]
    assert all(14400 * scale <= 501 for _, scale in seen)
    assert max(Image.open(io.BytesIO(first)).size) <= 501
