"""Public load-test results page (GET /load-test, app/routers/public_loadtest.py, docs/load-test-page.md "Public page")
and its "Load test results" navbar link.

Covers: the link for anonymous visitors and every signed-in role (desktop bar + mobile menu, active state), the page
for all of them, the fixture run (verdict + SLO checklist, the six headline numbers, the two SVG step charts, how it
was tested, the disclaimer), the curated scope (no endpoint table, log lines, findings, abuse / IP details, downloads or
file paths), the latest READABLE run + the picker (only published runs; anything else a 404), the empty and the
neutral "results unavailable" states (never a 500), PUBLIC_LOADTEST_PAGE=false (404 + no link), escaping, strict CSP,
no SQL beyond the layout's session check, the links between this page and /superadmin/load-test, and phone widths.

Fixture runs: tests/fixtures/loadtest/results (see tests/test_superadmin_load_page.py)."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import BASE_DIR, Settings, get_settings
from app.core.nav import LOAD_TEST_ITEM, NAV, nav_for
from app.db import QueryCounter
from app.models.enums import Role
from app.routers import public_loadtest as page
from app.security.bans import ban_registry, threat_tracker
from app.services import loadtest_results as lt
from tests.conftest import login_as
from tests.factories import make_user
from tests.test_csp_static import HANDLER_ATTR, SCRIPT_TAG, STYLE_ATTR
from tests.test_superadmin_load_page import small_summary, write_run

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "loadtest" / "results"
PASS_RUN = "20261110-1800-prod-like"
FAIL_RUN = "20261109-1200-prod-like"
URL = "/load-test"
ROLES = [Role.CUSTOMER, Role.STAFF, Role.OWNER, Role.SUPERADMIN]
TILE_LABELS = ("Max users within SLO", "Max throughput within SLO", "p95 at ", "Error rate", "Requests",
               "5xx under random junk input")
# what the super-admin page shows of the fixture run and the public page must not: the endpoint table's owner / staff /
# admin routes, ...
ROUTES = ("/owner/finance/transactions", "/owner/customers", "/superadmin/security", "/superadmin/audit",
          "/staff/clients", "/chat/[id]", "/maps/tiles", "/api/ai/status", "data-lt-endpoints")
# ... server log lines, findings, the abuse note, downloads, the target host, the kit's paths and notes, the timeline,
# the server panel, the class table and the run settings
INTERNAL = (
    "QueuePool", "statement timeout", "sqlalchemy", "data-lt-log-errors",  # server log lines
    "N+1 queries", "Database pool saturates", "Static files are served by the app server", "Eager-loaded",  # findings
    "data-lt-findings", "Login floods and scraping", "109 requests got through", "data-lt-abuse",  # abuse details
    "report.md", "locust_report", "locust.html", "/superadmin/load-test/", " download",  # downloads
    "127.0.0.1", ":12000", "loadtest/", "summary.json", "seed_loadtest", "fake gateway",  # host, paths, notes
    "data-lt-timeline", "data-lt-classes", "data-lt-server", "data-lt-settings", "data-lt-db-rows", "data-lt-error",
    "Run folder", "Pool size", "Server CPU", "security_events",
)


# ------------------------------------------------------------------ helpers / fixtures
def use_results(monkeypatch, path: Path) -> Path:
    monkeypatch.setattr(get_settings(), "loadtest_results_dir", path)
    lt.clear_caches()
    return path


@pytest.fixture(autouse=True)
def _fresh_caches():
    lt.clear_caches()
    yield
    lt.clear_caches()


@pytest.fixture
def fixtures(monkeypatch) -> Path:
    return use_results(monkeypatch, FIXTURES)


@pytest.fixture
def results(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "results"
    root.mkdir()
    return use_results(monkeypatch, root)


async def sign_in(client, db, role: Role):
    user = await make_user(db, role=role, full_name=f"{role.value.title()} Person")
    await login_as(client, user)
    return user


def desktop_nav(html: str) -> str:
    return re.search(r'<div class="hidden min-w-0 items-center gap-0\.5 lg:flex">(.*?)</div>', html, re.S).group(1)


def mobile_nav(html: str) -> str:
    return re.search(r'<div id="mobile-nav"[^>]*>(.*?)</div>\s*</div>\s*</header>', html, re.S).group(1)


def nav_link(block: str) -> str | None:
    m = re.search(r'<a href="/load-test"[^>]*>.*?</a>', block, re.S)
    return m.group(0) if m else None


def kpis(html: str) -> str:
    return re.search(r"data-lt-kpis>(.*?)</section>", html, re.S).group(1)


def tile(html: str, key: str) -> str:
    return re.search(rf'data-lt-tile="{key}">(.*?)</div>\s*</div>', kpis(html), re.S).group(1)


def picker_hrefs(html: str) -> list[str]:
    block = re.search(r"<ul[^>]*data-lt-runs>(.*?)</ul>", html, re.S)
    return re.findall(r'href="/load-test\?run=([^"&#]+)"', block.group(1)) if block else []


def csp_problems(html: str) -> list[str]:
    problems = []
    if STYLE_ATTR.search(html):
        problems.append("inline style attribute")
    if HANDLER_ATTR.search(html):
        problems.append("inline event handler")
    problems += ["script without nonce" for m in SCRIPT_TAG.finditer(html) if "nonce=" not in m.group(0)]
    bodies = [b for b in re.findall(r"<script\b(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S | re.I) if b.strip()]
    if len(bodies) > 1:  # only base.html's nonce'd theme initialiser
        problems.append(f"{len(bodies)} inline script bodies")
    return problems


def kit_summary(run_id: str, started: datetime, **changes) -> dict:
    """A summary the way the kit's analyze.py writes it today: the junk SLO row, junk senders and abusers in the
    user mix, the AI mode in the notes."""
    data = small_summary(run_id, started)
    data["target"]["notes"] = ("profile stress, AI simulated, DB pool 10+5 per worker, WebSocket per-page; seeded "
                               "with loadtest/seed_loadtest.py")
    data["target"]["db_rows"] = {"users": 22857, "gyms": 308, "branches": 2016, "security_events": 150000}
    data["machine"] = {"cpus": 10, "memory_gb": 16.0, "os": "Darwin 27.2.0 (arm64)", "python": "3.15.2",
                       "locust_version": "2.47.0"}
    data["verdict"]["slos"] += [
        {"name": "zero 5xx at 100 users (every request of the step but the junk senders')", "target": "0",
         "actual": "0 seen by clients (503: 0), 0 in server.log", "passed": True, "users": 100},
        {"name": "zero 5xx under random junk input", "target": "0",
         "actual": "0 5xx, 0 timeouts/connection errors, 0 socket errors of 690", "passed": True}]
    data["classes"] = [
        {"name": "AnonymousVisitor", "weight_share": 0.1429, "requests": 1391, "failures": 0, "p95_ms": 20.5},
        {"name": "JunkUser", "weight_share": 0.2857, "requests": 690, "failures": 0, "p95_ms": 53.8},
        {"name": "Abuser", "weight_share": 0.0714, "requests": 994, "failures": 0, "p95_ms": 36.4},
        {"name": "Member", "weight_share": 0.2143, "requests": 1133, "failures": 0, "p95_ms": 24.5}]
    data["abuse"] = {"requests": 994, "throttled": 549, "throttled_share": 0.5523, "note": "SECRET-ABUSE-NOTE"}
    data["server"] = {"errors_5xx": 0, "timeouts": 0, "log_errors": ["SECRET-LOG-LINE Traceback"]}
    data["findings"] = [{"severity": "low", "title": "SECRET-FINDING", "detail": "x", "status": "accepted"}]
    data.update(changes)
    return data


# ------------------------------------------------------------------ the navbar link: everyone, desktop + mobile
def test_every_menu_ends_with_the_load_test_link_unless_it_is_switched_off(monkeypatch):
    assert (LOAD_TEST_ITEM.label, LOAD_TEST_ITEM.href) == ("Load test results", "/load-test") and page.PAGE_PATH == URL
    users = [None] + [SimpleNamespace(role=role) for role in Role]
    for user in users:
        items = nav_for(user)
        assert items[-1] is LOAD_TEST_ITEM and items[:-1] == NAV["guest" if user is None else user.role.value]
        assert items[0].href != URL  # the logo still leads to the role's home
    monkeypatch.setattr(get_settings(), "public_loadtest_page", False)
    assert all(LOAD_TEST_ITEM not in nav_for(user) for user in users)


async def test_anonymous_visitors_see_the_link_and_the_page(client, fixtures):
    home = (await client.get("/")).text
    desktop, mobile = nav_link(desktop_nav(home)), nav_link(mobile_nav(home))
    # the whole label in the bar, never wrapped (in a crowded bar it shrinks with an ellipsis, the tooltip keeps it)
    assert desktop and '<span class="min-w-0 truncate">Load test results</span></a>' in desktop
    assert 'title="Load test results"' in desktop and "<svg" not in desktop and "nav-link-active" not in desktop
    assert mobile and "Load test results" in mobile and "<svg" in mobile  # icon + label in the mobile menu
    r = await client.get(URL)
    assert r.status_code == 200
    assert "<title>Load test results · " in r.text and "Load test results</h1>" in r.text
    assert 'data-lt-verdict="pass"' in r.text
    desktop, mobile = nav_link(desktop_nav(r.text)), nav_link(mobile_nav(r.text))
    assert 'class="nav-link min-w-0 nav-link-active"' in desktop and 'aria-current="page"' in desktop
    assert "nav-link-active" in mobile  # the active state on both menus


@pytest.mark.parametrize("role", ROLES)
async def test_every_signed_in_role_sees_the_link_and_the_page(client, db, fixtures, role):
    await sign_in(client, db, role)
    terms = (await client.get("/terms")).text
    desktop, mobile = nav_link(desktop_nav(terms)), nav_link(mobile_nav(terms))
    # beside a full menu: the icon below xl, the label from xl - always named "Load test results", never wrapped
    assert desktop and 'aria-label="Load test results"' in desktop and 'title="Load test results"' in desktop
    assert 'class="size-4 shrink-0 xl:hidden"' in desktop
    assert '<span class="hidden xl:inline min-w-0 truncate">Load test results</span>' in desktop
    assert 'class="nav-link min-w-0 ' in desktop  # the one item that may shrink in a crowded bar
    assert mobile and "Load test results" in mobile
    assert desktop_nav(terms).count("<a ") == len(NAV[role.value]) + 1  # the role's menu, then the link
    r = await client.get(URL)
    assert r.status_code == 200 and 'data-lt-verdict="pass"' in r.text and "data-lt-kpis" in r.text
    desktop = nav_link(desktop_nav(r.text))
    assert "nav-link-active" in desktop and 'aria-current="page"' in desktop
    assert "nav-link-active" in nav_link(mobile_nav(r.text))


# ------------------------------------------------------------------ the fixture run
async def test_the_latest_run_shows_verdict_numbers_charts_and_how_it_was_tested(client, fixtures):
    html = (await client.get(URL)).text
    # verdict + SLO checklist
    assert '<span class="sr-only">Verdict: </span>PASS</h2>' in html
    assert "Holds 300 concurrent users within every SLO" in html and "5 of 5 met" in html
    assert "p95 latency at 200 users" in html and "Target ≤ 800 ms" in html and html.count('aria-label="Passed"') >= 5
    # the six headline numbers (the server's own 5xx tile stays on the super-admin page)
    figures = kpis(html)
    assert len(re.findall(r"data-lt-tile=", figures)) == 6 and all(label in figures for label in TILE_LABELS)
    for key, value in [("users", "300"), ("rps", "588 req/s"), ("p95", "236 ms"), ("errors", "0.29 %"),
                       ("requests", "199.9K"), ("junk", "—")]:
        assert f'<p class="kpi mt-2">{value}</p>' in tile(html, key), (key, value)
    assert "knee at 400 users" in figures and "SLO ≤ 800 ms · met" in figures and "p95 at 200 users" in figures
    assert "no junk input in this run" in tile(html, "junk") and "5xx responses" not in figures
    # the two per-step charts (the super-admin page's SVG macros) and their table
    for uid in ("lt-p95", "lt-rps"):
        svg = re.search(rf'<svg[^>]*aria-labelledby="{uid}-t {uid}-d"[^>]*>(.*?)</svg>', html, re.S)
        assert svg and 'role="img"' in svg.group(0) and f'<title id="{uid}-t">' in svg.group(1), uid
        assert f'<desc id="{uid}-d">' in svg.group(1) and "<path" in svg.group(1), uid
    assert "SLO ≤ 800 ms · judged at 200 users" in html and "Knee: where latency climbs" in html
    assert "<title>400 users · p95 1,450 ms · 642 req/s · 1.8 % errors · failed</title>" in html
    steps = re.search(r"<table[^>]*data-lt-steps>(.*?)</table>", html, re.S).group(1)
    assert len(re.findall(r"<tr\b", steps.split("</thead>", 1)[1])) == 6 and ">Knee</span>" in steps
    assert "lt-tl-" not in html  # no timeline (server CPU, memory, connections)
    # how it was tested (anonymous: UTC)
    tested = re.search(r"data-lt-tested>(.*?)</section>", html, re.S).group(1)
    for text in ("10 Nov 2026, 18:00–18:10 UTC", "10 min", "25 → 50 → 100 → 200 → 300 → 400 users · 1 min 40 s each",
                 "10 CPUs · 32 GB memory · macOS 27.2 (arm64) - the app server and the load generator shared it",
                 "4 worker processes · production settings", "PostgreSQL · Redis",
                 "380.5K rows in 7 tables · 24,318 users · 120 gyms · 342 branches · 21,877 memberships",
                 "<dt>AI</dt><dd>Not recorded</dd>", "Locust 2.43.1"):
        assert text in tested, text
    mix = re.search(r"<ul[^>]*data-lt-mix>(.*?)</ul>", tested, re.S).group(1)
    labels = re.findall(r'<span class="min-w-0 font-medium">(.*?)</span>', mix)
    assert labels == ["Anonymous visitors", "Members", "Gym owners", "Abusers", "Trainers and staff", "Super-admins"]
    assert "34 %" in mix and mix.count('role="meter"') == 6
    # the disclaimer, linking the terms
    note = re.search(r'<div class="alert alert-warning[^"]*" role="note" data-lt-disclaimer>(.*?)</div>', html, re.S)
    assert note and "Measured on one laptop with simulated AI; not a performance guarantee." in note.group(1)
    assert "Disclaimer &amp; no liability" in note.group(1) and 'href="/terms#disclaimer"' in note.group(1)


async def test_internal_details_never_reach_the_public_page(client, db, fixtures):
    for url in (URL, f"{URL}?run={FAIL_RUN}"):
        html = (await client.get(url)).text
        assert 'data-lt-verdict="' in html
        leaked = [text for text in ROUTES + INTERNAL if text in html]
        assert not leaked, (url, leaked)
        assert "/superadmin" not in html and "data-lt-admin-link" not in html  # anonymous: no admin link either
    # a super-admin gets a link to the full report - and still none of the internals on this page (its own navbar
    # links /superadmin/security: the routes are checked outside the layout's menus)
    await sign_in(client, db, Role.SUPERADMIN)
    html = (await client.get(URL)).text
    assert re.search(rf'<a href="/superadmin/load-test\?run={PASS_RUN}" class="btn btn-secondary btn-sm" '
                     r"data-lt-admin-link>", html)
    main = re.search(r'<main id="main".*?</main>', html, re.S).group(0)
    leaked = [text for text in ROUTES if text in main] + [text for text in INTERNAL if text in html]
    assert not leaked, leaked


async def test_the_kit_s_junk_row_ai_mode_and_user_mix(client, results):
    write_run(results, "stress-1", data=kit_summary("stress-1", datetime(2026, 11, 13, 5, 30, tzinfo=UTC)))
    html = (await client.get(URL)).text
    junk = tile(html, "junk")
    assert '<p class="kpi mt-2">0</p>' in junk and "690 junk requests · target 0 · met" in junk
    assert "badge-green" in junk  # not the step's own "zero 5xx at 100 users" row
    assert "Simulated - no AI model was called" in html
    mix = re.search(r"<ul[^>]*data-lt-mix>(.*?)</ul>", html, re.S).group(1)
    assert re.findall(r'<span class="min-w-0 font-medium">(.*?)</span>', mix) == [
        "Junk senders", "Members", "Anonymous visitors", "Abusers"]
    assert "random junk input into every form, URL and API" in mix and "28.6 %" in mix
    assert "175.2K rows in 4 tables · 22,857 users · 308 gyms · 2,016 branches" in html  # no internal table names
    for secret in ("SECRET-ABUSE-NOTE", "SECRET-LOG-LINE", "SECRET-FINDING", "Traceback", "seed_loadtest",
                   "security_events", "WebSocket per-page"):
        assert secret not in html, secret
    # a junk 5xx: the count and "missed", in red
    data = kit_summary("stress-2", datetime(2026, 11, 13, 6, tzinfo=UTC))
    data["verdict"]["slos"][-1].update(actual="3 5xx, 0 timeouts/connection errors, 0 socket errors of 690",
                                       passed=False)
    write_run(results, "stress-2", data=data)
    junk = tile((await client.get(URL)).text, "junk")
    assert '<p class="kpi mt-2">3</p>' in junk and "target 0 · missed" in junk and "badge-red" in junk


async def test_an_inconclusive_run_says_so(client, results):
    data = small_summary("busy", datetime(2026, 11, 13, tzinfo=UTC), passed=False)
    data["verdict"]["headline"] = "Inconclusive: started on a busy machine"
    write_run(results, "busy", data=data)
    html = (await client.get(URL)).text
    assert 'data-lt-verdict="inconclusive"' in html and "</span>INCONCLUSIVE</h2>" in html
    assert "alert alert-warning !items-start" in html


# ------------------------------------------------------------------ which run: the latest readable one + the picker
async def test_the_latest_readable_run_is_shown_and_the_picker_offers_only_published_runs(client, results):
    write_run(results, "old-ok", data=small_summary("old-ok", datetime(2026, 11, 1, 12, tzinfo=UTC), users=100))
    write_run(results, "mid-ok", data=small_summary("mid-ok", datetime(2026, 11, 5, 12, tzinfo=UTC), users=60))
    write_run(results, "new-broken", data='{"schema": 1, "started_at": "2026-11-09T10:00:00Z", "run_id": ')
    html = (await client.get(URL)).text
    assert "05 Nov 2026, 12:00–12:10 UTC" in html and "Run mid-ok" in html  # the newest READABLE run
    assert picker_hrefs(html) == ["mid-ok", "old-ok"] and "new-broken" not in html
    assert ">Latest run<" in html and "show the latest" not in html
    older = (await client.get(f"{URL}?run=old-ok")).text
    assert "01 Nov 2026, 12:00–12:10 UTC" in older and "Run old-ok" in older
    assert '<a href="/load-test" class="font-medium text-accent hover:underline">An earlier run · show the latest</a>' in older
    current = re.findall(r'<a href="/load-test\?run=([^"]+)"[^>]*aria-current="page"', older)
    assert current == ["old-ok"]
    for bad in ("new-broken", "no-such-run", "%2e%2e", "a" * 65, ".hidden"):
        r = await client.get(f"{URL}?run={bad}")
        assert r.status_code == 404, bad
    for bad in ("../old-ok", "..%2Fold-ok"):  # the security gate answers "../" before routing
        r = await client.get(f"{URL}?run={bad}")
        assert r.status_code in (403, 404) and "Run old-ok" not in r.text, bad
        threat_tracker.reset()
        ban_registry.clear()


async def test_the_picker_lists_the_five_newest_readable_runs(client, results):
    start = datetime(2026, 11, 1, tzinfo=UTC)
    for i in range(7):
        write_run(results, f"run-{i}", data=small_summary(f"run-{i}", start + timedelta(days=i)))
    html = (await client.get(URL)).text
    assert picker_hrefs(html) == ["run-6", "run-5", "run-4", "run-3", "run-2"] == [f"run-{6 - i}" for i in range(5)]
    assert (await client.get(f"{URL}?run=run-2")).status_code == 200
    assert (await client.get(f"{URL}?run=run-1")).status_code == 404  # older than the published five


# ------------------------------------------------------------------ empty / unavailable: never a 500
async def test_empty_state_without_runs(client, results, tmp_path, monkeypatch):
    for _ in range(2):
        r = await client.get(URL)
        assert r.status_code == 200 and "No load test results yet" in r.text and "data-lt-empty" in r.text
        assert "data-lt-verdict" not in r.text and "data-lt-disclaimer" not in r.text and "data-lt-runs" not in r.text
        assert "loadtest" not in r.text and "README" not in r.text and csp_problems(r.text) == []
        use_results(monkeypatch, tmp_path / "missing")  # then: no results folder at all


async def test_only_unreadable_runs_give_a_neutral_unavailable_card(client, results):
    write_run(results, "broken-run", data="{")
    invalid = small_summary("invalid-run", datetime(2026, 11, 2, tzinfo=UTC))
    invalid["files"] = {"report_md": "../../secrets.txt"}
    write_run(results, "invalid-run", data=invalid)
    r = await client.get(URL)
    assert r.status_code == 200 and "Results unavailable" in r.text and "data-lt-unavailable" in r.text
    for text in ("not valid JSON", "schema", "broken-run", "invalid-run", "secrets", "summary.json", "data-lt-error",
                 "data-lt-runs", "data-lt-verdict"):
        assert text not in r.text, text
    assert csp_problems(r.text) == []


async def test_a_run_that_cannot_be_displayed_is_unavailable_not_a_500(client, db, fixtures, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("a bug in the page model")

    monkeypatch.setattr(page, "public_view", broken)
    r = await client.get(URL)
    assert r.status_code == 200 and "Results unavailable" in r.text and "data-lt-verdict" not in r.text
    assert "a bug in the page model" not in r.text
    monkeypatch.undo()
    use_results(monkeypatch, FIXTURES)

    class BrokenFmt(page.admin_page.Fmt):
        @staticmethod
        def ms(value):
            raise RuntimeError("a bug in a template helper")

    monkeypatch.setattr(page, "FMT", BrokenFmt())
    await sign_in(client, db, Role.SUPERADMIN)
    r = await client.get(URL)
    assert r.status_code == 200 and "Results unavailable" in r.text and "data-lt-verdict" not in r.text
    assert "data-lt-admin-link" in r.text  # the super-admin can still open the full report


# ------------------------------------------------------------------ PUBLIC_LOADTEST_PAGE=false
async def test_switched_off_the_page_is_404_and_no_menu_links_it(client, db, fixtures, monkeypatch):
    monkeypatch.setattr(get_settings(), "public_loadtest_page", False)
    assert (await client.get(URL)).status_code == 404
    assert (await client.get(f"{URL}?run={PASS_RUN}")).status_code == 404
    for path in ("/", "/terms"):
        assert 'href="/load-test"' not in (await client.get(path)).text, path
    await sign_in(client, db, Role.SUPERADMIN)
    assert (await client.get(URL)).status_code == 404
    for path in ("/terms", "/superadmin/load-test"):
        html = (await client.get(path)).text
        assert 'href="/load-test"' not in html and "data-lt-public-link" not in html, path


# ------------------------------------------------------------------ the two pages link each other
async def test_the_superadmin_page_links_the_public_page_and_only_admins_get_the_way_back(client, db, fixtures):
    await sign_in(client, db, Role.SUPERADMIN)
    admin = (await client.get("/superadmin/load-test")).text
    assert '<a href="/load-test" class="btn btn-secondary btn-sm" data-lt-public-link>' in admin
    assert 'data-lt-verdict="pass"' in admin and "data-lt-endpoints" in admin  # the full report is unchanged
    public = (await client.get(f"{URL}?run={FAIL_RUN}")).text
    assert f'href="/superadmin/load-test?run={FAIL_RUN}"' in public and "data-lt-admin-link" in public


@pytest.mark.parametrize("role", [Role.CUSTOMER, Role.STAFF, Role.OWNER])
async def test_other_roles_get_no_admin_link(client, db, fixtures, role):
    await sign_in(client, db, role)
    html = (await client.get(URL)).text
    assert "data-lt-admin-link" not in html and "/superadmin/load-test" not in html


# ------------------------------------------------------------------ escaping + strict CSP
async def test_summary_text_is_escaped_everywhere(client, results):
    data = kit_summary("xss-run", datetime(2026, 11, 4, tzinfo=UTC))
    data["verdict"]["headline"] = '<script>alert("x")</script><img src=x onerror=alert(1)>'
    data["verdict"]["slos"][0].update(name="p95 <svg onload=alert(2)> at 50 users",
                                      target="<img src=x onerror=alert(3)> 800 ms", actual='<b>"120"</b>')
    data["verdict"]["slos"][-1]["actual"] = "<i>0</i> 5xx"
    data["capacity"]["note"] = "<iframe src=//evil.example></iframe>"
    data["target"]["environment"] = '"><script>alert(4)</script>'
    data["machine"]["os"] = "<img src=x onerror=alert(5)>"
    data["machine"]["locust_version"] = "<u>2</u>"
    data["classes"][0]["name"] = "<script>alert(6)</script>"
    write_run(results, "xss-run", data=data)
    r = await client.get(URL)
    html = r.text
    assert r.status_code == 200 and 'data-lt-verdict="pass"' in html and csp_problems(html) == []
    for raw in ("<script>alert", "<img src=x", "<svg onload", "<iframe", "<b>", "<i>0", "<u>2"):
        assert raw not in html, raw
    for escaped in ("&lt;script&gt;alert(&#34;x&#34;)&lt;/script&gt;", "p95 &lt;svg onload=alert(2)&gt; at 50 users",
                    "&lt;iframe src=//evil.example&gt;&lt;/iframe&gt;", "&lt;img src=x onerror=alert(5)&gt;",
                    "&lt;b&gt;&#34;120&#34;&lt;/b&gt;", "&#34;&gt;&lt;script&gt;alert(4)&lt;/script&gt; settings",
                    "&lt;script&gt;alert(6)&lt;/script&gt;", "Locust &lt;u&gt;2&lt;/u&gt;", "&lt;i&gt;0&lt;/i&gt; 5xx"):
        assert escaped in html, escaped
    desc = re.search(r'<desc id="lt-p95-d">(.*?)</desc>', html, re.S).group(1)
    assert "The dashed line is the SLO (&lt;img src=x onerror=alert(3)&gt; 800 ms)" in desc


async def test_rendered_pages_are_strict_csp_safe(client, db, fixtures):
    for url in (URL, f"{URL}?run={FAIL_RUN}"):
        r = await client.get(url)
        assert r.status_code == 200 and csp_problems(r.text) == [], url
        assert "'unsafe-inline'" not in r.headers["content-security-policy"].split("script-src", 1)[-1].split(";")[0]
    await sign_in(client, db, Role.OWNER)
    assert csp_problems((await client.get(URL)).text) == []


def test_the_page_template_and_the_navbar_have_no_inline_styles_scripts_or_handlers():
    for name in ("public/load_test.html", "partials/navbar.html"):
        text = re.sub(r"\{#.*?#\}", "", (BASE_DIR / "app" / "templates" / name).read_text(encoding="utf-8"), flags=re.S)
        assert not STYLE_ATTR.search(text) and not HANDLER_ATTR.search(text), name
    assert "<script" not in (BASE_DIR / "app" / "templates" / "public" / "load_test.html").read_text(encoding="utf-8")


# ------------------------------------------------------------------ no SQL beyond the layout's session check
async def _count(client, url: str) -> int:
    with QueryCounter() as qc:
        r = await client.get(url)
    assert r.status_code == 200, url
    return qc.count


async def test_the_page_and_the_link_add_no_queries(client, db, fixtures, monkeypatch):
    await client.get(URL)  # warm-up: caches, first-render work
    await client.get("/terms")
    anon = {url: await _count(client, url) for url in (URL, f"{URL}?run={FAIL_RUN}", "/terms")}
    assert set(anon.values()) == {anon["/terms"]}, anon  # a public page without SQL of its own: the same as /terms
    for role in ROLES:
        await sign_in(client, db, role)
        await client.get(URL)
        counts = {url: await _count(client, url) for url in (URL, "/terms")}
        assert counts[URL] == counts["/terms"] <= 3, (role, counts)  # the session lookup only
        monkeypatch.setattr(get_settings(), "public_loadtest_page", False)
        assert await _count(client, "/terms") == counts["/terms"], role  # the nav link costs no query
        monkeypatch.setattr(get_settings(), "public_loadtest_page", True)
        client.cookies.clear()


# ------------------------------------------------------------------ phones (375 px): wrapping, own scroll boxes
async def test_long_text_wraps_and_wide_parts_scroll_in_their_own_box(client, results):
    token = "x" * 150
    data = kit_summary("long-text", datetime(2026, 11, 6, tzinfo=UTC))
    data["verdict"]["headline"] = token
    data["verdict"]["slos"][0].update(name="p95 " + token[:100] + " at 50 users", actual=token[:60])
    data["capacity"]["note"] = token
    data["machine"]["os"] = token[:120]
    data["classes"][0]["name"] = "Z" * 80
    write_run(results, "long-text" + "-x" * 25, data=data)  # a 59-character run id
    write_run(results, "older", data=small_summary("older", datetime(2026, 11, 1, tzinfo=UTC)))
    html = (await client.get(URL)).text
    assert picker_hrefs(html) == ["long-text" + "-x" * 25, "older"]
    verdict = re.search(r"data-lt-verdict=.*?</section>", html, re.S).group(0)
    assert verdict.count("[overflow-wrap:anywhere]") == len(data["verdict"]["slos"]) + 1
    assert html.count('class="sa-kpi [overflow-wrap:anywhere]"') == 6
    assert re.search(r'<section class="[^"]*\[overflow-wrap:anywhere\][^"]*"[^>]*data-lt-tested>', html)
    assert re.search(r'<div class="mt-6 overflow-x-auto">\s*<table[^>]*data-lt-steps>', html)  # its own scroll box
    assert "w-[min(24rem,calc(100vw-2rem))]" in html  # the picker menu fits a phone (16 px gutters)
    assert '<span class="max-w-[12rem] truncate">Latest run</span>' in html
    assert csp_problems(html) == []


def test_the_docs_describe_the_page_and_the_setting():
    doc = (BASE_DIR / "docs" / "load-test-page.md").read_text(encoding="utf-8")
    for text in ("## Public page", "`GET /load-test`", "PUBLIC_LOADTEST_PAGE", "Load test results",
                 "app/routers/public_loadtest.py", "tests/test_public_load_page.py"):
        assert text in doc, text
    assert Settings.model_fields["public_loadtest_page"].default is True  # on unless switched off


# ------------------------------------------------------------------ the data volume: first, in bold (owner's rule)
DATA_ITEM = r'<li><strong class="font-bold tabular-nums">([\d,]+)</strong> ([^<]+)</li>'


async def test_the_data_volume_is_the_first_thing_the_results_show_in_bold(client, fixtures):
    html = (await client.get(URL)).text
    box = re.search(r"<section[^>]*data-lt-data-volume>(.*?)</section>", html, re.S)
    assert box, "the data-volume banner is missing"
    body = box.group(1)
    assert re.search(r'<h2 id="lt-data-volume" class="[^"]*\bfont-bold\b[^"]*">380,517 database rows ', body)
    assert "in 7 tables" in body and "Data volume" in body
    assert re.findall(DATA_ITEM, body) == [("24,318", "users"), ("120", "gyms"), ("342", "branches"),
                                           ("21,877", "memberships"), ("186,420", "transactions"),
                                           ("96,210", "chat messages")]
    assert "security_events" not in body  # only the main tables are named; internal ones only count in the total
    first = html.index("data-lt-data-volume")
    for later in ("data-lt-disclaimer", "data-lt-run-line", "data-lt-verdict=", "data-lt-kpis", "data-lt-steps",
                  "data-lt-tested"):
        assert first < html.index(later), f"the data volume must come before {later}"
