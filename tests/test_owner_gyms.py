"""WP-B owner console: overview, gyms, branches (hours, features, pin), documents, photos, close/reopen."""

from __future__ import annotations

import asyncio
import html
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.core.utils import utcnow
from app.db import QueryCounter
from app.models import (
    AuditLog,
    Branch,
    BranchDocument,
    BranchFeature,
    BranchHours,
    BranchPhoto,
    CustomerProfile,
    Gym,
    Job,
    MisconductReport,
    Notification,
    OtpChallenge,
    SalaryStatement,
    Transaction,
)
from app.models.enums import (
    BranchDocType,
    IdStatus,
    PlanMode,
    StatementStatus,
    TxCategory,
    TxDirection,
    TxStatus,
    VerificationStatus,
)
from app.services import storage
from app.services.visibility import is_branch_published
from tests.conftest import login_as, post_form, run_jobs
from tests.test_superadmin import flash_text
from tests.factories import (
    INDIRANAGAR,
    KORAMANGALA,
    make_branch,
    make_customer,
    make_employee,
    make_gym,
    make_owner,
    make_plans,
    make_subscription,
    png_bytes,
)

VS = VerificationStatus
GSTIN = "27AAPFU0939F1ZV"


# ------------------------------------------------------------------ helpers
def branch_data(**over):
    data = {
        "name": "Koramangala", "address_line1": "80 Feet Road, 4th Block", "address_line2": "", "city": "Bengaluru",
        "state": "Karnataka", "postal_code": "560034", "country_code": "IN", "timezone": "Asia/Kolkata",
        "phone": "+91 80 4000 1234", "email": "kora@pulse.example", "registration_number": GSTIN,
        "latitude": str(KORAMANGALA[0]), "longitude": str(KORAMANGALA[1]),
        "features": ["Sauna", "Lockers", "Not a real feature"], "custom_features": "Boxing ring, Kids area",
    }
    for wd in range(7):
        data[f"open_{wd}"], data[f"close_{wd}"] = "06:00", "20:00"
    data.update(over)
    return data


def details_of(branch: Branch, **over):
    """Form data that re-submits an existing branch unchanged (plus overrides)."""
    data = branch_data(
        name=branch.name, address_line1=branch.address_line1, address_line2=branch.address_line2 or "",
        city=branch.city, state=branch.state or "", postal_code=branch.postal_code or "",
        country_code=branch.country_code, timezone=branch.timezone, phone="", email="",
        registration_number=branch.registration_number or "",
        latitude=f"{branch.latitude:.6f}", longitude=f"{branch.longitude:.6f}", features=[], custom_features="",
    )
    data.update(over)
    return data


class Page:
    def __init__(self, response):
        self.status_code = response.status_code
        self.text = html.unescape(response.text)


async def follow(client, response):
    assert response.status_code == 303, response.text[:500]
    return Page(await client.get(response.headers["location"].split("#")[0]))


async def add_doc(db, branch, doc_type: BranchDocType, *, text: str = "doc"):
    stored, _ = await storage.save_private_file(png_bytes(text=text), kind="branch_docs")
    doc = BranchDocument(branch_id=branch.id, doc_type=doc_type, file_key=stored.key, original_filename=f"{doc_type.value}.png",
                         content_type=stored.content_type, sha256=stored.sha256, size_bytes=stored.size)
    db.add(doc)
    await db.commit()
    return doc


async def ready_branch(db, gym, **kw):
    """A DRAFT branch that satisfies every submission requirement (India rules)."""
    b = await make_branch(db, gym, verified=False, photos=0, **kw)
    b.registration_number = GSTIN
    await db.commit()
    for t in (BranchDocType.TAX_REGISTRATION, BranchDocType.FIRE_SAFETY_CERTIFICATE, BranchDocType.LIABILITY_INSURANCE):
        await add_doc(db, b, t, text=t.value)
    return b


@pytest.fixture
async def owner_setup(db):
    owner = await make_owner(db)
    gym = await make_gym(db, owner, name="Pulse Fitness")
    branch = await make_branch(db, gym, name="Indiranagar")
    return owner, gym, branch


# ------------------------------------------------------------------ pages & access
async def test_pages_render(client, db, owner_setup):
    owner, gym, branch = owner_setup
    await login_as(client, owner)
    expectations = {
        "/owner": "Launch checklist",
        "/owner/gyms": "Pulse Fitness",
        "/owner/gyms/new": "Create a gym",
        f"/owner/gyms/{gym.id}": "How you operate",
        f"/owner/gyms/{gym.id}/branches/new": "Opening hours",
        f"/owner/branches/{branch.id}": "Compliance documents",
    }
    for url, needle in expectations.items():
        r = await client.get(url)
        assert r.status_code == 200, (url, r.text[:2000])
        assert needle in r.text, url
        assert "<script>" not in r.text and " onclick=" not in r.text and ' style="' not in r.text  # strict CSP


