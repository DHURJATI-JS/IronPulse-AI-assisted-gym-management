"""WP-G super-admin console: access control, overview KPIs (+ N+1 guard), review queue (branch / employee /
identity), authorised previews, AI provider page + live test, the AI usage & abuse panel (queue, top users,
cooldowns + Lift, recent blocks, presence; cancelled calls are not provider failures), self-check, users (search,
soft delete, restore, unlock), e-mail outbox (retry, escaped bodies) and the audit log."""

from __future__ import annotations

import asyncio
import json
import re
import sys
import types
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select

from app.core.utils import utcnow
from app.db import QueryCounter
from app.models import (
    AICallLog,
    AuditLog,
    BranchDocument,
    EmailOutbox,
    EmployeeDocument,
    IdentityDocument,
    IpBan,
    Job,
    Notification,
    Product,
    Refund,
    SecurityEvent,
    ShopOrder,
    Transaction,
    User,
    VerificationRun,
    WorkoutSession,
)
from app.models.enums import (
    BranchDocType,
    EmailStatus,
    EmploymentStatus,
    IdDocType,
    IdStatus,
    JobStatus,
    Role,
    ThreatCategory,
    TxCategory,
    TxDirection,
    TxStatus,
    VerificationStatus,
)
from app.models.shop import OrderStatus, RefundStatus
from app.services import storage
from tests.ask_ai_helpers import connections_while_the_ai_works, pool_probe  # noqa: F401 - pool_probe: a fixture
from tests.conftest import api, login_as, post_form
from tests.factories import (
    make_branch,
    make_customer,
    make_employee,
    make_gym,
    make_owner,
    make_plans,
    make_subscription,
    make_user,
    png_bytes,
)

PAGES = ["/superadmin", "/superadmin/security", "/superadmin/bans", "/superadmin/reviews", "/superadmin/ai",
         "/superadmin/users", "/superadmin/outbox", "/superadmin/audit", "/superadmin/selfcheck"]


# ------------------------------------------------------------------ helpers
async def make_admin(db, **kwargs) -> User:
    return await make_user(db, role=Role.SUPERADMIN, full_name=kwargs.pop("full_name", "Platform Admin"), **kwargs)


@pytest.fixture
async def admin(db) -> User:
    return await make_admin(db)


@pytest.fixture
async def admin_client(client, admin):
    await login_as(client, admin)
    return client


def flash_text(response: httpx.Response) -> str:
    """Decode the signed flash cookie of a redirect (message text only)."""
    from itsdangerous import URLSafeSerializer

    from app.config import get_settings
    from app.core.templating import FLASH_COOKIE

    raw = response.cookies.get(FLASH_COOKIE)
    if not raw:
        return ""
    data = URLSafeSerializer(get_settings().secret_key, salt="ironpulse-flash").loads(raw)
    return " | ".join(f"{d['c']}: {d['m']}" for d in data)


def add_event(db, ip: str, *, category: ThreatCategory = ThreatCategory.SQLI, severity: int = 3, detail: str = "UNION SELECT in query",
              path: str = "/search", reviewed: bool = False, user_id: int | None = None, when=None, action: str = "logged"):
    e = SecurityEvent(ip=ip, method="GET", path=path, user_agent="Mozilla/5.0 test", category=category, severity=severity,
                      detail=detail, action=action, ai_reviewed=reviewed, user_id=user_id)
    if when is not None:
        e.created_at = when
    db.add(e)
    return e


async def seed_platform(db, n: int) -> None:
    """``n`` rows of everything the overview aggregates."""
    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    plans = await make_plans(db, gym)
    for i in range(n):
        cust = await make_customer(db)
        await make_subscription(db, cust, plans[4])
        b = await make_branch(db, gym, verified=bool(i % 2))
        if i % 2 == 0:
            b.verification_status = VerificationStatus.NEEDS_REVIEW
        add_event(db, f"198.51.100.{i + 1}", severity=2 + i % 3)
        db.add(IpBan(ip=f"203.0.113.{i + 1}", reason="test ban", severity=3, banned_until=utcnow() + timedelta(hours=2)))
        db.add(Job(kind="noop", payload={}, status=JobStatus.FAILED if i % 2 else JobStatus.QUEUED))
        db.add(EmailOutbox(to_email=f"x{i}@example.com", subject="Hello", text_body="Hi", status=EmailStatus.FAILED))
        db.add(AICallLog(feature="coach", provider="gateway", model="gemma", ok=bool(i % 2), latency_ms=100 + i, error=None))
        db.add(Transaction(gym_id=gym.id, direction=TxDirection.INCOMING, category=TxCategory.SUBSCRIPTION,
                           status=TxStatus.SUCCESS, amount=Decimal("999.00"), currency="INR", description="Plan",
                           provider="test", completed_at=utcnow()))
        product = Product(gym_id=gym.id, name=f"Whey {i}", slug=f"whey-{i}", description="Protein", price=Decimal("1999"),
                          currency="INR", stock=2)
        db.add(product)
        order = ShopOrder(order_number=f"ORD-{gym.id}-{i}", gym_id=gym.id, customer_id=cust.id, customer_label="Test",
                          status=OrderStatus.PAID, subtotal=Decimal("1999"), total=Decimal("1999"), currency="INR",
                          paid_at=utcnow(), fulfillment="pickup")
        db.add(order)
        await db.flush()
        db.add(Refund(order_id=order.id, amount=Decimal("100"), reason="Damaged", status=RefundStatus.REQUESTED))
        db.add(WorkoutSession(customer_id=cust.id, title="Push day", status="active" if i % 2 else "completed"))
    await db.commit()


# ------------------------------------------------------------------ access control
@pytest.mark.parametrize("path", PAGES + ["/superadmin/security/ip/203.0.113.9", "/superadmin/reviews/branches/1"])
async def test_anonymous_is_sent_to_login(client, path):
    r = await client.get(path)
    assert r.status_code == 303 and r.headers["location"].startswith("/auth/login?next=")


@pytest.mark.parametrize("role", [Role.CUSTOMER, Role.OWNER, Role.STAFF])
async def test_other_roles_are_forbidden(client, db, role):
    user = await make_user(db, role=role)
    await login_as(client, user)
    for path in PAGES:
        assert (await client.get(path)).status_code == 403, path
    for path, data in [("/superadmin/bans", {"ip": "203.0.113.50", "duration": "1h", "severity": "3", "reason": "test reason"}),
                       ("/superadmin/security/triage", {}), ("/superadmin/ai/test", {}),
                       ("/superadmin/outbox/retry-failed", {}), (f"/superadmin/users/{user.id}/unlock", {}),
                       (f"/superadmin/ai/cooldowns/{user.id}/lift", {"next": "/superadmin/ai"})]:
        assert (await post_form(client, path, data)).status_code == 403, path
    assert (await client.get("/superadmin/files/branch-doc/1")).status_code == 403
    assert IpBan.__tablename__ and (await db.scalar(select(func.count()).select_from(IpBan))) == 0


async def test_every_page_renders_on_an_empty_platform(admin_client):
    for path in PAGES:
        r = await admin_client.get(path)
        assert r.status_code == 200, (path, r.text[:500])
        assert 'aria-label="Super-admin sections"' in r.text
        assert "<script>" not in r.text.replace('<script nonce', '')  # no inline script bodies besides the theme init
    home = await admin_client.get("/superadmin")
    assert "All clear" in home.text and "Platform overview" in home.text


# ------------------------------------------------------------------ overview
async def test_overview_shows_kpis(admin_client, db):
    await seed_platform(db, 3)
    r = await admin_client.get("/superadmin")
    assert r.status_code == 200
    text = r.text
    assert "verification review" in text and "e-mails failed to send" in text
    assert "₹2,997.00" in text  # 3 × 999 incoming in the last 30 days
    assert "Refunds pending" in text and "Live AI coaching" in text
    assert "Gemma (gateway)" in text


