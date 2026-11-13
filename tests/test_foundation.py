"""Foundation tests: security middleware, CSRF, bans, AI failover, notifications, deletion/purge,
jobs, storage, schema constraints, N+1 guard."""

from __future__ import annotations

import io
from datetime import time, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError, InvalidRequestError

from app.ai.client import AIUnavailableError, FakeProvider, ai
from app.core.utils import utcnow
from app.models import (
    BranchHours,
    ChatMessage,
    ChatThread,
    EmailOutbox,
    Gym,
    Notification,
    User,
)
from app.models.enums import NotificationCategory, Role
from app.security.bans import ban_duration, ban_registry, flush_security_queue
from tests.conftest import api, csrf_token, login_as, make_client, post_form, run_jobs
from tests.factories import make_customer, make_gym, make_owner, make_user, png_bytes


# ------------------------------------------------------------------ headers / CSP / CSRF
@pytest.fixture(autouse=True)
def _echo_route(app):
    if not any(getattr(r, "path", None) == "/__test/echo" for r in app.router.routes):
        @app.post("/__test/echo")
        async def _echo():
            return {"ok": True}


async def test_security_headers_and_unique_nonce(client):
    r1 = await client.get("/healthz")
    r2 = await client.get("/healthz")
    assert r1.status_code == 200
    csp1, csp2 = r1.headers["content-security-policy"], r2.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp1 and "object-src 'none'" in csp1
    n1 = csp1.split("'nonce-")[1].split("'")[0]
    n2 = csp2.split("'nonce-")[1].split("'")[0]
    assert n1 != n2 and len(n1) >= 16
    assert r1.headers["x-frame-options"] == "DENY"
    assert r1.headers["x-content-type-options"] == "nosniff"
    assert "geolocation=(self)" in r1.headers["permissions-policy"]


async def test_csrf_rejects_missing_token(client):
    await client.get("/healthz")
    r = await client.post("/__test/echo", data={"x": "1"})
    assert r.status_code == 403
    ok = await post_form(client, "/__test/echo", {"x": "1"})
    assert ok.status_code == 200
    ok = await api(client, "POST", "/__test/echo", json={"x": 1})
    assert ok.status_code == 200


