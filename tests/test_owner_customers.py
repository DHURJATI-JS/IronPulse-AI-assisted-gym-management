"""WP-F owner member management: scoping, filters, N+1 guard, manual registration, health edits, reactivation,
invites and the audit log."""

from __future__ import annotations

import asyncio
import re
import sys
import types
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select, update

from app.core.utils import utcnow
from app.db import QueryCounter
from app.models import (
    AuditLog,
    AuthToken,
    CustomerProfile,
    EmailOutbox,
    HealthCondition,
    IdentityDocument,
    Job,
    MetricEntry,
    Notification,
    SuspensionEvent,
    User,
)
from app.models.enums import ConditionKind, IdStatus, Role, SubscriptionStatus, TokenPurpose
from app.security.crypto import token_hash
from app.services.audit import audit
from app.services.identity_lookup import member_id
from tests.conftest import login_as, make_client, post_form
from tests.factories import make_customer, make_gym, make_owner, make_plans, make_subscription, png_bytes
from tests.test_superadmin import flash_text


# ====================================================================== helpers
def years_ago(n: int, days: int = 0) -> date:
    today = date.today()
    try:
        d = today.replace(year=today.year - n)
    except ValueError:
        d = today.replace(year=today.year - n, day=28)
    return d - timedelta(days=days)


async def owner_with_gym(db, **kw: Any):
    owner = await make_owner(db, **kw)
    gym = await make_gym(db, owner)
    plans = await make_plans(db, gym)
    return owner, gym, plans


def reg_form(**over: Any) -> dict[str, Any]:
    data = {
        "full_name": "Riya Kapoor", "email": "riya@example.com", "phone": "98765 43210", "country_code": "IN",
        "date_of_birth": years_ago(25).isoformat(), "gender": "female", "city": "Bengaluru", "goal": "weight_loss",
        "conditions": "asthama, high BP", "allergies": "peanuts", "medications": "inhaler", "injuries": "left knee injury",
        "health_notes": "Prefers morning sessions", "guardian_name": "", "guardian_relation": "", "guardian_phone": "",
        "guardian_email": "", "emergency_contact_name": "Priya Kapoor", "emergency_contact_phone": "98111 22233",
        "intake_source": "owner_manual", "id_doc_type": "aadhaar", "id_country": "IN", "consent": "1",
    }
    data.update(over)
    return data


def id_files(front: bytes | None = None) -> dict[str, Any]:
    return {"id_front": ("front.png", front if front is not None else png_bytes(text="ID"), "image/png")}


def no_csp_violations(html: str) -> None:
    assert "<script>" not in html and " onclick=" not in html and ' style="' not in html


async def users_named(db, name: str) -> int:
    return int(await db.scalar(select(func.count(User.id)).where(User.full_name == name)) or 0)


# ====================================================================== list scoping + filters
async def test_members_list_scoping_and_filters(client, db):
    owner, gym, plans = await owner_with_gym(db)
    other_owner, other_gym, other_plans = await owner_with_gym(db)
    active_minor = await make_customer(db, full_name="Aarav Active", conditions=["Asthma"], date_of_birth=years_ago(15),
                                       phone="+91 90000 11111")
    await make_subscription(db, active_minor, plans[0])
    expired = await make_customer(db, full_name="Esha Expired", email="esha@example.com")
    await make_subscription(db, expired, plans[1], status=SubscriptionStatus.EXPIRED)
    registered = await make_customer(db, full_name="Rohit Registered")
    prof = await db.get(CustomerProfile, registered.id)
    prof.registered_by_id = owner.id
    paused = await db.get(CustomerProfile, expired.id)
    paused.activity_suspended = True
    await db.commit()
    foreign = await make_customer(db, full_name="Farah Foreign")
    await make_subscription(db, foreign, other_plans[0])
    gone = await make_customer(db, full_name="Gita Gone")
    await make_subscription(db, gone, plans[0])
    gone.deleted_at = utcnow()
    gone.purge_after = utcnow() + timedelta(days=30)
    await db.commit()

    await login_as(client, owner)
    r = await client.get("/owner/customers")
    assert r.status_code == 200
    no_csp_violations(r.text)
    for name in ("Aarav Active", "Esha Expired", "Rohit Registered"):
        assert name in r.text
    assert "Farah Foreign" not in r.text and "Gita Gone" not in r.text
    assert member_id(active_minor) in r.text

    async def names(query: str) -> set[str]:
        resp = await client.get("/owner/customers" + query)
        assert resp.status_code == 200, query
        return {n for n in ("Aarav Active", "Esha Expired", "Rohit Registered", "Farah Foreign") if n in resp.text}

    assert await names("?status=active") == {"Aarav Active"}
    assert await names("?status=expired") == {"Esha Expired"}
    assert await names("?status=none") == {"Rohit Registered"}
    assert await names("?minors=1") == {"Aarav Active"}
    assert await names("?paused=1") == {"Esha Expired"}
    assert await names("?tag=asthma") == {"Aarav Active"}
    assert await names("?q=esha%40example") == {"Esha Expired"}
    assert await names(f"?q={member_id(registered)}") == {"Rohit Registered"}
    assert await names("?q=90000") == {"Aarav Active"}
    assert await names(f"?gym={gym.id}") == {"Aarav Active", "Esha Expired"}
    assert await names(f"?gym={other_gym.id}") == {"Aarav Active", "Esha Expired", "Rohit Registered"}  # foreign id ignored
    assert await names("?sort=name&page=abc&status=bogus") == {"Aarav Active", "Esha Expired", "Rohit Registered"}
    r = await client.get("/owner/customers?q=nobody-matches")
    assert "No members match" in r.text


