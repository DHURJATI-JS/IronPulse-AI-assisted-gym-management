"""WP-G security centre: event filters (+ N+1 guard), per-IP drill-down, manual bans / lifting (DB + in-memory
registry), AI security triage within the ban policy (fake AI), deterministic triage when the AI is down, the
"Run AI triage now" endpoint and the stress-test script driven in-process through httpx.ASGITransport."""

from __future__ import annotations

import importlib.util
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select

from app.core.utils import utcnow
from app.db import QueryCounter
from app.models import AuditLog, IpBan, Notification, SecurityEvent
from app.models.enums import Role, ThreatCategory
from app.security.bans import BanInfo, ban_registry
from app.services import security_triage as st
from tests.conftest import api, login_as, post_form
from tests.factories import make_customer, make_gym, make_owner, make_plans, make_subscription, make_user
from tests.test_superadmin import add_event, flash_text, make_admin

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
async def admin(db):
    return await make_admin(db)


@pytest.fixture
async def admin_client(client, admin):
    await login_as(client, admin)
    return client


def ban(db, ip: str, *, severity: int = 3, hours: float | None = 2, created_by: str = "auto", user_id: int | None = None,
        offense_count: int = 1, lifted: bool = False, created_at: datetime | None = None) -> IpBan:
    b = IpBan(ip=ip, reason="test ban", severity=severity, created_by=created_by, offense_count=offense_count,
              banned_until=None if hours is None else utcnow() + timedelta(hours=hours), user_id=user_id,
              lifted_at=utcnow() if lifted else None)
    if created_at is not None:
        b.created_at = created_at
    db.add(b)
    return b


def triage_reply(*verdicts: dict) -> str:
    return json.dumps({"ips": list(verdicts)})


# ------------------------------------------------------------------ security page
async def test_security_page_filters_and_validation(admin_client, db, admin):
    yesterday = utcnow() - timedelta(days=1)
    add_event(db, "203.0.113.5", category=ThreatCategory.SQLI, severity=4, detail="UNION SELECT in query")
    add_event(db, "203.0.113.6", category=ThreatCategory.XSS, severity=2, detail="<script> tag in body", reviewed=True)
    add_event(db, "2001:db8::1", category=ThreatCategory.PROBE, severity=3, detail="probe for /.env", when=yesterday)
    await db.commit()

    async def rows(**params) -> str:
        r = await admin_client.get("/superadmin/security", params=params)
        assert r.status_code == 200, r.text[:300]
        return r.text

    text = await rows()
    assert "203.0.113.5" in text and "2001:db8::1" in text and "3 matches" in text
    text = await rows(category="xss")
    assert "&lt;script&gt; tag in body" in text and "UNION SELECT in query" not in text
    text = await rows(min_severity="4")
    assert "UNION SELECT in query" in text and "probe for /.env" not in text
    text = await rows(ip="203.0.113.6")
    assert "&lt;script&gt; tag in body" in text and "UNION SELECT" not in text
    text = await rows(ip="203.0.113.")
    assert "2 matches" in text
    text = await rows(ip="2001:DB8::1")  # canonicalised
    assert "probe for /.env" in text and "1 match" in text
    local_yesterday = yesterday.astimezone(__import__("zoneinfo").ZoneInfo(admin.timezone)).date()
    text = await rows(day=local_yesterday.isoformat())
    assert "probe for /.env" in text and "UNION SELECT in query" not in text
    text = await rows(triage="pending")
    assert "UNION SELECT in query" in text and "&lt;script&gt; tag" not in text

    bad = await admin_client.get("/superadmin/security", params={"category": "bogus", "min_severity": "9", "ip": "x' OR 1=1",
                                                                  "day": "2020-13-45", "triage": "maybe"})
    assert bad.status_code == 400
    for msg in ("Unknown category.", "Severity goes from 1 to 5.", "Enter an IP address", "Pick a valid date.",
                "Unknown triage filter."):
        assert msg in bad.text, msg