async def test_overview_query_count_is_constant(admin_client, db):
    await seed_platform(db, 2)
    await admin_client.get("/superadmin")  # warm-up (session cache)
    with QueryCounter() as small:
        assert (await admin_client.get("/superadmin")).status_code == 200
    await seed_platform(db, 6)
    with QueryCounter() as big:
        assert (await admin_client.get("/superadmin")).status_code == 200
    assert big.count == small.count, (small.count, big.count)
    assert big.count <= 25


# ------------------------------------------------------------------ review queue: branches
async def _branch_in_review(db, status: VerificationStatus = VerificationStatus.NEEDS_REVIEW):
    owner = await make_owner(db)
    gym = await make_gym(db, owner, name="Forge Fitness")
    branch = await make_branch(db, gym, name="Koramangala", verified=False)
    branch.verification_status = status
    branch.verification_summary = "Fire certificate could not be read by OCR."
    branch.verification_checks = [{"check": "fire", "label": "Fire safety certificate", "status": "fail", "passed": False,
                                   "detail": "Not legible"}]
    stored, _ = await storage.save_private_file(png_bytes(text="FIRE CERT"), kind="branch_docs")
    db.add(BranchDocument(branch_id=branch.id, doc_type=BranchDocType.FIRE_SAFETY_CERTIFICATE, file_key=stored.key,
                          original_filename="fire.png", content_type=stored.content_type, sha256=stored.sha256,
                          size_bytes=stored.size))
    db.add(VerificationRun(branch_id=branch.id, outcome=status, summary="AI unavailable", checks=branch.verification_checks))
    await db.commit()
    return owner, gym, branch


async def test_branch_review_queue_and_approve(admin_client, db, admin):
    owner, gym, branch = await _branch_in_review(db)
    queue = await admin_client.get("/superadmin/reviews")
    assert queue.status_code == 200 and "Koramangala" in queue.text and "1 failed check" in queue.text
    page = await admin_client.get(f"/superadmin/reviews/branches/{branch.id}")
    assert page.status_code == 200
    assert "Fire safety certificate" in page.text and "/superadmin/files/branch-doc/" in page.text
    assert "AI can make mistakes" in page.text

    bad = await post_form(admin_client, f"/superadmin/reviews/branches/{branch.id}/decide", {"decision": "maybe", "note": ""})
    assert bad.status_code == 400 and "Choose whether to approve or reject" in bad.text and "Write a short note" in bad.text

    r = await post_form(admin_client, f"/superadmin/reviews/branches/{branch.id}/decide",
                        {"decision": "approve", "note": "Certificates checked by hand - all valid."})
    assert r.status_code == 303 and "approved" in flash_text(r)
    await db.refresh(branch)
    assert branch.verification_status == VerificationStatus.VERIFIED
    run = (await db.execute(select(VerificationRun).where(VerificationRun.branch_id == branch.id, VerificationRun.manual.is_(True)))).scalar_one()
    assert run.reviewer_id == admin.id and run.outcome == VerificationStatus.VERIFIED
    titles = (await db.execute(select(Notification.title).where(Notification.user_id == owner.id))).scalars().all()
    assert titles, "the owner must be notified"
    assert await db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "branch.review_approve")) == 1
    # no longer decidable
    again = await post_form(admin_client, f"/superadmin/reviews/branches/{branch.id}/decide", {"decision": "reject", "note": "Changed my mind"})
    assert again.status_code == 400 and "not waiting for a review" in again.text


async def test_branch_review_reject_from_ai_unavailable(admin_client, db):
    owner, gym, branch = await _branch_in_review(db, VerificationStatus.AI_UNAVAILABLE)
    r = await post_form(admin_client, f"/superadmin/reviews/branches/{branch.id}/decide",
                        {"decision": "reject", "note": "The fire certificate expired in 2023."})
    assert r.status_code == 303
    await db.refresh(branch)
    assert branch.verification_status == VerificationStatus.REJECTED
    assert (await admin_client.get("/superadmin/reviews/branches/999999")).status_code == 404


# ------------------------------------------------------------------ review queue: employees
async def _employee_in_review(db):
    owner = await make_owner(db)
    gym = await make_gym(db, owner, name="Harbor Strength")
    staff = await make_user(db, role=Role.STAFF, full_name="Kabir Rao")
    emp = await make_employee(db, gym, user=staff, status=EmploymentStatus.PENDING_VERIFICATION)
    emp.verification_status = VerificationStatus.NEEDS_REVIEW
    emp.verification_summary = "Could not confirm the certification automatically - the gym admin needs to review it."
    stored, _ = await storage.save_private_file(png_bytes(text="ACE CERT"), kind="employee_docs")
    db.add(EmployeeDocument(employee_id=emp.id, doc_type="certification", file_key=stored.key, original_filename="ace.png",
                            content_type=stored.content_type, sha256=stored.sha256, size_bytes=stored.size,
                            assessment={"name_match": False, "method": "ocr"}))
    db.add(VerificationRun(employee_id=emp.id, outcome=VerificationStatus.NEEDS_REVIEW,
                           checks=[{"check": "Certification: ace.png", "passed": False, "detail": "Name not found"}]))
    await db.commit()
    return owner, staff, emp


async def test_employee_review_approve_notifies_and_requeues_activation(admin_client, db, admin):
    owner, staff, emp = await _employee_in_review(db)
    queue = await admin_client.get("/superadmin/reviews?tab=staff")
    assert "Kabir Rao" in queue.text
    page = await admin_client.get(f"/superadmin/reviews/employees/{emp.id}")
    assert page.status_code == 200 and "Name not found" in page.text and "/superadmin/files/employee-doc/" in page.text
    r = await post_form(admin_client, f"/superadmin/reviews/employees/{emp.id}/decide",
                        {"decision": "approve", "note": "ACE certificate verified with the issuer."})
    assert r.status_code == 303
    await db.refresh(emp)
    assert emp.verification_status == VerificationStatus.VERIFIED and emp.verified_at is not None
    run = (await db.execute(select(VerificationRun).where(VerificationRun.employee_id == emp.id, VerificationRun.manual.is_(True)))).scalar_one()
    assert run.reviewer_id == admin.id
    owner_titles = (await db.execute(select(Notification.title).where(Notification.user_id == owner.id))).scalars().all()
    staff_titles = (await db.execute(select(Notification.title).where(Notification.user_id == staff.id))).scalars().all()
    assert any("approved" in t for t in owner_titles) and any("approved" in t for t in staff_titles)
    job = (await db.execute(select(Job).where(Job.kind == "staff_activation_check"))).scalar_one()
    assert job.payload == {"employee_id": emp.id}


async def test_employee_review_reject(admin_client, db):
    owner, staff, emp = await _employee_in_review(db)
    r = await post_form(admin_client, f"/superadmin/reviews/employees/{emp.id}/decide",
                        {"decision": "reject", "note": "The certificate belongs to someone else."})
    assert r.status_code == 303
    await db.refresh(emp)
    assert emp.verification_status == VerificationStatus.REJECTED
    body = (await db.execute(select(Notification.body).where(Notification.user_id == staff.id))).scalars().all()
    assert any("belongs to someone else" in b for b in body)
    assert await db.scalar(select(func.count()).select_from(Job)) == 0  # no activation re-check on rejection


# ------------------------------------------------------------------ review queue: identity (WP-H via monkeypatch)
async def _identity_in_review(db):
    cust = await make_customer(db, full_name="Riya Kapoor", id_status=IdStatus.NEEDS_REVIEW)
    doc = IdentityDocument(user_id=cust.id, doc_type=IdDocType.PASSPORT, country_code="IN", front_key="private/identity/" + "a" * 32 + ".bin",
                           status=IdStatus.NEEDS_REVIEW, name_on_id="RIYA KAPOOR", id_number_masked="••••4321",
                           consensus_score=0.42, engines=["tesseract", "apple_vision"], ai_summary="Engines disagree on the DOB.",
                           checks=[{"check": "MRZ check digits", "passed": False, "detail": "Digit 3 invalid"}])
    db.add(doc)
    await db.commit()
    return cust, doc