async def test_members_list_requires_owner(client, db):
    customer = await make_customer(db)
    await login_as(client, customer)
    assert (await client.get("/owner/customers")).status_code == 403
    assert (await client.get("/owner/audit")).status_code == 403
    async with make_client() as anon:
        r = await anon.get("/owner/customers")
        assert r.status_code == 303 and "/auth/login" in r.headers["location"]


async def test_members_list_constant_queries(db):
    owner, gym, plans = await owner_with_gym(db)

    async def add(n: int) -> None:
        for i in range(n):
            c = await make_customer(db, conditions=["Asthma", "Knee pain"] if i % 2 else ["Diabetes"])
            await make_subscription(db, c, plans[i % len(plans)])
            await make_subscription(db, c, plans[0], status=SubscriptionStatus.EXPIRED)

    async def count() -> int:
        async with make_client() as c:
            await login_as(c, owner)
            with QueryCounter() as qc:
                r = await c.get("/owner/customers")
            assert r.status_code == 200
            return qc.count

    await add(3)
    small = await count()
    await add(15)
    assert await count() == small


# ====================================================================== member detail
async def test_member_detail_scoping_and_content(client, db):
    owner, gym, plans = await owner_with_gym(db)
    other_owner, _, other_plans = await owner_with_gym(db)
    member = await make_customer(db, full_name="Maya Member", conditions=["Asthma"], date_of_birth=years_ago(16))
    await make_subscription(db, member, plans[0])
    prof = await db.get(CustomerProfile, member.id)
    prof.guardian_name, prof.guardian_relation, prof.guardian_email = "Priya Member", "Mother", "priya@example.com"
    prof.activity_suspended, prof.suspension_reason = True, "BMI dropped sharply in a week"
    db.add(SuspensionEvent(customer_id=member.id, action="suspend", reason="BMI dropped sharply", actor="rule"))
    for i, w in enumerate((61.0, 60.2, 59.1)):
        db.add(MetricEntry(customer_id=member.id, recorded_on=date.today() - timedelta(days=3 - i), height_cm=165,
                           weight_kg=w, bmi=round(w / 1.65 ** 2, 1)))
    doc = IdentityDocument(user_id=member.id, doc_type="aadhaar", country_code="IN",
                           front_key="private/identity/" + "a" * 32 + ".bin", status=IdStatus.PENDING,
                           id_number_masked="•••• 1234")
    db.add(doc)
    await db.commit()
    foreign = await make_customer(db, full_name="Farah Foreign")
    await make_subscription(db, foreign, other_plans[0])

    await login_as(client, owner)
    r = await client.get(f"/owner/customers/{member.id}")
    assert r.status_code == 200
    no_csp_violations(r.text)
    for text in ("Maya Member", member_id(member), "Under 18", "Paused for safety", "BMI dropped sharply in a week",
                 "Priya Member", "Asthma", "Last 30 days", f"/identity/{doc.id}/front", "polyline", "Reactivate activities"):
        assert text in r.text, text
    assert (await client.get(f"/owner/customers/{foreign.id}")).status_code == 404
    assert (await client.get("/owner/customers/999999")).status_code == 404
    async with make_client() as c:
        await login_as(c, other_owner)
        assert (await c.get(f"/owner/customers/{member.id}")).status_code == 404