async def test_security_page_query_count_is_constant(admin_client, db):
    cust = await make_customer(db)

    async def seed(n: int, start: int) -> None:
        for i in range(n):
            e = add_event(db, f"198.51.100.{start + i}", severity=2 + i % 3, user_id=cust.id)
            e.ai_reviewed = True
            e.ai_verdict = json.dumps({"verdict": "suspicious", "source": "gateway", "rationale": "probing"})
        await db.commit()

    await seed(2, 1)
    with QueryCounter() as small:
        assert (await admin_client.get("/superadmin/security")).status_code == 200
    await seed(25, 10)
    with QueryCounter() as big:
        assert (await admin_client.get("/superadmin/security")).status_code == 200
    assert small.count == big.count, (small.count, big.count)


async def test_ip_drill_down(admin_client, db):
    cust = await make_customer(db, full_name="Seen Here")
    cust.last_login_ip = "203.0.113.44"
    add_event(db, "203.0.113.44", severity=4)
    e = add_event(db, "203.0.113.44", category=ThreatCategory.BRUTEFORCE, severity=2, detail="5 failed sign-ins", reviewed=True)
    e.ai_verdict = json.dumps({"verdict": "malicious", "action": "extend_ban", "source": "gateway", "applied": "banned",
                               "rationale": "Credential stuffing pattern"})
    ban(db, "203.0.113.44", severity=4, hours=12)
    ban(db, "203.0.113.44", severity=2, hours=1, lifted=True)
    await db.commit()
    r = await admin_client.get("/superadmin/security/ip/203.0.113.44")
    assert r.status_code == 200
    for needle in ("Banned until", "Credential stuffing pattern", "Seen Here", "Brute force", "Lift ban", "Ban history"):
        assert needle in r.text, needle
    redirect_ = await admin_client.get("/superadmin/security/ip/2001:DB8:0::7")
    assert redirect_.status_code == 307 and redirect_.headers["location"] == "/superadmin/security/ip/2001:db8::7"
    assert (await admin_client.get("/superadmin/security/ip/not-an-ip")).status_code == 404
    clean = await admin_client.get("/superadmin/security/ip/192.0.2.200")
    assert clean.status_code == 200 and "Not banned" in clean.text and "Ban this address" in clean.text


# ------------------------------------------------------------------ manual bans
async def test_manual_ban_updates_db_registry_and_audit(admin_client, db):
    ban(db, "198.51.100.77", severity=2, hours=1)  # automatic ban that the manual one replaces
    await db.commit()
    r = await post_form(admin_client, "/superadmin/bans", {"ip": "198.51.100.77", "duration": "6h", "severity": "4",
                                                           "reason": "Credential stuffing from a botnet"})
    assert r.status_code == 303 and "banned for 6 hours" in flash_text(r) and "replaces 1" in flash_text(r)
    rows = (await db.execute(select(IpBan).where(IpBan.ip == "198.51.100.77").order_by(IpBan.id))).scalars().all()
    assert rows[0].lifted_at is not None and rows[1].created_by == "superadmin" and rows[1].lifted_at is None
    info = ban_registry.get("198.51.100.77")
    assert info is not None and info.until is not None and timedelta(hours=5.9) < info.until - utcnow() <= timedelta(hours=6)
    assert await db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "security.ban_manual")) == 1

    perm = await post_form(admin_client, "/superadmin/bans", {"ip": "2001:DB8::99", "duration": "permanent", "severity": "5",
                                                              "reason": "Persistent scanner", "next": "/superadmin/security/ip/2001:db8::99"})
    assert perm.status_code == 303 and perm.headers["location"] == "/superadmin/security/ip/2001:db8::99"
    assert ban_registry.get("2001:db8::99") is not None and ban_registry.get("2001:db8::99").until is None
    # the gate now blocks that address
    blocked = await post_form(admin_client, "/superadmin/bans", {"ip": "127.0.0.1", "duration": "1h", "severity": "1", "reason": "oops me"})
    assert blocked.status_code == 400 and "banning it would lock you out" in blocked.text


