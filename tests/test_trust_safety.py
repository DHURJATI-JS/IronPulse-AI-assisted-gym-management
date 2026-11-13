"""Employment lifecycle + trust & safety (app/services/trust_safety.py, owner_staff remove / change-position,
/superadmin/trust): position change = end + re-add, removal reasons and their records, re-hire eligibility without
leaking another gym's reasons, the auto-ban threshold across gyms (also under concurrency), severe -> account + IP
bans with the shared-address / acting-admin / private-range exclusions, the blocked-identity registry (self delete,
admin delete, ban, after the purge, e-mail / phone variants), the super-admin page (authz, paging, lift with a note,
audit) and the caps of the staff paths."""

from __future__ import annotations

import asyncio
import html
import re
from datetime import date, timedelta

import httpx
import pytest
from sqlalchemy import func, select

from app.core.utils import utcnow
from app.models import (
    AccountBan,
    AuditLog,
    AuthToken,
    BlockedIdentity,
    Employee,
    IdentityDocument,
    IpBan,
    Notification,
    StaffViolation,
    Subscription,
    TrainerClient,
    TrainerShift,
    TrustIpAction,
    User,
    UserSession,
)
from app.models.enums import (
    EmploymentStatus,
    EmployeeRole,
    IdDocType,
    IdStatus,
    Role,
    ShiftMode,
    TokenPurpose,
)
from app.security.bans import ban_registry
from app.security.crypto import pepper_hash
from app.services import staffing as S
from app.services import storage
from app.services import trust_safety as TS
from tests.conftest import login_as, make_client, post_form
from tests.factories import (
    DEFAULT_PASSWORD,
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
def flash_text(response: httpx.Response) -> str:
    from itsdangerous import URLSafeSerializer

    from app.config import get_settings
    from app.core.templating import FLASH_COOKIE

    raw = response.cookies.get(FLASH_COOKIE)
    if not raw:
        return ""
    data = URLSafeSerializer(get_settings().secret_key, salt="ironpulse-flash").loads(raw)
    return " | ".join(d["m"] for d in data)


def page(r: httpx.Response) -> str:
    return html.unescape(r.text)


async def reload(db, model, ident):
    return await db.get(model, ident, populate_existing=True)


async def count(db, model, *where) -> int:
    return int(await db.scalar(select(func.count()).select_from(model).where(*where)) or 0)


async def staff_user(db, **kw) -> User:
    return await make_user(db, role=Role.STAFF, **kw)


async def owner_with_gym(db, **kw):
    owner = await make_owner(db, **kw)
    gym = await make_gym(db, owner)
    return owner, gym


async def remove(client, emp_id: int, reason: str, note: str = "", confirm: str = "1") -> httpx.Response:
    return await post_form(client, f"/owner/staff/{emp_id}/remove", {"reason": reason, "note": note, "confirm": confirm})


def staff_form(**overrides: str) -> dict[str, str]:
    data = {
        "full_name": "Rahul Mehta", "invite_email": "rahul.coach@example.com", "phone": "+91 98450 11111",
        "role": "receptionist", "role_title": "", "employment_type": "full_time", "work_mode": "online",
        "country_code": "IN", "currency": "", "monthly_salary": "35000", "hire_date": date.today().isoformat(),
        "bio": "", "specialties": "", "certifications": "", "years_experience": "", "is_public": "1",
    }
    data.update(overrides)
    return data


def photo_file() -> list[tuple[str, tuple[str, bytes, str]]]:
    return [("photo", ("me.png", png_bytes((120, 120), "teal"), "image/png"))]


async def add_staff(client, gym, **overrides) -> httpx.Response:
    return await post_form(client, f"/owner/gyms/{gym.id}/staff/new", staff_form(**overrides), files=photo_file())


async def client_for(user: User, ip: str = "127.0.0.1") -> httpx.AsyncClient:
    c = make_client(ip)
    await login_as(c, user)
    return c


async def add_session(db, user: User, ip: str, *, days_ago: float = 1.0) -> None:
    from app.security.crypto import new_token, token_hash

    when = utcnow() - timedelta(days=days_ago)
    db.add(UserSession(id=token_hash(new_token(16)), user_id=user.id, ip=ip, created_at=when, last_seen_at=when,
                       expires_at=utcnow() + timedelta(days=1), user_agent="pytest"))
    await db.commit()


# ====================================================================== reasons, hashing (pure)
def test_reason_list_and_categories():
    cats = {r.code: r.category for r in TS.REMOVAL_REASONS.values()}
    assert [c for c, cat in cats.items() if cat == TS.BENIGN] == [
        "position_change", "contract_ended", "resigned", "restructuring", "relocation"]
    assert [c for c, cat in cats.items() if cat == TS.VIOLATION] == [
        "behavioural_issue", "policy_violation", "repeated_no_shows", "unprofessional_conduct"]
    assert [c for c, cat in cats.items() if cat == TS.SEVERE] == [
        "harassment", "violence_threats", "theft_fraud", "sexual_misconduct", "endangering_members"]
    assert TS.validate_removal("resigned", "") == {}
    assert "note" in TS.validate_removal("policy_violation", "too short")
    assert "note" in TS.validate_removal("harassment", "x" * (TS.NOTE_MAX + 1))
    assert "reason" in TS.validate_removal("made_up", "")
    assert TS.validate_removal("harassment", "Threatened a member in the changing room.") == {}


def test_readme_limits_row_states_the_values():
    from app.config import BASE_DIR, Settings

    row = next(line for line in (BASE_DIR / "README.md").read_text(encoding="utf-8").splitlines()
               if line.startswith("| **Trust & safety**") and "`NOTE_MIN`" in line)
    d = {k: Settings.model_fields[k].default for k in ("violation_autoban_threshold", "violation_window_days",
                                                        "severe_ip_ban_days", "shared_ip_threshold")}
    for text in (f"{TS.NOTE_MIN}-{TS.NOTE_MAX} character note", f"({TS.NOTE_MAX} for any note)",
                 f"{TS.LIFT_NOTE_MIN}-{TS.LIFT_NOTE_MAX} character note", f"by {d['violation_autoban_threshold']} different",
                 f"within {d['violation_window_days']} days", f"at most {TS.MAX_BANNED_IPS} of",
                 f"last {TS.IP_LOOKBACK_DAYS} days, for {d['severe_ip_ban_days']} days",
                 f"more than {d['shared_ip_threshold']} other"):
        assert text in row, text


def test_email_and_phone_normalisation():
    base = TS.email_hash("rahul.mehta@example.com")
    for variant in ("  Rahul.Mehta@Example.COM ", "rahul.mehta+work@example.com", "RAHUL.MEHTA+x+y@EXAMPLE.com"):
        assert TS.email_hash(variant) == base, variant
    assert TS.email_hash("rahulmehta@example.com") != base  # dots matter outside Gmail
    assert TS.email_hash("j.o.h.n@googlemail.com") == TS.email_hash("John@gmail.com")
    assert TS.email_hash("") is None and TS.email_hash(None) is None
    ph = TS.phone_hash("+91 98765 40099")
    for variant, country in (("98765 40099", "IN"), ("098765-40099", "IN"), ("0091 98765 40099", None),
                             ("+919876540099", None)):
        assert TS.phone_hash(variant, country) == ph, variant
    assert TS.phone_hash("12", "IN") is None
    # keyed and domain-separated: never the plain value, an e-mail hash is never a phone hash
    assert "example" not in base and len(base) == 64
    assert TS._keyed("email", "+919876540099") != TS._keyed("phone", "+919876540099")


# ====================================================================== position change = end + re-add
async def _linked_trainer(db, owner, gym, *, with_client: bool = True):
    branch = await make_branch(db, gym)
    person = await staff_user(db, full_name="Asha Rao", email="asha@example.com", phone="+91 90000 11111")
    emp = await make_employee(db, gym, user=person)
    photo = await storage.save_public_image(png_bytes((80, 80), "red"), kind="staff")
    emp.photo_key = photo.key
    await db.commit()
    await make_shift(db, emp, branch=branch, weekday=1)
    sub = None
    if with_client:
        plans = {p.code: p for p in await make_plans(db, gym)}
        member = await make_customer(db, full_name="Member Mia")
        sub = await make_subscription(db, member, plans["offline_trainer"], branch=branch)
        sub.trainer_id = emp.id
        db.add(TrainerClient(employee_id=emp.id, customer_id=member.id, subscription_id=sub.id, mode=ShiftMode.OFFLINE))
        await db.commit()
    return person, emp, sub


async def test_position_change_ends_and_re_adds(client, db):
    owner, gym = await owner_with_gym(db)
    person, emp, sub = await _linked_trainer(db, owner, gym)
    await login_as(client, owner)

    r = await client.get(f"/owner/staff/{emp.id}/change-position")
    assert r.status_code == 200 and "Why a new employment?" in page(r) and 'value="asha@example.com"' in r.text
    values = S.staff_values_from_employee(emp)
    values.update(role="receptionist", role_title="Front desk lead", hire_date=date.today().isoformat(), is_public="1")
    values.pop("invite_email")
    same = dict(values, role="trainer", role_title="")
    r = await post_form(client, f"/owner/staff/{emp.id}/change-position", same)
    assert r.status_code == 400 and "this is their current one" in page(r)

    r = await post_form(client, f"/owner/staff/{emp.id}/change-position", values)
    assert r.status_code == 303, page(r)[:2000]
    new = await db.scalar(select(Employee).where(Employee.previous_employment_id == emp.id))
    assert r.headers["location"] == f"/owner/staff/{new.id}" and "Position changed" in flash_text(r)
    old = await reload(db, Employee, emp.id)
    assert old.status == EmploymentStatus.TERMINATED and old.removal_reason == "position_change"
    assert old.removal_category == TS.BENIGN and old.removed_by_id == owner.id and old.termination_date is not None
    assert new.status == EmploymentStatus.INVITED and new.role == EmployeeRole.RECEPTIONIST
    assert new.role_title == "Front desk lead" and new.invite_email == "asha@example.com" and new.user_id is None
    # its own copy of the photo (the history record keeps its file)
    assert new.photo_key != old.photo_key and storage.path_for(new.photo_key).is_file()
    assert storage.path_for(old.photo_key).is_file()
    # the old employment's access ended; the new one goes through the normal invitation
    assert await count(db, TrainerShift, TrainerShift.employee_id == emp.id) == 0
    assert await count(db, TrainerClient, TrainerClient.employee_id == emp.id, TrainerClient.active.is_(True)) == 0
    assert (await reload(db, Subscription, sub.id)).trainer_id is None
    assert await count(db, AuthToken, AuthToken.employee_id == new.id, AuthToken.purpose == TokenPurpose.STAFF_INVITE) == 1
    note = await db.scalar(select(Notification).where(Notification.user_id == person.id,
                                                      Notification.title.like("Your position at%")))
    assert note is not None and "Front desk lead" in note.title and "accept the new" in note.body.lower()
    assert await count(db, StaffViolation) == 0
    assert await db.scalar(select(AuditLog.id).where(AuditLog.action == "staff.position_change",
                                                     AuditLog.entity_id == emp.id)) is not None
    # the history links both ways
    assert "Re-added as Front desk lead" in page(await client.get(f"/owner/staff/{emp.id}"))
    assert "Re-added after a position change" in page(await client.get(f"/owner/staff/{new.id}"))
    # a second submit of the same form changes nothing
    r = await post_form(client, f"/owner/staff/{emp.id}/change-position", values)
    assert r.status_code in (303, 302) and await count(db, Employee) == 2
    # former staff can't change position again
    assert (await client.get(f"/owner/staff/{emp.id}/change-position")).status_code == 303


async def test_position_change_double_submit_at_the_same_moment(client, db):
    owner, gym = await owner_with_gym(db)
    _person, emp, _ = await _linked_trainer(db, owner, gym, with_client=False)
    await login_as(client, owner)
    values = S.staff_values_from_employee(emp)
    values.update(role="manager", is_public="1", hire_date=date.today().isoformat())
    values.pop("invite_email")
    r1, r2 = await asyncio.gather(post_form(client, f"/owner/staff/{emp.id}/change-position", values),
                                  post_form(client, f"/owner/staff/{emp.id}/change-position", values))
    assert {r1.status_code, r2.status_code} <= {303}
    assert await count(db, Employee, Employee.previous_employment_id == emp.id) == 1
    assert await count(db, Employee) == 2


async def test_role_is_never_edited_in_place(client, db):
    owner, gym = await owner_with_gym(db)
    _person, emp, _ = await _linked_trainer(db, owner, gym, with_client=False)
    await login_as(client, owner)
    page_html = page(await client.get(f"/owner/staff/{emp.id}?tab=profile"))
    assert f"/owner/staff/{emp.id}/change-position" in page_html and 'name="role"' not in page_html
    values = S.staff_values_from_employee(emp)
    values["is_public"] = "1"
    for change in ({"role": "receptionist"}, {"role_title": "Head coach"}):
        r = await post_form(client, f"/owner/staff/{emp.id}", dict(values, **change))
        assert r.status_code == 400 and "can't be changed in place" in page(r), change
        assert f"/owner/staff/{emp.id}/change-position" in r.text
        fresh = await reload(db, Employee, emp.id)
        assert fresh.role == EmployeeRole.TRAINER and fresh.role_title is None
    # other details are still edited in place (the form without role fields keeps the position)
    values.pop("role")
    values.pop("role_title")
    r = await post_form(client, f"/owner/staff/{emp.id}", dict(values, monthly_salary="41000"))
    assert r.status_code == 303
    fresh = await reload(db, Employee, emp.id)
    assert str(fresh.monthly_salary) == "41000.00" and fresh.role == EmployeeRole.TRAINER


# ====================================================================== removal reasons and records
async def test_removal_reasons_records_and_access(client, db):
    owner, gym = await owner_with_gym(db)
    person, emp, sub = await _linked_trainer(db, owner, gym)
    await login_as(client, owner)
    r = await client.get(f"/owner/staff/{emp.id}/remove")
    assert r.status_code == 200
    for reason in TS.REMOVAL_REASONS.values():
        assert f'value="{reason.code}"' in r.text
    assert r.text.count("<optgroup") == 3
    # an ACTIVE employee's benign end waits for payroll (no final settlement yet)
    r = await remove(client, emp.id, "resigned")
    assert r.status_code == 400 and "pay isn't settled yet" in page(r)
    r = await remove(client, emp.id, "policy_violation", note="short")
    assert r.status_code == 400 and "at least" in page(r)
    r = await remove(client, emp.id, "made_up_reason", note="x" * 20)
    assert r.status_code == 400 and "Choose a reason" in page(r)
    r = await remove(client, emp.id, "repeated_no_shows", note="Missed 6 of 10 sessions after two warnings.", confirm="")
    assert r.status_code == 400 and "Tick the box" in page(r)
    assert (await reload(db, Employee, emp.id)).status == EmploymentStatus.ACTIVE

    r = await remove(client, emp.id, "repeated_no_shows", note="Missed 6 of 10 sessions after two warnings.")
    assert r.status_code == 303 and "conduct violation" in flash_text(r) and "Payroll" in flash_text(r)
    fresh = await reload(db, Employee, emp.id)
    assert fresh.status == EmploymentStatus.TERMINATED and fresh.removal_reason == "repeated_no_shows"
    assert fresh.removal_category == TS.VIOLATION and fresh.removal_note.startswith("Missed 6")
    assert fresh.removed_by_id == owner.id and fresh.removed_at is not None
    v = await db.scalar(select(StaffViolation))
    assert v.category == TS.VIOLATION and v.owner_id == owner.id and v.gym_id == gym.id
    assert v.subject_user_id == person.id and TS.email_hash("asha@example.com") in v.subject_hashes
    assert v.note.startswith("Missed 6") and v.actor_id == owner.id
    # access ended at once: shifts, client links, the member's trainer - and the member is never told why
    assert await count(db, TrainerShift, TrainerShift.employee_id == emp.id) == 0
    assert await count(db, TrainerClient, TrainerClient.employee_id == emp.id, TrainerClient.active.is_(True)) == 0
    assert (await reload(db, Subscription, sub.id)).trainer_id is None
    member_note = await db.scalar(select(Notification).where(Notification.user_id == sub.customer_id,
                                                             Notification.title == "Trainer change"))
    assert member_note is not None and "no-show" not in member_note.body.lower() and "Missed" not in member_note.body
    staff_note = await db.scalar(select(Notification).where(Notification.user_id == person.id,
                                                            Notification.title.like("Your employment at%")))
    assert staff_note is not None and "Repeated no-shows" in staff_note.body and "Missed 6" not in staff_note.body
    # the staff history shows the reason, category and note to this gym's owner
    text = page(await client.get(f"/owner/staff/{emp.id}"))
    assert "Repeated no-shows" in text and "Conduct violation" in text and "Missed 6 of 10" in text
    activity = page(await client.get(f"/owner/staff/{emp.id}?tab=activity"))
    assert "Employment ended" in activity
    # a second removal is refused; the violation record is immutable
    r = await remove(client, emp.id, "harassment", note="Trying to remove the same record again.")
    assert r.status_code == 303 and "already ended" in flash_text(r)
    assert await count(db, StaffViolation) == 1
    v.note = "rewritten"
    with pytest.raises(RuntimeError):
        await db.flush()
    await db.rollback()


async def test_benign_removal_of_an_invitation_needs_no_note(client, db):
    owner, gym = await owner_with_gym(db)
    emp = await make_employee(db, gym, status=EmploymentStatus.INVITED)
    await login_as(client, owner)
    r = await remove(client, emp.id, "contract_ended")
    assert r.status_code == 303 and "can be added again later" in flash_text(r)
    fresh = await reload(db, Employee, emp.id)
    assert fresh.status == EmploymentStatus.TERMINATED and fresh.removal_category == TS.BENIGN
    assert fresh.removal_note is None and await count(db, StaffViolation) == 0
    assert await count(db, AuthToken, AuthToken.employee_id == emp.id, AuthToken.used_at.is_(None)) == 0
    # rehire-eligible: the same person can be added again
    r = await add_staff(client, gym, invite_email=emp.invite_email, full_name="Back Again")
    assert r.status_code == 303, page(r)[:1500]


async def test_owners_never_see_or_manage_other_gyms_records(client, db):
    owner_a, gym_a = await owner_with_gym(db)
    owner_b, _gym_b = await owner_with_gym(db)
    emp = await make_employee(db, gym_a, status=EmploymentStatus.INVITED)
    await login_as(client, owner_b)
    assert (await client.get(f"/owner/staff/{emp.id}/remove")).status_code == 404
    assert (await remove(client, emp.id, "harassment", note="Not my employee at all here.")).status_code == 404
    assert (await client.get(f"/owner/staff/{emp.id}/change-position")).status_code == 404
    assert (await reload(db, Employee, emp.id)).status == EmploymentStatus.INVITED


# ====================================================================== re-hire eligibility
async def test_rehire_refused_at_this_owner_never_reveals_reasons_elsewhere(client, db):
    owner_a, gym_a1 = await owner_with_gym(db)
    gym_a2 = await make_gym(db, owner_a)
    owner_b, gym_b = await owner_with_gym(db)
    person = await staff_user(db, email="rahul.mehta@example.com")
    emp = await make_employee(db, gym_a1, user=person)
    await login_as(client, owner_a)
    note = "Shouted at a member during a session."
    assert (await remove(client, emp.id, "unprofessional_conduct", note=note)).status_code == 303
    await client.get("/owner/staff")  # shows (and clears) owner A's own confirmation message
    # owner A: refused at any of their gyms, with a neutral message, matched by the normalised e-mail too
    for gym, email in ((gym_a1, "rahul.mehta@example.com"), (gym_a2, " Rahul.Mehta+again@Example.com ")):
        r = await add_staff(client, gym, invite_email=email.strip(), full_name="Rahul Mehta")
        text = page(r)
        assert r.status_code == 400 and TS.CANT_ADD in text, email
        assert note not in text and "Unprofessional" not in text and "conduct violation" not in text.lower()
    # owner B: allowed - and nothing of A's record shows up
    async with make_client() as cb:
        await login_as(cb, owner_b)
        r = await add_staff(cb, gym_b, invite_email="rahul.mehta@example.com", full_name="Rahul Mehta")
        assert r.status_code == 303, page(r)[:1500]
        new = await db.scalar(select(Employee).where(Employee.gym_id == gym_b.id))
        text = page(await cb.get(f"/owner/staff/{new.id}"))
        assert note not in text and "Unprofessional" not in text
        assert (await cb.get(f"/owner/staff/{emp.id}")).status_code == 404
    # a pending invitation of owner A for the person can't be re-sent either
    invited = Employee(gym_id=gym_a2.id, invite_email="rahul.mehta@example.com", full_name="Rahul Mehta",
                       role=EmployeeRole.CLEANER, country_code="IN", currency="INR", monthly_salary=1000,
                       hire_date=date.today(), status=EmploymentStatus.INVITED, photo_key="public/staff/x.jpg")
    db.add(invited)
    await db.commit()
    r = await post_form(client, f"/owner/staff/{invited.id}/resend-invite")
    assert r.status_code == 303 and TS.CANT_ADD in flash_text(r)
    assert await count(db, AuthToken, AuthToken.employee_id == invited.id) == 0


async def test_rehire_matches_a_verified_id_number(client, db):
    owner, gym = await owner_with_gym(db)
    id_hash = pepper_hash("ABCDE1234F")
    first = await staff_user(db, email="first.identity@example.com")
    db.add(IdentityDocument(user_id=first.id, doc_type=IdDocType.PAN, country_code="IN", front_key="private/x/a.bin",
                            status=IdStatus.VERIFIED, id_number_hash=id_hash))
    await db.commit()
    emp = await make_employee(db, gym, user=first)
    await login_as(client, owner)
    assert (await remove(client, emp.id, "policy_violation", note="Shared member data outside the gym.")).status_code == 303
    second = await staff_user(db, email="other.address@example.com")  # same verified ID, another e-mail
    db.add(IdentityDocument(user_id=second.id, doc_type=IdDocType.PAN, country_code="IN", front_key="private/x/b.bin",
                            status=IdStatus.VERIFIED, id_number_hash=id_hash))
    await db.commit()
    r = await add_staff(client, gym, invite_email="other.address@example.com")
    assert r.status_code == 400 and TS.CANT_ADD in page(r)


# ====================================================================== auto-ban threshold
async def test_two_violations_at_two_gyms_ban_the_account(client, db):
    admin = await make_user(db, role=Role.SUPERADMIN)
    owner_a, gym_a = await owner_with_gym(db)
    owner_b, gym_b = await owner_with_gym(db)
    owner_c, gym_c = await owner_with_gym(db)
    person = await staff_user(db, email="vikram@example.com", phone="+91 91234 56789")
    emp_a = await make_employee(db, gym_a, user=person)
    emp_b = await make_employee(db, gym_b, user=person)
    emp_c = await make_employee(db, gym_c, user=person)
    from app.models import Job
    from app.models.enums import JobStatus

    job = Job(kind="coach_plan", payload={}, status=JobStatus.QUEUED, user_id=person.id, exit_policy="keep")
    db.add(job)
    await db.commit()
    async with make_client() as staff_client:
        await login_as(staff_client, person)
        await login_as(client, owner_a)
        assert (await remove(client, emp_a.id, "policy_violation", note="Used the gym's card for personal buys.")).status_code == 303
        assert (await reload(db, User, person.id)).is_active and await count(db, AccountBan) == 0
        async with make_client() as cb:
            await login_as(cb, owner_b)
            r = await remove(cb, emp_b.id, "behavioural_issue", note="Repeatedly rude to colleagues this month.")
            assert r.status_code == 303 and "banned" not in flash_text(r).lower()  # never hints at other gyms
        fresh = await reload(db, User, person.id)
        assert not fresh.is_active
        ban = await db.scalar(select(AccountBan))
        assert ban.user_id == person.id and ban.source == "violations" and ban.lifted_at is None
        assert await count(db, UserSession, UserSession.user_id == person.id, UserSession.revoked_at.is_(None)) == 0
        assert (await staff_client.get("/staff")).status_code in (302, 303)  # signed out everywhere
        stopped = await reload(db, Job, job.id)  # their background / AI work stops too, whatever its exit policy
        assert stopped.status == JobStatus.CANCELLED and stopped.cancel_reason == "account_banned"
    # the person's other current employment is suspended; its owner learns only that the account was disabled
    assert (await reload(db, Employee, emp_c.id)).status == EmploymentStatus.SUSPENDED
    c_note = await db.scalar(select(Notification).where(Notification.user_id == owner_c.id))
    assert "disabled by the platform" in c_note.body and "Used the gym" not in c_note.body
    assert "violation" not in c_note.body.lower()
    # the identity is registered; the super-admin is notified; sign-in is refused neutrally
    kinds = {(b.kind, b.reason) for b in (await db.execute(select(BlockedIdentity))).scalars()}
    assert {("email", "banned"), ("phone", "banned")} <= kinds
    assert await count(db, Notification, Notification.user_id == admin.id, Notification.link.like("/superadmin/trust%")) >= 1
    async with make_client() as anon:
        r = await post_form(anon, "/auth/login", {"identifier": "vikram@example.com", "password": DEFAULT_PASSWORD})
        assert r.status_code == 400 and "disabled" in page(r) and "violation" not in page(r).lower()
    assert await db.scalar(select(AuditLog.id).where(AuditLog.action == "trust.account_ban")) is not None


async def test_concurrent_removals_at_two_gyms_still_reach_the_threshold(db):
    owner_a, gym_a = await owner_with_gym(db)
    owner_b, gym_b = await owner_with_gym(db)
    person = await staff_user(db, email="same.moment@example.com")
    emp_a = await make_employee(db, gym_a, user=person)
    emp_b = await make_employee(db, gym_b, user=person)
    ca, cb = await client_for(owner_a), await client_for(owner_b)
    try:
        r1, r2 = await asyncio.gather(
            remove(ca, emp_a.id, "policy_violation", note="Took cash payments off the books."),
            remove(cb, emp_b.id, "repeated_no_shows", note="No-show for nine booked sessions."))
    finally:
        await ca.aclose()
        await cb.aclose()
    assert r1.status_code == r2.status_code == 303
    assert await count(db, StaffViolation) == 2
    assert await count(db, AccountBan) == 1 and not (await reload(db, User, person.id)).is_active


async def test_lifting_a_ban_resets_the_violation_count(client, db):
    admin = await make_user(db, role=Role.SUPERADMIN)
    owners = [await owner_with_gym(db) for _ in range(3)]
    person = await staff_user(db, email="second.chance@example.com")
    emps = [await make_employee(db, gym, user=person) for _owner, gym in owners]
    for (owner, _gym), emp in zip(owners[:2], emps[:2]):
        async with make_client() as c:
            await login_as(c, owner)
            assert (await remove(c, emp.id, "policy_violation", note="Broke the equipment-safety policy.")).status_code == 303
    ban = await db.scalar(select(AccountBan))
    await login_as(client, admin)
    r = await post_form(client, f"/superadmin/trust/bans/{ban.id}/lift", {"note": "Gym B withdrew its complaint."})
    assert r.status_code == 303 and "was lifted" in flash_text(r)
    assert (await reload(db, User, person.id)).is_active
    emp3 = await reload(db, Employee, emps[2].id)
    emp3.status = EmploymentStatus.ACTIVE  # its owner reactivated them
    await db.commit()
    async with make_client() as c:
        await login_as(c, owners[2][0])
        assert (await remove(c, emp3.id, "policy_violation", note="Late again after the review.")).status_code == 303
    assert (await reload(db, User, person.id)).is_active and await count(db, AccountBan) == 1


# ====================================================================== severe -> ban + IP bans
async def test_severe_bans_account_and_recent_public_addresses(db):
    admin = await make_user(db, role=Role.SUPERADMIN)
    owner, gym = await owner_with_gym(db)
    person = await staff_user(db, email="danger@example.com")
    emp = await make_employee(db, gym, user=person)
    own_ip, shared_ip, owner_home_ip, request_ip = "81.2.69.160", "81.2.69.161", "81.2.69.162", "81.2.69.163"
    await add_session(db, person, own_ip, days_ago=0.5)
    await add_session(db, person, shared_ip, days_ago=1)
    await add_session(db, person, "192.168.1.20", days_ago=2)  # LAN: never banned
    await add_session(db, person, owner_home_ip, days_ago=3)  # the acting admin uses it too
    await add_session(db, person, request_ip, days_ago=4)  # the admin's own address right now
    await add_session(db, person, "81.2.69.199", days_ago=45)  # too old: not considered
    for _ in range(4):  # gym Wi-Fi / carrier NAT: more than SHARED_IP_THRESHOLD (3) other active accounts
        await add_session(db, await make_customer(db), shared_ip, days_ago=2)
    await add_session(db, owner, owner_home_ip, days_ago=1)
    c = await client_for(owner, ip=request_ip)
    try:
        r = await remove(c, emp.id, "violence_threats", note="Threatened a member with a dumbbell on 3 Nov.")
    finally:
        await c.aclose()
    assert r.status_code == 303 and "account is banned" in flash_text(r)
    ban = await db.scalar(select(AccountBan))
    assert ban.source == "severe" and not (await reload(db, User, person.id)).is_active
    actions = {a.ip: a for a in (await db.execute(select(TrustIpAction))).scalars()}
    assert set(actions) == {own_ip, shared_ip, "192.168.1.20", owner_home_ip, request_ip}
    assert actions[own_ip].action == "banned" and actions[own_ip].ip_ban_id is not None
    assert actions[shared_ip].action == "shared" and actions[shared_ip].other_accounts == 4
    assert actions["192.168.1.20"].action == "not_public"
    assert actions[owner_home_ip].action == "actor" and actions[request_ip].action == "actor"
    bans = (await db.execute(select(IpBan))).scalars().all()
    assert [b.ip for b in bans] == [own_ip]
    b = bans[0]
    assert b.created_by == "trust" and b.severity == 5 and b.user_id == person.id
    assert timedelta(days=29, hours=23) < b.banned_until - utcnow() <= timedelta(days=30)
    assert ban_registry.get(own_ip) is not None and ban_registry.get(shared_ip) is None
    assert ban_registry.get(request_ip) is None
    kinds = {(x.kind, x.reason) for x in (await db.execute(select(BlockedIdentity))).scalars()}
    assert ("email", "severe_violation") in kinds
    titles = [n.title for n in (await db.execute(select(Notification).where(Notification.user_id == admin.id))).scalars()]
    assert any(t.startswith("Account banned") for t in titles) and "Shared network address not banned" in titles
    # the gate now refuses the banned address
    async with make_client(own_ip) as banned:
        assert (await banned.get("/")).status_code == 403


async def test_severe_ip_bans_spare_platform_admins_and_stop_at_five(db):
    admin = await make_user(db, role=Role.SUPERADMIN)
    owner, gym = await owner_with_gym(db)
    person = await staff_user(db, email="many.places@example.com")
    emp = await make_employee(db, gym, user=person)
    admin_ip = "81.2.69.180"
    await add_session(db, admin, admin_ip, days_ago=2)
    await add_session(db, person, admin_ip, days_ago=0.1)  # newest: the super-admin's office network
    public = [f"81.2.69.{190 + i}" for i in range(6)]
    for i, ip in enumerate(public):
        await add_session(db, person, ip, days_ago=1 + i)
    c = await client_for(owner, ip="81.2.69.250")
    try:
        assert (await remove(c, emp.id, "theft_fraud", note="Took money from the member lockers.")).status_code == 303
    finally:
        await c.aclose()
    actions = {a.ip: a.action for a in (await db.execute(select(TrustIpAction))).scalars()}
    assert len(actions) == TS.MAX_BANNED_IPS == 5  # the newest five addresses only
    assert actions[admin_ip] == "platform_admin"
    assert [ip for ip in public if actions.get(ip) == "banned"] == public[:4]
    assert public[4] not in actions and public[5] not in actions
    assert ban_registry.get(admin_ip) is None and await count(db, IpBan) == 4


async def test_never_accepted_invitation_never_bans_or_blocks_anyone(client, db):
    """An owner can't weaponise removals: inviting someone's e-mail and 'removing' it bans nobody, registers nothing,
    and doesn't count toward that person's threshold - only the owner's own re-hire is barred."""
    admin = await make_user(db, role=Role.SUPERADMIN)
    owner, gym = await owner_with_gym(db)
    victim = await staff_user(db, email="victim@example.com")
    unlinked = await make_employee(db, gym, status=EmploymentStatus.INVITED)
    unlinked.invite_email = "victim@example.com"
    await db.commit()
    await login_as(client, owner)
    assert (await remove(client, unlinked.id, "theft_fraud", note="Took the till money on their trial day.")).status_code == 303
    assert await count(db, AccountBan) == 0 and await count(db, BlockedIdentity) == 0
    assert (await reload(db, User, victim.id)).is_active
    assert await TS.registration_blocked(db, email="victim@example.com") is None
    assert await count(db, Notification, Notification.user_id == admin.id,
                       Notification.title.like("Severe violation reported%")) == 1
    # one real violation elsewhere: the bogus invitation doesn't make it two
    owner_b, gym_b = await owner_with_gym(db)
    emp_b = await make_employee(db, gym_b, user=victim)
    async with make_client() as cb:
        await login_as(cb, owner_b)
        assert (await remove(cb, emp_b.id, "policy_violation", note="Left the front desk unattended twice.")).status_code == 303
    assert (await reload(db, User, victim.id)).is_active and await count(db, AccountBan) == 0
    # ... but the owner who wrote it can't add the address again
    r = await add_staff(client, gym, invite_email="victim@example.com")
    assert r.status_code == 400 and TS.CANT_ADD in page(r)


async def test_one_owner_with_two_gyms_counts_once(client, db):
    owner, gym_1 = await owner_with_gym(db)
    gym_2 = await make_gym(db, owner)
    person = await staff_user(db, email="two.jobs@example.com")
    emp_1 = await make_employee(db, gym_1, user=person)
    emp_2 = await make_employee(db, gym_2, user=person)
    await login_as(client, owner)
    for emp in (emp_1, emp_2):
        assert (await remove(client, emp.id, "behavioural_issue", note="Argued with members at the counter.")).status_code == 303
    assert await count(db, StaffViolation) == 2
    assert (await reload(db, User, person.id)).is_active and await count(db, AccountBan) == 0


# ====================================================================== the blocked-identity registry
async def test_self_delete_registers_identity_and_survives_the_purge(client, db):
    from app.services.accounts import identifier_conflict, purge_due_accounts

    user = await make_customer(db, email="bye@example.com", phone="+91 98765 40099")
    db.add(IdentityDocument(user_id=user.id, doc_type=IdDocType.AADHAAR, country_code="IN", front_key="private/x/c.bin",
                            status=IdStatus.VERIFIED, id_number_hash=pepper_hash("1234 5678 9012")))
    db.add(IdentityDocument(user_id=user.id, doc_type=IdDocType.PAN, country_code="IN", front_key="private/x/d.bin",
                            status=IdStatus.REJECTED, id_number_hash=pepper_hash("SOMEONEELSE1")))
    await db.commit()
    await login_as(client, user)
    r = await client.get("/account/delete")
    assert "can never be used to create a new IronPulse account" in page(r) and 'name="confirm_identity"' in r.text
    r = await post_form(client, "/account/delete", {"password": DEFAULT_PASSWORD, "confirm_text": "DELETE"})
    assert r.status_code == 400 and "Tick the box" in page(r)
    assert (await reload(db, User, user.id)).deleted_at is None
    r = await post_form(client, "/account/delete", {"password": DEFAULT_PASSWORD, "confirm_text": "DELETE",
                                                    "confirm_identity": "1"})
    assert r.status_code == 303
    rows = (await db.execute(select(BlockedIdentity))).scalars().all()
    assert {(x.kind, x.reason) for x in rows} == {("email", "self_deleted"), ("phone", "self_deleted"),
                                                  ("id_number", "self_deleted")}
    assert all(x.user_id == user.id for x in rows)
    stored = " ".join(f"{x.value_hash} {x.note or ''}" for x in rows)
    assert "bye@example.com" not in stored and "98765" not in stored  # hashes only
    assert pepper_hash("SOMEONEELSE1") not in stored  # a rejected card's number is never registered
    # the purge erases the account but never the registry
    acct = await reload(db, User, user.id)
    acct.purge_after = utcnow() - timedelta(minutes=1)
    await db.commit()
    await purge_due_accounts(db)
    await db.commit()
    assert await db.get(User, user.id, populate_existing=True) is None
    assert await count(db, BlockedIdentity) == 3
    for email in ("bye@example.com", " BYE@Example.com ", "bye+new@example.com"):
        assert await TS.registration_blocked(db, email=email) == TS.BLOCKED_MESSAGES["email"], email
    for phone, country in (("+91 98765 40099", None), ("98765 40099", "IN"), ("0091-98765-40099", None)):
        assert await TS.registration_blocked(db, phone=phone, country_code=country), phone
    assert await TS.registration_blocked(db, id_number_hash=pepper_hash("123456789012"))
    assert await TS.registration_blocked(db, email="someone.new@example.com", phone="+91 90000 00001") is None
    conflict = await identifier_conflict(db, email="BYE@example.com")
    assert conflict is not None and "can't be used" in conflict.message
    conflict = await identifier_conflict(db, phone="9876540099", country_code="IN")
    assert conflict is not None and conflict.field == "phone"
    # sign-up with the e-mail is refused for good
    async with make_client() as anon:
        r = await post_form(anon, "/auth/signup", {
            "role": "customer", "full_name": "Bye Again", "email": "bye@example.com", "phone": "90000 22222",
            "phone_country": "IN", "date_of_birth": "1995-01-01", "country": "IN", "city": "Bengaluru",
            "timezone": "Asia/Kolkata", "password": "Str0ng!Passw0rd-x", "password_confirm": "Str0ng!Passw0rd-x",
            "accept_terms": "1", "id_doc_type": "aadhaar", "id_country": "IN"},
            files={"id_front": ("front.png", png_bytes((320, 200), "white", "ID"), "image/png")})
        assert r.status_code == 400 and "can't be used" in page(r)
    assert await count(db, User, User.email == "bye@example.com") == 0


async def test_admin_delete_registers_and_own_account_is_not_blocked(client, db):
    from app.services.accounts import identifier_conflict

    admin = await make_user(db, role=Role.SUPERADMIN)
    user = await make_customer(db, email="removed@example.com", phone="+91 90000 33333")
    await login_as(client, admin)
    r = await post_form(client, f"/superadmin/users/{user.id}/delete", {"reason": "Fraudulent chargebacks"})
    assert r.status_code == 303
    rows = (await db.execute(select(BlockedIdentity))).scalars().all()
    assert {x.reason for x in rows} == {"admin_deleted"} and {x.actor_id for x in rows} == {admin.id}
    # restored within the retention window: the account keeps its own identifiers
    r = await post_form(client, f"/superadmin/users/{user.id}/restore")
    assert r.status_code == 303
    assert await identifier_conflict(db, email="removed@example.com", exclude_user_id=user.id) is None
    assert await TS.registration_blocked(db, email="removed@example.com", exclude_user_id=user.id) is None
    assert await TS.registration_blocked(db, email="removed@example.com") is not None  # anyone else: blocked


# ====================================================================== /superadmin/trust
async def _ban_for(db, n: int, *, lifted: bool = False) -> AccountBan:
    user = await staff_user(db, full_name=f"Banned Person {n}")
    user.is_active = lifted
    ban = AccountBan(user_id=user.id, person_name=user.full_name, source="violations", reason=f"Test ban {n}",
                     lifted_at=utcnow() if lifted else None, lift_note="ok" if lifted else None)
    db.add(ban)
    await db.commit()
    return ban


async def test_trust_page_authz_paging_filters_and_csp(client, db):
    for n in range(30):
        await _ban_for(db, n)
    await _ban_for(db, 99, lifted=True)
    # anonymous -> sign-in; every other role -> 403
    r = await client.get("/superadmin/trust")
    assert r.status_code == 303 and r.headers["location"].startswith("/auth/login")
    for role in (Role.CUSTOMER, Role.STAFF, Role.OWNER):
        async with make_client() as c:
            await login_as(c, await make_user(db, role=role))
            assert (await c.get("/superadmin/trust")).status_code == 403, role
            ban = await db.scalar(select(AccountBan).where(AccountBan.lifted_at.is_(None)).limit(1))
            r = await post_form(c, f"/superadmin/trust/bans/{ban.id}/lift", {"note": "trying to lift it"})
            assert r.status_code == 403, role
    admin = await make_user(db, role=Role.SUPERADMIN)
    await login_as(client, admin)
    r = await client.get("/superadmin/trust")
    assert r.status_code == 200 and r.text.count('action="/superadmin/trust/bans/') == 25
    assert "script-src" in r.headers.get("Content-Security-Policy", "")
    assert not re.search(r"<script(?![^>]*\bnonce=)[^>]*>", r.text)  # CSP-safe: no script without the nonce
    assert 'href="/superadmin/trust" class="sa-subnav-link" aria-current="page"' in r.text
    r2 = await client.get("/superadmin/trust?tab=bans&state=active&page=2")
    assert r2.text.count('action="/superadmin/trust/bans/') == 5
    lifted = await client.get("/superadmin/trust?tab=bans&state=lifted")
    assert "Banned Person 99" in lifted.text and "Banned Person 3 <" not in lifted.text
    found = await client.get("/superadmin/trust?tab=bans&q=Banned+Person+7")
    assert "Banned Person 7" in found.text and "Banned Person 8" not in found.text
    for tab in ("violations", "ips", "identities"):
        assert (await client.get(f"/superadmin/trust?tab={tab}")).status_code == 200, tab
    assert (await client.get("/superadmin/trust?tab=bogus&page=-4&state=x")).status_code == 200


async def test_trust_page_lists_violations_and_looks_identities_up(client, db):
    owner, gym = await owner_with_gym(db)
    emp = await make_employee(db, gym, status=EmploymentStatus.INVITED)
    await login_as(client, owner)
    await remove(client, emp.id, "sexual_misconduct", note="Reported by two members on 2 Nov.")
    user = await make_customer(db, email="lookup.me@example.com", phone="+91 90000 44444")
    from app.services.accounts import soft_delete_user

    await soft_delete_user(db, user, reason="test")
    await db.commit()
    admin = await make_user(db, role=Role.SUPERADMIN)
    await login_as(client, admin)
    r = await client.get("/superadmin/trust?tab=violations&category=severe")
    assert "Reported by two members" in page(r) and "Sexual misconduct" in page(r)
    assert "Reported by two" not in page(await client.get("/superadmin/trust?tab=violations&category=violation"))
    r = await client.get("/superadmin/trust?tab=identities&q=LOOKUP.ME%40example.com")
    assert "Deleted by the holder" in page(r) and r.text.count('<li class="card flex') == 1
    r = await client.get("/superadmin/trust?tab=identities&q=90000+44444&country=IN")
    assert r.text.count('<li class="card flex') == 1 and "Phone" in r.text
    # the deleted account's e-mail + phone (a never-accepted invitation registers nothing - see the anti-abuse test)
    assert (await client.get("/superadmin/trust?tab=identities")).text.count('<li class="card flex') == 2
    by_holder = await client.get("/superadmin/trust?tab=identities&reason=self_deleted&kind=phone")
    assert by_holder.text.count('<li class="card flex') == 1
    assert (await client.get("/superadmin/trust?tab=identities&reason=banned")).text.count('<li class="card flex') == 0
    r = await client.get("/superadmin/trust?tab=identities&q=nobody%40example.com")
    assert "Not blocked" in r.text


async def test_lift_requires_a_note_and_lifts_its_ip_bans(client, db):
    owner, gym = await owner_with_gym(db)
    person = await staff_user(db, email="appeal@example.com")
    emp = await make_employee(db, gym, user=person)
    await add_session(db, person, "81.2.69.170")
    c = await client_for(owner, ip="81.2.69.171")
    try:
        assert (await remove(c, emp.id, "harassment", note="Repeated unwanted messages to a member.")).status_code == 303
    finally:
        await c.aclose()
    ban = await db.scalar(select(AccountBan))
    assert ban_registry.get("81.2.69.170") is not None
    admin = await make_user(db, role=Role.SUPERADMIN)
    await login_as(client, admin)
    r = await post_form(client, f"/superadmin/trust/bans/{ban.id}/lift", {"note": "no"})
    assert r.status_code == 400 and "Write why the ban is lifted" in page(r)
    assert (await reload(db, AccountBan, ban.id)).lifted_at is None
    r = await post_form(client, f"/superadmin/trust/bans/{ban.id}/lift",
                        {"note": "Investigated: the messages were a misunderstanding."})
    assert r.status_code == 303 and "1 network address ban" in flash_text(r)
    fresh = await reload(db, AccountBan, ban.id)
    assert fresh.lifted_at is not None and fresh.lifted_by_id == admin.id and "misunderstanding" in fresh.lift_note
    assert (await reload(db, User, person.id)).is_active
    ip_ban = await db.scalar(select(IpBan).where(IpBan.ip == "81.2.69.170"))
    assert ip_ban.lifted_at is not None and ban_registry.get("81.2.69.170") is None
    actions = set((await db.execute(select(AuditLog.action))).scalars().all())
    assert {"trust.ban_lift", "security.ban_lift", "trust.violation", "trust.account_ban", "trust.ip_ban"} <= actions
    lift_audit = await db.scalar(select(AuditLog).where(AuditLog.action == "security.ban_lift"))
    assert lift_audit.detail["ip"] == "81.2.69.170"  # the other workers' ban sync reloads this address
    note = await db.scalar(select(Notification).where(Notification.user_id == person.id,
                                                      Notification.title.like("%active again%")))
    assert note is not None
    r = await post_form(client, f"/superadmin/trust/bans/{ban.id}/lift", {"note": "Lifting it a second time."})
    assert r.status_code == 303 and "already lifted" in flash_text(r)
    async with make_client() as anon:
        r = await post_form(anon, "/auth/login", {"identifier": "appeal@example.com", "password": DEFAULT_PASSWORD})
        assert r.status_code == 303


# ====================================================================== caps on the staff paths (race-safe)
@pytest.fixture
def tight_caps(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "caps_overrides",
                        "staff_invites_per_gym=1,employee_documents=1,shifts_per_employee=1,open_jobs_per_gym=1")


async def test_staff_invite_cap_holds_under_concurrency(client, db, tight_caps):
    owner, gym = await owner_with_gym(db)
    await login_as(client, owner)
    r1, r2 = await asyncio.gather(add_staff(client, gym, invite_email="one@example.com"),
                                  add_staff(client, gym, invite_email="two@example.com"))
    assert sorted([r1.status_code, r2.status_code]) == [303, 409]
    refused = r1 if r1.status_code == 409 else r2
    assert "invitations waiting" in page(refused)
    assert await count(db, Employee) == 1


async def test_identical_staff_adds_at_once_create_one_record(client, db):
    owner, gym = await owner_with_gym(db)
    await login_as(client, owner)
    r1, r2 = await asyncio.gather(add_staff(client, gym, invite_email="twice@example.com"),
                                  add_staff(client, gym, invite_email="twice@example.com"))
    assert sorted([r1.status_code, r2.status_code]) == [303, 400]
    assert await count(db, Employee, Employee.invite_email == "twice@example.com") == 1


async def test_document_and_shift_caps(client, db, tight_caps):
    owner, gym = await owner_with_gym(db)
    branch = await make_branch(db, gym)
    emp = await make_employee(db, gym)
    await login_as(client, owner)
    doc = [("document", ("cv.png", png_bytes((90, 90), "white"), "image/png"))]
    r1, r2 = await asyncio.gather(
        post_form(client, f"/owner/staff/{emp.id}/documents", {"doc_type": "resume"}, files=doc),
        post_form(client, f"/owner/staff/{emp.id}/documents", {"doc_type": "contract"}, files=doc))
    assert sorted([r1.status_code, r2.status_code]) == [303, 400]
    from app.models import EmployeeDocument

    assert await count(db, EmployeeDocument, EmployeeDocument.employee_id == emp.id) == 1
    shift = {"weekdays": ["0", "1"], "start_time": "06:00", "end_time": "08:00", "mode": "offline",
             "branch_id": str(branch.id)}
    r = await post_form(client, f"/owner/staff/{emp.id}/shifts", shift)
    assert r.status_code == 400 and "at most 1 weekly shift" in page(r)
    assert await count(db, TrainerShift, TrainerShift.employee_id == emp.id) == 0


async def test_open_job_cap(client, db, tight_caps):
    owner, gym = await owner_with_gym(db)
    await login_as(client, owner)
    form = {"title": "Coach", "role": "trainer", "employment_type": "full_time", "location": "Bengaluru",
            "currency": "INR", "salary_period": "month", "requirements": "Certified coach with experience.",
            "is_open": "1"}
    assert (await post_form(client, f"/owner/gyms/{gym.id}/jobs/new", form)).status_code == 303
    r = await post_form(client, f"/owner/gyms/{gym.id}/jobs/new", dict(form, title="Second coach"))
    assert r.status_code == 409 and "open job postings" in page(r)


# ====================================================================== ban side effects at other gyms (F44, F46)
async def _two_gym_person(db, *, role_b: EmployeeRole = EmployeeRole.RECEPTIONIST):
    owner_a = await make_owner(db, full_name="Riya Kapoor")
    gym_a = await make_gym(db, owner_a, name="Forge and Flow")
    owner_b = await make_owner(db, full_name="Aman Sethi")
    gym_b = await make_gym(db, owner_b, name="Capital Calisthenics Co")
    person = await staff_user(db, email="pat.both@example.com", full_name="Pat Both")
    emp_a = await make_employee(db, gym_a, user=person)
    emp_b = await make_employee(db, gym_b, user=person, role=role_b)
    return owner_a, gym_a, owner_b, gym_b, person, emp_a, emp_b


async def test_ban_side_effects_never_tell_other_owners_who_reported_or_show_other_tenants_records(db):
    """F44: owner A's SEVERE removal suspends the person's job at owner B's gym. That gym-B audit row was written with
    owner A as actor and owner A's request, so owner B's audit log and the staff timeline said 'Suspended ... Riya
    Kapoor' (which gym reported their staff member), and owner A's audit log listed 'employee #N - Capital Calisthenics
    Co.' (another tenant's record) and the banned person's network addresses. The side effect is now a platform
    action, and an owner's audit log never shows another tenant's gym nor the platform's own trust records."""
    owner_a, _gym_a, owner_b, gym_b, person, emp_a, emp_b = await _two_gym_person(db)
    await add_session(db, person, "81.2.69.140", days_ago=0.5)  # a public address: an IP-ban row
    ca, cb = await client_for(owner_a, ip="81.2.69.141"), await client_for(owner_b, ip="81.2.69.142")
    try:
        r = await remove(ca, emp_a.id, "harassment", note="Repeated unwanted messages to a member.")
        assert r.status_code == 303 and "account is banned" in flash_text(r)
        assert (await reload(db, Employee, emp_b.id)).status == EmploymentStatus.SUSPENDED
        row = await db.scalar(select(AuditLog).where(AuditLog.action == "staff.suspend", AuditLog.entity_id == emp_b.id))
        assert row.gym_id == gym_b.id and row.actor_id is None and row.ip is None
        for url in ("/owner/audit", "/owner/audit?who=others", f"/owner/audit?gym={gym_b.id}",
                    f"/owner/staff/{emp_b.id}?tab=activity"):
            text = page(await cb.get(url))
            assert "IronPulse platform" in text, url
            assert "Riya Kapoor" not in text and "Forge and Flow" not in text and "81.2.69.141" not in text, url
        for url in ("/owner/audit", "/owner/audit?who=me", "/owner/audit?q=staff", "/owner/audit?area=trust"):
            text = page(await ca.get(url))
            assert "Capital Calisthenics" not in text and f"employee #{emp_b.id}" not in text, url
            assert "81.2.69.140" not in text, url  # the banned person's address
    finally:
        await ca.aclose()
        await cb.aclose()


async def test_an_earlier_side_effect_row_is_shown_as_the_platform(db):
    """F44, rows written before the fix (actor = the reporting owner, on the other gym's record): owner B sees
    'IronPulse platform', never the other owner's name; owner A no longer sees the other tenant's row."""
    from app.services.audit import audit

    owner_a, _gym_a, owner_b, gym_b, _person, _emp_a, emp_b = await _two_gym_person(db)
    await audit(db, owner_a, "staff.suspend", "employee", emp_b.id, gym_id=gym_b.id, detail={
        "from": "active", "summary": "IronPulse account disabled by the platform", "by": "trust"})
    await db.commit()
    ca, cb = await client_for(owner_a), await client_for(owner_b)
    try:
        for url in ("/owner/audit", f"/owner/staff/{emp_b.id}?tab=activity"):
            text = page(await cb.get(url))
            assert "IronPulse platform" in text and "Riya Kapoor" not in text, url
        for url in ("/owner/audit", "/owner/audit?who=me"):
            text = page(await ca.get(url))
            assert "Capital Calisthenics" not in text and f"employee #{emp_b.id}" not in text, url
    finally:
        await ca.aclose()
        await cb.aclose()


async def test_an_auto_ban_is_never_shown_to_the_owners_whose_reports_triggered_it(db):
    """F44: the second reporting owner's audit log showed the platform's 'account ban' row with 'Conduct violations
    reported by 2 gyms' - telling them another gym had reported the person (their flash message deliberately never
    hints at that)."""
    owner_a, _gym_a, owner_b, _gym_b, person, emp_a, emp_b = await _two_gym_person(db)
    ca, cb = await client_for(owner_a), await client_for(owner_b)
    try:
        assert (await remove(ca, emp_a.id, "policy_violation", note="Took cash payments off the books.")).status_code == 303
        assert (await remove(cb, emp_b.id, "repeated_no_shows", note="No-show for nine booked sessions.")).status_code == 303
        assert not (await reload(db, User, person.id)).is_active
        for c in (ca, cb):
            text = page(await c.get("/owner/audit")).lower()
            assert "account ban" not in text and "reported by" not in text and "gyms within" not in text
    finally:
        await ca.aclose()
        await cb.aclose()


async def test_a_banned_persons_suspended_job_cannot_be_reactivated_or_given_clients(db):
    """F46: the ban suspends the person's job at gym B; gym B's owner pressed Reactivate and the job went straight back
    to ACTIVE while the ban was in force (the activation requirements ignored the disabled account) - the person was
    listed on the public gym page again, could be given clients and received members' health alerts."""
    owner_a, _gym_a, owner_b, gym_b, person, emp_a, emp_b = await _two_gym_person(db)
    ca, cb = await client_for(owner_a), await client_for(owner_b)
    try:
        r = await remove(ca, emp_a.id, "sexual_misconduct", note="Inappropriate touching reported by two members.")
        assert r.status_code == 303 and (await reload(db, Employee, emp_b.id)).status == EmploymentStatus.SUSPENDED
        r = await post_form(cb, f"/owner/staff/{emp_b.id}/reactivate")
        assert r.status_code == 303 and "account is disabled" in flash_text(r)
        assert "violation" not in flash_text(r).lower() and "Forge" not in flash_text(r)
        assert (await reload(db, Employee, emp_b.id)).status == EmploymentStatus.SUSPENDED
        assert await count(db, Notification, Notification.user_id == person.id,
                           Notification.title.like("%is restored")) == 0
        # the requirement itself: a disabled account never makes a job ACTIVE (activation check, re-check, approval)
        fresh = await reload(db, Employee, emp_b.id)
        fresh.status = EmploymentStatus.PENDING_VERIFICATION  # e.g. a record the ban's suspension did not reach
        await db.commit()
        from app.db import get_sessionmaker

        async with get_sessionmaker()() as s:  # as the activation job runs it: its own session
            result = await S.run_activation_check(s, emp_b.id)
            await s.commit()
        assert result is not None and result.new_status == EmploymentStatus.PENDING_VERIFICATION
        assert "account" in {m.key for m in result.missing}
        assert "disabled" in page(await cb.get(f"/owner/staff/{emp_b.id}"))
    finally:
        await ca.aclose()
        await cb.aclose()
    # lifted by the platform: the owner can reactivate again
    await reload(db, User, person.id)  # the ban disabled the account in another session
    admin = await make_user(db, role=Role.SUPERADMIN)
    ban = await db.scalar(select(AccountBan))
    await TS.lift_account_ban(db, ban, actor=admin, note="Reviewed: the report was withdrawn.")
    emp = await reload(db, Employee, emp_b.id)
    emp.status = EmploymentStatus.SUSPENDED
    await db.commit()
    async with make_client() as cb2:
        await login_as(cb2, owner_b)
        r = await post_form(cb2, f"/owner/staff/{emp_b.id}/reactivate")
        assert r.status_code == 303 and "active again" in flash_text(r)
    assert (await reload(db, Employee, emp_b.id)).status == EmploymentStatus.ACTIVE


async def test_a_disabled_trainer_account_is_never_given_clients(db):
    """F46: an ACTIVE trainer record whose account is disabled (reactivated before the fix, or not reached by the ban)
    can't be assigned members."""
    owner, gym = await owner_with_gym(db)
    branch = await make_branch(db, gym)
    trainer = await staff_user(db, email="disabled.coach@example.com")
    emp = await make_employee(db, gym, user=trainer)
    plans = {p.code: p for p in await make_plans(db, gym)}
    member = await make_customer(db, full_name="Khalid Member")
    sub = await make_subscription(db, member, plans["offline_trainer"], branch=branch)
    trainer.is_active = False
    await db.commit()
    async with make_client() as c:
        await login_as(c, owner)
        r = await post_form(c, f"/owner/staff/{emp.id}/clients", {"subscription_id": str(sub.id)})
        assert r.status_code == 400 and "account is disabled" in page(r)
    assert await count(db, TrainerClient) == 0
    assert await count(db, Notification, Notification.user_id == trainer.id) == 0


async def test_change_position_rechecks_rehire_after_taking_the_person_lock(client, db, monkeypatch):
    """F47: 'Change position' checked re-hire eligibility only BEFORE the person lock. The same owner ending another
    of the person's jobs for a VIOLATION at that moment committed first, and the change still ended the old record and
    invited a successor the owner may never add again. The check now runs again under the lock."""
    from app.db import get_sessionmaker
    from app.routers import owner_staff as OS

    owner, gym = await owner_with_gym(db)
    await make_branch(db, gym)  # a verified branch: the form's work mode stays valid
    person = await staff_user(db, email="two.jobs.here@example.com")
    emp_x = await make_employee(db, gym, user=person, role=EmployeeRole.RECEPTIONIST)
    emp_y = await make_employee(db, gym, user=person, role=EmployeeRole.CLEANER)
    photo = await storage.save_public_image(png_bytes((80, 80), "red"), kind="staff")
    emp_y.photo_key = photo.key
    await db.commit()
    original, copies = OS._copy_public_image, []

    async def violation_meanwhile(key: str):
        # after the change's first check, before its lock: the violation removal commits
        async with get_sessionmaker()() as s:
            x = await s.get(Employee, emp_x.id)
            actor = await s.get(User, owner.id)
            who = await TS.person_of_employee(s, x)
            await TS.lock_person(s, who)
            await TS.lock_employee(s, x.id)
            await TS.end_employment(s, x, reason="repeated_no_shows", note="Missed seven shifts this month.",
                                    actor=actor, person=who)
            await s.commit()
        stored = await original(key)
        copies.append(stored.key)
        return stored

    monkeypatch.setattr(OS, "_copy_public_image", violation_meanwhile)
    await login_as(client, owner)
    values = S.staff_values_from_employee(emp_y)
    values.update(role="manager", role_title="Shift Manager", is_public="1", hire_date=date.today().isoformat())
    values.pop("invite_email")
    r = await post_form(client, f"/owner/staff/{emp_y.id}/change-position", values)
    assert r.status_code == 400 and TS.CANT_ADD in page(r)
    assert await count(db, StaffViolation) == 1
    assert await count(db, Employee, Employee.previous_employment_id == emp_y.id) == 0
    assert (await reload(db, Employee, emp_y.id)).status == EmploymentStatus.ACTIVE
    assert copies and not storage.path_for(copies[0]).exists()  # the photo copied for the new record is removed