# ====================================================================== manual registration
async def test_register_member_creates_everything(client, db):
    owner, gym, _ = await owner_with_gym(db, full_name="Olivia Owner")
    await login_as(client, owner)
    r = await client.get("/owner/customers/new")
    assert r.status_code == 200 and 'data-component="intake-ai"' in r.text and 'enctype="multipart/form-data"' in r.text
    no_csp_violations(r.text)

    r = await post_form(client, "/owner/customers/new",
                        reg_form(guardian_email="dad@example.com", guardian_name="Raj Kapoor", guardian_relation="dad"),
                        files=id_files())
    assert r.status_code == 303, r.text[-3000:]
    user = (await db.execute(select(User).where(User.email == "riya@example.com"))).scalar_one()
    assert r.headers["location"] == f"/owner/customers/{user.id}"
    assert user.role == Role.CUSTOMER and user.password_hash is None and user.phone_normalized == "+919876543210"
    assert user.username and user.username.startswith("riya") and user.id_status == IdStatus.PENDING
    prof = await db.get(CustomerProfile, user.id)
    assert prof.registered_by_id == owner.id and prof.intake_source == "owner_manual" and prof.goal.value == "weight_loss"
    assert prof.guardian_relation == "Father" and prof.emergency_contact_name == "Priya Kapoor"

    rows = (await db.execute(select(HealthCondition).where(HealthCondition.customer_id == user.id))).scalars().all()
    by_kind = {(c.kind, c.name): c.tag for c in rows}
    assert by_kind[(ConditionKind.CONDITION, "Asthma")] == "asthma"  # misspelling fixed
    assert by_kind[(ConditionKind.CONDITION, "High BP")] == "hypertension"
    assert by_kind[(ConditionKind.ALLERGY, "Peanuts")] == "food_allergy"
    assert (ConditionKind.MEDICATION, "Inhaler") in by_kind
    assert by_kind[(ConditionKind.INJURY, "Left knee injury")] == "knee_injury"
    assert all(c.source == "owner" for c in rows)

    doc = (await db.execute(select(IdentityDocument).where(IdentityDocument.user_id == user.id))).scalar_one()
    assert doc.uploaded_by_id == owner.id and doc.status == IdStatus.PENDING
    assert await db.scalar(select(func.count(Job.id)).where(Job.kind == "verify_identity")) == 1

    token = (await db.execute(select(AuthToken).where(AuthToken.user_id == user.id))).scalar_one()
    assert token.purpose == TokenPurpose.INVITE and token.email == "riya@example.com" and token.used_at is None
    assert timedelta(days=6, hours=23) < token.expires_at - utcnow() <= timedelta(days=7)
    invite = (await db.execute(select(EmailOutbox).where(EmailOutbox.to_email == "riya@example.com"))).scalar_one()
    m = re.search(r"/auth/invite\?token=([A-Za-z0-9_-]+)", invite.text_body)
    assert m and token_hash(m.group(1)) == token.token_hash and "http://testserver/auth/invite" in invite.text_body
    guardian = (await db.execute(select(EmailOutbox).where(EmailOutbox.to_email == "dad@example.com"))).scalar_one()
    assert "Riya Kapoor" in guardian.text_body and member_id(user) in guardian.text_body
    welcome = (await db.execute(select(Notification).where(Notification.user_id == user.id))).scalar_one()
    assert welcome.emailed is False and member_id(user) in welcome.body
    log = (await db.execute(select(AuditLog).where(AuditLog.action == "customer.register"))).scalar_one()
    assert log.actor_id == owner.id and log.gym_id == gym.id and log.entity_id == user.id

    r = await client.get(f"/owner/customers/{user.id}")
    assert r.status_code == 200 and "Invite pending" in r.text and "Riya Kapoor" in r.text


async def test_register_requires_id_card(client, db):
    owner, _, _ = await owner_with_gym(db)
    await login_as(client, owner)
    r = await post_form(client, "/owner/customers/new", reg_form())
    assert r.status_code == 400 and "ID card" in r.text and "mandatory" in r.text
    r = await post_form(client, "/owner/customers/new", reg_form(), files=id_files(b"definitely not an image"))
    assert r.status_code == 400 and "not a valid image" in r.text
    r = await post_form(client, "/owner/customers/new", reg_form(), files=id_files(b"%PDF-1.4 fake"))
    assert r.status_code == 400 and "not a PDF" in r.text
    r = await post_form(client, "/owner/customers/new", reg_form(id_doc_type="library_card", id_country="India"),
                        files=id_files())
    assert r.status_code == 400 and "type of ID card" in r.text
    assert await users_named(db, "Riya Kapoor") == 0