async def test_manual_ban_validation(admin_client, db):
    r = await post_form(admin_client, "/superadmin/bans", {"ip": "999.1.1.1", "duration": "forever", "severity": "9", "reason": "x"})
    assert r.status_code == 400
    for msg in ("not a valid IPv4 or IPv6", "Choose how long", "Severity goes from 1", "at least 5 characters"):
        assert msg in r.text, msg
    empty = await post_form(admin_client, "/superadmin/bans", {})
    assert empty.status_code == 400 and "Enter the IP address" in empty.text
    assert await db.scalar(select(func.count()).select_from(IpBan)) == 0 and len(ban_registry) == 0


async def test_lift_ban_updates_db_registry_and_notifies(admin_client, db, admin, anon_client_factory):
    cust = await make_customer(db)
    b = ban(db, "203.0.113.90", severity=3, hours=5, user_id=cust.id)
    ban(db, "203.0.113.90", severity=2, hours=1)
    await db.commit()
    ban_registry.put("203.0.113.90", BanInfo(until=utcnow() + timedelta(hours=5), reason="test", severity=3))
    banned = anon_client_factory("203.0.113.90")
    assert (await banned.get("/healthz")).status_code == 403
    page = await admin_client.get("/superadmin/bans")
    assert "203.0.113.90" in page.text and "Active <span" in page.text
    r = await post_form(admin_client, f"/superadmin/bans/{b.id}/lift", {"note": "False positive"})
    assert r.status_code == 303 and "lifted (2 active bans)" in flash_text(r)
    admin_id, cust_id = admin.id, cust.id
    db.expire_all()
    rows = (await db.execute(select(IpBan).where(IpBan.ip == "203.0.113.90"))).scalars().all()
    assert all(x.lifted_at is not None and x.lifted_by_id == admin_id for x in rows)
    assert ban_registry.get("203.0.113.90") is None
    assert (await banned.get("/healthz")).status_code == 200
    assert (await db.execute(select(Notification.title).where(Notification.user_id == cust_id))).scalars().all() == ["Access restored"]
    assert await db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "security.ban_lift")) == 1
    again = await post_form(admin_client, f"/superadmin/bans/{b.id}/lift", {})
    assert "nothing to lift" in flash_text(again)
    assert (await post_form(admin_client, "/superadmin/bans/999999/lift", {})).status_code == 404
    past = await admin_client.get("/superadmin/bans?state=past")
    assert "203.0.113.90" in past.text and "Lifted" in past.text