async def test_owner_pages_require_an_owner(client, anon_client_factory, db, owner_setup):
    _, gym, _ = owner_setup
    anon = anon_client_factory()
    r = await anon.get("/owner")
    assert r.status_code == 303 and r.headers["location"].startswith("/auth/login")
    customer = await make_customer(db)
    await login_as(client, customer)
    assert (await client.get("/owner")).status_code == 403
    assert (await client.get(f"/owner/gyms/{gym.id}")).status_code == 403


async def test_other_owner_gets_404_everywhere(client, db, owner_setup):
    _, gym, branch = owner_setup
    doc = await add_doc(db, branch, BranchDocType.TAX_REGISTRATION)
    intruder = await make_owner(db)
    await login_as(client, intruder)
    for url in (f"/owner/gyms/{gym.id}", f"/owner/gyms/{gym.id}/branches/new", f"/owner/branches/{branch.id}",
                f"/owner/branches/{branch.id}/documents/{doc.id}/file"):
        assert (await client.get(url)).status_code == 404, url
    for url, data in ((f"/owner/gyms/{gym.id}", {"name": "Hijack", "country_code": "IN", "currency": "INR"}),
                      (f"/owner/gyms/{gym.id}/online", {"action": "close"}),
                      (f"/owner/branches/{branch.id}/close", {"reason": "hijacked"}),
                      (f"/owner/branches/{branch.id}/submit", {}),
                      (f"/owner/branches/{branch.id}/documents/{doc.id}/delete", {})):
        assert (await post_form(client, url, data)).status_code == 404, url
    r = await client.get(f"/api/owner/branches/{branch.id}/status")
    assert r.status_code == 404 and r.headers["content-type"].startswith("application/json")
    await db.refresh(gym)
    assert gym.name == "Pulse Fitness"


# ------------------------------------------------------------------ gyms
async def test_gym_create_unique_slug_and_audit(client, db):
    owner = await make_owner(db)
    await login_as(client, owner)
    form = {"name": "Iron Temple", "tagline": "Lift heavy", "country_code": "IN", "currency": "", "contact_email": "hi@iron.example",
            "contact_phone": "+91 98765 43210", "website": "irontemple.example"}
    r1 = await post_form(client, "/owner/gyms/new", form)
    r2 = await post_form(client, "/owner/gyms/new", form)
    assert r1.status_code == 303 and r2.status_code == 303
    gyms = (await db.execute(select(Gym).where(Gym.owner_id == owner.id).order_by(Gym.id))).scalars().all()
    assert [g.slug for g in gyms] == ["iron-temple", "iron-temple-2"]
    assert gyms[0].currency == "INR" and gyms[0].website == "https://irontemple.example"
    assert not gyms[0].online_enabled and not gyms[0].offline_enabled
    assert await db.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == "gym.create")) == 2
    # the owner's personal e-mail is never pre-filled as a public gym contact
    page = await client.get("/owner/gyms/new")
    assert owner.email not in page.text.split('name="contact_email"')[1][:200]


async def test_gym_create_survives_a_slug_race(client, db, monkeypatch):
    from app.routers import owner_gyms

    owner = await make_owner(db)
    await make_gym(db, owner, name="Taken")
    taken = (await db.execute(select(Gym.slug))).scalar_one()

    async def stale_slug(session, name):  # simulate a concurrent request that grabbed the slug first
        return taken

    monkeypatch.setattr(owner_gyms, "_unique_slug", stale_slug)
    await login_as(client, owner)
    r = await post_form(client, "/owner/gyms/new", {"name": "Taken", "country_code": "IN", "currency": "INR"})
    assert r.status_code == 303
    new_slug = (await db.execute(select(Gym.slug).where(Gym.slug != taken))).scalar_one()
    assert new_slug.startswith("taken-") and len(new_slug) == len("taken-") + 6  # retried with a random suffix


async def test_gym_form_validation_errors_rerender_400(client, db):
    owner = await make_owner(db)
    await login_as(client, owner)
    r = await post_form(client, "/owner/gyms/new", {"name": "X", "country_code": "ZZ", "currency": "INR",
                                                     "contact_email": "not-an-email", "website": "ht tp://bad"})
    assert r.status_code == 400
    assert "Give your gym a name" in r.text and "valid e-mail" in r.text and "Choose the country" in r.text
    assert await db.scalar(select(func.count(Gym.id))) == 0