async def test_register_minor_requires_guardian(client, db):
    owner, _, _ = await owner_with_gym(db)
    await login_as(client, owner)
    minor = reg_form(date_of_birth=years_ago(15).isoformat(), email="teen@example.com", phone="")
    r = await post_form(client, "/owner/customers/new", minor, files=id_files())
    assert r.status_code == 400 and "mandatory for members under 18" in r.text
    r = await post_form(client, "/owner/customers/new", dict(minor, guardian_name="Priya Kapoor", guardian_relation=""),
                        files=id_files())
    assert r.status_code == 400 and "how the guardian is related" in r.text
    r = await post_form(client, "/owner/customers/new", dict(minor, guardian_name="Priya Kapoor", guardian_relation="Mother"),
                        files=id_files())
    assert r.status_code == 400 and "phone number or e-mail" in r.text
    assert await users_named(db, "Riya Kapoor") == 0
    r = await post_form(client, "/owner/customers/new",
                        dict(minor, guardian_name="Priya Kapoor", guardian_relation="Mother", guardian_email="mum@example.com"),
                        files=id_files())
    assert r.status_code == 303
    user = (await db.execute(select(User).where(User.email == "teen@example.com"))).scalar_one()
    assert user.is_minor
    # the guardian is e-mailed about the registration (and CC'd on the member's notifications from now on)
    assert await db.scalar(select(func.count(EmailOutbox.id)).where(EmailOutbox.to_email == "mum@example.com")) == 1


async def test_register_identifier_conflicts(client, db):
    from app.services.accounts import soft_delete_user

    owner, _, _ = await owner_with_gym(db)
    await make_customer(db, email="taken@example.com", phone="+91 91234 56789")
    gone = await make_customer(db, email="gone@example.com", phone="+91 99999 88888")
    await soft_delete_user(db, gone, reason="user request")
    await db.commit()
    await login_as(client, owner)
    r = await post_form(client, "/owner/customers/new", reg_form(email="TAKEN@example.com", phone=""), files=id_files())
    assert r.status_code == 400 and "already exists" in r.text
    r = await post_form(client, "/owner/customers/new", reg_form(email="", phone="9123456789"), files=id_files())
    assert r.status_code == 400 and "already exists" in r.text
    r = await post_form(client, "/owner/customers/new", reg_form(email="gone@example.com", phone=""), files=id_files())
    assert r.status_code == 400 and "deleted recently" in r.text
    r = await post_form(client, "/owner/customers/new", reg_form(email="new@example.com", phone="+91 99999 88888"),
                        files=id_files())
    assert r.status_code == 400 and "deleted recently" in r.text
    assert await users_named(db, "Riya Kapoor") == 0
    assert await db.scalar(select(func.count(IdentityDocument.id))) == 0


async def test_register_validation_errors(client, db):
    owner, gym, _ = await owner_with_gym(db)
    other_owner, other_gym, _ = await owner_with_gym(db)
    await login_as(client, owner)
    cases = {
        "full_name": reg_form(full_name=""),
        "contact": reg_form(email="", phone=""),
        "email": reg_form(email="not-an-email"),
        "phone": reg_form(phone="00000000"),
        "dob_future": reg_form(date_of_birth=(date.today() + timedelta(days=3)).isoformat()),
        "dob_missing": reg_form(date_of_birth=""),
        "goal": reg_form(goal="become-famous"),
        "gender": reg_form(gender="dragon"),
        "consent": reg_form(consent=""),
        "foreign_gym": reg_form(gym_id=str(other_gym.id)),
        "long_item": reg_form(conditions="x" * 130),
        "emergency": reg_form(emergency_contact_name="", emergency_contact_phone="98111 22233"),
    }
    for label, data in cases.items():
        r = await post_form(client, "/owner/customers/new", data, files=id_files())
        assert r.status_code == 400, label
        assert "Please fix the following" in r.text, label
        no_csp_violations(r.text)
    assert await users_named(db, "Riya Kapoor") == 0
    # values typed by the owner are kept after an error
    r = await post_form(client, "/owner/customers/new", reg_form(email="bad"), files=id_files())
    assert 'value="Riya Kapoor"' in r.text and "choose the ID photo again" in r.text


async def test_register_phone_only_and_ai_source(client, db):
    owner, gym, _ = await owner_with_gym(db)
    await login_as(client, owner)
    r = await post_form(client, "/owner/customers/new", reg_form(email="", intake_source="owner_ai"), files=id_files())
    assert r.status_code == 303
    user = (await db.execute(select(User).where(User.phone_normalized == "+919876543210"))).scalar_one()
    assert user.email is None
    assert await db.scalar(select(func.count(AuthToken.id))) == 0  # no e-mail -> no invite
    prof = await db.get(CustomerProfile, user.id)
    assert prof.intake_source == "owner_ai"
    sources = set((await db.execute(select(HealthCondition.source).where(HealthCondition.customer_id == user.id))).scalars())
    assert sources == {"ai_intake"}
    r = await client.get(r.headers["location"])
    assert "No e-mail address was given" in r.text