# ------------------------------------------------------------------ AI triage
async def test_triage_extends_and_lifts_within_policy(db, fake_ai, admin):
    add_event(db, "198.51.100.10", severity=4, detail="UNION SELECT in query")
    add_event(db, "198.51.100.10", severity=4, detail="time-based injection")
    add_event(db, "198.51.100.11", category=ThreatCategory.RATE_LIMIT, severity=2, detail="rate limit 'global' exceeded")
    old = ban(db, "198.51.100.11", severity=2, hours=2)
    await db.commit()
    ban_registry.put("198.51.100.11", BanInfo(until=old.banned_until, reason="test", severity=2))
    seen = {}

    def handler(**kw):
        seen.update(kw)
        return triage_reply(
            {"ip": "198.51.100.10", "verdict": "malicious", "recommended_action": "extend_ban", "severity": 5,
             "rationale": "Repeated SQL injection payloads."},
            {"ip": "198.51.100.11", "verdict": "benign", "recommended_action": "lift_ban", "severity": 1,
             "rationale": "A busy but normal client."},
            {"ip": "192.0.2.1", "verdict": "malicious", "recommended_action": "extend_ban", "severity": 5,
             "rationale": "Invented by the model."},
        )

    fake_ai(handler=handler)
    report = await st.run_triage(db, actor=None)
    await db.commit()
    assert report.mode == "ai" and report.ips_reviewed == 2 and report.events_reviewed == 3
    assert "UNTRUSTED" in seen["system"] and "UNTRUSTED" in seen["prompt"] and seen["max_tokens"] <= 1500

    new = (await db.execute(select(IpBan).where(IpBan.ip == "198.51.100.10"))).scalar_one()
    assert new.created_by == "ai" and new.severity == 5 and new.banned_until is not None
    assert timedelta(hours=23) < new.banned_until - utcnow() <= timedelta(hours=24)  # first offence: 24 h max
    assert ban_registry.get("198.51.100.10") is not None
    await db.refresh(old)
    assert old.lifted_at is not None and ban_registry.get("198.51.100.11") is None
    assert await db.scalar(select(func.count()).select_from(IpBan).where(IpBan.ip == "192.0.2.1")) == 0
    events = (await db.execute(select(SecurityEvent))).scalars().all()
    assert all(e.ai_reviewed for e in events)
    verdicts = {e.ip: json.loads(e.ai_verdict) for e in events}
    assert verdicts["198.51.100.10"]["verdict"] == "malicious" and verdicts["198.51.100.10"]["applied"] == "banned"
    assert verdicts["198.51.100.11"]["applied"] == "lifted"


async def test_triage_cannot_lift_severity_5_or_touch_manual_bans(db, fake_ai):
    add_event(db, "198.51.100.20", category=ThreatCategory.RATE_LIMIT, severity=2)
    sev5 = ban(db, "198.51.100.20", severity=5, hours=20)
    add_event(db, "198.51.100.21", severity=3)
    manual = ban(db, "198.51.100.21", severity=3, hours=3, created_by="superadmin")
    add_event(db, "198.51.100.22", severity=4)
    perm = ban(db, "198.51.100.22", severity=3, hours=None)
    await db.commit()
    fake_ai(responses=[triage_reply(
        {"ip": "198.51.100.20", "verdict": "benign", "recommended_action": "lift_ban", "severity": 1, "rationale": "fine"},
        {"ip": "198.51.100.21", "verdict": "malicious", "recommended_action": "extend_ban", "severity": 5, "rationale": "bad"},
        {"ip": "198.51.100.22", "verdict": "benign", "recommended_action": "lift_ban", "severity": 1, "rationale": "fine"},
    )])
    report = await st.run_triage(db)
    await db.commit()
    actions = {a.ip: a for a in report.actions}
    assert actions["198.51.100.20"].action == "kept" and "severity is above 3" in actions["198.51.100.20"].detail
    assert actions["198.51.100.21"].action == "kept" and "super-admin" in actions["198.51.100.21"].detail
    assert actions["198.51.100.22"].action == "kept" and "permanent" in actions["198.51.100.22"].detail
    for b in (sev5, manual, perm):
        await db.refresh(b)
        assert b.lifted_at is None
    assert manual.banned_until is not None and manual.severity == 3  # untouched


async def test_triage_repeat_offender_permanent_ban_notifies_superadmins(db, fake_ai, admin):
    ban(db, "198.51.100.30", severity=3, hours=-1, created_at=utcnow() - timedelta(days=3))  # expired earlier offence
    add_event(db, "198.51.100.30", severity=4, detail="directory traversal in path")
    await db.commit()
    fake_ai(responses=[triage_reply({"ip": "198.51.100.30", "verdict": "malicious", "recommended_action": "extend_ban",
                                     "severity": 4, "rationale": "Came back with traversal attempts."})])
    report = await st.run_triage(db)
    await db.commit()
    assert report.actions[0].permanent
    newest = (await db.execute(select(IpBan).where(IpBan.ip == "198.51.100.30").order_by(IpBan.id.desc()))).scalars().first()
    assert newest.banned_until is None and newest.offense_count == 2
    titles = (await db.execute(select(Notification.title).where(Notification.user_id == admin.id))).scalars().all()
    assert titles == ["Permanent IP ban: 198.51.100.30"]
    assert ban_registry.get("198.51.100.30").until is None