async def test_gym_update_logo_and_remove(client, db, owner_setup):
    owner, gym, _ = owner_setup
    await login_as(client, owner)
    data = {"name": "Pulse Fitness Pro", "tagline": "", "description": "Best gym", "country_code": "IN", "currency": "INR",
            "contact_email": "", "contact_phone": "", "website": ""}
    r = await post_form(client, f"/owner/gyms/{gym.id}", data, files={"logo": ("logo.png", png_bytes((300, 300), "red"), "image/png")})
    assert r.status_code == 303
    await db.refresh(gym)
    assert gym.name == "Pulse Fitness Pro" and gym.slug.startswith("pulse-gym")  # slug stays stable
    assert gym.logo_key and gym.logo_key.startswith("public/gym_logos/") and storage.path_for(gym.logo_key).is_file()
    old = gym.logo_key
    r = await post_form(client, f"/owner/gyms/{gym.id}", {**data, "remove_logo": "1"})
    assert r.status_code == 303
    await db.refresh(gym)
    assert gym.logo_key is None and not storage.path_for(old).exists()
    bad = await post_form(client, f"/owner/gyms/{gym.id}", data, files={"logo": ("x.png", b"not an image", "image/png")})
    assert bad.status_code == 400 and "not a valid image" in bad.text


async def test_online_mode_requires_verified_owner(client, db):
    owner = await make_owner(db, id_status=IdStatus.PENDING)
    gym = await make_gym(db, owner, online=False)
    await login_as(client, owner)
    page = await follow(client, await post_form(client, f"/owner/gyms/{gym.id}/online", {"action": "enable"}))
    assert "needs a verified owner" in page.text
    await db.refresh(gym)
    assert gym.online_enabled is False
    owner.id_status = IdStatus.VERIFIED
    owner.photo_key = None
    await db.commit()
    await post_form(client, f"/owner/gyms/{gym.id}/online", {"action": "enable"})
    await db.refresh(gym)
    assert gym.online_enabled is False  # photo still missing
    owner.photo_key = "public/avatars/" + "a" * 32 + ".jpg"
    await db.commit()
    assert (await post_form(client, f"/owner/gyms/{gym.id}/online", {"action": "enable"})).status_code == 303
    await db.refresh(gym)
    assert gym.online_enabled is True and gym.online_open is True


async def test_online_close_and_reopen_notify_online_members(client, db, owner_setup):
    owner, gym, branch = owner_setup
    plans = {p.code: p for p in await make_plans(db, gym)}
    online_member = await make_customer(db)
    offline_member = await make_customer(db)
    await make_subscription(db, online_member, plans["online"])
    await make_subscription(db, offline_member, plans["offline"], branch=branch)
    await login_as(client, owner)
    r = await post_form(client, f"/owner/gyms/{gym.id}/online", {"action": "close"})
    assert r.status_code == 303 and "Online classes are closed. 1 member will be notified." in flash_text(r)
    await db.refresh(gym)
    assert gym.online_enabled and gym.online_open is False
    titles = lambda uid: select(Notification.title).where(Notification.user_id == uid)  # noqa: E731
    assert not (await db.execute(titles(online_member.id))).scalars().all()  # queued: sent by the job, not the request
    assert await run_jobs() == 1
    assert any("paused" in t for t in (await db.execute(titles(online_member.id))).scalars())
    assert not (await db.execute(titles(offline_member.id))).scalars().all()
    await post_form(client, f"/owner/gyms/{gym.id}/online", {"action": "reopen"})
    await run_jobs()
    await db.refresh(gym)
    assert gym.online_open is True
    assert any("back" in t for t in (await db.execute(titles(online_member.id))).scalars())
    assert await db.scalar(select(func.count(AuditLog.id)).where(AuditLog.action.like("gym.online_%"))) == 2


async def test_offline_toggle(client, db, owner_setup):
    owner, gym, branch = owner_setup
    await login_as(client, owner)
    assert is_branch_published(branch, gym, 3)
    await post_form(client, f"/owner/gyms/{gym.id}/offline", {"action": "disable"})
    await db.refresh(gym)
    assert gym.offline_enabled is False and not is_branch_published(branch, gym, 3)
    await post_form(client, f"/owner/gyms/{gym.id}/offline", {"action": "enable"})
    await db.refresh(gym)
    assert gym.offline_enabled is True