async def test_identity_review_calls_wp_h_service(admin_client, db, admin, monkeypatch):
    cust, doc = await _identity_in_review(db)
    calls = []

    async def fake_review(session, document, *, reviewer, approve, note):
        calls.append((document.id, reviewer.id, approve, note))
        document.status = IdStatus.VERIFIED if approve else IdStatus.REJECTED
        from app.services.notify import notify

        await notify(session, document.user_id, "ID verified", note)

    fake = types.ModuleType("app.services.id_verification")
    fake.review_identity_document = fake_review
    monkeypatch.setitem(sys.modules, "app.services.id_verification", fake)
    import app.services as services_pkg

    monkeypatch.setattr(services_pkg, "id_verification", fake, raising=False)

    page = await admin_client.get(f"/superadmin/reviews/identity/{doc.id}")
    assert page.status_code == 200 and "RIYA KAPOOR" in page.text and "/identity/%d/front" % doc.id in page.text
    r = await post_form(admin_client, f"/superadmin/reviews/identity/{doc.id}/decide",
                        {"decision": "approve", "note": "Photo and passport match the account."})
    assert r.status_code == 303, r.text[:300]
    assert calls == [(doc.id, admin.id, True, "Photo and passport match the account.")]
    await db.refresh(doc)
    assert doc.status == IdStatus.VERIFIED
    assert await db.scalar(select(func.count()).select_from(Notification).where(Notification.user_id == cust.id)) == 1
    assert await db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "identity.review_approve")) == 1


async def test_identity_review_not_implemented_changes_nothing(admin_client, db, monkeypatch):
    cust, doc = await _identity_in_review(db)

    async def not_ready(*args, **kwargs):
        raise NotImplementedError

    fake = types.ModuleType("app.services.id_verification")
    fake.review_identity_document = not_ready
    monkeypatch.setitem(sys.modules, "app.services.id_verification", fake)
    import app.services as services_pkg

    monkeypatch.setattr(services_pkg, "id_verification", fake, raising=False)
    r = await post_form(admin_client, f"/superadmin/reviews/identity/{doc.id}/decide", {"decision": "reject", "note": "Blurry photo"})
    assert r.status_code == 303 and "not available yet" in flash_text(r)
    await db.refresh(doc)
    assert doc.status == IdStatus.NEEDS_REVIEW
    assert await db.scalar(select(func.count()).select_from(AuditLog)) == 0


# ------------------------------------------------------------------ authorised previews
async def test_private_previews_are_authorised_and_never_cached(admin_client, db, anon_client_factory):
    owner, gym, branch = await _branch_in_review(db)
    _, staff, emp = await _employee_in_review(db)
    bdoc = (await db.execute(select(BranchDocument).where(BranchDocument.branch_id == branch.id))).scalar_one()
    edoc = (await db.execute(select(EmployeeDocument).where(EmployeeDocument.employee_id == emp.id))).scalar_one()
    for url in (f"/superadmin/files/branch-doc/{bdoc.id}", f"/superadmin/files/employee-doc/{edoc.id}"):
        r = await admin_client.get(url)
        assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
        assert "no-store" in r.headers["cache-control"] and r.headers["content-disposition"].startswith("inline")
        assert r.content[:2] == b"\xff\xd8"  # decrypted JPEG
    assert (await admin_client.get("/superadmin/files/branch-doc/987654")).status_code == 404
    other = anon_client_factory("203.0.113.70")
    await login_as(other, owner)  # even the owner of the branch must use the owner routes
    assert (await other.get(f"/superadmin/files/branch-doc/{bdoc.id}")).status_code == 403
    # a document whose encrypted file vanished -> 404, never a 500
    bdoc.file_key = "private/branch_docs/" + "b" * 32 + ".bin"
    await db.commit()
    assert (await admin_client.get(f"/superadmin/files/branch-doc/{bdoc.id}")).status_code == 404


# ------------------------------------------------------------------ AI providers
async def test_ai_page_and_provider_test_with_fake_ai(admin_client, db, fake_ai):
    provider = fake_ai(responses=["OK"])
    db.add(AICallLog(feature="coach", provider="gateway", model="fake-model", ok=False, latency_ms=900, error="gateway: timeout"))
    await db.commit()
    page = await admin_client.get("/superadmin/ai")
    assert page.status_code == 200 and "How AI requests are routed" in page.text and "Fake (gateway)" in page.text
    assert "Gemma gateway backends" in page.text and "Session store" in page.text and "Demo data" in page.text

    r = await post_form(admin_client, "/superadmin/ai/test", {})
    assert r.status_code == 303 and "answered" in flash_text(r) and "OK" in flash_text(r)
    assert provider.prompts and provider.prompts[-1]["max_tokens"] <= 16
    logged = (await db.execute(select(AICallLog).where(AICallLog.feature == "selftest"))).scalars().all()
    assert len(logged) == 1 and logged[0].ok

    j = await api(admin_client, "POST", "/superadmin/ai/test")
    data = j.json()
    assert j.status_code == 200 and data["ok"] is True and data["provider"] == "gateway" and data["latency_ms"] >= 0
    assert data["disclaimer"].startswith("AI can make mistakes") and data["providers"][0]["status"] == "up"
    assert await db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "ai.selftest")) == 2


async def test_ai_provider_test_when_everything_is_down(admin_client, db, ai_down):
    r = await post_form(admin_client, "/superadmin/ai/test", {})
    assert r.status_code == 303 and "AI unavailable" in flash_text(r)
    j = await api(admin_client, "POST", "/superadmin/ai/test")
    err = j.json()["error"]
    assert j.status_code == 200 and j.json()["ok"] is False and ("health check failed" in err or "simulated outage" in err)
    assert "(None)" not in err
    page = await admin_client.get("/superadmin/ai")
    assert page.status_code == 200 and "AI is unavailable right now" in page.text  # ai_feature banner


async def test_ai_selftest_runs_in_the_admins_tab(admin_client, fake_ai):
    """The test call is tied to the tab that asked: the page_id field of the plain form, the X-Page-Id header of
    IP.ai - so closing that tab stops it like any other interactive AI call."""
    from app.ai.owner import current_owner

    seen: list[str | None] = []

    async def handler(**kwargs):
        owner = current_owner()  # the provider call runs in the request's context
        seen.append(owner.page_id if owner is not None else None)
        return "OK"

    fake_ai(handler=handler)
    r = await post_form(admin_client, "/superadmin/ai/test", {"page_id": "tab-admin-0001-selftest"})
    assert r.status_code == 303 and "answered" in flash_text(r)
    j = await api(admin_client, "POST", "/superadmin/ai/test", headers={"X-Page-Id": "tab-admin-0002-selftest"})
    assert j.status_code == 200 and j.json()["ok"] is True
    # what IP.ai.request(form.action, {form}) sends: the form as multipart FormData (with the stamped page_id field)
    m = await post_form(admin_client, "/superadmin/ai/test", {"page_id": "tab-admin-0003-selftest"},
                        files={"unused": ("unused.txt", b"", "text/plain")},
                        headers={"Accept": "application/json", "X-Page-Id": "tab-admin-0003-selftest"})
    assert m.status_code == 200 and m.json()["ok"] is True
    assert seen == ["tab-admin-0001-selftest", "tab-admin-0002-selftest", "tab-admin-0003-selftest"]


async def test_ai_selftest_refused_by_the_queue_says_why(admin_client, db, fake_ai, monkeypatch):
    from app.config import get_settings

    provider = fake_ai(responses=["OK"])
    monkeypatch.setattr(get_settings(), "ai_queue_max_per_user", 0)  # every AI request: "too many at once"
    j = await api(admin_client, "POST", "/superadmin/ai/test")
    body = j.json()
    assert j.status_code == 429 and body["code"] == "too_many" and body["retry_after"] == 5
    assert j.headers["retry-after"] == "5" and body["ai_available"] is True  # the refusal JSON, not "every provider failed"
    r = await post_form(admin_client, "/superadmin/ai/test", {})
    assert r.status_code == 303 and r.headers["location"] == "/superadmin/ai"
    assert "did not run" in flash_text(r) and "warning" in flash_text(r) and "every provider failed" not in flash_text(r)
    assert provider.prompts == []  # nothing reached a provider ...
    assert await db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "ai.selftest")) == 0