async def test_register_without_gym_redirects(client, db):
    owner = await make_owner(db)
    await login_as(client, owner)
    r = await client.get("/owner/customers/new")
    assert r.status_code == 200 and "Create your gym first" in r.text
    r = await post_form(client, "/owner/customers/new", reg_form(), files=id_files())
    assert r.status_code == 303 and r.headers["location"] == "/owner/gyms/new"


# ====================================================================== health edits
async def test_owner_edits_health_and_member_is_notified(client, db):
    owner, gym, plans = await owner_with_gym(db)
    member = await make_customer(db, full_name="Teen Member", date_of_birth=years_ago(15), conditions=["Asthma", "Migraine"])
    await make_subscription(db, member, plans[0])
    prof = await db.get(CustomerProfile, member.id)
    prof.guardian_name, prof.guardian_relation, prof.guardian_email = "Old Guardian", "Mother", "old@example.com"
    await db.commit()
    await login_as(client, owner)

    from app.services.intake import health_snapshot

    shown = (await db.execute(select(HealthCondition).where(HealthCondition.customer_id == member.id))).scalars().all()
    form = {"conditions": "Asthma, diabeties", "allergies": "dust", "medications": "", "injuries": "",
            "guardian_name": "", "guardian_relation": "", "guardian_phone": "", "guardian_email": "",
            "health_seen": health_snapshot(member.id, shown)}  # the form's snapshot of the entries it showed
    r = await post_form(client, f"/owner/customers/{member.id}/health", form)
    assert r.status_code == 400 and "mandatory for members under 18" in r.text  # minor: guardian required

    form.update(guardian_name="New Guardian", guardian_relation="Father", guardian_email="new@example.com")
    r = await post_form(client, f"/owner/customers/{member.id}/health", form)
    assert r.status_code == 303 and r.headers["location"].endswith("#health")
    rows = (await db.execute(select(HealthCondition).where(HealthCondition.customer_id == member.id))).scalars().all()
    assert {(c.kind, c.name) for c in rows} == {(ConditionKind.CONDITION, "Asthma"), (ConditionKind.CONDITION, "Diabetes"),
                                               (ConditionKind.ALLERGY, "Dust")}
    await db.refresh(prof)
    assert prof.guardian_email == "new@example.com" and prof.guardian_name == "New Guardian"
    note = (await db.execute(select(Notification).where(Notification.user_id == member.id))).scalar_one()
    assert note.title == "Your health profile was updated" and note.emailed
    mail = (await db.execute(select(EmailOutbox).where(EmailOutbox.notification_id == note.id))).scalar_one()
    assert mail.cc == ["new@example.com"]  # guardian CC for minors
    assert await db.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == "customer.health_update")) == 1

    # saving the same values again changes nothing
    r = await post_form(client, f"/owner/customers/{member.id}/health", form)
    assert r.status_code == 303
    assert await db.scalar(select(func.count(Notification.id)).where(Notification.user_id == member.id)) == 1

    other_owner, _, _ = await owner_with_gym(db)
    async with make_client() as c:
        await login_as(c, other_owner)
        assert (await post_form(c, f"/owner/customers/{member.id}/health", form)).status_code == 404


# ====================================================================== reactivation (WP-E1 service)
async def test_reactivate_requires_suspension_and_note(client, db):
    owner, gym, plans = await owner_with_gym(db)
    member = await make_customer(db, full_name="Paused Member")
    await make_subscription(db, member, plans[0])
    await login_as(client, owner)
    r = await post_form(client, f"/owner/customers/{member.id}/reactivate", {"note": "Doctor cleared her today"})
    assert r.status_code == 303  # not paused -> nothing to do
    prof = await db.get(CustomerProfile, member.id)
    prof.activity_suspended, prof.suspension_reason, prof.suspended_by = True, "Weight dropped 6 kg in 3 days", "rule"
    await db.commit()
    r = await post_form(client, f"/owner/customers/{member.id}/reactivate", {"note": "ok"})
    assert r.status_code == 400 and "at least 10 characters" in r.text
    r = await post_form(client, f"/owner/customers/{member.id}/reactivate", {"note": "Doctor's clearance received today."})
    assert r.status_code == 303
    await db.refresh(prof)
    assert prof.activity_suspended is False
    ev = (await db.execute(select(SuspensionEvent).where(SuspensionEvent.customer_id == member.id))).scalar_one()
    assert ev.action == "reactivate" and ev.actor_user_id == owner.id
    assert await db.scalar(select(func.count(Notification.id)).where(Notification.user_id == member.id)) == 1