async def _members_on(db, gym, branch, n: int = 3, plan: str = "combo"):
    plans = {p.code: p for p in await make_plans(db, gym)}
    members = [await make_customer(db) for _ in range(n)]
    for m in members:
        await make_subscription(db, m, plans[plan], branch=branch)
    return members


async def _notices(db, member, like: str) -> int:
    return int(await db.scalar(select(func.count(Notification.id)).where(Notification.user_id == member.id,
                                                                         Notification.title.like(like))) or 0)


async def test_parallel_mode_switches_change_once_and_notify_members_once(client, db, owner_setup):
    """F38: 'switch off physical branches' (or online classes) sent from several tabs or by two admins at once - each
    request read the old flag (no row lock), flipped it, queued its own notice job and wrote its own audit row, so every
    member got the same 'branches unavailable' notice several times. The gym row is now locked before the flag is read:
    one change, one notice job, one audit row; the other submits see 'already off'."""
    owner, gym, branch = owner_setup
    members = await _members_on(db, gym, branch)
    await login_as(client, owner)
    for url, action, audit_action, like in (
            (f"/owner/gyms/{gym.id}/offline", "disable", "gym.offline_disable", "%branches unavailable"),
            (f"/owner/gyms/{gym.id}/online", "close", "gym.online_close", "%online classes paused")):
        jobs_before = await db.scalar(select(func.count(Job.id)).where(Job.kind == "notify_members"))
        rs = await asyncio.gather(*[post_form(client, url, {"action": action}) for _ in range(8)])
        assert {r.status_code for r in rs} == {303}, url
        flashes = [flash_text(r) for r in rs]
        assert sum("already" in f for f in flashes) == 7, flashes
        assert await db.scalar(select(func.count(Job.id)).where(Job.kind == "notify_members")) == jobs_before + 1
        assert await db.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == audit_action)) == 1
        await run_jobs()
        for m in members:
            assert await _notices(db, m, like) == 1, (url, m.id)


async def test_parallel_branch_close_notifies_members_once(client, db, owner_setup):
    """F38: the same race on a branch's close / reopen buttons (the branch row is locked before its state is read)."""
    owner, gym, branch = owner_setup
    members = await _members_on(db, gym, branch, plan="offline")
    await login_as(client, owner)
    rs = await asyncio.gather(*[post_form(client, f"/owner/branches/{branch.id}/close", {"reason": "Flooded floor"})
                                for _ in range(6)])
    assert {r.status_code for r in rs} == {303}
    assert sum("already closed" in flash_text(r) for r in rs) == 5
    rs = await asyncio.gather(*[post_form(client, f"/owner/branches/{branch.id}/reopen") for _ in range(6)])
    assert sum("already open" in flash_text(r) for r in rs) == 5
    for action in ("branch.close", "branch.reopen"):
        assert await db.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == action)) == 1, action
    await run_jobs()
    for m in members:
        assert await _notices(db, m, "%is closed") == 1 and await _notices(db, m, "%has reopened") == 1


async def test_identical_notice_jobs_running_at_once_notify_each_member_once(db, owner_setup):
    """F38: two notify_members jobs carrying the same notice (a retried request, two workers) ran their 'already
    notified?' check at the same moment and both inserted it. Each chunk now checks and inserts under the notice's
    lock, so the second run skips the members the first one reached."""
    from app.models.enums import Severity
    from app.services import member_notices as MN

    _owner, gym, branch = owner_setup
    members = await _members_on(db, gym, branch)
    since = await db.scalar(select(func.now()))
    kw = dict(gym_id=gym.id, title=f"{gym.name}: branches unavailable", body="Switched off.", link="/gyms/x",
              severity=Severity.WARNING, since=since, as_of=date.today(),
              modes=(PlanMode.OFFLINE, PlanMode.COMBO))
    sent = await asyncio.gather(MN.send_member_notice(**kw), MN.send_member_notice(**kw))
    assert sum(sent) == len(members)
    for m in members:
        assert await _notices(db, m, "%branches unavailable") == 1