async def test_ai_selftest_stopped_because_the_admin_left_records_nothing(admin_client, db, fake_ai, monkeypatch):
    from app.ai.client import ai
    from app.ai.errors import AICancelled

    fake_ai(responses=["OK"])

    async def tab_closed(**kwargs):
        raise AICancelled("page_closed")

    monkeypatch.setattr(ai, "complete", tab_closed)
    j = await api(admin_client, "POST", "/superadmin/ai/test")
    assert j.status_code == 503 and j.json()["code"] == "cancelled"
    # not recorded as a failed provider test
    assert await db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "ai.selftest")) == 0


async def test_no_pooled_db_connection_is_held_while_the_provider_test_runs(admin_client, db, pool_probe, monkeypatch):
    """The provider test ends the transaction that loaded the admin before it pings the providers and waits for the AI
    (queue included), so a slow provider never pins a pooled connection; the call log and the audit row are written
    afterwards in a new transaction."""
    j, queued, answering = await connections_while_the_ai_works(
        lambda: api(admin_client, "POST", "/superadmin/ai/test"), pool_probe, lambda **kwargs: "OK", monkeypatch)
    assert j.status_code == 200, j.text
    assert j.json()["ok"] is True and j.json()["reply"] == "OK"
    assert (queued, answering) == (0, [0])  # no connection checked out while queued, none while the provider answered
    assert await db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "ai.selftest")) == 1


# ------------------------------------------------------------------ AI usage & abuse (docs/ai-guard-and-queue.md section 8)
PANEL = "AI usage &amp; abuse"


@pytest.fixture
def guard_on(monkeypatch):
    """The per-user AI guard as configured by default (whatever the local .env says)."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "ai_guard_enabled", True)
    monkeypatch.setattr(get_settings(), "ai_quota_scale", 1.0)


def ai_owner(user: User, *, page: str | None = None, ip: str = "198.51.100.77"):
    from app.ai.owner import AIOwner

    return AIOwner(user_id=user.id, role=user.role.value, ip=ip, session_hash=f"{user.id:064x}", page_id=page)


async def pause_ai(user: User) -> dict:
    """Two spam refusals (3 + 3 strike points) pause the account's AI for 2 minutes (app/ai/policy.py ladder)."""
    from app.ai.errors import AIRejected
    from app.ai.guard import guard

    for text in ("#" * 30, "%" * 30):
        with pytest.raises(AIRejected):
            await guard.admit(ai_owner(user), "assistant", text)
    status = await guard.status(user.id)
    assert status["cooldown_until"] is not None
    return status


def ai_call(user_id: int | None, *, feature: str = "assistant", ok: bool = True, cancelled: bool = False,
            kind: str = "user", provider: str = "gateway") -> AICallLog:
    error = None if ok else ("cancelled (page_closed)" if cancelled else "gateway: timeout")
    return AICallLog(feature=feature, provider=provider, model="gemma", ok=ok, latency_ms=120, error=error, user_id=user_id,
                     owner_kind=kind if user_id else "system", queue_ms=40, cancelled=cancelled)


def section(html: str, start: str, end: str) -> str:
    i = html.index(start)
    return html[i:html.index(end, i)]


async def test_ai_usage_panel_on_an_empty_platform(admin_client):
    r = await admin_client.get("/superadmin/ai")
    assert r.status_code == 200
    assert PANEL in r.text and 'id="usage"' in r.text and 'href="#usage"' in r.text
    for empty in ("No queue", "Language model", "Voice · Kokoro", "No AI use by signed-in people in 24 hours",
                  "No account is paused", "No AI blocks", "Nothing was stopped since the server started.",
                  "The queue refused nothing", "The guard refused nothing today.", "Who is here", "Jobs paused"):
        assert empty in r.text, empty
    assert re.search(r">0<span[^>]*> / \d+ running</span>", r.text)
    # the provider test manages its own busy state (a local cooldown refusal settles before 00-core.js's busy timer)
    assert 'data-component="sa-ai-test" data-no-busy' in r.text


def guard_panel(html: str) -> str:
    """The per-person guard's block of the panel: today's refusals by reason, pauses started and escalations."""
    return section(html, 'id="guard-refusals-title"', 'id="top-users-title"')


def mini_value(html: str, label: str) -> str:
    """The figure of the sa.mini tile called ``label``."""
    m = re.search(re.escape(label) + r"</p>\s*<p[^>]*>([^<]*)</p>", html)
    assert m, label
    return m.group(1).strip()


def bar_rows(html: str) -> list[tuple[str, int]]:
    """(label, count) of every sa.ranked_bars row, top to bottom."""
    return [(label, int(n)) for label, n in re.findall(
        r'<span class="min-w-0 truncate font-medium">([^<]+)</span>\s*<span class="shrink-0 tabular-nums muted">(\d+)</span>',
        html)]


async def test_ai_usage_panel_shows_the_guards_refusals_today(admin_client, db, guard_on, monkeypatch):
    """Next to the queue's refusals, the guard's own of today (guard.snapshot()): per reason, plus the pauses they
    started. The queue's 'busy' / 'too many' stay in their own list, so nothing is counted twice."""
    from app.ai.errors import AIRejected
    from app.ai.guard import guard
    from app.config import get_settings

    asker, greedy, spammer = [await make_customer(db, full_name=n) for n in ("Asker", "Greedy", "Spammer")]
    monkeypatch.setattr(get_settings(), "ai_quota_scale", 0.01)  # every limit becomes 1 per window: one call each
    assert await guard.admit(ai_owner(asker), "assistant", "How many squats should I do today?") is not None
    with pytest.raises(AIRejected) as dup:
        await guard.admit(ai_owner(asker), "assistant", "how many squats should I do today")
    assert await guard.admit(ai_owner(greedy), "assistant", "Plan my week please") is not None
    with pytest.raises(AIRejected) as quota:
        await guard.admit(ai_owner(greedy), "assistant", "And my diet too")
    await pause_ai(spammer)  # two spam refusals: the second starts a 2-minute pause
    with pytest.raises(AIRejected) as paused:
        await guard.admit(ai_owner(spammer), "assistant", "Can I ask now?")
    assert (dup.value.code, quota.value.code, paused.value.code) == ("duplicate", "quota", "cooldown")

    html = (await admin_client.get("/superadmin/ai")).text
    block = guard_panel(html)
    assert "today · 5" in block
    assert bar_rows(block) == [("Looked like spam", 2), ("Asked the same again", 1), ("Asked while paused", 1),
                               ("Over their limit", 1)]
    assert mini_value(block, "Pauses started") == "1" and mini_value(block, "Escalated") == "0"
    queue = section(html, 'id="saved-title"', 'id="guard-refusals-title"')
    assert "The queue refused nothing since the server started." in queue  # the guard's refusals are not the queue's


async def test_ai_usage_panel_shows_escalations_and_where_the_counts_come_from(admin_client, monkeypatch):
    from app.ai.guard import guard

    async def snapshot() -> dict:
        return {"refused": {"quota": 7, "duplicate": 0, "spam": 3, "cooldown": 12}, "cooldowns_started_today": 4,
                "escalations_today": 2, "day": "2026-11-10", "backend": "memory"}

    monkeypatch.setattr(guard, "snapshot", snapshot)
    block = guard_panel((await admin_client.get("/superadmin/ai")).text)
    assert bar_rows(block) == [("Asked while paused", 12), ("Over their limit", 7), ("Looked like spam", 3)]  # no zeros
    assert "today · 22" in block
    assert mini_value(block, "Pauses started") == "4" and mini_value(block, "Escalated") == "2"
    assert 'href="/superadmin/security?category=ai_abuse"' in block  # escalations open the AI-abuse events
    assert "2026-11-10 (UTC), this server process only (no Redis)" in block

    async def broken() -> dict:
        raise RuntimeError("redis and memory both gone")

    monkeypatch.setattr(guard, "snapshot", broken)
    r = await admin_client.get("/superadmin/ai")  # the rest of the panel still renders
    assert r.status_code == 200 and "The guard&#39;s counters are unavailable right now." in guard_panel(r.text)