async def test_reactivate_calls_health_service_with_contract(client, db, monkeypatch):
    calls: list[tuple[Any, ...]] = []

    async def fake_reactivate(db_, customer, actor_user, note):  # noqa: ANN001
        calls.append((customer.id, actor_user.id, note))
        prof_ = await db_.get(CustomerProfile, customer.id)
        prof_.activity_suspended = False

    fake = types.ModuleType("app.services.health")
    fake.reactivate_customer = fake_reactivate
    monkeypatch.setitem(sys.modules, "app.services.health", fake)
    owner, gym, plans = await owner_with_gym(db)
    member = await make_customer(db)
    prof = await db.get(CustomerProfile, member.id)
    prof.activity_suspended = True
    prof.registered_by_id = owner.id
    await db.commit()
    await login_as(client, owner)
    r = await post_form(client, f"/owner/customers/{member.id}/reactivate", {"note": "  Cleared by   physio  "})
    assert r.status_code == 303 and calls == [(member.id, owner.id, "Cleared by physio")]

    async def deny(db_, customer, actor_user, note):  # noqa: ANN001
        raise PermissionError("Only an admin of this member's gym can reactivate them.")

    fake.reactivate_customer = deny
    await db.refresh(prof)
    assert prof.activity_suspended is False
    prof.activity_suspended = True
    await db.commit()
    r = await post_form(client, f"/owner/customers/{member.id}/reactivate", {"note": "Cleared by the doctor"})
    assert r.status_code == 303
    r = await client.get(r.headers["location"])
    assert "Only an admin of this member" in r.text


# ====================================================================== invites
async def test_resend_invite(client, db):
    owner, gym, _ = await owner_with_gym(db)
    await login_as(client, owner)
    await post_form(client, "/owner/customers/new", reg_form(), files=id_files())
    user = (await db.execute(select(User).where(User.email == "riya@example.com"))).scalar_one()
    r = await post_form(client, f"/owner/customers/{user.id}/invite")
    assert r.status_code == 303
    assert await db.scalar(select(func.count(AuthToken.id))) == 1  # throttled: one was sent a moment ago
    first = (await db.execute(select(AuthToken))).scalar_one()
    first.created_at = utcnow() - timedelta(minutes=5)
    await db.commit()
    r = await post_form(client, f"/owner/customers/{user.id}/invite")
    assert r.status_code == 303
    db.expire_all()
    tokens = (await db.execute(select(AuthToken).order_by(AuthToken.id))).scalars().all()
    assert len(tokens) == 2 and tokens[0].used_at is not None and tokens[1].used_at is None  # old link superseded
    assert await db.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == "customer.invite")) == 1
    user.password_hash = "x"
    await db.commit()
    await post_form(client, f"/owner/customers/{user.id}/invite")
    assert await db.scalar(select(func.count(AuthToken.id))) == 2  # already activated -> nothing sent


async def _waiting_member(db, plans, email: str = "waiting@example.com") -> User:
    """A member of the owner's gym who never set a password (an invite can be re-sent)."""
    member = await make_customer(db, email=email, password=None, full_name="Waiting Member")
    await make_subscription(db, member, plans[0])
    return member


async def _invites(db, member_id: int) -> list[AuthToken]:
    return list((await db.execute(select(AuthToken).where(
        AuthToken.user_id == member_id, AuthToken.purpose == TokenPurpose.INVITE).order_by(AuthToken.id)
        .execution_options(populate_existing=True))).scalars())


async def _age_invites(db, member_id: int, minutes: int) -> None:
    await db.execute(update(AuthToken).where(AuthToken.user_id == member_id)
                     .values(created_at=utcnow() - timedelta(minutes=minutes)))
    await db.commit()


async def test_invite_throttle_holds_under_parallel_resends(client, db):
    """F13: twelve 'Re-send invite' posts at the same moment (several tabs, a script) all passed the 2-minute throttle
    - a plain SELECT of the last invite - and queued ten invite e-mails to the member's address in a second, leaving
    two working invite links. The check now runs under a per-member lock: one invite, one e-mail, one live link."""
    owner, _gym, plans = await owner_with_gym(db)
    member = await _waiting_member(db, plans)
    await login_as(client, owner)
    rs = await asyncio.gather(*[post_form(client, f"/owner/customers/{member.id}/invite") for _ in range(12)])
    assert {r.status_code for r in rs} == {303}
    flashes = [flash_text(r) for r in rs]
    assert sum("Invite e-mailed" in f for f in flashes) == 1
    assert sum("a moment ago" in f for f in flashes) == 11
    tokens = await _invites(db, member.id)
    assert len(tokens) == 1 and tokens[0].used_at is None
    assert await db.scalar(select(func.count(EmailOutbox.id)).where(EmailOutbox.to_email == "waiting@example.com")) == 1
    assert await db.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == "customer.invite")) == 1