# ------------------------------------------------------------------ branches
async def test_branch_create_saves_hours_features_and_pin(client, db):
    owner = await make_owner(db)
    gym = await make_gym(db, owner, offline=False)
    await login_as(client, owner)
    data = branch_data(open_6="", close_6="", closed_5="1")
    r = await post_form(client, f"/owner/gyms/{gym.id}/branches/new", data)
    assert r.status_code == 303 and r.headers["location"].endswith("#documents")
    branch = (await db.execute(select(Branch).where(Branch.gym_id == gym.id))).scalar_one()
    assert branch.verification_status == VS.DRAFT and branch.registration_number == GSTIN
    assert branch.latitude == pytest.approx(KORAMANGALA[0]) and branch.longitude == pytest.approx(KORAMANGALA[1])
    hours = (await db.execute(select(BranchHours.weekday).where(BranchHours.branch_id == branch.id))).scalars().all()
    assert sorted(hours) == [0, 1, 2, 3, 4]  # Saturday ticked closed, Sunday left empty
    features = set((await db.execute(select(BranchFeature.name).where(BranchFeature.branch_id == branch.id))).scalars())
    assert features == {"Sauna", "Lockers", "Boxing ring", "Kids area"}
    await db.refresh(gym)
    assert gym.offline_enabled is True


@pytest.mark.parametrize(("open_", "close", "message"), [
    ("05:00", "20:00", "at most 14 h a day - you entered 15 h"),
    ("20:00", "06:00", "closing time must be later"),
    ("09:00", "09:00", "closing time must be later"),
    ("9am", "18:00", "24-hour HH:MM"),
])
async def test_hours_validation(client, db, open_, close, message):
    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    await login_as(client, owner)
    r = await post_form(client, f"/owner/gyms/{gym.id}/branches/new", branch_data(open_2=open_, close_2=close))
    assert r.status_code == 400
    assert "Wednesday" in r.text and message in r.text
    assert await db.scalar(select(func.count(Branch.id))) == 0


async def test_branch_form_field_validation(client, db):
    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    await login_as(client, owner)
    r = await post_form(client, f"/owner/gyms/{gym.id}/branches/new", branch_data(
        registration_number="27AAPFU0939F1ZX", latitude="999", custom_features=",".join(f"Extra {i}" for i in range(11)),
        timezone="Mars/Olympus", city=""))
    assert r.status_code == 400
    for needle in ("GSTIN checksum", "Drop the pin", "at most 10 custom", "valid time zone", "Enter the city"):
        assert needle in r.text, needle


async def test_editing_address_of_verified_branch_needs_confirmation_and_resets(client, db, owner_setup):
    owner, gym, branch = owner_setup
    branch.registration_number = GSTIN
    await db.commit()
    await login_as(client, owner)
    # harmless edit keeps the verification
    r = await post_form(client, f"/owner/branches/{branch.id}", details_of(branch, name="Indiranagar 100ft"))
    assert r.status_code == 303
    await db.refresh(branch)
    assert branch.name == "Indiranagar 100ft" and branch.verification_status == VS.VERIFIED
    # address change without the confirmation tick -> 400, nothing saved
    r = await post_form(client, f"/owner/branches/{branch.id}", details_of(branch, city="Mysuru"))
    assert r.status_code == 400 and "tick the box to confirm" in r.text
    await db.refresh(branch)
    assert branch.city == "Bengaluru" and branch.verification_status == VS.VERIFIED
    # confirmed -> saved, reset to draft, owner notified, hidden from the website
    r = await post_form(client, f"/owner/branches/{branch.id}", details_of(branch, city="Mysuru", confirm_reverify="1",
                                                                           latitude=str(INDIRANAGAR[0] + 0.01)))
    assert r.status_code == 303 and r.headers["location"].endswith("#verification")
    await db.refresh(branch)
    assert branch.city == "Mysuru" and branch.verification_status == VS.DRAFT and branch.verified_at is None
    assert not is_branch_published(branch, gym, 3)
    n = (await db.execute(select(Notification).where(Notification.user_id == owner.id))).scalars().all()
    assert any("re-verification" in x.title for x in n)


async def test_address_locked_while_verification_runs(client, db, owner_setup):
    owner, gym, branch = owner_setup
    branch.verification_status = VS.PROCESSING
    branch.registration_number = GSTIN
    await db.commit()
    await login_as(client, owner)
    r = await post_form(client, f"/owner/branches/{branch.id}", details_of(branch, address_line1="Somewhere else 1"))
    assert r.status_code == 400 and "Verification is running" in r.text
    r = await post_form(client, f"/owner/branches/{branch.id}", details_of(branch, phone="+91 80 1111 2222"))
    assert r.status_code == 303  # contacts may still change