async def test_triage_subscriber_is_capped_at_24_hours(db, fake_ai):
    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    plans = await make_plans(db, gym)
    cust = await make_customer(db)
    await make_subscription(db, cust, plans[4])
    for _ in range(3):
        ban(db, "198.51.100.40", severity=4, hours=-1, created_at=utcnow() - timedelta(days=2))
    add_event(db, "198.51.100.40", severity=4, user_id=cust.id)
    await db.commit()
    fake_ai(responses=[triage_reply({"ip": "198.51.100.40", "verdict": "malicious", "recommended_action": "extend_ban",
                                     "severity": 5, "rationale": "x"})])
    await st.run_triage(db)
    await db.commit()
    newest = (await db.execute(select(IpBan).where(IpBan.ip == "198.51.100.40", IpBan.lifted_at.is_(None))
                               .order_by(IpBan.id.desc()))).scalars().first()
    assert newest.had_subscription and newest.banned_until is not None
    assert newest.banned_until - utcnow() <= timedelta(hours=24)


async def test_deterministic_triage_when_ai_is_down(db, ai_down, admin):
    for detail, cat in (("UNION SELECT in query", ThreatCategory.SQLI), ("directory traversal in path", ThreatCategory.TRAVERSAL),
                        ("shell command chaining in query", ThreatCategory.COMMAND_INJECTION)):
        add_event(db, "198.51.100.50", category=cat, severity=4, detail=detail)
    add_event(db, "198.51.100.51", severity=4, detail="UNION SELECT in query")
    add_event(db, "198.51.100.51", severity=5, detail="system file access in path", category=ThreatCategory.TRAVERSAL)
    add_event(db, "198.51.100.52", severity=2, detail="SQL statement in body")
    await db.commit()
    report = await st.run_triage(db)
    await db.commit()
    assert report.mode == "rules" and report.ips_reviewed == 1 and report.events_pending == 3
    assert "AI unavailable" in report.summary()
    escalated = (await db.execute(select(IpBan).where(IpBan.ip == "198.51.100.50"))).scalar_one()
    assert escalated.created_by == "auto" and escalated.severity == 4 and escalated.banned_until is not None
    assert ban_registry.get("198.51.100.50") is not None
    reviewed = (await db.execute(select(SecurityEvent).where(SecurityEvent.ip == "198.51.100.50"))).scalars().all()
    assert all(e.ai_reviewed and json.loads(e.ai_verdict)["source"] == "rules" for e in reviewed)
    pending = (await db.execute(select(SecurityEvent).where(SecurityEvent.ip != "198.51.100.50"))).scalars().all()
    assert not any(e.ai_reviewed for e in pending)  # left for the AI
    assert await db.scalar(select(func.count()).select_from(IpBan).where(IpBan.ip == "198.51.100.51")) == 0


async def test_triage_idle_and_periodic_registration(db):
    from app.services.jobs import registered_periodic

    report = await st.run_triage(db)
    assert report.mode == "idle" and "Nothing to triage" in report.summary()
    task = registered_periodic()["security_triage"]
    assert task.every_seconds == 300


async def test_run_triage_now_endpoint(admin_client, db, fake_ai):
    add_event(db, "198.51.100.60", severity=3)
    await db.commit()
    fake_ai(responses=[triage_reply({"ip": "198.51.100.60", "verdict": "suspicious", "recommended_action": "none",
                                     "severity": 3, "rationale": "Single probe - watch it."})])
    r = await post_form(admin_client, "/superadmin/security/triage", {})
    assert r.status_code == 303 and "AI triage (gateway) reviewed 1 event(s)" in flash_text(r)
    page = await admin_client.get("/superadmin/security")
    assert "Single probe - watch it." in page.text and "AI can make mistakes" in page.text
    assert await db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "security.triage_run")) == 1
    add_event(db, "198.51.100.61", severity=3)
    await db.commit()
    j = await api(admin_client, "POST", "/superadmin/security/triage")
    data = j.json()
    assert j.status_code == 200 and data["mode"] == "ai" and data["disclaimer"].startswith("AI can make mistakes")
    idle = await api(admin_client, "POST", "/superadmin/security/triage")
    assert idle.json()["mode"] == "idle"