async def test_invites_per_member_are_capped_per_day(client, db):
    """F13: even one at a time, the 2-minute throttle allowed 720 invite e-mails a day to one address. At most
    INVITES_PER_MEMBER_DAY invite e-mails reach a member in any 24 hours (the registration's own invite included)."""
    from app.routers import owner_customers as OC

    owner, _gym, plans = await owner_with_gym(db)
    member_id = (await _waiting_member(db, plans)).id
    await login_as(client, owner)
    for _ in range(OC.INVITES_PER_MEMBER_DAY):
        await _age_invites(db, member_id, minutes=3)  # past the 2-minute throttle
        r = await post_form(client, f"/owner/customers/{member_id}/invite")
        assert r.status_code == 303 and "Invite e-mailed" in flash_text(r)
    await _age_invites(db, member_id, minutes=3)
    r = await post_form(client, f"/owner/customers/{member_id}/invite")
    assert r.status_code == 303 and f"{OC.INVITES_PER_MEMBER_DAY} invites in the last 24 hours" in flash_text(r)
    tokens = await _invites(db, member_id)
    assert len(tokens) == OC.INVITES_PER_MEMBER_DAY and sum(t.used_at is None for t in tokens) == 1
    assert await db.scalar(select(func.count(EmailOutbox.id)).where(
        EmailOutbox.to_email == "waiting@example.com")) == OC.INVITES_PER_MEMBER_DAY
    # the window is rolling: a day later the owner can send one again
    await _age_invites(db, member_id, minutes=25 * 60)
    r = await post_form(client, f"/owner/customers/{member_id}/invite")
    assert r.status_code == 303 and "Invite e-mailed" in flash_text(r)


# ====================================================================== audit log
async def test_audit_log_scoping_and_filters(client, db):
    owner, gym, _ = await owner_with_gym(db)
    other_owner, other_gym, _ = await owner_with_gym(db)
    member = await make_customer(db, full_name="Audit Member")
    await audit(db, owner, "customer.register", "user", member.id, gym_id=gym.id, detail={"source": "owner_manual"})
    await audit(db, member, "report.create", "misconduct_report", 7, gym_id=gym.id, detail={"category": "rude"})
    await audit(db, owner, "account.password_change", "user", owner.id)  # own action without a gym
    await audit(db, other_owner, "customer.register", "user", 999, gym_id=other_gym.id, detail={"secret": "theirs"})
    await db.commit()
    await login_as(client, owner)
    r = await client.get("/owner/audit")
    assert r.status_code == 200
    no_csp_violations(r.text)
    assert "Registered a member" in r.text and "Misconduct report filed" in r.text and "Account · password change" in r.text
    assert "theirs" not in r.text and "/owner/reports/7" in r.text
    r = await client.get("/owner/audit?who=me")
    assert "Misconduct report filed" not in r.text and "Registered a member" in r.text
    r = await client.get("/owner/audit?area=report")
    assert "Misconduct report filed" in r.text and "Registered a member" not in r.text
    r = await client.get("/owner/audit?q=Audit%20Member")
    assert "Misconduct report filed" in r.text and "Registered a member" not in r.text
    r = await client.get("/owner/audit?gym=" + str(other_gym.id))  # a foreign gym id is ignored
    assert "theirs" not in r.text


async def test_audit_log_constant_queries(db):
    owner, gym, _ = await owner_with_gym(db)

    async def add(n: int) -> None:
        for i in range(n):
            m = await make_customer(db)
            await audit(db, m if i % 2 else owner, "customer.register", "user", m.id, gym_id=gym.id, detail={"i": i})
        await db.commit()

    async def count() -> int:
        async with make_client() as c:
            await login_as(c, owner)
            with QueryCounter() as qc:
                r = await c.get("/owner/audit")
            assert r.status_code == 200
            return qc.count

    await add(3)
    small = await count()
    await add(20)
    assert await count() == small