# ------------------------------------------------------------------ documents
async def test_document_upload_duplicate_download_and_delete(client, db, owner_setup):
    owner, gym, _ = owner_setup
    branch = await make_branch(db, gym, verified=False, photos=0)
    await login_as(client, owner)
    content = png_bytes((120, 80), "white", text="GSTIN 27AAPFU0939F1ZV")
    url = f"/owner/branches/{branch.id}/documents"
    r = await post_form(client, url, {"doc_type": "tax_registration", "expires_on": "2099-01-31"},
                        files={"file": ("gst.png", content, "image/png")})
    assert r.status_code == 303
    doc = (await db.execute(select(BranchDocument).where(BranchDocument.branch_id == branch.id))).scalar_one()
    assert doc.doc_type == BranchDocType.TAX_REGISTRATION and doc.file_key.startswith("private/branch_docs/")
    assert storage.path_for(doc.file_key).read_bytes() != await storage.load_private(doc.file_key)  # encrypted at rest
    dup = await post_form(client, url, {"doc_type": "fire_safety_certificate"}, files={"file": ("again.png", content, "image/png")})
    assert dup.status_code == 400 and "already uploaded" in dup.text
    bad = await post_form(client, url, {"doc_type": "fire_safety_certificate"}, files={"file": ("x.gif", b"GIF89a....", "image/gif")})
    assert bad.status_code == 400 and "PDF, JPG or PNG" in bad.text
    expired = await post_form(client, url, {"doc_type": "fire_safety_certificate", "expires_on": "2001-01-01"},
                              files={"file": ("old.png", png_bytes(text="old"), "image/png")})
    assert expired.status_code == 400 and "has expired" in expired.text
    notype = await post_form(client, url, {"doc_type": "passport"}, files={"file": ("p.png", png_bytes(text="p"), "image/png")})
    assert notype.status_code == 400

    f = await client.get(f"/owner/branches/{branch.id}/documents/{doc.id}/file")
    assert f.status_code == 200 and f.headers["content-type"] == "image/jpeg"
    assert f.headers["cache-control"] == "no-store" and f.headers["content-disposition"].startswith("inline")

    r = await post_form(client, f"/owner/branches/{branch.id}/documents/{doc.id}/delete")
    assert r.status_code == 303
    assert await db.scalar(select(func.count(BranchDocument.id))) == 0
    assert not storage.path_for(doc.file_key).exists()


async def test_documents_of_verified_branch_cannot_be_deleted(client, db, owner_setup):
    owner, _, branch = owner_setup
    doc = await add_doc(db, branch, BranchDocType.TAX_REGISTRATION)
    await login_as(client, owner)
    page = await follow(client, await post_form(client, f"/owner/branches/{branch.id}/documents/{doc.id}/delete"))
    assert "can't be deleted" in page.text
    assert await db.scalar(select(func.count(BranchDocument.id))) == 1


async def test_document_upload_accepts_pdf(client, db, owner_setup):
    import io

    from PIL import Image

    owner, gym, _ = owner_setup
    branch = await make_branch(db, gym, verified=False, photos=0)
    buf = io.BytesIO()
    Image.new("RGB", (200, 260), "white").save(buf, "PDF")
    await login_as(client, owner)
    r = await post_form(client, f"/owner/branches/{branch.id}/documents", {"doc_type": "business_registration"},
                        files={"file": ("registration.pdf", buf.getvalue(), "application/pdf")})
    assert r.status_code == 303
    doc = (await db.execute(select(BranchDocument))).scalar_one()
    assert doc.content_type == "application/pdf" and doc.original_filename == "registration.pdf"


# ------------------------------------------------------------------ submit gating
async def test_submit_requires_verified_owner(client, db):
    owner = await make_owner(db, id_status=IdStatus.PENDING)
    gym = await make_gym(db, owner)
    branch = await ready_branch(db, gym)
    await login_as(client, owner)
    page = await follow(client, await post_form(client, f"/owner/branches/{branch.id}/submit"))
    assert "Not ready yet" in page.text and "Verify your government ID" in page.text
    await db.refresh(branch)
    assert branch.verification_status == VS.DRAFT
    assert await db.scalar(select(func.count(Job.id)).where(Job.kind == "verify_branch")) == 0


async def test_submit_requires_documents_and_pin(client, db, owner_setup):
    owner, gym, _ = owner_setup
    branch = await make_branch(db, gym, verified=False, photos=0)
    branch.registration_number = GSTIN
    branch.latitude = branch.longitude = None
    await db.commit()
    await login_as(client, owner)
    page = await follow(client, await post_form(client, f"/owner/branches/{branch.id}/submit"))
    assert "Fire NOC" in page.text and "map pin" in page.text
    await db.refresh(branch)
    assert branch.verification_status == VS.DRAFT