async def test_ai_usage_panel_shows_queue_people_cooldowns_and_blocks(admin_client, db, monkeypatch, guard_on):
    from app.ai.errors import AICancelled
    from app.ai.scheduler import scheduler
    from app.config import get_settings
    from app.services.presence import presence

    heavy = await make_customer(db, full_name="Heavy Hitter")
    light = await make_user(db, role=Role.STAFF, full_name="Light Trainer")
    spammer = await make_customer(db, full_name="Spammy Sam")
    patient = await make_customer(db, full_name="Patient Pat")
    db.add_all([ai_call(heavy.id) for _ in range(3)] + [
        ai_call(heavy.id, feature="coach", kind="job"), ai_call(heavy.id, ok=False),
        ai_call(heavy.id, ok=False, cancelled=True), ai_call(light.id, feature="intake"),
        ai_call(None, feature="ocr"),  # system work: nobody's usage
    ])
    add_event(db, "198.51.100.77", category=ThreatCategory.AI_ABUSE, severity=2, user_id=spammer.id, action="blocked",
              path="/ai/assistant", detail=f"AI paused 1 h for user #{spammer.id} after 20 strike points in 15 min (spam)")
    add_event(db, "198.51.100.78", category=ThreatCategory.RATE_LIMIT, severity=2, path="/api/assistant/chat",
              detail="rate limit 'ai' exceeded (31 rejections/min)")
    add_event(db, "198.51.100.79", category=ThreatCategory.RATE_LIMIT, severity=2, detail="rate limit 'global' exceeded (40 rejections/min)")
    add_event(db, "198.51.100.80", category=ThreatCategory.SQLI, severity=4, detail="UNION SELECT in query")
    db.add_all([Job(kind="coach_plan", payload={}, status=JobStatus.PAUSED, user_id=heavy.id),
                Job(kind="coach_plan", payload={}, status=JobStatus.CANCELLED, user_id=heavy.id)])
    await db.commit()
    await pause_ai(spammer)

    # the AI queue right now (this process): a closed tab stopped one call, one runs, one person waits for the slot
    monkeypatch.setattr(get_settings(), "ai_queue_concurrency", 1)
    stopped = await scheduler.acquire(ai_owner(heavy, page="tab-heavy-0001"), "assistant")
    assert scheduler.cancel(ticket_id=stopped.id, reason="page_closed") == 1
    scheduler.release(stopped, outcome="cancelled")
    running = await scheduler.acquire(ai_owner(light, page="tab-light-0001"), "intake")
    waiter = asyncio.create_task(scheduler.acquire(ai_owner(patient, page="tab-patient-0001"), "assistant", max_wait=30))
    for _ in range(100):
        if scheduler.snapshot()["lanes"]["llm"]["queued"]:
            break
        await asyncio.sleep(0.01)
    monkeypatch.setattr(presence, "snapshot", lambda: {
        "process": "testhost:4242:abc123", "users": 3, "sockets": 4, "pages": 4, "grace_timers": 1, "away": 2,
        "shutting_down": False, "stats": {"pages_closed": 5, "exits": 2, "logouts": 1, "returns": 1, "jobs_resumed": 1}})
    try:
        r = await admin_client.get("/superadmin/ai")
    finally:
        scheduler.cancel(user_id=patient.id, reason="left")
        with pytest.raises(AICancelled):
            await waiter
        scheduler.release(running, outcome="done")
    assert r.status_code == 200, r.text[:500]
    html = r.text

    queue = section(html, 'id="queue-title"', 'id="top-users-title"')
    assert re.search(r">1<span[^>]*> / 1 running</span>", queue)  # the language-model lane: its one slot is taken
    assert "1 waiting" in queue and "Patient Pat" in queue  # ... and who waits longest
    assert f"/superadmin/users?q=IP-{patient.id:06d}&amp;state=all" in queue
    assert "Tab closed" in queue and "Stopped" in queue  # compute saved: why work stopped

    top = section(html, 'id="top-users-title"', 'id="cooldowns-title"')
    assert top.index("Heavy Hitter") < top.index("Light Trainer")  # most calls first
    assert f"/superadmin/users?q=IP-{heavy.id:06d}&amp;state=all" in top and ">Member<" in top and ">Staff<" in top
    assert "Mostly assistant" in top and "1 in background jobs" in top and "1 failed" in top and "1 stopped" in top
    assert "Spammy Sam" not in top and "Patient Pat" not in top  # no AI calls logged for them

    paused = section(html, 'id="cooldowns-title"', 'id="blocks-title"')
    assert "Spammy Sam" in paused and "1 paused" in paused and "level 1" in paused
    assert "Because of messages that looked like spam." in paused
    assert f'action="/superadmin/ai/cooldowns/{spammer.id}/lift"' in paused and 'name="csrf_token"' in paused

    blocks = section(html, 'id="blocks-title"', 'id="presence-title"')
    assert f"AI paused 1 h for user #{spammer.id}" in blocks and "exceeded (31 rejections/min)" in blocks
    assert "&#39;global&#39;" not in blocks and "UNION SELECT" not in blocks  # only AI blocks
    assert "/superadmin/security/ip/198.51.100.77" in blocks and ">AI abuse<" in blocks and "Spammy Sam" in blocks
    assert "/superadmin/security?category=ai_abuse" in blocks

    here = section(html, 'id="presence-title"', "</section>")
    assert "testhost:4242:abc123" in html and "4 tabs · 4 sockets" in here and "Jobs paused" in here


async def test_ai_page_query_count_is_constant(admin_client, db, guard_on):
    async def seed(n: int, start: int) -> None:
        for i in range(start, start + n):
            person = await make_customer(db)
            db.add_all([ai_call(person.id), ai_call(person.id, ok=False), ai_call(person.id, ok=False, cancelled=True),
                        ai_call(person.id, feature="coach", kind="job")])
            add_event(db, f"198.51.100.{i}", category=ThreatCategory.AI_ABUSE, severity=2, user_id=person.id,
                      action="blocked", detail=f"AI paused 1 h for user #{person.id}")
            add_event(db, f"198.51.101.{i}", category=ThreatCategory.RATE_LIMIT, severity=2,
                      detail="rate limit 'ai' exceeded (9 rejections/min)")
            await db.commit()
            await pause_ai(person)

    await admin_client.get("/superadmin/ai")  # warm-up (session cache)
    with QueryCounter() as empty:
        assert (await admin_client.get("/superadmin/ai")).status_code == 200
    await seed(1, 10)
    with QueryCounter() as small:
        assert (await admin_client.get("/superadmin/ai")).status_code == 200
    await seed(6, 20)
    with QueryCounter() as big:
        page = await admin_client.get("/superadmin/ai")
    assert page.status_code == 200 and page.text.count("/lift") == 7
    assert empty.count == small.count == big.count, (empty.count, small.count, big.count)
    assert big.count <= 20