# ====================================================================== health entries: bounded, safety first
def _padded_profile(member_id: int) -> list[dict[str, Any]]:
    """181 entries from before the per-member cap: 60 allergies, 60 medications and 60 harmless injuries named so they
    sort before the one that matters - a tagged knee injury, recorded last."""
    return ([{"customer_id": member_id, "kind": ConditionKind.ALLERGY, "name": f"Allergen {j:03d}"} for j in range(60)]
            + [{"customer_id": member_id, "kind": ConditionKind.MEDICATION, "name": f"Medicine {j:03d}"} for j in range(60)]
            + [{"customer_id": member_id, "kind": ConditionKind.INJURY, "name": f"Old sprain {j:03d}"} for j in range(60)]
            + [{"customer_id": member_id, "kind": ConditionKind.INJURY, "name": "Zz knee injury", "tag": "knee_injury"}])


async def test_member_profile_reads_at_most_100_health_entries_safety_first(client, db):
    """Before: up to CONDITIONS_PER_KIND (50) of every kind by name - 150 rows here, and the tagged knee injury (named
    last) cut off behind 50 harmless sprains."""
    from sqlalchemy import insert

    from app.routers import owner_customers
    from tests.test_finance_scale import Sent

    owner, gym, plans = await owner_with_gym(db)
    member = await make_customer(db, full_name="Padded Profile")
    await make_subscription(db, member, plans[0])
    await db.execute(insert(HealthCondition), _padded_profile(member.id))
    await db.commit()
    await login_as(client, owner)
    with Sent() as sent:
        r = await client.get(f"/owner/customers/{member.id}")
    assert r.status_code == 200
    health = r.text.split('id="health"', 1)[1].split("<details", 1)[0]
    chips = health.count('<li class="inline-flex items-center gap-1 rounded-full bg-white')
    assert chips == owner_customers.CONDITIONS_SHOWN == 100
    assert "Zz knee injury" in health and "Knee injury" in health  # the safety tag is never the one cut
    assert "181 items" in r.text  # every kind still says how many it holds ...
    assert "+11 more" in health and "+59 more" in health  # ... injuries 50/61, allergies 49/60, medications 1/60
    reads = [(s, p) for s, p in sent.items if "FROM health_conditions" in s and "row_number()" in s]
    assert len(reads) == 1 and "LIMIT" in reads[0][0] and owner_customers.CONDITIONS_SHOWN in list(reads[0][1])


async def test_member_list_reads_each_members_health_entries_through_a_bounded_lateral(client, db):
    from sqlalchemy import insert

    from app.routers import owner_customers
    from tests.test_finance_scale import Sent

    owner, gym, plans = await owner_with_gym(db)
    member = await make_customer(db, full_name="Padded Listing")
    await make_subscription(db, member, plans[0])
    await db.execute(insert(HealthCondition), _padded_profile(member.id))
    await db.commit()
    await login_as(client, owner)
    with Sent() as sent:
        r = await client.get("/owner/customers")
    assert r.status_code == 200 and "Padded Listing" in r.text
    (sql, params), = [(s, p) for s, p in sent.items if "health_conditions" in s and "row_number()" in s]
    assert "LATERAL" in sql and "LIMIT" in sql and owner_customers.CONDITIONS_SHOWN in list(params)


def _one_tag_over_and_over(member_id: int, n: int) -> list[dict[str, Any]]:
    """``n`` sprains that all carry the same safety tag (from before the per-member cap), then one injury with another
    tag - recorded last."""
    return ([{"customer_id": member_id, "kind": ConditionKind.INJURY, "name": f"Ankle sprain {j:03d}",
              "tag": "ankle_injury"} for j in range(n)]
            + [{"customer_id": member_id, "kind": ConditionKind.INJURY, "name": "Back pain", "tag": "lower_back"}])


async def test_one_tag_over_and_over_never_hides_another_safety_tag(client, db):
    """Every distinct safety tag comes before the repeats of another (the order of app/services/health.safety_first):
    a profile padded with one tag can't push a different tag off the member's profile or the list's chips. Before:
    tagged entries by id - the back injury, recorded last, was cut behind 50 (profile) / 100 (list) ankle sprains."""
    from sqlalchemy import insert

    owner, gym, plans = await owner_with_gym(db)
    member = await make_customer(db, full_name="One Tag Padded")
    await make_subscription(db, member, plans[0])
    await db.execute(insert(HealthCondition), _one_tag_over_and_over(member.id, 120))
    await db.commit()
    await login_as(client, owner)
    r = await client.get(f"/owner/customers/{member.id}")
    assert r.status_code == 200
    health = r.text.split('id="health"', 1)[1].split("<details", 1)[0]
    assert "Back pain" in health and "+71 more" in health  # 50 of the 121 injuries are listed, the back pain among them
    listing = (await client.get("/owner/customers")).text
    chip = '<span class="chip !px-2 !py-0.5 !text-[11px]">{}</span>'
    assert chip.format("Back problem") in listing and chip.format("Ankle / foot injury") in listing
