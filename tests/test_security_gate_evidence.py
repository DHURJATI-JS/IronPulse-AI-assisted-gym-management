"""Forged evidence never bans (hunt F39) - and outdated pages are not attacks (hunt F15, the scoring half).

Any web page, HTML e-mail or forum post can make a visitor's browser request any URL of the app or post any form to it.
Before: one cross-site ``<img src=".../favicon.svg?f=/etc/passwd">`` banned the visitor's address for 24 hours (and
PERMANENTLY the second time for a non-member), and so did 5 cross-site ``fetch(url, {method: "POST", mode: "no-cors"})``
calls (3 CSRF threat points each), a cross-site sign-in flood (429 escalation) or 60 cross-site 404s - locking out
everyone behind the same gym Wi-Fi or carrier NAT with the victim.

Now (app/security/net.py ``attributable``): what a browser was made to send is refused and logged exactly as before,
stored already reviewed ("Not attributable" - the AI triage never bans on it), but adds no threat points. Requests
without browser metadata (scripts, tools) and a scanner User-Agent on any request still score and ban.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from app.models import IpBan, SecurityEvent
from app.models.enums import ThreatCategory
from app.security import bans
from app.security.bans import NOT_ATTRIBUTABLE, ban_registry, flush_security_queue, security_events, threat_tracker
from app.security.net import attributable, request_site
from tests.conftest import make_client

CHROME = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 "
          "Safari/537.36")
# what Chrome sends for <img src="https://<app>/..."> on another site's page, and for a cross-site no-cors POST
CROSS_SITE_IMAGE = {"User-Agent": CHROME, "Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors",
                    "Sec-Fetch-Dest": "image", "Accept": "image/avif,image/webp,*/*"}
CROSS_SITE_POST = {"User-Agent": CHROME, "Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site",
                   "Sec-Fetch-Mode": "no-cors", "Content-Type": "application/x-www-form-urlencoded"}
PASSWD = "/static/img/favicon.svg?f=/etc/passwd"


def _scope(method: str = "GET", **headers: str) -> dict[str, Any]:
    return {"type": "http", "method": method, "path": "/", "client": ("198.51.100.1", 5000),
            "headers": [(k.lower().encode(), v.encode()) for k, v in {"host": "testserver", **headers}.items()]}


def test_who_made_the_browser_send_it():
    assert request_site(_scope()) == "unknown"  # a script / tool
    assert request_site(_scope(**{"Sec-Fetch-Site": "cross-site"})) == "cross-site"
    assert request_site(_scope(**{"Sec-Fetch-Site": "same-site"})) == "cross-site"
    assert request_site(_scope(**{"Sec-Fetch-Site": "none"})) == "cross-site"  # a link in a mail / chat app, a bookmark
    assert request_site(_scope(**{"Origin": "https://evil.example"})) == "cross-site"
    assert request_site(_scope(**{"Origin": "null"})) == "cross-site"  # sandboxed / opaque pages
    assert request_site(_scope(**{"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"})) == "same-origin"
    assert request_site(_scope(**{"Origin": "http://testserver", "Sec-Fetch-Site": "cross-site"})) == "cross-site"
    assert attributable(_scope(), "GET") and attributable(_scope(), "POST")
    assert not attributable(_scope(**{"Sec-Fetch-Site": "cross-site"}), "GET")
    assert not attributable(_scope(**{"Origin": "https://evil.example"}), "POST")
    # a GET from the app's own pages: a link or image in user content (a chat message, a review) can point anywhere
    assert not attributable(_scope(**{"Sec-Fetch-Site": "same-origin"}), "GET")
    assert attributable(_scope(**{"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"}), "POST")


async def test_a_cross_site_image_never_bans_the_visitor_but_a_tool_still_is(db):
    """The hunter's t_forced_ban.py: one cross-site image request banned the victim's address for 24 h; the second,
    after a super-admin lift, PERMANENTLY."""
    victim = "13.251.7.9"
    async with make_client(victim) as browser:
        assert (await browser.get("/auth/login", headers={"User-Agent": CHROME})).status_code == 200
        for _ in range(5):  # the page fires again and again
            r = await browser.get(PASSWD, headers=CROSS_SITE_IMAGE)
            assert r.status_code == 403 and "banned" not in r.text.lower()  # still refused
        r = await browser.get("/auth/login", headers={"User-Agent": CHROME})
        assert r.status_code == 200, r.text[:300]  # ... but the visitor can still sign in
    assert ban_registry.get(victim) is None and not security_events.bans
    assert threat_tracker.score(victim) == 0.0
    events = [e for e in security_events.events if e.ip == victim]
    assert events and all(e.reviewed is not None and NOT_ATTRIBUTABLE in e.reviewed for e in events)
    assert all(e.category == ThreatCategory.TRAVERSAL for e in events)
    # persisted already reviewed, with the reason in the detail: the AI triage never bans on them
    assert await flush_security_queue(db) == len(events)
    await db.commit()
    rows = (await db.execute(select(SecurityEvent).where(SecurityEvent.ip == victim))).scalars().all()
    assert rows and all(r.ai_reviewed and NOT_ATTRIBUTABLE in (r.ai_verdict or "") for r in rows)
    assert all("not counted against the address" in r.detail and r.action == "blocked" for r in rows)
    assert (await db.scalar(select(IpBan.id).where(IpBan.ip == victim))) is None

    # control: the same request from a script / tool (no browser metadata) - attributable, banned as before
    tool = "198.51.100.50"
    async with make_client(tool) as attacker:
        r = await attacker.get(PASSWD)
        assert r.status_code == 403 and "banned" in r.text.lower()
        assert (await attacker.get("/healthz")).status_code == 403
    assert ban_registry.get(tool) is not None


async def test_a_scanner_user_agent_counts_even_with_forged_browser_headers():
    """A tool can add Sec-Fetch-Site to every probe - that spares it nothing a page could forge for a browser: a scanner
    User-Agent (which no page can set for a visitor's browser) still scores and bans."""
    ip = "198.51.100.51"
    async with make_client(ip) as scanner:
        for _ in range(2):  # 2 x severity 4 (scanner UA) = 20 points
            await scanner.get("/gyms?q=1", headers={"User-Agent": "sqlmap/1.7.2#stable", "Sec-Fetch-Site": "cross-site"})
        assert (await scanner.get("/healthz")).status_code == 403
    assert ban_registry.get(ip) is not None


async def test_cross_site_no_cors_posts_never_ban_the_visitor():
    """Hunt F39, second vector: cross-site POSTs are refused as 'foreign Origin' - which was 3 threat points each."""
    victim = "198.51.100.52"
    async with make_client(victim) as browser:
        await browser.get("/healthz", headers={"User-Agent": CHROME})
        for _ in range(8):
            r = await browser.post("/tasks", content=b"title=x", headers=CROSS_SITE_POST)
            assert r.status_code == 403 and "Cross-site request blocked" in r.text
        assert (await browser.get("/healthz", headers={"User-Agent": CHROME})).status_code == 200
    assert ban_registry.get(victim) is None and threat_tracker.score(victim) == 0.0
    csrf = [e for e in security_events.events if e.ip == victim and e.category == ThreatCategory.CSRF]
    assert len(csrf) == 8 and all(e.reviewed for e in csrf)  # still logged for the security centre


async def test_a_cross_site_sign_in_flood_is_throttled_but_never_banned():
    """The sign-in bucket's 429s escalate to a ban - but a page on another site can post the sign-in form again and
    again through a visitor's browser."""
    victim = "198.51.100.53"
    async with make_client(victim) as browser:
        statuses = [(await browser.post("/auth/login", content=b"identifier=a&password=b", headers=CROSS_SITE_POST))
                    .status_code for _ in range(30)]
        assert 429 in statuses
        assert (await browser.get("/healthz", headers={"User-Agent": CHROME})).status_code == 200
    assert ban_registry.get(victim) is None and threat_tracker.score(victim) == 0.0


async def test_cross_site_not_found_requests_are_not_directory_brute_forcing(monkeypatch):
    """<img> tags on another site pointing at missing pages were '404 flood' reports (6 threat points per 30): a ban."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "rate_limit_per_minute", 1000)
    victim, tool = "198.51.100.54", "198.51.100.55"
    async with make_client(victim) as browser, make_client(tool) as attacker:
        for i in range(95):  # three floods' worth
            assert (await browser.get(f"/no-such-page-{i}", headers=CROSS_SITE_IMAGE)).status_code == 404
            await attacker.get(f"/no-such-page-{i}")  # control: a tool walking the same paths
        assert (await browser.get("/healthz", headers={"User-Agent": CHROME})).status_code == 200
    assert ban_registry.get(victim) is None
    assert ban_registry.get(tool) is not None


async def test_a_link_on_the_apps_own_pages_never_bans_whoever_clicks_it():
    """User content on the app's own pages (a chat message, a review) can carry a link to an attack URL: the click is a
    same-origin navigation - refused, never a ban."""
    ip = "198.51.100.56"
    nav = {"User-Agent": CHROME, "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
    async with make_client(ip) as browser:
        for _ in range(3):
            r = await browser.get("/gyms?q=1%27%20UNION%20SELECT%20password%20FROM%20users--", headers=nav)
            assert r.status_code == 403
        assert (await browser.get("/healthz", headers={"User-Agent": CHROME})).status_code == 200
    assert ban_registry.get(ip) is None


async def test_reports_marked_not_attributable_add_no_points():
    from starlette.requests import Request

    req = Request({"type": "http", "method": "POST", "path": "/x", "raw_path": b"/x", "query_string": b"",
                   "headers": [(b"user-agent", b"pytest")], "client": ("198.51.100.57", 5000),
                   "server": ("testserver", 80), "scheme": "http"})
    for _ in range(6):
        security_events.record_from_request(req, ThreatCategory.CSRF, 2, "foreign Origin header", attributable=False)
    assert threat_tracker.score("198.51.100.57") == 0.0 and ban_registry.get("198.51.100.57") is None
    assert len([e for e in security_events.events if e.ip == "198.51.100.57"]) == 6


async def test_the_security_centre_shows_such_events_as_not_attributable(client, db):
    """They stay visible to the super-admin (with the reason) - just never scored or triaged."""
    from app.models.enums import Role
    from tests.conftest import login_as
    from tests.factories import make_user

    async with make_client("198.51.100.58") as browser:
        assert (await browser.get(PASSWD, headers=CROSS_SITE_IMAGE)).status_code == 403
    await flush_security_queue(db)
    await db.commit()
    admin = await make_user(db, role=Role.SUPERADMIN, full_name="Platform Admin")
    await login_as(client, admin)
    for url in ("/superadmin/security", "/superadmin/security/ip/198.51.100.58"):
        r = await client.get(url)
        assert r.status_code == 200 and "Not attributable" in r.text, url


@pytest.fixture(autouse=True)
def _no_leftover_bans():
    yield
    bans.ban_registry.clear()