async def test_lift_ai_cooldown_is_audited_and_restores_ai(admin_client, db, admin, guard_on):
    from app.ai.guard import guard

    member = await make_customer(db, full_name="Paused Paula")
    before = await pause_ai(member)
    url = f"/superadmin/ai/cooldowns/{member.id}/lift"
    assert (await admin_client.post(url, data={"next": "/superadmin/ai"})).status_code == 403  # CSRF token required
    assert (await guard.status(member.id))["cooldown_until"] is not None

    r = await post_form(admin_client, url, {"next": "/superadmin/ai#usage"})
    assert r.status_code == 303 and r.headers["location"] == "/superadmin/ai#usage"
    assert "AI pause of Paused Paula lifted" in flash_text(r)
    status = await guard.status(member.id)
    assert status["cooldown_until"] is None and status["strikes"] == 0
    assert await guard.active_cooldowns() == []
    assert await guard.admit(ai_owner(member), "assistant", "How many sets of squats today?") is not None  # AI works again
    row = (await db.execute(select(AuditLog).where(AuditLog.action == "ai.cooldown_lift"))).scalar_one()
    assert (row.actor_id, row.entity_type, row.entity_id) == (admin.id, "user", member.id)
    assert row.detail["cooldown_until"] == before["cooldown_until"] and row.detail["strikes"] == before["strikes"]
    assert row.detail["level"] == 1 and "spam" in row.detail["reason"]

    # nothing left to lift: an info flash and no second audit row; an unsafe "next" falls back to the AI page
    again = await post_form(admin_client, url, {"next": "https://evil.example/"})
    assert again.status_code == 303 and again.headers["location"] == "/superadmin/ai#usage"
    assert "no AI pause or strikes to lift" in flash_text(again)
    assert await db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "ai.cooldown_lift")) == 1
    paused = section((await admin_client.get("/superadmin/ai")).text, 'id="cooldowns-title"', 'id="blocks-title"')
    assert "No account is paused" in paused and "Paused Paula" not in paused


@pytest.mark.parametrize("role", [Role.CUSTOMER, Role.OWNER, Role.STAFF])
async def test_only_superadmins_lift_ai_cooldowns(client, db, role, guard_on):
    from app.ai.guard import guard

    other = await make_customer(db)
    user = await make_user(db, role=role)
    for person in (other, user):
        await pause_ai(person)
    await login_as(client, user)
    for person in (other, user):  # nobody else's pause - and not their own either
        r = await post_form(client, f"/superadmin/ai/cooldowns/{person.id}/lift", {"next": "/superadmin/ai"})
        assert r.status_code == 403
        assert (await guard.status(person.id))["cooldown_until"] is not None
    assert await db.scalar(select(func.count()).select_from(AuditLog)) == 0


async def test_security_filter_accepts_ai_abuse(admin_client, db):
    add_event(db, "198.51.100.90", category=ThreatCategory.AI_ABUSE, severity=2, action="blocked", path="/ai/assistant",
              detail="AI paused 1 h for user #9 after 20 strike points in 15 min (spam)")
    add_event(db, "198.51.100.91", category=ThreatCategory.SQLI, severity=3)
    await db.commit()
    r = await admin_client.get("/superadmin/security", params={"category": "ai_abuse"})
    assert r.status_code == 200 and "Unknown category." not in r.text
    assert "AI paused 1 h for user #9" in r.text and "198.51.100.91" not in r.text
    assert 'value="ai_abuse"' in r.text and "AI abuse" in r.text
    home = await admin_client.get("/superadmin")
    assert "/superadmin/security?category=ai_abuse" in home.text and "AI abuse" in home.text


async def test_cancelled_ai_calls_are_not_provider_failures(admin_client, db, fake_ai):
    from app.routers.superadmin import _provider_rows

    fake_ai(responses=["OK"])
    db.add_all([ai_call(7) for _ in range(4)] + [ai_call(7, ok=False)]
               + [ai_call(7, ok=False, cancelled=True) for _ in range(3)])
    await db.commit()
    gateway = next(p for p in await _provider_rows(db, utcnow() - timedelta(hours=24)) if p["name"] == "gateway")
    assert (gateway["calls_24h"], gateway["failures_24h"], gateway["cancelled_24h"]) == (8, 1, 3)
    assert gateway["error_rate"] == 20  # 1 failure in 5 completed calls - people leaving pages are not failures
    page = await admin_client.get("/superadmin/ai")
    html = page.text
    assert "8 calls · 1 failure · 3 stopped because people left in 24 h" in html
    assert "1 failed · 3 stopped" in html and ">20%</p>" in html
    log = section(html, 'id="log-title"', 'id="usage"')
    assert log.count(">Failed<") == 1 and log.count(">Stopped<") == 3
    assert "Tab closed - not a provider failure" in log
    features = section(html, 'id="feat-title"', 'id="log-title"')
    assert "1 failed</span> · 3 stopped" in features


async def test_paused_and_cancelled_jobs_are_counted(admin_client, db):
    db.add_all([Job(kind="coach_plan", payload={}, status=JobStatus.PAUSED),
                Job(kind="health_ac_note", payload={}, status=JobStatus.PAUSED),
                Job(kind="coach_plan", payload={}, status=JobStatus.CANCELLED)])
    await db.commit()
    home = await admin_client.get("/superadmin")
    assert "0 running · 2 paused · 0 failed" in home.text
    assert "Jobs paused" in home.text and "Jobs cancelled" in home.text
    check = await admin_client.get("/superadmin/selfcheck")
    assert "2 paused (requester away)" in check.text and "1 cancelled" in check.text


# ------------------------------------------------------------------ AI page: bounded aggregates (load safety)
class _Sent:
    """Every statement (with its parameters) the app sends while the block runs."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, object]] = []

    def _on(self, conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        self.sent.append((statement, parameters))

    def __enter__(self) -> "_Sent":
        from sqlalchemy import event

        from app import db as dbmod

        event.listen(dbmod.get_engine().sync_engine, "before_cursor_execute", self._on)
        return self

    def __exit__(self, *exc: object) -> None:
        from sqlalchemy import event

        from app import db as dbmod

        event.remove(dbmod.get_engine().sync_engine, "before_cursor_execute", self._on)

    def on(self, pattern: str) -> list[tuple[str, object]]:
        return [(s, p) for s, p in self.sent if re.search(pattern, s, re.S)]


async def _plan(statement: str, parameters: object) -> str:
    """EXPLAIN of a statement exactly as the page sent it (the planner may not pick a seq scan: like
    tests/test_db_timeouts.py, this proves the query can use the index on a seeded table)."""
    from sqlalchemy import text

    from app import db as dbmod

    async with dbmod.get_engine().connect() as conn:
        await conn.execute(text("SET LOCAL enable_seqscan = off"))
        rows = (await conn.exec_driver_sql("EXPLAIN " + statement, parameters)).all()
    return "\n".join(r[0] for r in rows)


async def _seed_history(db) -> None:
    """A month of AI calls, two weeks of finished jobs and three months of security events - and a little of today."""
    from sqlalchemy import insert, text

    now = utcnow()
    people = [await make_customer(db) for _ in range(3)]
    await db.execute(insert(AICallLog), [
        {"feature": ("assistant", "coach", "ocr")[i % 3], "provider": "gateway", "model": "gemma", "ok": i % 7 != 0,
         "latency_ms": 100 + i % 50, "user_id": people[i % 3].id if i % 4 else None, "owner_kind": "user",
         "queue_ms": 20, "cancelled": False, "created_at": now - timedelta(days=2 + (i % 27), minutes=i % 600)}
        for i in range(6000)
    ] + [
        {"feature": "assistant", "provider": "gateway", "model": "gemma", "ok": True, "latency_ms": 90,
         "user_id": people[i % 3].id, "owner_kind": "user", "queue_ms": 10, "cancelled": False,
         "created_at": now - timedelta(minutes=5 + i)} for i in range(40)
    ])
    await db.execute(insert(Job), [
        {"kind": "notify_members", "payload": {}, "status": (JobStatus.DONE, JobStatus.CANCELLED, JobStatus.FAILED)[i % 3],
         "finished_at": now - timedelta(days=2 + i % 12, minutes=i % 300)} for i in range(3000)
    ] + [{"kind": "coach_plan", "payload": {}, "status": JobStatus.PAUSED} for _ in range(4)])
    await db.execute(insert(SecurityEvent), [
        {"ip": f"198.18.{i // 250}.{i % 250}", "method": "GET", "path": "/x", "user_agent": "bot", "severity": 2,
         "category": (ThreatCategory.SQLI, ThreatCategory.RATE_LIMIT, ThreatCategory.XSS)[i % 3], "action": "logged",
         "detail": "rate limit 'global' exceeded" if i % 3 == 1 else "probe", "ai_reviewed": True,
         "created_at": now - timedelta(days=8 + i % 80)} for i in range(3000)
    ])
    await db.commit()
    for table in ("ai_call_logs", "jobs", "security_events", "users"):
        await db.execute(text(f"ANALYZE {table}"))
    await db.commit()


def _window_through_an_index(plan: str, table: str, column: str) -> bool:
    """The plan never scans ``table`` whole and an index condition is a range scan of the window: ``column >= ...``
    with nothing but equalities before it - whichever index the planner picks (asserting an index NAME is
    planner-dependent; the tables were ANALYZEd first). A condition led by anything else - ``user_id IS NOT NULL`` of
    the composite (user_id, created_at) index - reads the whole index, every person's calls."""
    if f"Seq Scan on {table}" in plan:
        return False
    for cond in re.findall(r"Index Cond: \((.*)\)\s*$", plan, re.M):
        terms = [term.strip("() ") for term in cond.split(") AND (")]
        for i, term in enumerate(terms):
            if re.match(rf"(\w+\.)?{column} >= ", term):
                if all(" = " in earlier for earlier in terms[:i]):
                    return True
                break
    return False


