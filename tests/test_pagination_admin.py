"""Super-admin console lists and aggregates stay bounded however large the platform's tables grow (pagination audit,
admin items): list counts stop at COUNT_CAP ("10,000+"), attacker-driven aggregates read at most the newest SCAN_CAP
events of a time window, event lists are keyset windows, the audit area list is a loose index scan and every page runs
a constant number of statements. Each test seeds more rows than one page (150) or shrinks the caps to prove the
bound, and checks the SQL that was sent."""

from __future__ import annotations

import html
import re
from datetime import timedelta

import pytest
from sqlalchemy import event, insert

from app import db as dbmod
from app.core.utils import utcnow
from app.db import QueryCounter
from app.models import AuditLog, Branch, EmailOutbox, IpBan, Job, SecurityEvent
from app.models.enums import EmailStatus, JobStatus, ThreatCategory, VerificationStatus
from app.routers import superadmin as sa_router
from tests.conftest import login_as
from tests.factories import make_branch, make_customer, make_gym, make_owner, make_user
from tests.test_superadmin import make_admin

N = 150


# ============================================================================================ helpers
class Statements:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def _on(self, conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        self.sent.append(statement)

    def __enter__(self) -> "Statements":
        event.listen(dbmod.get_engine().sync_engine, "before_cursor_execute", self._on)
        return self

    def __exit__(self, *exc: object) -> None:
        event.remove(dbmod.get_engine().sync_engine, "before_cursor_execute", self._on)

    def on(self, table: str) -> list[str]:
        pat = re.compile(rf"\b(FROM|JOIN)\s+{table}\b")
        return [s for s in self.sent if pat.search(s)]


@pytest.fixture
async def admin(db):
    return await make_admin(db)


@pytest.fixture
async def admin_client(client, admin):
    await login_as(client, admin)
    return client


async def get(client, url: str, status: int = 200) -> tuple[str, int]:
    with QueryCounter() as qc:
        r = await client.get(url)
    assert r.status_code == status, (url, r.status_code, r.text[:500])
    return r.text, qc.count


def links(page: str, pattern: str) -> list[str]:
    return [html.unescape(m) for m in re.findall(pattern, page)]


async def seed_events(db, n: int = N, *, ip: str | None = None, ago: timedelta = timedelta(minutes=5),
                      user_id: int | None = None, category: ThreatCategory = ThreatCategory.SQLI) -> list[int]:
    when = utcnow() - ago
    ids = list((await db.execute(insert(SecurityEvent).returning(SecurityEvent.id), [
        {"ip": ip or f"198.51.{i // 250}.{i % 250 + 1}", "method": "GET", "path": f"/probe/{i}", "user_agent": "test",
         "category": category, "severity": 2 + i % 3, "detail": f"event {i:03d}", "action": "logged",
         "ai_reviewed": False, "user_id": user_id, "created_at": when} for i in range(n)
    ])).scalars().all())
    await db.commit()
    return ids


ROW = 'class="sa-row sa-cols-events"'


# ============================================================================================ security centre
async def test_security_events_walk_150_in_keyset_windows(admin_client, db):
    ids = await seed_events(db)
    seen: list[str] = []
    counts: list[int] = []
    url: str | None = "/superadmin/security"
    with Statements() as st:
        while url:
            page, n = await get(admin_client, url)
            counts.append(n)
            assert 0 < page.count(ROW) <= sa_router.EVENTS_PER_PAGE
            assert "150 matches" in page
            seen += re.findall(r'title="(event \d+)"', page)
            older = links(page, r'href="(/superadmin/security\?before_id=\d+)" class="btn btn-secondary btn-sm" rel="next"')
            url = older[0] if older else None
    assert sorted(seen) == sorted(f"event {i:03d}" for i in range(N)) and len(seen) == N
    assert len(counts) == 4 and len(set(counts)) == 1  # the 4th window costs what the 1st does
    lists = [s for s in st.on("security_events") if "ORDER BY security_events.created_at DESC, security_events.id DESC" in s]
    assert lists and all("LIMIT" in s and "OFFSET" not in s for s in lists)
    counts_sql = [s for s in st.on("security_events") if "count(*)" in s]
    assert counts_sql and all("LIMIT" in s for s in counts_sql)  # bounded COUNT and bounded aggregates
    # back up from the last window
    last, _ = await get(admin_client, f"/superadmin/security?before_id={ids[2]}")
    assert last.count(ROW) == 2 and "back to the newest" in last
    newer = links(last, r'href="(/superadmin/security\?after_id=\d+)"')
    assert newer
    up, _ = await get(admin_client, newer[0])
    assert up.count(ROW) == sa_router.EVENTS_PER_PAGE


async def test_security_page_defaults_to_the_last_7_days(admin_client, db):
    await seed_events(db, 5)
    await seed_events(db, 3, ago=timedelta(days=10))
    page, _ = await get(admin_client, "/superadmin/security")
    assert "5 matches" in page and "last 7 days" in page
    page, _ = await get(admin_client, "/superadmin/security?range=all")
    assert "8 matches" in page and "all time" in page
    page, _ = await get(admin_client, "/superadmin/security?range=30d")
    assert "8 matches" in page
    page, _ = await get(admin_client, "/superadmin/security?range=forever", status=400)
    assert "Choose a time range" in page


async def test_security_counts_and_aggregates_stop_at_their_caps(admin_client, db, monkeypatch):
    await seed_events(db, N, ip="203.0.113.9")
    monkeypatch.setattr(sa_router, "COUNT_CAP", 100)
    monkeypatch.setattr(sa_router, "SCAN_CAP", 40)
    with Statements() as st:
        page, _ = await get(admin_client, "/superadmin/security")
    assert "100+ matches" in page and "from the latest 40 events" in page
    assert "Category counts cover the latest 40 matching events" in page
    # facets and top addresses aggregate the newest SCAN_CAP rows: the category chip counts 40, not 150
    assert re.search(r"SQL injection <span class=\"tabular-nums muted\">40</span>", page)
    assert all("LIMIT" in s for s in st.on("security_events") if "GROUP BY" in s)
    page, _ = await get(admin_client, "/superadmin")
    assert "100+" in page  # the overview KPI


async def test_security_page_statement_count_is_constant(admin_client, db):
    await seed_events(db, 3)
    await get(admin_client, "/superadmin/security")  # warm-up
    _, few = await get(admin_client, "/superadmin/security")
    ids = await seed_events(db)
    _, many = await get(admin_client, "/superadmin/security")
    _, deep = await get(admin_client, f"/superadmin/security?before_id={ids[60]}&category=sqli&range=all")
    assert few == many == deep


async def test_ip_drill_down_is_bounded(admin_client, db, monkeypatch):
    seen_user = await make_customer(db, full_name="Seen In Events")
    login_user = await make_customer(db, full_name="Logged In Here")
    login_user.last_login_ip = "203.0.113.77"
    await db.commit()
    ids = await seed_events(db, N, ip="203.0.113.77", user_id=seen_user.id)
    with Statements() as st:
        page, n_first = await get(admin_client, "/superadmin/security/ip/203.0.113.77")
    assert page.count('class="sa-row md:grid-cols-[8.5rem_minmax(0,1fr)_auto] md:items-center"') == sa_router.IP_EVENTS_PER_PAGE
    assert "Seen In Events" in page and "Logged In Here" in page
    reads = st.on("security_events")
    assert all("LIMIT" in s or "min(security_events.created_at)" in s for s in reads)
    assert any("UNION" in s for s in st.on("users"))  # accounts: one IN over two index lookups, no OR scan
    older = links(page, r'href="(/superadmin/security/ip/203\.0\.113\.77\?before_id=\d+)"')
    assert older
    deep_page, n_deep = await get(admin_client, f"/superadmin/security/ip/203.0.113.77?before_id={ids[5]}")
    assert deep_page.count('md:items-center"') >= 5 and n_first == n_deep
    monkeypatch.setattr(sa_router, "SCAN_CAP", 50)
    page, _ = await get(admin_client, "/superadmin/security/ip/203.0.113.77")
    assert "50+" in page and "latest 50 counted" in page


# ============================================================================================ overview
async def test_overview_aggregates_are_bounded_and_constant(admin_client, db, monkeypatch):
    await seed_events(db, 3)
    await get(admin_client, "/superadmin")  # warm-up
    _, few = await get(admin_client, "/superadmin")
    await seed_events(db, N)
    await db.execute(insert(IpBan), [{"ip": f"192.0.2.{i % 250}", "reason": "test", "severity": 3, "created_by": "auto",
                                      "banned_until": utcnow() + timedelta(hours=1)} for i in range(N)])
    await db.execute(insert(Job), [{"kind": "noop", "payload": {}, "status": JobStatus.FAILED} for _ in range(N)])
    await db.commit()
    monkeypatch.setattr(sa_router, "SCAN_CAP", 60)
    with Statements() as st:
        page, many = await get(admin_client, "/superadmin")
    assert few == many
    # windowed aggregates over security events read the newest SCAN_CAP rows of the window
    grouped = [s for s in st.on("security_events") if "GROUP BY" in s]
    assert grouped and all("LIMIT" in s for s in grouped)
    assert "60+ events" in page and "drawn from the latest 60" in page
    # counts on attacker-driven / log tables are bounded reads
    for table in ("ip_bans", "jobs", "email_outbox"):
        assert all("LIMIT" in s for s in st.on(table)), table
    latest = [s for s in st.on("security_events") if "security_events.severity >=" in s and "ORDER BY" in s]
    assert latest and all("max(security_events.id)" in s for s in latest)  # newest-ids window, not a full scan
    monkeypatch.setattr(sa_router, "COUNT_CAP", 100)
    page, _ = await get(admin_client, "/superadmin")
    assert "100+ background jobs failed" in html.unescape(page)


# ============================================================================================ bans / outbox / users
async def test_bans_counts_and_pages_are_bounded(admin_client, db, monkeypatch):
    await db.execute(insert(IpBan), [{"ip": f"192.0.2.{i % 250}", "reason": "test", "severity": 3, "created_by": "auto",
                                      "banned_until": utcnow() + timedelta(hours=1)} for i in range(N)])
    await db.commit()
    monkeypatch.setattr(sa_router, "COUNT_CAP", 100)
    with Statements() as st:
        page, n1 = await get(admin_client, "/superadmin/bans")
    assert "of <span class=\"tabular-nums\">100+</span> bans" in page and "100+</span></a>" in page
    assert all("LIMIT" in s for s in st.on("ip_bans"))
    last, n2 = await get(admin_client, "/superadmin/bans?page=999")
    assert "Page 4 / 4+" in last and n1 == n2  # OFFSET never goes past the cap


async def test_outbox_counts_are_bounded(admin_client, db, monkeypatch):
    await db.execute(insert(EmailOutbox), [{"to_email": f"x{i}@example.com", "subject": "Hello", "text_body": "Hi",
                                            "status": EmailStatus.FAILED} for i in range(N)])
    await db.commit()
    monkeypatch.setattr(sa_router, "COUNT_CAP", 100)
    with Statements() as st:
        page, _ = await get(admin_client, "/superadmin/outbox")
    assert page.count('href="/superadmin/outbox/') >= sa_router.OUTBOX_PER_PAGE
    assert "Retry all failed (100+)" in page and "100+" in page
    assert all("GROUP BY" not in s for s in st.on("email_outbox"))
    assert all("LIMIT" in s for s in st.on("email_outbox"))


async def test_users_paging_stops_at_the_cap(admin_client, db, monkeypatch):
    for _ in range(30):
        await make_user(db, password=None)
    monkeypatch.setattr(sa_router, "COUNT_CAP", 12)
    monkeypatch.setattr(sa_router, "USERS_PER_PAGE", 5)
    with Statements() as st:
        page, n1 = await get(admin_client, "/superadmin/users?state=all")
    assert 'of <span class="tabular-nums">12+</span> accounts' in page
    assert len([s for s in st.on("users") if "count(" in s]) == 1  # the total comes from the role counts
    page, n2 = await get(admin_client, "/superadmin/users?state=all&page=50")
    assert "Page 3 / 3+" in page and n1 == n2  # paging (OFFSET) stops at the cap


# ============================================================================================ audit log
async def test_audit_areas_come_from_a_loose_index_scan(admin_client, db, monkeypatch):
    await db.execute(insert(AuditLog), [
        {"action": f"{area}.thing_{i % 7}", "entity_type": "x", "entity_id": i, "detail": {"i": i}}
        for i, area in enumerate(["security", "shop", "staff"] * 50)
    ])
    await db.commit()
    with Statements() as st:
        page, n1 = await get(admin_client, "/superadmin/audit")
    options = re.findall(r'<option value="([a-z_]+)"', page)
    assert options == ["security", "shop", "staff"]
    reads = st.on("audit_logs")
    assert any("WITH RECURSIVE" in s for s in reads) and all("GROUP BY" not in s for s in reads)
    assert all("LIMIT" in s for s in reads)
    assert page.count('<li class="sa-row sa-cols-audit">') == sa_router.AUDIT_PER_PAGE
    monkeypatch.setattr(sa_router, "COUNT_CAP", 100)
    page, n2 = await get(admin_client, "/superadmin/audit?page=9999")
    assert 'of <span class="tabular-nums">100+</span> entries' in page and "Page 3 / 3+" in page and n1 == n2
    assert page.count('<li class="sa-row sa-cols-audit">') == sa_router.AUDIT_PER_PAGE  # OFFSET 80: inside the cap


# ============================================================================================ review queue
async def test_review_queue_tabs_are_paginated_with_true_counts(admin_client, db):
    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    for _ in range(55):
        b = await make_branch(db, gym, verified=False, photos=0)
        b.verification_status = VerificationStatus.NEEDS_REVIEW
    await db.commit()
    with Statements() as st:
        page, n1 = await get(admin_client, "/superadmin/reviews")
    assert page.count('href="/superadmin/reviews/branches/') == sa_router.QUEUE_LIMIT
    assert re.search(r"Branches\s*<span[^>]*>55</span>", page)  # the true queue size, not len(list)
    assert not [s for s in st.on("employees") if "ORDER BY employees.updated_at" in s]  # other queues: counted, not listed
    assert not [s for s in st.on("identity_documents") if "ORDER BY" in s]
    branch_lists = [s for s in st.on("branches") if "ORDER BY branches.updated_at" in s]
    assert len(branch_lists) == 1 and "LIMIT" in branch_lists[0]
    page2, n2 = await get(admin_client, "/superadmin/reviews?tab=branches&page=2")
    assert page2.count('href="/superadmin/reviews/branches/') == 5 and n1 == n2
    staff, _ = await get(admin_client, "/superadmin/reviews?tab=staff")
    assert 'href="/superadmin/reviews/branches/' not in staff and 'aria-current="page"' in staff