async def test_submit_queues_a_deduplicated_job(client, db, owner_setup):
    owner, gym, _ = owner_setup
    branch = await ready_branch(db, gym)
    await login_as(client, owner)
    r = await post_form(client, f"/owner/branches/{branch.id}/submit")
    assert r.status_code == 303
    await db.refresh(branch)
    assert branch.verification_status == VS.QUEUED
    job = (await db.execute(select(Job).where(Job.kind == "verify_branch"))).scalar_one()
    assert job.dedupe_key == f"verify_branch:{branch.id}" and job.payload == {"branch_id": branch.id, "attempt": 1}
    again = await follow(client, await post_form(client, f"/owner/branches/{branch.id}/submit"))
    assert "already running" in again.text
    assert await db.scalar(select(func.count(Job.id))) == 1
    st = await client.get(f"/api/owner/branches/{branch.id}/status")
    assert st.json()["status"] == "queued" and st.json()["in_progress"] is True
    assert any("submitted" in t for t in (await db.execute(select(Notification.title))).scalars())


# ------------------------------------------------------------------ photos
async def test_photos_blocked_before_verification(client, db, owner_setup):
    owner, gym, _ = owner_setup
    branch = await make_branch(db, gym, verified=False, photos=0)
    await login_as(client, owner)
    r = await post_form(client, f"/owner/branches/{branch.id}/photos", {"kind": "machines"},
                        files={"photos": ("a.png", png_bytes(), "image/png")})
    assert r.status_code == 400 and "once the branch is AI-verified" in r.text
    assert await db.scalar(select(func.count(BranchPhoto.id)).where(BranchPhoto.branch_id == branch.id)) == 0


async def test_three_photos_publish_the_branch(client, db, owner_setup):
    owner, gym, _ = owner_setup
    branch = await make_branch(db, gym, photos=0)
    await login_as(client, owner)
    url = f"/owner/branches/{branch.id}/photos"
    r = await post_form(client, url, {"kind": "machines", "caption": "Racks"}, files=[
        ("photos", ("a.png", png_bytes((80, 60), "red"), "image/png")),
        ("photos", ("b.png", png_bytes((80, 60), "blue"), "image/png")),
    ])
    assert r.status_code == 303
    assert not is_branch_published(branch, gym, 2)
    r = await post_form(client, url, {"kind": "overall"}, files=[("photos", ("c.png", png_bytes((80, 60), "green"), "image/png"))])
    assert r.status_code == 303
    photos = (await db.execute(select(BranchPhoto).where(BranchPhoto.branch_id == branch.id))).scalars().all()
    assert len(photos) == 3 and {p.kind.value for p in photos} == {"machines", "overall"}
    assert all(p.image_key.startswith("public/branch_photos/") for p in photos)
    assert is_branch_published(branch, gym, len(photos))
    assert any("is live" in t for t in (await db.execute(select(Notification.title).where(Notification.user_id == owner.id))).scalars())
    page = await client.get(f"/owner/branches/{branch.id}")
    assert "Live on the website" in page.text
    # deleting one hides it again
    r = await post_form(client, f"/owner/branches/{branch.id}/photos/{photos[0].id}/delete")
    assert r.status_code == 303
    assert await db.scalar(select(func.count(BranchPhoto.id)).where(BranchPhoto.branch_id == branch.id)) == 2


async def test_photo_limit_is_twelve(client, db, owner_setup):
    owner, gym, _ = owner_setup
    branch = await make_branch(db, gym, photos=11)
    await login_as(client, owner)
    r = await post_form(client, f"/owner/branches/{branch.id}/photos", {"kind": "other"}, files=[
        ("photos", ("a.png", png_bytes(), "image/png")), ("photos", ("b.png", png_bytes(), "image/png"))])
    assert r.status_code == 400 and "at most 12 photos" in r.text


# ------------------------------------------------------------------ close / reopen
async def test_close_and_reopen_keep_verification_and_notify_members(client, db, owner_setup):
    owner, gym, branch = owner_setup
    plans = {p.code: p for p in await make_plans(db, gym)}
    member = await make_customer(db)
    await make_subscription(db, member, plans["offline"], branch=branch)
    await login_as(client, owner)
    bad = await post_form(client, f"/owner/branches/{branch.id}/close", {"reason": ""})
    assert bad.status_code == 400 and "Tell your members why" in bad.text
    r = await post_form(client, f"/owner/branches/{branch.id}/close", {"reason": "Renovation until March"})
    assert r.status_code == 303 and "1 member will be notified." in flash_text(r)
    await db.refresh(branch)
    assert branch.is_open is False and branch.verification_status == VS.VERIFIED and branch.closed_reason
    assert not is_branch_published(branch, gym, 3)
    assert await run_jobs() == 1  # the notice is queued by the request and sent by the notify_members job
    msgs = (await db.execute(select(Notification).where(Notification.user_id == member.id))).scalars().all()
    assert any("closed" in n.title and "Renovation" in n.body for n in msgs)
    r = await post_form(client, f"/owner/branches/{branch.id}/reopen")
    assert r.status_code == 303
    await run_jobs()
    await db.refresh(branch)
    assert branch.is_open is True and branch.verification_status == VS.VERIFIED and branch.closed_at is None
    assert is_branch_published(branch, gym, 3)
    assert await db.scalar(select(func.count(Notification.id)).where(Notification.user_id == member.id)) == 2