async def test_ai_page_aggregates_read_their_window_through_an_index(admin_client, db):
    """The 24 h call-log windows (ix_ai_call_logs_created_at), the jobs tiles (ix_jobs_status_finished: paused + the
    window's cancelled ones, never a GROUP BY over every job) and the AI blocks (the security events' category index)
    each read their window through an index - never a sequential scan of the table."""
    await _seed_history(db)
    await admin_client.get("/superadmin/ai")  # warm-up: the demo-account count is cached from here on
    with _Sent() as sent:
        page = await admin_client.get("/superadmin/ai")
    assert page.status_code == 200
    windows = sent.on(r"FROM ai_call_logs\b.*created_at >= ")
    assert len(windows) == 3, [s for s, _ in sent.on("ai_call_logs")]  # providers, features, top users
    for statement, parameters in windows:
        plan = await _plan(statement, parameters)
        assert _window_through_an_index(plan, "ai_call_logs", "created_at"), plan
    [(jobs_sql, jobs_params)] = sent.on(r"FROM jobs\b")
    assert "GROUP BY" not in jobs_sql and "finished_at >= " in jobs_sql
    plan = await _plan(jobs_sql, jobs_params)
    assert _window_through_an_index(plan, "jobs", "finished_at"), plan
    [(blocks_sql, blocks_params)] = sent.on(r"FROM security_events\b.*LEFT OUTER JOIN users")
    plan = await _plan(blocks_sql, blocks_params)
    assert _window_through_an_index(plan, "security_events", "created_at"), plan
    assert not sent.on(r"(?i)users\.email I?LIKE")  # the demo count came from the cache
    top = section(page.text, 'id="top-users-title"', 'id="cooldowns-title"')
    assert top.count("/superadmin/users?q=IP-") == 3  # the window's three people, joined to users after the GROUP BY


async def test_ai_page_counts_paused_jobs_and_the_windows_cancelled_ones(admin_client, db):
    now = utcnow()
    db.add_all([Job(kind="coach_plan", payload={}, status=JobStatus.PAUSED) for _ in range(3)]
               + [Job(kind="coach_plan", payload={}, status=JobStatus.CANCELLED, finished_at=now - timedelta(hours=2))
                  for _ in range(2)]
               + [Job(kind="coach_plan", payload={}, status=JobStatus.CANCELLED, finished_at=now - timedelta(days=3))
                  for _ in range(4)])
    await db.commit()
    html = (await admin_client.get("/superadmin/ai")).text
    here = section(html, 'id="presence-title"', "</section>")
    assert mini_value(here, "Jobs paused") == "3" and mini_value(here, "Jobs cancelled · 24 h") == "2"


async def test_recent_ai_blocks_cover_the_last_seven_days(admin_client, db):
    add_event(db, "198.51.100.61", category=ThreatCategory.AI_ABUSE, severity=2, action="blocked",
              detail="AI paused 1 h for user #1 after 20 strike points in 15 min (spam)", when=utcnow() - timedelta(days=1))
    add_event(db, "198.51.100.62", category=ThreatCategory.AI_ABUSE, severity=2, action="blocked",
              detail="AI paused 1 h for user #2 after 20 strike points in 15 min (spam)", when=utcnow() - timedelta(days=10))
    await db.commit()
    blocks = section((await admin_client.get("/superadmin/ai")).text, 'id="blocks-title"', 'id="presence-title"')
    assert "last 7 days" in blocks and "198.51.100.61" in blocks and "198.51.100.62" not in blocks
    assert "/superadmin/security?category=ai_abuse" in blocks  # older ones: the security centre


async def test_the_demo_account_count_is_cached_per_process(admin_client, db):
    from app.routers import superadmin as sa_router
    from app.services.selfcheck import demo_domain

    for i in range(2):
        await make_customer(db, email=f"demo{i}@{demo_domain()}")
    with _Sent() as cold:
        first = await admin_client.get("/superadmin/ai")
    assert "2 demo accounts @" in first.text and len(cold.on(r"(?i)users\.email I?LIKE")) == 1
    await make_customer(db, email=f"demo9@{demo_domain()}")
    with _Sent() as warm:
        second = await admin_client.get("/superadmin/ai")
    assert "2 demo accounts @" in second.text and not warm.on(r"(?i)users\.email I?LIKE")  # served from the cache
    assert len(warm.sent) == len(cold.sent) - len(cold.on(r"(?i)users\.email I?LIKE")) - 2  # - SAVEPOINT / RELEASE
    sa_router._AI_DEMO_ACCOUNTS.reset()  # the TTL ran out
    assert "3 demo accounts @" in (await admin_client.get("/superadmin/ai")).text


# ------------------------------------------------------------------ self-check
async def test_selfcheck_page_runs_every_check(admin_client):
    r = await admin_client.get("/superadmin/selfcheck")
    assert r.status_code == 200
    for label in ("Secret key strength", "Debug mode", "Secure cookies", "CORS origins", "Allowed hosts", "Super-admin password",
                  "E-mail (SMTP)", "Geoapify", "Payments (Cashfree)", "Tesseract OCR binary", "OCR engines",
                  "Gemma in Docker", "Claude (last-resort AI)", "Gemma gateway backends", "Voice (Kokoro TTS)",
                  "pgvector extension", "Database connectivity", "Uploads folder writable", "Built front-end assets",
                  "Generated workout videos", "Session store (Redis)", "Demo data"):
        assert label in r.text, label