async def test_csrf_rejects_foreign_origin(client):
    token = await csrf_token(client)
    r = await client.post("/__test/echo", data={"csrf_token": token}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


async def test_unknown_host_rejected():
    async with make_client() as c:
        r = await c.get("/healthz", headers={"Host": "evil.example"})
        assert r.status_code == 400


# ------------------------------------------------------------------ scanner / bans / rate limit
async def test_probe_and_traversal_get_blocked_then_banned(anon_client_factory, db):
    c = anon_client_factory("198.51.100.7")
    r = await c.get("/.env")
    assert r.status_code in (403, 404)
    r = await c.get("/static/../../etc/passwd")
    assert r.status_code == 403
    for _ in range(3):
        await c.get("/?q=1%27%20UNION%20SELECT%20password%20FROM%20users--")
    assert ban_registry.get("198.51.100.7") is not None
    r = await c.get("/healthz")
    assert r.status_code == 403 and "banned" in r.text.lower()
    written = await flush_security_queue(db)
    await db.commit()
    assert written > 0


async def test_scanner_user_agent_blocked(anon_client_factory):
    c = anon_client_factory("198.51.100.8")
    r = await c.get("/healthz", headers={"User-Agent": "sqlmap/1.7.2#stable"})
    assert r.status_code == 403


async def test_rate_limit_returns_429(anon_client_factory, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "rate_limit_per_minute", 30)
    c = anon_client_factory("198.51.100.9")
    codes = [(await c.get("/healthz")).status_code for _ in range(60)]
    assert 429 in codes
    r = await c.get("/healthz")
    assert r.status_code in (403, 429)


def test_ban_duration_policy():
    assert ban_duration(2, offense_count=1, has_subscription=True) == timedelta(hours=2)
    assert ban_duration(5, offense_count=4, has_subscription=True) == timedelta(hours=24)
    assert ban_duration(3, offense_count=1, has_subscription=False) == timedelta(hours=6)
    assert ban_duration(3, offense_count=2, has_subscription=False) is None  # repeat offender -> permanent


# ------------------------------------------------------------------ AI router
async def test_ai_failover_order_and_unavailable():
    calls = []
    p1 = FakeProvider("gateway", fail=True)
    p2 = FakeProvider("docker", handler=lambda **k: calls.append("docker") or '{"ok": true}')
    ai.use_providers([p1, p2])
    from pydantic import BaseModel

    class Out(BaseModel):
        ok: bool

    res = await ai.complete(feature="test", system="s", prompt="p", schema=Out)
    assert res.provider == "docker" and res.data.ok is True and calls == ["docker"]
    assert ai.status()["state"] in ("degraded", "online")

    ai.use_providers([FakeProvider("gateway", fail=True), FakeProvider("docker", fail=True)])
    with pytest.raises(AIUnavailableError):
        await ai.complete(feature="test", system="s", prompt="p")
    with pytest.raises(AIUnavailableError):
        await ai.complete(feature="test", system="s", prompt="p")
    assert ai.status()["state"] == "offline" and ai.status()["available"] is False


async def test_ai_json_repair():
    from pydantic import BaseModel

    class Out(BaseModel):
        n: int

    p = FakeProvider("gateway", responses=["not json at all", '```json\n{"n": 3,}\n```'])
    ai.use_providers([p])
    res = await ai.complete(feature="test", system="s", prompt="p", schema=Out)
    assert res.data.n == 3 and len(p.prompts) == 2


async def test_ai_status_endpoint(client, ai_down):
    for _ in range(3):
        try:
            await ai.complete(feature="t", system="s", prompt="p")
        except AIUnavailableError:
            pass
    r = await client.get("/api/ai/status")
    assert r.status_code == 200 and r.json()["available"] is False


# ------------------------------------------------------------------ notifications + e-mail
async def test_notify_creates_notification_email_and_guardian_cc(db):
    from datetime import date

    from app.models import CustomerProfile
    from app.services.notify import notify

    minor = await make_user(db, role=Role.CUSTOMER, date_of_birth=date.today().replace(year=date.today().year - 15))
    prof = await db.get(CustomerProfile, minor.id)
    prof.guardian_email = "parent@example.com"
    await db.commit()
    n = await notify(db, minor, "Plan changed", "Your plan was updated.", category=NotificationCategory.ACCOUNT, link="/me")
    await db.commit()
    assert n is not None and n.emailed
    mail = (await db.execute(select(EmailOutbox).where(EmailOutbox.user_id == minor.id))).scalar_one()
    assert mail.cc == ["parent@example.com"] and "Plan changed" in mail.subject
    from app.services.mailer import send_pending_emails

    sent = await send_pending_emails(db)
    await db.commit()
    assert sent == 1


# ------------------------------------------------------------------ deletion / reservation / purge
async def test_soft_delete_reserves_identifiers_and_purge_cascades(db):
    from app.services.accounts import identifier_conflict, purge_due_accounts, soft_delete_user

    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    cust = await make_customer(db, email="gone@example.com", phone="+91 98765 43210")
    thread = ChatThread(gym_id=gym.id, customer_id=cust.id, subject="Hi")
    db.add(thread)
    await db.flush()
    db.add(ChatMessage(thread_id=thread.id, sender_id=cust.id, sender_role="customer", body="hello"))
    await db.commit()

    await soft_delete_user(db, cust, reason="user request")
    await db.commit()
    conflict = await identifier_conflict(db, email="GONE@example.com")
    assert conflict is not None and "deleted recently" in conflict.message
    conflict = await identifier_conflict(db, phone="9876543210", country_code="IN")
    assert conflict is not None and conflict.field == "phone"
    assert await db.scalar(select(func.count(ChatMessage.id))) == 1  # data retained for 30 days

    cust.purge_after = utcnow() - timedelta(minutes=1)
    await db.commit()
    result = await purge_due_accounts(db)
    await db.commit()
    assert result["users"] == 1
    assert await db.scalar(select(func.count(ChatMessage.id))) == 0
    assert await db.scalar(select(func.count(ChatThread.id))) == 0
    # one person per e-mail, forever: the blocked-identity registry outlives the purge (app/services/trust_safety.py)
    conflict = await identifier_conflict(db, email="gone@example.com")
    assert conflict is not None and "can't be used" in conflict.message


async def test_owner_with_active_staff_cannot_delete(db):
    from app.services.accounts import AccountDeletionBlocked, soft_delete_user
    from tests.factories import make_employee

    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    await make_employee(db, gym)
    with pytest.raises(AccountDeletionBlocked):
        await soft_delete_user(db, owner, reason="test")


# ------------------------------------------------------------------ schema constraints / N+1 guard
async def test_db_enforces_14_hour_limit(db):
    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    from tests.factories import make_branch

    b = await make_branch(db, gym, hours=("06:00", "20:00"))
    await db.execute(text("DELETE FROM branch_hours WHERE branch_id = :b AND weekday = 1"), {"b": b.id})
    db.add(BranchHours(branch_id=b.id, weekday=1, opens_at=time(5, 0), closes_at=time(20, 30)))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


async def test_lazy_loading_raises(db):
    owner = await make_owner(db)
    await make_gym(db, owner)
    gym = (await db.execute(select(Gym))).scalar_one()
    with pytest.raises(InvalidRequestError):
        _ = gym.owner  # lazy="raise" - must use joinedload/selectinload


# ------------------------------------------------------------------ jobs / storage / embeddings
async def test_job_queue_retries_and_runs(db, monkeypatch):
    from app.services import jobs as jobs_mod
    from app.services.jobs import enqueue, job_handler

    # the test's own handler must not stay registered for the rest of the session (the N+1 guard lists every handler)
    monkeypatch.setattr(jobs_mod, "_handlers", dict(jobs_mod._handlers))
    monkeypatch.setattr(jobs_mod, "_exit_policies", dict(jobs_mod._exit_policies))
    seen = []

    @job_handler("test_job")
    async def _h(session, payload):
        seen.append(payload["x"])

    await enqueue(db, "test_job", {"x": 1}, dedupe_key="t1")
    await enqueue(db, "test_job", {"x": 2}, dedupe_key="t1")  # deduplicated
    await db.commit()
    assert await run_jobs() == 1 and seen == [1]


async def test_storage_strips_metadata_and_encrypts():
    from PIL import Image

    from app.services import storage

    im = Image.new("RGB", (40, 30), "red")
    exif = Image.Exif()
    exif[0x010F] = "SecretCamera"
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=exif.tobytes())
    stored = await storage.save_public_image(buf.getvalue(), kind="test")
    data = storage.path_for(stored.key).read_bytes()
    assert b"SecretCamera" not in data
    priv, plain = await storage.save_private_file(png_bytes(), kind="ids")
    raw = storage.path_for(priv.key).read_bytes()
    assert raw != plain and await storage.load_private(priv.key) == plain
    with pytest.raises(storage.UploadError):
        await storage.save_private_file(b"%PDF-1.4 /JavaScript (alert(1))", kind="ids")
    with pytest.raises(storage.UploadError):
        await storage.save_public_image(b"GIF89a not really", kind="x")


def test_embeddings_are_deterministic_and_similar():
    from app.ai.embeddings import embed

    a, b, c = embed("knee friendly squat"), embed("knee-friendly squats"), embed("chocolate cake recipe")
    dot = lambda x, y: sum(i * j for i, j in zip(x, y))  # noqa: E731
    assert embed("knee friendly squat") == a
    assert dot(a, b) > dot(a, c)


async def test_conditions_normalisation():
    from app.services.conditions import normalize_condition

    assert normalize_condition("High BP") == "hypertension"
    assert normalize_condition("asthmatic since childhood") == "asthma"
    assert normalize_condition("ACL tear left knee") == "knee_injury"
    assert normalize_condition("likes pizza") is None


async def test_login_as_helper_authenticates(client, db):
    user = await make_customer(db)
    await login_as(client, user)
    r = await client.get("/api/ai/status")
    assert r.status_code == 200