# ------------------------------------------------------------------ overview KPIs + N+1
async def _populate(db, owner, n_gyms: int, n_branches: int):
    for g in range(n_gyms):
        gym = await make_gym(db, owner)
        plans = {p.code: p for p in await make_plans(db, gym)}
        for b in range(n_branches):
            branch = await make_branch(db, gym, verified=b % 2 == 0, photos=3 if b % 2 == 0 else 0)
            cust = await make_customer(db)
            await make_subscription(db, cust, plans["offline"], branch=branch)


async def test_overview_and_gym_list_query_count_is_constant(client, db):
    owner = await make_owner(db)
    await _populate(db, owner, 1, 1)
    await login_as(client, owner)
    await client.get("/owner")  # warm-up (session, CSRF cookie)
    counts = {}
    for url in ("/owner", "/owner/gyms"):
        with QueryCounter() as qc:
            assert (await client.get(url)).status_code == 200
        counts[url] = qc.count
    await _populate(db, owner, 3, 4)
    for url in ("/owner", "/owner/gyms"):
        with QueryCounter() as qc:
            assert (await client.get(url)).status_code == 200
        assert qc.count == counts[url], (url, counts[url], qc.count)
    assert counts["/owner"] <= 12 and counts["/owner/gyms"] <= 5


async def test_overview_kpis(client, db, owner_setup):
    owner, gym, branch = owner_setup
    plans = {p.code: p for p in await make_plans(db, gym)}
    paused = await make_customer(db)
    active = await make_customer(db)
    sub = await make_subscription(db, paused, plans["offline"], branch=branch)
    await make_subscription(db, active, plans["online"])
    prof = await db.get(CustomerProfile, paused.id)
    prof.activity_suspended = True
    staff = await make_employee(db, gym)
    db.add_all([
        Transaction(gym_id=gym.id, direction=TxDirection.INCOMING, category=TxCategory.SUBSCRIPTION, status=TxStatus.SUCCESS,
                    amount=Decimal("1499.00"), currency="INR", description="Offline plan", provider="test", completed_at=utcnow()),
        Transaction(gym_id=gym.id, direction=TxDirection.INCOMING, category=TxCategory.SUBSCRIPTION, status=TxStatus.FAILED,
                    amount=Decimal("999.00"), currency="INR", description="Failed", provider="test"),
        Transaction(gym_id=gym.id, direction=TxDirection.OUTGOING, category=TxCategory.SALARY, status=TxStatus.SUCCESS,
                    amount=Decimal("30000.00"), currency="INR", description="Salary", provider="test", completed_at=utcnow()),
        MisconductReport(gym_id=gym.id, employee_id=staff.id, reporter_id=active.id, category="rude", description="Rude"),
        SalaryStatement(employee_id=staff.id, gym_id=gym.id, period_start=utcnow().date().replace(day=1),
                        period_end=utcnow().date().replace(day=1), days_in_period=30, days_worked=30, monthly_salary=Decimal("30000"),
                        gross_pay=Decimal("30000"), net_pay=Decimal("30000"), currency="INR", country_code="IN",
                        status=StatementStatus.DUE, due_date=utcnow().date()),
        OtpChallenge(subscription_id=sub.id, branch_id=branch.id, customer_id=paused.id, recipient_user_id=owner.id,
                     code_hash="x" * 64, expires_at=utcnow() + timedelta(minutes=5)),
    ])
    await db.commit()
    await login_as(client, owner)
    r = await client.get("/owner")
    assert r.status_code == 200
    html = r.text
    assert "₹1,499.00" in html  # only INCOMING + SUCCESS counts as revenue
    assert "Active members" in html and "Paused members" in html and "Safety holds awaiting you" in html
    assert "Customers waiting at the desk" in html and "Need your response" in html
    assert "1 / 1" in html  # verified branches