async def test_run_triage_now_forbidden_for_owner(client, db):
    owner = await make_owner(db)
    await login_as(client, owner)
    assert (await post_form(client, "/superadmin/security/triage", {})).status_code == 403


# ------------------------------------------------------------------ stress-test script (in-process)
def _load_stress_module():
    spec = importlib.util.spec_from_file_location("ironpulse_stress_test", ROOT / "scripts" / "stress_test.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


async def test_stress_script_public_and_mixed_in_process(app):
    stress = _load_stress_module()
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 40001))  # whitelisted in tests: no rate limit
    cfg = stress.Config(base_url="http://testserver", concurrency=4, requests=24, scenario="public", seed=7)
    result = await stress.run(cfg, transport=transport)
    assert result.total == 24 and result.server_errors == 0 and not result.errors
    assert result.statuses[200] >= 20 and result.rps > 0
    assert 0 < result.percentile(50) <= result.percentile(95) <= result.percentile(99)
    assert stress.exit_code(result, cfg) == 0 and "verdict      PASS" in stress.format_report(result, cfg)
    strict = stress.Config(base_url="http://testserver", requests=24, scenario="public", max_p95_ms=0.001)
    assert stress.exit_code(result, strict) == 1 and any("p95" in p for p in stress.failures(result, strict))
    mixed = await stress.run(stress.Config(base_url="http://testserver", concurrency=3, requests=18, scenario="mixed", seed=1),
                             transport=transport)
    assert mixed.server_errors == 0 and mixed.total == 18
    assert json.loads(json.dumps(mixed.as_dict()))["requests"] == 18


async def test_stress_script_auth_and_security_in_process(app):
    stress = _load_stress_module()
    auth = await stress.run(stress.Config(base_url="http://testserver", concurrency=2, requests=12, scenario="auth"),
                            transport=httpx.ASGITransport(app=app, client=("198.51.100.150", 40002)))
    assert auth.server_errors == 0 and set(auth.statuses) <= {200, 400, 401, 403, 429}
    security = stress.Config(base_url="http://testserver", scenario="security")
    result = await stress.run(security, transport=httpx.ASGITransport(app=app, client=("198.51.100.151", 40003)))
    assert result.checks and all(c.ok for c in result.checks), [(c.name, c.detail) for c in result.checks]
    assert result.statuses[429] >= 1 and result.statuses[403] >= len(stress.ATTACK_PROBES)
    assert stress.exit_code(result, security) == 0
    assert ban_registry.get("198.51.100.151") is not None  # the probes really tripped the gate
    # a whitelisted caller is never rate limited -> the check fails with a helpful hint
    whitelisted = await stress.run(security, transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 40004)))
    rate = next(c for c in whitelisted.checks if c.name.startswith("Rate limiting"))
    assert not rate.ok and "RATE_LIMIT_WHITELIST" in rate.detail and stress.exit_code(whitelisted, security) == 1


def test_stress_script_cli_arguments():
    stress = _load_stress_module()
    cfg = stress.parse_args(["--scenario", "api", "--concurrency", "3", "--duration", "2.5", "--max-p95-ms", "500", "--json"])
    assert cfg.scenario == "api" and cfg.duration == 2.5 and cfg.requests is None and cfg.json_output
    assert stress.parse_args([]).base_url == "http://127.0.0.1:10000" and stress.parse_args([]).requests == 200
    assert stress.main(["--scenario", "nope"]) == 2
    assert stress.main(["--base-url", "ftp://example.com"]) == 2