async def test_selfcheck_network_probes_with_mock_transport(db, monkeypatch):
    from app.config import get_settings
    from app.services import selfcheck

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json={"this_pc_cpu_percent": 12.5, "backends": [
                {"name": "this-pc", "url": "http://127.0.0.1:8080", "online": True, "busy_slots": "1/2", "overloaded": False},
                {"name": "other-pc", "url": "http://10.0.0.2:8080", "online": False, "busy_slots": "0/2", "overloaded": False}]})
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "kokoro"}]})
        return httpx.Response(404)

    monkeypatch.setattr(selfcheck, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    monkeypatch.setattr(get_settings(), "ai_gateway_status_url", "http://gateway.test/status")
    monkeypatch.setattr(get_settings(), "tts_base_url", "http://tts.test/v1")
    results = {r.key: r for r in await selfcheck.run_selfcheck(db)}
    assert set(selfcheck.ORDER) <= set(results)
    gw = results["gateway_backends"]
    assert gw.status == "warn" and "1 of 2 online" in gw.detail and "this-pc online (1/2 busy)" in gw.detail
    assert results["tts"].status == "ok" and "kokoro" in results["tts"].detail
    assert results["database"].status == "ok" and results["pgvector"].status == "ok"
    services = await selfcheck.platform_services(db)
    assert services["gateway"]["online"] == 1 and services["tts"]["ok"] is True

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    monkeypatch.setattr(selfcheck, "HTTP_TRANSPORT", httpx.MockTransport(down))
    assert (await selfcheck.check_tts()).status == "warn"
    assert "unreachable" in (await selfcheck.check_gateway_backends()).detail.lower()
    summary = selfcheck.summarize(list(results.values()))
    assert summary["total"] == len(results) == summary["ok"] + summary["warn"] + summary["fail"]


# ------------------------------------------------------------------ users
async def test_user_search_by_every_identifier(admin_client, db):
    riya = await make_customer(db, full_name="Riya Kapoor", email="riya@example.com", username="riya.k", phone="+91 98450 11111")
    await make_customer(db, full_name="Someone Else")
    for q in ("Riya", "riya@example.com", "riya.k", "@riya.k", "98450 11111", f"IP-{riya.id:06d}", f"ip{riya.id}"):
        r = await admin_client.get("/superadmin/users", params={"q": q})
        assert r.status_code == 200 and "Riya Kapoor" in r.text and "Someone Else" not in r.text, q
    r = await admin_client.get("/superadmin/users", params={"q": "nobody-at-all"})
    assert "No one matches" in r.text


async def test_user_soft_delete_validation_and_success(admin_client, db, admin):
    cust = await make_customer(db, full_name="Delete Me")
    short = await post_form(admin_client, f"/superadmin/users/{cust.id}/delete", {"reason": "x"})
    assert short.status_code == 400 and "at least 5 characters" in short.text
    own = await post_form(admin_client, f"/superadmin/users/{admin.id}/delete", {"reason": "testing self"})
    assert own.status_code == 400 and "cannot be deleted" in own.text
    r = await post_form(admin_client, f"/superadmin/users/{cust.id}/delete", {"reason": "Fraudulent sign-up"})
    assert r.status_code == 303 and "was deleted" in flash_text(r)
    await db.refresh(cust)
    assert cust.deleted_at is not None and cust.deletion_reason == "Fraudulent sign-up" and not cust.is_active
    listing = await admin_client.get("/superadmin/users?state=deleted")
    assert "Delete Me" in listing.text and "Restore" in listing.text


async def test_user_soft_delete_blocked_shows_reasons(admin_client, db):
    owner = await make_owner(db, full_name="Busy Owner")
    gym = await make_gym(db, owner)
    await make_employee(db, gym)
    r = await post_form(admin_client, f"/superadmin/users/{owner.id}/delete", {"reason": "Owner asked to leave"})
    assert r.status_code == 400 and "can&#39;t be deleted yet" in r.text and "active employee" in r.text
    await db.refresh(owner)
    assert owner.deleted_at is None


async def test_user_restore_within_window_and_after(admin_client, db):
    cust = await make_customer(db, full_name="Come Back")
    cust.deleted_at, cust.purge_after, cust.is_active = utcnow() - timedelta(days=2), utcnow() + timedelta(days=28), False
    cust.deletion_reason = "user request"
    gone = await make_customer(db, full_name="Too Late")
    gone.deleted_at, gone.purge_after, gone.is_active = utcnow() - timedelta(days=31), utcnow() - timedelta(days=1), False
    await db.commit()
    r = await post_form(admin_client, f"/superadmin/users/{cust.id}/restore", {})
    assert r.status_code == 303 and "restored" in flash_text(r)
    await db.refresh(cust)
    assert cust.deleted_at is None and cust.purge_after is None and cust.is_active
    assert (await db.execute(select(Notification.title).where(Notification.user_id == cust.id))).scalars().all() == ["Your account has been restored"]
    late = await post_form(admin_client, f"/superadmin/users/{gone.id}/restore", {})
    assert late.status_code == 303 and "can no longer be restored" in flash_text(late)
    await db.refresh(gone)
    assert gone.deleted_at is not None
    assert (await post_form(admin_client, "/superadmin/users/424242/restore", {})).status_code == 404


async def test_user_unlock(admin_client, db):
    cust = await make_customer(db, full_name="Locked Out")
    cust.locked_until, cust.failed_login_count = utcnow() + timedelta(minutes=10), 5
    await db.commit()
    page = await admin_client.get("/superadmin/users?state=locked")
    assert "Locked Out" in page.text and "Unlock" in page.text
    r = await post_form(admin_client, f"/superadmin/users/{cust.id}/unlock", {"next": "/superadmin/users?state=locked"})
    assert r.status_code == 303 and r.headers["location"] == "/superadmin/users?state=locked"
    await db.refresh(cust)
    assert cust.locked_until is None and cust.failed_login_count == 0
    assert await db.scalar(select(func.count()).select_from(Notification).where(Notification.user_id == cust.id)) == 1
    again = await post_form(admin_client, f"/superadmin/users/{cust.id}/unlock", {"next": "https://evil.example/"})
    assert again.headers["location"] == "/superadmin/users"  # unsafe next ignored
    assert "not locked" in flash_text(again)


async def test_users_page_query_count_is_constant(admin_client, db):
    for _ in range(2):
        await make_customer(db)
    with QueryCounter() as small:
        assert (await admin_client.get("/superadmin/users?state=all")).status_code == 200
    for _ in range(8):
        await make_customer(db)
    with QueryCounter() as big:
        assert (await admin_client.get("/superadmin/users?state=all")).status_code == 200
    assert small.count == big.count


# ------------------------------------------------------------------ outbox
async def test_outbox_retry_and_safe_body(admin_client, db):
    failed = EmailOutbox(to_email="a@example.com", subject="Receipt", text_body="<script>alert(1)</script> Thanks!",
                         html_body="<img src=x onerror=alert(2)>", status=EmailStatus.FAILED, attempts=6, last_error="SMTP 550")
    sent = EmailOutbox(to_email="b@example.com", subject="Welcome", text_body="Hi", status=EmailStatus.SENT)
    db.add_all([failed, sent])
    await db.commit()
    listing = await admin_client.get("/superadmin/outbox?status=failed")
    assert "a@example.com" in listing.text and "b@example.com" not in listing.text and "SMTP 550" in listing.text
    view = await admin_client.get(f"/superadmin/outbox/{failed.id}")
    assert view.status_code == 200
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in view.text and "<script>alert(1)" not in view.text
    assert "onerror=alert(2)" not in view.text and "not rendered here" in view.text

    r = await post_form(admin_client, f"/superadmin/outbox/{failed.id}/retry", {})
    assert r.status_code == 303
    await db.refresh(failed)
    assert failed.status == EmailStatus.QUEUED and failed.attempts == 0 and failed.next_attempt_at is None
    noop = await post_form(admin_client, f"/superadmin/outbox/{sent.id}/retry", {})
    assert "Only failed" in flash_text(noop)
    db.add_all([EmailOutbox(to_email=f"c{i}@example.com", subject="S", text_body="T", status=EmailStatus.FAILED) for i in range(3)])
    await db.commit()
    bulk = await post_form(admin_client, "/superadmin/outbox/retry-failed", {})
    assert "3 failed e-mails queued again" in flash_text(bulk)
    assert await db.scalar(select(func.count()).select_from(EmailOutbox).where(EmailOutbox.status == EmailStatus.FAILED)) == 0
    assert (await admin_client.get("/superadmin/outbox/99999")).status_code == 404


# ------------------------------------------------------------------ audit log
async def test_audit_log_filters(admin_client, db, admin):
    db.add_all([
        AuditLog(actor_id=admin.id, action="security.ban_manual", entity_type="ip_ban", entity_id=1, detail={"ip": "203.0.113.5"}),
        AuditLog(actor_id=None, action="staff.status", entity_type="employee", entity_id=2, detail={"to": "active"}),
    ])
    await db.commit()
    r = await admin_client.get("/superadmin/audit?area=security")
    assert "security.ban_manual" in r.text and "staff.status" not in r.text and "Platform Admin" in r.text
    r = await admin_client.get("/superadmin/audit?q=status")
    assert "staff.status" in r.text and "System" in r.text
    bad = await admin_client.get("/superadmin/audit", params={"q": "<script>"})
    assert bad.status_code == 400 and "Use letters" in bad.text
    mine = await admin_client.get(f"/superadmin/audit?actor={admin.id}")
    assert "security.ban_manual" in mine.text and "staff.status" not in mine.text
