"""Super-admin load-test results page (/superadmin/load-test, docs/load-test-page.md): access control, empty state,
fixture runs (verdict, KPIs, tables, SVG charts), run selection, sort / show, path traversal and symlink escapes,
broken and schema-invalid runs, NaN rejection, the 20-run picker, downloads (attachments, nosniff, 404s, size cap),
strict-CSP output, no SQL beyond the session check, the parse cache, downsampling and the documented schema example;
plus the review fixes: out-of-range times (never a 500), the p95 the SLO judges (slo_p95_ms, KPI = SLO actual, axis
clipping), the cache key (inode + ctime), capped error messages, files swapped after their check (symlink / FIFO),
direct end labels, pool-line headroom, neutral missing-data tiles, time zones, a11y text and the display safety net.

Fixture runs: tests/fixtures/loadtest/results (regenerate with tests/fixtures/loadtest/make_fixtures.py)."""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from urllib.parse import unquote

import pytest

from app.config import BASE_DIR, get_settings
from app.db import QueryCounter
from app.models.enums import Role
from app.routers import superadmin_loadtest as page
from app.security.bans import ban_registry, threat_tracker
from app.services import loadtest_results as lt
from tests.conftest import login_as
from tests.factories import make_user
from tests.test_csp_static import HANDLER_ATTR, SCRIPT_TAG, STYLE_ATTR

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "loadtest" / "results"
PASS_RUN = "20261110-1800-prod-like"
FAIL_RUN = "20261109-1200-prod-like"
BROKEN_RUN = "20261108-0900-broken"
INVALID_RUN = "20261107-0900-invalid"
URL = "/superadmin/load-test"
FIXTURE_FILES = {"report.md": "report.md", "locust.html": "locust_report.html"}  # URL name -> file in PASS_RUN
CHART_IDS = ("lt-p95", "lt-rps", "lt-tl-users", "lt-tl-rps", "lt-tl-failures", "lt-tl-latency", "lt-tl-cpu",
             "lt-tl-rss", "lt-tl-db")


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


@pytest.fixture
async def admin_client(client, db):
    admin = await make_user(db, role=Role.SUPERADMIN, full_name="Platform Admin")
    await login_as(client, admin)
    return client


def pass_summary() -> dict:
    return json.loads((FIXTURES / PASS_RUN / "summary.json").read_text(encoding="utf-8"))


def small_summary(run_id: str, started: datetime, *, passed: bool = True, users: int = 100) -> dict:
    """A minimal valid schema-1 summary (2 steps, 3 points, 2 endpoints)."""
    return {
        "schema": 1, "run_id": run_id, "started_at": started.isoformat(), "ended_at": (started + timedelta(minutes=10)).isoformat(),
        "duration_s": 600,
        "target": {"host": "http://127.0.0.1:12000", "workers": 2, "environment": "production", "redis": True},
        "verdict": {"passed": passed, "headline": f"Run {run_id}", "slos": [
            {"name": "p95 latency at 50 users", "target": "≤ 800 ms", "actual": "120 ms", "passed": passed}]},
        "capacity": {"max_users_within_slo": users // 2, "max_rps_within_slo": 90.5, "knee_users": users},
        "totals": {"requests": 1000, "failures": 3, "error_rate": 0.003, "rps": 50.0, "p50_ms": 40, "p95_ms": 120,
                   "p99_ms": 300, "max_ms": 900},
        "steps": [{"users": users // 2, "start_s": 0, "end_s": 300, "rps": 45, "error_rate": 0.0, "p50_ms": 35,
                   "p95_ms": 120, "p99_ms": 250, "passed": True},
                  {"users": users, "start_s": 300, "end_s": 600, "rps": 55, "error_rate": 0.006, "p50_ms": 80,
                   "p95_ms": 950, "p99_ms": 1800, "passed": False}],
        "timeline": [{"t_s": t, "users": users, "rps": 50.0, "p50_ms": 40, "p95_ms": 120} for t in (10, 300, 600)],
        "endpoints": [{"name": "/", "method": "GET", "requests": 600, "failures": 0, "p95_ms": 50, "rps": 1.0},
                      {"name": "/gyms", "method": "GET", "requests": 400, "failures": 3, "p95_ms": 210, "rps": 0.7}],
        "files": {"report_md": "report.md", "locust_html": "locust.html"},
    }


def write_run(root: Path, run_id: str, data: dict | str | bytes | None = None, *, report: str | None = "# report\n",
              html: str | None = "<html><script>x()</script></html>") -> Path:
    folder = root / run_id
    folder.mkdir(parents=True, exist_ok=True)
    if data is None:
        data = small_summary(run_id, datetime(2026, 11, 1, 12, tzinfo=UTC))
    raw = data if isinstance(data, (str, bytes)) else json.dumps(data)
    (folder / "summary.json").write_bytes(raw if isinstance(raw, bytes) else raw.encode("utf-8"))
    if report is not None:
        (folder / "report.md").write_text(report, encoding="utf-8")
    if html is not None:
        (folder / "locust.html").write_text(html, encoding="utf-8")
    return folder


def picker_runs(html: str) -> list[str]:
    block = re.search(r"<ul[^>]*data-lt-runs>(.*?)</ul>", html, re.S)
    if not block:
        return []
    return re.findall(r'href="/superadmin/load-test\?run=([^"&#]+)"', block.group(1))


def endpoint_names(html: str) -> list[str]:
    table = re.search(r"<table[^>]*data-lt-endpoints>(.*?)</table>", html, re.S).group(1)
    names = re.findall(r'\[overflow-wrap:anywhere\]">(.*?)</span>', table)
    return [n.replace("<wbr>", "") for n in names]


def table_rows(html: str, marker: str) -> int:
    table = re.search(rf"<table[^>]*{marker}>(.*?)</table>", html, re.S)
    return len(re.findall(r"<tr\b", table.group(1).split("</thead>", 1)[1])) if table else 0


def csp_problems(html: str) -> list[str]:
    problems = []
    if STYLE_ATTR.search(html):
        problems.append("inline style attribute")
    if HANDLER_ATTR.search(html):
        problems.append("inline event handler")
    for m in SCRIPT_TAG.finditer(html):
        if "nonce=" not in m.group(0):
            problems.append("script without nonce")
    bodies = [b for b in re.findall(r"<script\b(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S | re.I) if b.strip()]
    if len(bodies) > 1:  # only base.html's nonce'd theme initialiser
        problems.append(f"{len(bodies)} inline script bodies")
    return problems


# ------------------------------------------------------------------ access control
@pytest.mark.parametrize("path", [URL, f"{URL}?run={PASS_RUN}", f"{URL}/{PASS_RUN}/report.md", f"{URL}/{PASS_RUN}/locust.html"])
async def test_anonymous_is_sent_to_login_like_every_superadmin_page(client, fixtures, path):
    r = await client.get(path)
    other = await client.get("/superadmin/selfcheck")
    assert r.status_code == other.status_code == 303
    assert r.headers["location"].startswith("/auth/login?next=%2Fsuperadmin%2Fload-test")


@pytest.mark.parametrize("role", [Role.CUSTOMER, Role.OWNER, Role.STAFF])
async def test_members_owners_and_staff_are_forbidden(client, db, fixtures, role):
    await login_as(client, await make_user(db, role=role))
    for path in (URL, f"{URL}?run={PASS_RUN}", f"{URL}/{PASS_RUN}/report.md", f"{URL}/{PASS_RUN}/locust.html"):
        r = await client.get(path)
        assert r.status_code == 403, path
        assert "attachment" not in r.headers.get("content-disposition", "")


# ------------------------------------------------------------------ empty state
async def test_empty_state_when_the_results_folder_does_not_exist(admin_client, tmp_path, monkeypatch):
    use_results(monkeypatch, tmp_path / "missing")
    r = await admin_client.get(URL)
    assert r.status_code == 200
    assert "No load-test results yet" in r.text and "data-lt-empty" in r.text
    assert "loadtest/README.md" in r.text and page.RUN_COMMAND.split()[0] in r.text
    assert "does not exist yet" in r.text
    assert "data-lt-runs" not in r.text and "data-lt-verdict" not in r.text
    assert 'aria-label="Super-admin sections"' in r.text


async def test_empty_state_ignores_entries_that_are_not_runs(admin_client, results):
    (results / "notes.txt").write_text("hello")
    (results / "in-progress").mkdir()  # Locust still running: no summary.json yet
    (results / "-bad-name").mkdir()
    write_run(results, "-bad-name")  # invalid run id: never listed
    (results / "folder-with-dir").mkdir()
    (results / "folder-with-dir" / "summary.json").mkdir()  # not a regular file
    r = await admin_client.get(URL)
    assert r.status_code == 200 and "No load-test results yet" in r.text
    assert "does not exist yet" not in r.text


# ------------------------------------------------------------------ the latest fixture run
async def test_latest_run_renders_verdict_kpis_tables_and_charts(admin_client, fixtures):
    r = await admin_client.get(URL)
    assert r.status_code == 200
    html = r.text
    # verdict + SLO checklist
    assert 'data-lt-verdict="pass"' in html and '<span class="sr-only">Verdict: </span>PASS</h2>' in html
    assert "Holds 300 concurrent users within every SLO" in html
    assert "p95 latency at 200 users" in html and "5 of 5 met" in html and html.count('aria-label="Passed"') >= 5
    # KPI tiles
    kpis = re.search(r"data-lt-kpis>(.*?)</section>", html, re.S).group(1)
    for label, value in [("Max users within SLO", "300"), ("Max throughput within SLO", "588 req/s"),
                         ("p95 at 200 users", "236 ms"), ("Error rate", "0.29 %"), ("Requests", "199.9K"),
                         ("5xx responses", "37")]:
        assert label in kpis and f">{value}<" in kpis, (label, value)
    assert "knee at 400 users" in kpis and "SLO ≤ 800 ms · met" in kpis
    # tables
    assert table_rows(html, "data-lt-steps") == 6
    assert table_rows(html, "data-lt-endpoints") == 25
    assert table_rows(html, "data-lt-classes") == 6
    assert table_rows(html, "data-lt-timeline") == 120
    assert ">Knee</span>" in html
    # panels
    assert "98.7 %" in html and "Login floods and scraping" in html  # abuse
    assert "QueuePool limit of size 10 overflow 4 reached" in html  # server log errors
    assert "N+1 queries on /owner/customers" in html and ">High</span>" in html and ">Fixed</span>" in html
    assert "http://127.0.0.1:12000" in html and "locust" in html.lower() and "2.43.1" in html  # run settings
    assert "25 → 50 → 100 → 200 → 300 → 400 users" in html
    assert "transactions" in re.search(r"data-lt-db-rows>(.*?)</ul>", html, re.S).group(1)
    # SVG charts: accessible names, descriptions, CSP-safe geometry
    for uid in CHART_IDS:
        svg = re.search(rf'<svg[^>]*aria-labelledby="{uid}-t {uid}-d"[^>]*>(.*?)</svg>', html, re.S)
        assert svg, uid
        assert 'role="img"' in svg.group(0) and f'<title id="{uid}-t">' in svg.group(1) and f'<desc id="{uid}-d">' in svg.group(1)
        assert 'viewBox="0 0 ' in svg.group(0) and "w-full" in svg.group(0)
    assert "SLO ≤ 800 ms" in html and "Knee: where latency climbs" in html and "Pool size 40" in html
    assert "<title>400 users · p95 1,450 ms · 642 req/s · 1.8 % errors · failed</title>" in html
    # navigation + downloads
    assert re.search(r'href="/superadmin/load-test" class="sa-subnav-link" aria-current="page"', html)
    assert f'href="/superadmin/load-test/{PASS_RUN}/report.md"' in html
    assert f'href="/superadmin/load-test/{PASS_RUN}/locust.html"' in html


async def test_run_picker_lists_runs_newest_first_with_peak_users_and_verdicts(admin_client, fixtures):
    html = (await admin_client.get(URL)).text
    assert picker_runs(html) == [PASS_RUN, FAIL_RUN, BROKEN_RUN, INVALID_RUN]
    block = re.search(r"<ul[^>]*data-lt-runs>(.*?)</ul>", html, re.S).group(1)
    items = re.findall(r"<li>(.*?)</li>", block, re.S)
    assert "400 users" in items[0] and ">Pass</span>" in items[0] and 'aria-current="page"' in items[0]
    assert "300 users" in items[1] and ">Fail</span>" in items[1]
    assert ">Unreadable</span>" in items[2] and ">Unreadable</span>" in items[3]
    # times in the viewer's zone (the factory's Asia/Kolkata, UTC+5:30); the broken run's started_at was peeked
    assert "10 Nov 2026, 23:30" in items[0] and "08 Nov 2026, 14:30" in items[2]


# ------------------------------------------------------------------ run selection
async def test_run_selection_shows_the_requested_run(admin_client, fixtures):
    r = await admin_client.get(f"{URL}?run={FAIL_RUN}")
    assert r.status_code == 200
    html = r.text
    assert 'data-lt-verdict="fail"' in html and ">FAIL<" in html and "Fails the 200-user SLOs" in html
    assert "1 of 4 met" not in html and "0 of 4 met" in html
    assert table_rows(html, "data-lt-steps") == 5 and table_rows(html, "data-lt-endpoints") == 20
    assert "p95 at 200 users" in html and "SLO &lt; 1 s · missed" in html and ">1,380 ms<" in html
    assert f'href="/superadmin/load-test/{FAIL_RUN}/report.md"' in html
    assert f"/{FAIL_RUN}/locust.html" not in html  # files.locust_html is null
    items = re.findall(r"<li>(.*?)</li>", re.search(r"<ul[^>]*data-lt-runs>(.*?)</ul>", html, re.S).group(1), re.S)
    assert 'aria-current="page"' in items[1] and 'aria-current="page"' not in items[0]


async def test_unknown_or_invalid_run_is_404(admin_client, fixtures):
    for run in ("no-such-run", "..", "%2e%2e", ".hidden", "a" * 65, "/tmp", "a%2Fb", "a%5Cb", "~root", " "):
        r = await admin_client.get(f"{URL}?run={run}")
        if run == " ":
            assert r.status_code == 200  # blank = the latest run
            continue
        assert r.status_code == 404, run
        assert "attachment" not in r.headers.get("content-disposition", "")


# ------------------------------------------------------------------ endpoints: sort / show
def _expected(sort: str) -> list[str]:
    eps = [lt.Endpoint.model_validate(e) for e in pass_summary()["endpoints"]]
    return [e.name for e in sorted(eps, key=page._sort_key(sort))]


@pytest.mark.parametrize("sort", ["p95", "p99", "requests", "failures", "name"])
async def test_endpoints_sort_param(admin_client, fixtures, sort):
    html = (await admin_client.get(f"{URL}?run={PASS_RUN}&sort={sort}")).text
    assert endpoint_names(html) == _expected(sort)[:25]
    assert f">{page.SORTS[sort][0]}</a>" in html and 'aria-current="true"' in html
    assert f"by {page.SORTS[sort][1]}" in html


async def test_endpoints_default_order_show_all_and_bad_params(admin_client, fixtures):
    default = (await admin_client.get(URL)).text
    assert endpoint_names(default) == _expected("p95")[:25] and "Top 25 of 40" in default
    assert f'href="/superadmin/load-test?run={PASS_RUN}&amp;show=all#endpoints"' in default  # keeps the run
    everything = (await admin_client.get(f"{URL}?run={PASS_RUN}&show=all")).text
    assert endpoint_names(everything) == _expected("p95") and len(endpoint_names(everything)) == 40
    assert "Show the top 25 only" in everything
    sorted_all = (await admin_client.get(f"{URL}?run={PASS_RUN}&sort=name&show=all")).text
    assert endpoint_names(sorted_all) == _expected("name")
    bad = await admin_client.get(f"{URL}?run={PASS_RUN}&sort=drop+table&show=everything")
    assert bad.status_code == 200 and endpoint_names(bad.text) == _expected("p95")[:25]


async def test_failure_rates_are_flagged_with_an_icon_from_one_percent(admin_client, fixtures):
    html = (await admin_client.get(f"{URL}?run={FAIL_RUN}&sort=failures")).text
    table = re.search(r"<table[^>]*data-lt-endpoints>(.*?)</table>", html, re.S).group(1)
    assert 'class="badge badge-red"' in table and 'aria-label="Above 1 %"' in table
    assert "bg-bad/[0.04]" in table  # the row is highlighted too


# ------------------------------------------------------------------ path traversal & symlinks
def test_resolver_rejects_traversal_absolute_and_overlong_ids(tmp_path):
    root = tmp_path / "results"
    write_run(root, "ok-run")
    outside = write_run(tmp_path, "outside")
    assert lt.load_run("ok-run", root) is not None
    for bad in ("../outside", "..", ".", "", "ok-run/../ok-run", "ok-run/", str(outside), "/etc", "a" * 65,
                "ok run", "ok-run\n", "\x00", "..%2Foutside", "ok-run\\..", "-x", "_x"):
        assert lt.load_run(bad, root) is None, bad
        assert not lt.valid_run_id(bad), bad
    assert lt.valid_run_id("a" * 64) and lt.load_run("a" * 64, root) is None  # valid shape, no such folder


async def test_http_traversal_attempts_never_serve_a_file_outside(admin_client, results, tmp_path):
    secret = tmp_path / "secret-run"
    write_run(tmp_path, "secret-run", report="TOP-SECRET-REPORT", html="TOP-SECRET-HTML")
    write_run(results, "ok-run")
    attempts = [
        f"{URL}?run=..", f"{URL}?run=%2e%2e", f"{URL}?run={secret}", f"{URL}?run={'a' * 65}",
        f"{URL}/../report.md", f"{URL}/%2e%2e/report.md", f"{URL}/..%2Fsecret-run/report.md",
        f"{URL}/%2E%2E%2Fsecret-run/locust.html", f"{URL}/{'a' * 65}/report.md", f"{URL}/ok-run/..%2Freport.md",
        f"{URL}?run=../secret-run", f"{URL}?run=..%2Fsecret-run",
    ]
    for url in attempts:
        r = await admin_client.get(url)
        # the security gate answers 403 to anything with "../" before routing; everything else is our 404
        assert r.status_code in (403, 404), (url, r.status_code)
        assert "TOP-SECRET" not in r.text and "attachment" not in r.headers.get("content-disposition", ""), url
        if "../" not in unquote(unquote(url)):  # reached the page / download route itself
            assert r.status_code == 404, url
        threat_tracker.reset()
        ban_registry.clear()


async def test_symlinked_run_folders_escaping_the_results_folder_are_rejected(admin_client, results, tmp_path):
    outside = write_run(tmp_path, "elsewhere", data=small_summary("elsewhere", datetime(2026, 11, 9, tzinfo=UTC)),
                        report="ESCAPED-REPORT")
    (results / "escape").symlink_to(outside, target_is_directory=True)
    write_run(results, "inside", data=small_summary("inside", datetime(2026, 11, 1, tzinfo=UTC)))
    (results / "alias").symlink_to(results / "inside", target_is_directory=True)  # stays inside: allowed, listed once
    html = (await admin_client.get(URL)).text
    assert picker_runs(html) == ["inside"]
    assert (await admin_client.get(f"{URL}?run=escape")).status_code == 404
    r = await admin_client.get(f"{URL}/escape/report.md")
    assert r.status_code == 404 and "ESCAPED-REPORT" not in r.text
    assert (await admin_client.get(f"{URL}?run=alias")).status_code == 200


async def test_symlinked_summary_or_download_escaping_is_rejected(admin_client, results, tmp_path):
    outside = write_run(tmp_path, "elsewhere", report="ESCAPED-REPORT")
    folder = write_run(results, "linked-files", report=None)
    (folder / "report.md").symlink_to(outside / "report.md")  # report escapes the run folder
    other = results / "linked-summary"
    other.mkdir()
    (other / "summary.json").symlink_to(outside / "summary.json")  # summary escapes the results folder
    r = await admin_client.get(f"{URL}/linked-files/report.md")
    assert r.status_code == 404 and "ESCAPED-REPORT" not in r.text
    assert (await admin_client.get(f"{URL}/linked-files/locust.html")).status_code == 200
    assert (await admin_client.get(f"{URL}?run=linked-summary")).status_code == 404
    assert "linked-summary" not in picker_runs((await admin_client.get(URL)).text)


# ------------------------------------------------------------------ broken / invalid runs
async def test_broken_json_run_is_an_error_card_not_a_500(admin_client, fixtures):
    r = await admin_client.get(f"{URL}?run={BROKEN_RUN}")
    assert r.status_code == 200
    assert "This run could not be read" in r.text and "data-lt-error" in r.text
    assert "not valid JSON" in r.text
    assert f'href="/superadmin/load-test?run={PASS_RUN}"' in r.text  # newest readable run
    assert "data-lt-verdict" not in r.text
    assert (await admin_client.get(f"{URL}/{BROKEN_RUN}/report.md")).status_code == 404


async def test_schema_invalid_run_is_an_error_card(admin_client, fixtures):
    r = await admin_client.get(f"{URL}?run={INVALID_RUN}")
    assert r.status_code == 200 and "data-lt-error" in r.text
    assert "does not match schema version 1" in r.text and "verdict.passed" in r.text
    assert (await admin_client.get(f"{URL}/{INVALID_RUN}/report.md")).status_code == 404  # never "../../secrets.txt"


async def test_newest_run_broken_still_renders_the_page(admin_client, results):
    write_run(results, "older-ok", data=small_summary("older-ok", datetime(2026, 11, 1, tzinfo=UTC)))
    write_run(results, "newest-broken", data='{"schema": 1, "started_at": "2026-11-09T10:00:00Z", "run_id": ')
    r = await admin_client.get(URL)
    assert r.status_code == 200 and "This run could not be read" in r.text
    assert picker_runs(r.text) == ["newest-broken", "older-ok"]
    assert 'href="/superadmin/load-test?run=older-ok"' in r.text


@pytest.mark.parametrize("bad, message", [
    ('"rps": NaN', "NaN"), ('"rps": Infinity', "Infinity"), ('"rps": -Infinity', "-Infinity"), ('"rps": 1e999', "1e999"),
])
async def test_non_finite_numbers_are_rejected(admin_client, results, bad, message):
    text = json.dumps(small_summary("nan-run", datetime(2026, 11, 2, tzinfo=UTC)))
    text = text.replace('"rps": 50.0', bad, 1)
    assert bad in text
    write_run(results, "nan-run", data=text)
    r = await admin_client.get(f"{URL}?run=nan-run")
    assert r.status_code == 200 and "data-lt-error" in r.text
    assert f"contains the number {message}" in r.text and "must be finite" in r.text
    with pytest.raises(lt.SummaryError):
        lt.parse_summary(text.encode())


def test_parse_summary_errors_are_specific():
    good = small_summary("x", datetime(2026, 11, 2, tzinfo=UTC))
    assert lt.parse_summary(json.dumps(good).encode()).verdict.passed is True
    cases = [
        (b"\xff\xfe not utf-8", "not UTF-8"), (b"[1, 2]", "one JSON object"), (b"{}", 'no "schema" key'),
        (json.dumps({**good, "schema": 2}).encode(), "schema version 2"),
        (json.dumps({**good, "schema": True}).encode(), "schema version"),
        (json.dumps({**good, "totals": {**good["totals"], "failures": 5000}}).encode(), "cannot exceed requests"),
        (json.dumps({**good, "ended_at": "2020-01-01T00:00:00Z"}).encode(), "ended_at is before started_at"),
        (json.dumps({**good, "timeline": good["timeline"] * 700}).encode(), "timeline"),  # 2,100 > 2,000 points
        (json.dumps({**good, "files": {"report_md": "../x.md"}}).encode(), "bare file name"),
        (b"[" * 100_000, "summary.json"),  # nesting: a decode error, never a crash
    ]
    for raw, message in cases:
        with pytest.raises(lt.SummaryError) as exc:
            lt.parse_summary(raw)
        assert message in str(exc.value), (message, str(exc.value))


def test_schema_ignores_extra_fields_caps_text_and_accepts_nulls():
    data = small_summary("x", datetime(2026, 11, 2))  # naive time: read as UTC
    data["surprise"] = {"nested": [1, 2, 3]}
    data["verdict"]["headline"] = "Fast\x00 " + "x" * 500
    data["verdict"]["slos"][0]["future_field"] = True
    data["classes"] = None
    data["server"] = {"log_errors": None, "errors_5xx": None}
    data["target"]["notes"] = None
    s = lt.parse_summary(json.dumps(data).encode())
    assert len(s.verdict.headline) == 300 and s.verdict.headline.endswith("…") and "\x00" not in s.verdict.headline
    assert s.classes == [] and s.server.log_errors == [] and s.server.errors_5xx is None and s.target.notes == ""
    assert s.started_at.tzinfo is not None and s.started_at.utcoffset() == timedelta(0)


async def test_oversized_summary_is_an_error_state(admin_client, results, monkeypatch):
    write_run(results, "big-run")
    monkeypatch.setattr(lt, "MAX_SUMMARY_BYTES", 512)
    r = await admin_client.get(f"{URL}?run=big-run")
    assert r.status_code == 200 and "larger than" in r.text and "data-lt-error" in r.text


# ------------------------------------------------------------------ run list
async def test_thirty_runs_list_the_twenty_newest(admin_client, results):
    start = datetime(2026, 10, 1, 9, tzinfo=UTC)
    for i in range(30):  # folder names deliberately not in time order
        run_id = f"run-{(i * 7) % 30:02d}"
        write_run(results, run_id, data=small_summary(run_id, start + timedelta(days=(i * 7) % 30)), report=None, html=None)
    html = (await admin_client.get(URL)).text
    listed = picker_runs(html)
    assert listed == [f"run-{n:02d}" for n in range(29, 9, -1)]
    assert "Latest 20 of 30 runs" in html
    assert re.search(r'data-lt-run-line>\s*<span class="sa-mono">run-29</span>', html)  # newest is shown
    assert (await admin_client.get(f"{URL}?run=run-00")).status_code == 200  # older runs open by id


async def test_runs_without_a_start_time_sort_by_folder_time(results):
    write_run(results, "dated", data=small_summary("dated", datetime(2026, 11, 5, tzinfo=UTC)))
    write_run(results, "undated-new", data="not json")
    write_run(results, "undated-old", data="not json")
    os.utime(results / "undated-old", (datetime(2020, 1, 1).timestamp(),) * 2)
    os.utime(results / "undated-new", (datetime(2030, 1, 1).timestamp(),) * 2)
    listing = lt.list_runs(results)
    assert [r.run_id for r in listing.runs] == ["undated-new", "dated", "undated-old"]
    assert listing.runs[0].passed is None and listing.runs[0].started_at is None and listing.runs[0].error


def test_scan_is_capped(results, monkeypatch):
    for i in range(8):
        write_run(results, f"run-{i}", report=None, html=None)
    monkeypatch.setattr(lt, "MAX_SCAN", 5)
    listing = lt.list_runs(results)
    assert listing.truncated and listing.total == 5 and len(listing.runs) == 5
    monkeypatch.setattr(lt, "MAX_LISTED", 3)
    assert len(lt.list_runs(results).runs) == 3


# ------------------------------------------------------------------ downloads
async def test_downloads_are_attachments_with_nosniff(admin_client, fixtures):
    for kind, media in (("report.md", "text/markdown"), ("locust.html", "text/html")):
        r = await admin_client.get(f"{URL}/{PASS_RUN}/{kind}")
        assert r.status_code == 200, kind
        assert r.headers["content-disposition"] == f'attachment; filename="{PASS_RUN}-{kind}"'
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["content-type"].startswith(media) and "charset=utf-8" in r.headers["content-type"]
        assert "sandbox" in r.headers["content-security-policy"] and "default-src 'none'" in r.headers["content-security-policy"]
        assert "no-store" in r.headers["cache-control"]
        assert r.content == (FIXTURES / PASS_RUN / FIXTURE_FILES[kind]).read_bytes()
        assert int(r.headers["content-length"]) == len(r.content)


async def test_downloads_404_when_missing_or_not_listed(admin_client, results):
    folder = write_run(results, "partial")
    (folder / "locust.html").unlink()  # listed in summary.files, missing on disk
    data = small_summary("unlisted", datetime(2026, 11, 3, tzinfo=UTC))
    data["files"] = {"report_md": "report.md"}  # locust.html exists but is not in summary.files
    write_run(results, "unlisted", data=data)
    (results / "unlisted" / "summary-copy.json").write_text("{}")
    assert (await admin_client.get(f"{URL}/partial/report.md")).status_code == 200
    for path in ("partial/locust.html", "unlisted/locust.html", "no-such-run/report.md", "unlisted/summary.json",
                 "unlisted/summary-copy.json", "unlisted/report.MD"):
        r = await admin_client.get(f"{URL}/{path}")
        assert r.status_code == 404, path
        assert "attachment" not in r.headers.get("content-disposition", "")


async def test_download_size_cap(admin_client, results, monkeypatch):
    write_run(results, "capped", report="x" * 100)
    monkeypatch.setattr(lt, "MAX_DOWNLOAD_BYTES", 99)
    assert (await admin_client.get(f"{URL}/capped/report.md")).status_code == 404
    monkeypatch.setattr(lt, "MAX_DOWNLOAD_BYTES", 100)
    r = await admin_client.get(f"{URL}/capped/report.md")
    assert r.status_code == 200 and len(r.content) == 100


def test_open_download_streams_from_an_open_handle(results):
    write_run(results, "stream", report="hello report")
    found, fh = lt.open_download("stream", "report.md", results)
    try:
        assert found.size == 12 and found.filename == "stream-report.md" and found.media_type.startswith("text/markdown")
        assert fh.read() == b"hello report"
    finally:
        fh.close()
    assert lt.open_download("stream", "summary.json", results) is None


# ------------------------------------------------------------------ CSP + escaping
async def test_rendered_html_is_strict_csp_safe_and_escapes_run_text(admin_client, results):
    data = small_summary("xss-run", datetime(2026, 11, 4, tzinfo=UTC))
    data["verdict"]["headline"] = '<script>alert("x")</script><img src=x onerror=alert(1)>'
    data["endpoints"][0]["name"] = "/<svg onload=alert(1)>"
    data["server"] = {"log_errors": ["<b onclick=alert(1)>boom</b>"]}
    write_run(results, "xss-run", data=data)
    write_run(results, "broken", data="{")
    for url in (URL, f"{URL}?run=xss-run", f"{URL}?run=broken"):
        r = await admin_client.get(url)
        assert r.status_code == 200
        assert csp_problems(r.text) == [], (url, csp_problems(r.text))
        assert "<script>alert" not in r.text and "<img src=x" not in r.text and "<svg onload" not in r.text
    assert "&lt;script&gt;alert(&#34;x&#34;)&lt;/script&gt;" in (await admin_client.get(f"{URL}?run=xss-run")).text


async def test_fixture_pages_are_strict_csp_safe(admin_client, fixtures, tmp_path, monkeypatch):
    for url in (URL, f"{URL}?run={FAIL_RUN}", f"{URL}?run={BROKEN_RUN}", f"{URL}?run={INVALID_RUN}&show=all"):
        r = await admin_client.get(url)
        assert r.status_code == 200 and csp_problems(r.text) == [], url
    use_results(monkeypatch, tmp_path / "none")
    assert csp_problems((await admin_client.get(URL)).text) == []


def test_templates_of_the_page_have_no_inline_styles_scripts_or_handlers():
    folder = BASE_DIR / "app" / "templates" / "superadmin"
    files = [folder / "load_test.html", *sorted(folder.glob("_load_test_*.html"))]
    assert len(files) >= 7
    for f in files:
        text = re.sub(r"\{#.*?#\}", "", f.read_text(encoding="utf-8"), flags=re.S)
        assert not STYLE_ATTR.search(text) and not HANDLER_ATTR.search(text) and "<script" not in text, f.name


# ------------------------------------------------------------------ no SQL beyond the session check
async def test_page_runs_no_queries_beyond_the_superadmin_session_check(admin_client, fixtures, tmp_path, monkeypatch):
    await admin_client.get(URL)  # warm-up
    with QueryCounter() as auth_only:  # a 404 download: only the session lookup touches the database
        assert (await admin_client.get(f"{URL}/no-such-run/report.md")).status_code == 404
    counts = {}
    for url in (URL, f"{URL}?run={FAIL_RUN}&sort=name&show=all", f"{URL}?run={BROKEN_RUN}"):
        with QueryCounter() as qc:
            assert (await admin_client.get(url)).status_code == 200
        counts[url] = qc.count
    with QueryCounter() as dl:
        assert (await admin_client.get(f"{URL}/{PASS_RUN}/report.md")).status_code == 200
    use_results(monkeypatch, tmp_path / "none")
    with QueryCounter() as empty:
        assert (await admin_client.get(URL)).status_code == 200
    assert set(counts.values()) == {auth_only.count} == {empty.count} == {dl.count}, (counts, auth_only.count)
    assert auth_only.count <= 2


# ------------------------------------------------------------------ cache
def test_parsed_runs_are_cached_by_path_mtime_and_size(results, monkeypatch):
    calls = []
    real = lt.parse_summary
    monkeypatch.setattr(lt, "parse_summary", lambda raw: calls.append(len(raw)) or real(raw))
    folder = write_run(results, "cached")
    assert lt.load_run("cached", results).summary is not None
    assert lt.load_run("cached", results).summary is not None
    lt.list_runs(results)
    assert len(calls) == 1
    data = small_summary("cached", datetime(2026, 11, 6, tzinfo=UTC))
    data["verdict"]["headline"] = "rewritten with a different size"
    (folder / "summary.json").write_text(json.dumps(data))
    assert lt.load_run("cached", results).summary.verdict.headline == "rewritten with a different size"
    assert len(calls) == 2
    for i in range(12):
        write_run(results, f"many-{i:02d}", report=None, html=None)
        lt.load_run(f"many-{i:02d}", results)
    assert len(lt._parsed) == lt.CACHE_SIZE == 8
    broken = write_run(results, "broken-cached", data="{")
    calls.clear()
    assert lt.load_run("broken-cached", results).error and lt.load_run("broken-cached", results).error
    assert len(calls) == 1  # errors are cached too
    assert broken.exists()


# ------------------------------------------------------------------ charts & downsampling
def test_lttb_keeps_ends_and_peaks():
    points = [(float(i), float((i * 37) % 101)) for i in range(2000)]
    points[1234] = (1234.0, 10_000.0)
    out = page.lttb(points, 400)
    assert len(out) == 400 and out[0] == points[0] and out[-1] == points[-1] and (1234.0, 10_000.0) in out
    assert page.lttb(points[:10], 400) == points[:10]
    parts = page.downsample([(0, 1), (1, None), (2, 3), (3, 4)], 400)
    assert parts == [[(0, 1.0)], [(2, 3.0), (3, 4.0)]]


def test_scales_and_time_axis():
    assert page.nice_scale(1450) == [0, 500, 1000, 1500]
    assert page.nice_scale(1552) == [0, 500, 1000, 1500, 2000]
    assert page.nice_scale(400, intervals=(2,)) == [0, 200, 400]
    assert page.nice_scale(532, low=480) == [480, 500, 520, 540]
    assert page.nice_scale(0) == [0, 0.25, 0.5, 0.75, 1.0]
    assert page.time_axis(600) == (600.0, ["0", "2", "4", "6", "8", "10 min"])
    assert page.time_axis(45) == (50.0, ["0", "10", "20", "30", "40", "50 s"])
    assert page.time_axis(610) == (720.0, ["0", "2", "4", "6", "8", "10", "12 min"])
    assert page.time_axis(10_000)[1][-1].endswith(" h") or page.time_axis(10_000)[1][-1].endswith(" min")


async def test_long_timelines_are_downsampled_to_400_points_per_series(admin_client, results):
    data = small_summary("long-run", datetime(2026, 11, 7, tzinfo=UTC))
    data["duration_s"] = 10_000
    data["ended_at"] = (datetime(2026, 11, 7, tzinfo=UTC) + timedelta(seconds=10_000)).isoformat()
    data["timeline"] = [{"t_s": 5 * i, "users": 100 + i % 50, "rps": 40 + (i * 13) % 29, "p50_ms": 30 + i % 7,
                         "p95_ms": 90 + (i * 7) % 41, "failures_per_s": (i % 11) / 10, "cpu_pct": 20 + i % 60,
                         "rss_mb": 400 + i / 10, "db_connections": i % 20} for i in range(1, 2001)]
    write_run(results, "long-run", data=data)
    r = await admin_client.get(f"{URL}?run=long-run")
    assert r.status_code == 200
    lines = re.findall(r'<path d="(M[^"]*)" fill="none" stroke="currentColor"', r.text)
    assert len(lines) == 8  # users, rps, failures, p95, p50, cpu, rss, db
    for d in lines:
        assert 2 <= d.count("L") + d.count("M") <= page.MAX_PLOT_POINTS, d[:80]
    assert table_rows(r.text, "data-lt-timeline") <= page.MAX_TABLE_ROWS and "every 7th of 2,000 samples" in r.text
    assert len(r.content) < 400_000
    # the charts' text alternative says what the strided table really holds
    for uid in CHART_IDS[2:]:
        desc = re.search(rf'<desc id="{uid}-d">(.*?)</desc>', r.text, re.S).group(1)
        assert "The timeline table lists every 7th sample; the caption gives the peak." in desc, uid
        assert "Every value is in the timeline table" not in desc, uid


def test_kpi_step_follows_the_slo_that_names_it():
    s = lt.parse_summary(json.dumps(small_summary("x", datetime(2026, 11, 2, tzinfo=UTC), users=100)).encode())
    users, slo = page.slo_step(s)
    assert users == 50 and slo is not None  # "p95 latency at 50 users"
    tiles = {k["label"]: k for k in page.kpis(s)}
    assert tiles["p95 at 50 users"]["value"] == "120 ms"
    data = small_summary("y", datetime(2026, 11, 2, tzinfo=UTC))
    data["verdict"]["slos"] = []
    tiles = {k["label"]: k for k in page.kpis(lt.parse_summary(json.dumps(data).encode()))}
    assert tiles["p95 at 200 users"]["value"] == "—" and "no 200-user step" in tiles["p95 at 200 users"]["hint"]


# ------------------------------------------------------------------ settings, navigation, docs, fixtures
def test_results_dir_setting(monkeypatch, tmp_path):
    s = get_settings()
    monkeypatch.setattr(s, "loadtest_results_dir", None)
    assert lt.results_dir() == BASE_DIR / "loadtest" / "results"
    monkeypatch.setattr(s, "loadtest_results_dir", Path("."))
    assert lt.results_dir() == BASE_DIR / "loadtest" / "results"
    monkeypatch.setattr(s, "loadtest_results_dir", Path("var/lt"))
    assert lt.results_dir() == BASE_DIR / "var" / "lt"
    monkeypatch.setattr(s, "loadtest_results_dir", tmp_path)
    assert lt.results_dir() == tmp_path


async def test_load_test_is_linked_from_the_overview_and_the_section_nav(admin_client, fixtures):
    home = (await admin_client.get("/superadmin")).text
    assert 'href="/superadmin/load-test" class="btn btn-secondary btn-sm"' in home
    assert 'href="/superadmin/load-test" class="sa-subnav-link"' in home
    selfcheck = (await admin_client.get("/superadmin/selfcheck")).text
    assert 'href="/superadmin/load-test" class="sa-subnav-link"' in selfcheck


SCHEMA_MODELS = (lt.Summary, lt.Target, lt.Machine, lt.Verdict, lt.Slo, lt.Capacity, lt.Totals, lt.Step, lt.Point,
                 lt.Endpoint, lt.UserClass, lt.Abuse, lt.Server, lt.Finding, lt.Files)


def _json_keys(value) -> set[str]:
    if isinstance(value, dict):
        return set(value).union(*(_json_keys(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(_json_keys(v) for v in value))
    return set()


def test_docs_example_is_a_complete_valid_summary():
    doc = (BASE_DIR / "docs" / "load-test-page.md").read_text(encoding="utf-8")
    example = re.search(r"```json\n(.*?)\n```", doc, re.S).group(1)
    data = json.loads(example)
    s = lt.parse_summary(example.encode())
    assert s.timeline and s.steps and s.endpoints and s.classes and s.abuse and s.server and s.findings and s.machine
    # every field of every model, nested ones included, appears in the example and (in backticks) in the field table
    keys = _json_keys(data)
    for model in SCHEMA_MODELS:
        for name, f in model.model_fields.items():
            key = f.alias or name
            assert key in keys, f"{model.__name__}.{key} is missing from the example"
            assert f"`{key}`" in doc, f"{model.__name__}.{key} is not documented"
    assert data["files"]["locust_html"] == "locust_report.html"  # the kit's name for Locust's report
    assert any(st.slo_p95_ms is not None for st in s.steps)
    assert "LOADTEST_RESULTS_DIR" in doc and "loadtest/README.md" in doc and "allow_nan=False" in doc
    assert "2000-01-01" in doc and "2100-01-01" in doc
    module_doc = lt.__doc__ or ""
    for model in SCHEMA_MODELS:  # the module docstring documents the same fields
        for name, f in model.model_fields.items():
            assert (f.alias or name) in module_doc, f"{model.__name__}.{f.alias or name} is not in the module docstring"


def test_fixture_runs_match_their_description():
    good = lt.load_run(PASS_RUN, FIXTURES).summary
    assert len(good.timeline) == 120 and len(good.steps) == 6 and len(good.endpoints) == 40 and good.verdict.passed
    assert good.capacity.knee_users == 400 and good.knee_step.users == 400
    assert good.files.locust_html == "locust_report.html" and all(st.slo_p95_ms is None for st in good.steps)
    bad = lt.load_run(FAIL_RUN, FIXTURES).summary
    assert not bad.verdict.passed and bad.files.locust_html is None
    assert [st.slo_p95_ms for st in bad.steps] == [205, 270, 980, 1380, 3920]
    assert bad.verdict.slos[0].target == "< 1 s" and bad.verdict.slos[0].users is None  # users from its name
    assert "not valid JSON" in lt.load_run(BROKEN_RUN, FIXTURES).error
    assert "schema version 1" in lt.load_run(INVALID_RUN, FIXTURES).error


# ================================================================== review fixes
# ------------------------------------------------------------------ 1. extreme times: an error card, never a 500
@pytest.mark.parametrize("started, ended, message", [
    ("9999-12-31T23:00:00Z", "9999-12-31T23:10:00Z", "must be between 2000-01-01 and 2100-01-01 (UTC)"),
    ("2026-11-01T12:00:00Z", datetime.max.isoformat(), "must be between 2000-01-01 and 2100-01-01 (UTC)"),
    ("0001-01-01T00:00:00Z", "2026-11-01T12:00:00Z", "must be between 2000-01-01 and 2100-01-01 (UTC)"),
    ("9999-12-31T23:59:59-14:00", "9999-12-31T23:59:59-14:00", "time out of range"),
    ("0001-01-01T00:00:00+14:00", "2026-11-01T12:00:00Z", "time out of range"),
])
async def test_out_of_range_times_are_an_error_card_never_a_500(client, db, results, started, ended, message):
    # a viewer west of UTC (year 1 would underflow) - and the default Asia/Kolkata one (year 9999 would overflow)
    for tz in ("America/New_York", "Asia/Kolkata"):
        await login_as(client, await make_user(db, role=Role.SUPERADMIN, timezone=tz))
        data = small_summary("odd-time", datetime(2026, 11, 1, tzinfo=UTC))
        data["started_at"], data["ended_at"] = started, ended
        write_run(results, "odd-time", data=data)
        write_run(results, "fine", data=small_summary("fine", datetime(2026, 11, 2, tzinfo=UTC)))
        r = await client.get(f"{URL}?run=odd-time")
        assert r.status_code == 200 and "data-lt-error" in r.text and message in r.text, tz
        for url in (URL, f"{URL}?run=fine"):
            assert (await client.get(url)).status_code == 200, (tz, url)
    with pytest.raises(lt.SummaryError, match=re.escape(message)):
        lt.parse_summary(json.dumps(data).encode())


@pytest.mark.parametrize("peeked", ["9999-12-31T23:59:59-14:00", "0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59",
                                    "1970-01-01T00:00:00Z"])
async def test_a_broken_run_peeking_an_extreme_start_never_breaks_the_page(admin_client, results, peeked):
    write_run(results, "ok", data=small_summary("ok", datetime(2026, 11, 1, tzinfo=UTC)))
    write_run(results, "zz-broken", data='{"schema": 1, "run_id": "x", "started_at": "%s", ' % peeked)
    for url in (URL, f"{URL}?run=ok", f"{URL}?run=zz-broken"):
        assert (await admin_client.get(url)).status_code == 200, url
    broken = next(h for h in lt.list_runs(results).runs if h.run_id == "zz-broken")
    assert broken.started_at is None and broken.passed is None  # the peek is ignored: ordered by folder time
    assert "Start time unknown" in (await admin_client.get(URL)).text


async def test_a_summary_file_time_out_of_range_is_simply_not_shown(admin_client, results):
    year_2200 = datetime(2200, 1, 1, tzinfo=UTC).timestamp()
    for stamp in (0, year_2200):
        folder = write_run(results, "odd-mtime")
        os.utime(folder / "summary.json", (stamp, stamp))
        run = lt.load_run("odd-mtime", results)
        assert run.summary is not None and run.modified is None, stamp
        r = await admin_client.get(f"{URL}?run=odd-mtime")
        assert r.status_code == 200 and "schema version 1 · changed" not in r.text
        broken = write_run(results, "odd-broken", data="{")
        os.utime(broken / "summary.json", (stamp, stamp))
        r = await admin_client.get(f"{URL}?run=odd-broken")
        assert r.status_code == 200 and "data-lt-error" in r.text and "· changed" not in r.text
    assert lt.load_run("odd-mtime", results) is not None
    write_run(results, "normal-mtime")
    assert lt.load_run("normal-mtime", results).modified is not None


# ------------------------------------------------------------------ 2. the p95 the SLO judges
def test_the_p95_tile_shows_what_the_slo_judged_and_the_chart_agrees():
    data = small_summary("x", datetime(2026, 11, 2, tzinfo=UTC), users=200)  # steps at 100 and 200 users
    data["verdict"]["slos"] = [{"name": "page p95 < 1 s at 200 users", "target": "< 1 s", "actual": "640 ms",
                                "passed": True}]
    # every request at 200 users: p95 1,200 ms (over the SLO); the page requests the SLO judges: 640 ms (under it)
    data["steps"][1].update(p95_ms=1200, slo_p95_ms=640, passed=True)
    s = lt.parse_summary(json.dumps(data).encode())
    tile = {k["label"]: k for k in page.kpis(s)}["p95 at 200 users"]
    assert (tile["value"], tile["hint"], tile["tone"]) == ("640 ms", "SLO < 1 s · met", "green")
    view = page.steps_view(s)
    assert view["threshold"] == 1000.0 and view["slo_column"]
    assert view["p95_title"] == "p95 latency per step, as the SLO judges it"
    chart = view["p95"]
    assert chart["threshold"]["label"] == "SLO < 1 s · judged at 200 users"
    assert chart["bars"][0]["d"] is None  # the 100-user step has no slo_p95_ms: no column rather than a mixed one
    top = float(re.match(r"M[\d.]+,[\d.]+V([\d.]+)", chart["bars"][1]["d"]).group(1)) - 4  # 4 = rounded data end
    assert top > float(chart["threshold"]["y"])  # 640 ms: under the dashed SLO line, like the verdict says
    assert "SLO p95 640 ms" in chart["bars"][1]["title"] and "p95 1,200 ms" in chart["bars"][1]["title"]
    assert "the requests the SLO judges" in chart["desc"]
    # no SLO about p95: the tile falls back to the step (slo_p95_ms first)
    data["verdict"]["slos"] = []
    tile = {k["label"]: k for k in page.kpis(lt.parse_summary(json.dumps(data).encode()))}["p95 at 200 users"]
    assert tile["value"] == "640 ms" and tile["hint"] == "step passed its SLOs"


def test_the_p95_tile_and_slo_line_follow_the_slo_users_field():
    data = small_summary("x", datetime(2026, 11, 2, tzinfo=UTC), users=200)  # steps at 100 and 200 users
    data["verdict"]["slos"] = [{"name": "p95 latency", "target": "≤ 800 ms", "actual": "130 ms", "passed": True,
                                "users": 100}]  # no step named in the text: only the users field says which
    data["steps"][0]["p95_ms"] = 130
    s = lt.parse_summary(json.dumps(data).encode())
    assert page.slo_step(s) == (100, s.verdict.slos[0])
    tiles = {k["label"]: k for k in page.kpis(s)}
    assert "p95 at 200 users" not in tiles
    assert tiles["p95 at 100 users"]["value"] == "130 ms" and tiles["p95 at 100 users"]["hint"] == "SLO ≤ 800 ms · met"
    assert page.steps_view(s)["p95"]["threshold"]["label"] == "SLO ≤ 800 ms · judged at 100 users"


async def test_slo_targets_in_seconds_draw_the_line_in_milliseconds(admin_client, results):
    assert page._ms_target("< 1 s") == 1000.0 and page._ms_target("≤ 1.5 s") == 1500.0
    assert page._ms_target("≤ 800 ms") == 800.0 and page._ms_target("< 1,200 ms") == 1200.0
    assert page._ms_target("fast enough") is None
    data = small_summary("secs", datetime(2026, 11, 3, tzinfo=UTC))
    data["verdict"]["slos"][0]["target"] = "< 1 s"
    assert page.steps_view(lt.parse_summary(json.dumps(data).encode()))["threshold"] == 1000.0
    write_run(results, "secs", data=data)
    html = (await admin_client.get(f"{URL}?run=secs")).text
    assert "SLO &lt; 1 s · judged at 50 users" in html
    assert "The dashed line is the SLO (&lt; 1 s), judged at 50 users." in html


async def test_steps_show_the_p95_the_slo_judges_beside_every_request(admin_client, fixtures):
    html = (await admin_client.get(f"{URL}?run={FAIL_RUN}")).text
    assert ">p95 latency per step, as the SLO judges it<" in html
    head = re.search(r"<table[^>]*data-lt-steps>(.*?)</thead>", html, re.S).group(1)
    assert "SLO p95</abbr>" in head
    assert "<title>200 users · p95 1,240 ms · SLO p95 1,380 ms ·" in html
    assert table_rows(html, "data-lt-steps") == 5
    passing = (await admin_client.get(URL)).text  # no slo_p95_ms: no extra column, the plain chart title
    assert "SLO p95</abbr>" not in passing and ">p95 latency per step<" in passing


def test_step_chart_clips_far_beyond_three_times_the_slo():
    steps = [lt.Step(users=u, start_s=i * 60, end_s=(i + 1) * 60, rps=1, error_rate=0, passed=True)
             for i, u in enumerate((100, 200, 700))]

    def chart(values):
        return page.step_chart(steps, values, unit="ms", knee=None, titles=["a", "b", "c"], threshold=1000.0,
                               floor_top=10.0, clip_text=page.Fmt.short_ms)

    spike = chart([300.0, 640.0, 9000.0])
    assert spike["y_labels"][0] == "3,000" and spike["clipped"]  # not a 0-9,000 axis squashing 300 and 640
    assert [b["clip"]["text"] if b["clip"] else None for b in spike["bars"]] == [None, None, "9.0 s"]
    assert "V4Q" in spike["bars"][2]["d"]  # drawn to the top of the plot (its rounded end starts at y = 4)
    assert "The axis stops at 3,000 ms" in spike["desc"]
    within = chart([300.0, 640.0, 2900.0])  # up to 3x the SLO: the axis simply covers it
    assert not within["clipped"] and within["y_labels"][0] == "3,000" and "axis stops" not in within["desc"]
    at_slo = chart([300.0, 640.0, 900.0])  # the threshold never sits on the top edge
    assert float(at_slo["threshold"]["y"]) > 0
    no_slo = page.step_chart(steps, [300.0, 640.0, 9000.0], unit="ms", knee=None, titles=["a", "b", "c"])
    assert not no_slo["clipped"]  # only an SLO justifies a cut axis
    assert no_slo["y_labels"] == ["10K", "7.5K", "5K", "2.5K", "0"]  # one label style per axis (never 10K over 7,500)
    assert page.y_labels([0, 500, 1000, 1500]) == ["1,500", "1,000", "500", "0"]
    assert page.y_labels([0, 500_000, 1_000_000, 1_500_000]) == ["1.5M", "1M", "500K", "0"]
    assert page.Fmt.short_ms(640) == "640 ms" and page.Fmt.short_ms(3920) == "3.9 s" and page.Fmt.short_ms(12_400) == "12 s"


async def test_the_failing_run_clips_its_300_user_step_with_a_break_and_its_value(admin_client, fixtures):
    html = (await admin_client.get(f"{URL}?run={FAIL_RUN}")).text
    fig = re.search(r'<figure[^>]*aria-labelledby="lt-p95-cap">(.*?)</figure>', html, re.S).group(1)
    labels = re.search(r"data-lt-clip-labels>(.*?)</span>\s*</div>", fig, re.S).group(1)
    assert re.findall(r"<span[^>]*>([^<]*)</span>", labels) == ["", "", "", "", "3.9 s"]
    breaks = re.search(r"data-lt-breaks>(.*?)</g>", fig, re.S).group(1)
    assert breaks.count("<line ") == 2  # one double gap, on the one clipped column
    assert ">3,000<" in fig and "The axis stops at 3,000 ms" in fig
    passing = (await admin_client.get(URL)).text  # its 400-user step (1,450 ms) is within 3x the 800 ms SLO
    assert "data-lt-clip-labels" not in passing and "data-lt-breaks" not in passing


# ------------------------------------------------------------------ 3/5. the parse cache key
def test_cache_notices_same_size_rewrites_even_when_the_mtime_is_preserved(results, monkeypatch):
    calls = []
    real = lt.parse_summary
    monkeypatch.setattr(lt, "parse_summary", lambda raw: calls.append(1) or real(raw))
    folder = write_run(results, "same-size")
    f = folder / "summary.json"

    def rewrite(headline: str, *, atomic: bool) -> None:
        data = small_summary("same-size", datetime(2026, 11, 1, 12, tzinfo=UTC))
        data["verdict"]["headline"] = headline
        raw = json.dumps(data).encode()
        assert len(raw) == f.stat().st_size  # same size: only the content differs
        if atomic:  # the analysis' tmp + rename; the new file is a new inode
            tmp = folder / "summary.json.tmp"
            tmp.write_bytes(raw)
            os.replace(tmp, f)
        else:
            f.write_bytes(raw)

    def headline() -> str:
        return lt.load_run("same-size", results).summary.verdict.headline

    assert headline() == "Run same-size"
    st = f.stat()  # 1. in place, same size, a later mtime
    rewrite("Run SAME-size", atomic=False)
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    assert headline() == "Run SAME-size"
    st = f.stat()  # 2. replaced (new inode), same size, mtime preserved (cp -p, rsync -t, tar)
    rewrite("Run same-SIZE", atomic=True)
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert (f.stat().st_mtime_ns, f.stat().st_size) == (st.st_mtime_ns, st.st_size) and f.stat().st_ino != st.st_ino
    assert headline() == "Run same-SIZE"
    st = f.stat()  # 3. in place (same inode), same size, mtime preserved: the ctime still moves
    rewrite("RUN same-size", atomic=False)
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert (f.stat().st_mtime_ns, f.stat().st_size, f.stat().st_ino) == (st.st_mtime_ns, st.st_size, st.st_ino)
    assert headline() == "RUN same-size"
    assert len(calls) == 4
    assert headline() == "RUN same-size" and len(calls) == 4  # unchanged: from the cache


async def test_a_cold_page_parses_each_listed_run_once(admin_client, results, monkeypatch):
    start = datetime(2026, 10, 1, tzinfo=UTC)
    for i in range(12):  # more runs than the LRU holds (8)
        write_run(results, f"run-{i:02d}", data=small_summary(f"run-{i:02d}", start + timedelta(days=i)),
                  report=None, html=None)
    calls = []
    real = lt.parse_summary
    monkeypatch.setattr(lt, "parse_summary", lambda raw: calls.append(1) or real(raw))
    lt.clear_caches()
    r = await admin_client.get(URL)
    assert r.status_code == 200 and re.search(r'data-lt-run-line>\s*<span class="sa-mono">run-11</span>', r.text)
    assert len(calls) == 12  # the newest run, shown by default, is still cached: not parsed a second time
    await admin_client.get(URL)
    assert len(calls) == 12


# ------------------------------------------------------------------ 6. capped error messages
def test_error_messages_are_capped():
    data = small_summary("x", datetime(2026, 11, 2, tzinfo=UTC))
    data["target"]["db_rows"] = {"K" * 4_000_000: -1}  # the key ends up in the error location
    with pytest.raises(lt.SummaryError) as exc:
        lt.parse_summary(json.dumps(data).encode())
    assert len(str(exc.value)) <= lt.MAX_ERROR_CHARS and "target.db_rows.KKKK" in str(exc.value)
    text = json.dumps(small_summary("y", datetime(2026, 11, 2, tzinfo=UTC)))
    text = text.replace('"rps": 50.0', '"rps": ' + "9" * 5000 + "e999", 1)  # an overflowing literal, quoted back
    with pytest.raises(lt.SummaryError) as exc:
        lt.parse_summary(text.encode())
    assert len(str(exc.value)) <= lt.MAX_ERROR_CHARS and "must be finite" in str(exc.value)
    assert len(str(lt.SummaryError("x" * 10_000))) == lt.MAX_ERROR_CHARS


async def test_a_huge_error_reaches_the_error_card_capped(admin_client, results):
    data = small_summary("huge-error", datetime(2026, 11, 2, tzinfo=UTC))
    data["target"]["db_rows"] = {"K" * 1_000_000: -1}
    write_run(results, "huge-error", data=data)
    r = await admin_client.get(f"{URL}?run=huge-error")
    assert r.status_code == 200 and "data-lt-error" in r.text
    error = re.search(r'<p class="sa-pre[^"]*">(.*?)</p>', r.text, re.S).group(1)
    assert len(error) <= lt.MAX_ERROR_CHARS + 100 and len(r.content) < 200_000  # (+ escaping)


# ------------------------------------------------------------------ 12. files swapped after their check
def _without_hanging(fn, fifo: Path | None = None, seconds: float = 5.0):
    """Run ``fn`` in a thread; if it blocks (an open() on a FIFO), unblock it and fail instead of hanging pytest."""
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        future = pool.submit(fn)
        try:
            return future.result(timeout=seconds)
        except concurrent.futures.TimeoutError:
            if fifo is not None:
                os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
            pytest.fail("blocked opening a FIFO")


@pytest.mark.parametrize("swap", ["symlink", "other-file", "fifo"])
def test_a_download_swapped_after_its_check_is_refused(results, tmp_path, monkeypatch, swap):
    secret = tmp_path / "secret.md"
    secret.write_text("TOP-SECRET")
    write_run(results, "swap", report="the real report")
    real = lt.download_file

    def check_then_swap(run, kind):
        found = real(run, kind)
        if swap == "other-file":
            tmp = found.path.with_name("swap.tmp")
            tmp.write_text("a different file")
            os.replace(tmp, found.path)
        else:
            found.path.unlink()
            found.path.symlink_to(secret) if swap == "symlink" else os.mkfifo(found.path)
        return found

    monkeypatch.setattr(lt, "download_file", check_then_swap)
    path = results / "swap" / "report.md"
    assert _without_hanging(lambda: lt.open_download("swap", "report.md", results), path if swap == "fifo" else None) is None
    monkeypatch.setattr(lt, "download_file", real)
    if swap == "other-file":  # checked again from scratch, the new file is served
        found, fh = lt.open_download("swap", "report.md", results)
        with fh:
            assert fh.read() == b"a different file"


@pytest.mark.parametrize("swap", ["replace", "fifo"])
def test_a_summary_swapped_after_its_check_is_not_trusted_or_cached(results, monkeypatch, swap):
    folder = write_run(results, "swapped")
    summary = folder / "summary.json"
    real = lt._candidate

    def check_then_swap(base, run_id):
        c = real(base, run_id)
        if c is not None and run_id == "swapped":
            if swap == "fifo":
                summary.unlink()
                os.mkfifo(summary)
            else:
                data = small_summary("swapped", datetime(2026, 11, 1, 12, tzinfo=UTC))
                data["verdict"]["headline"] = "the replacement"
                tmp = folder / "summary.json.tmp"
                tmp.write_text(json.dumps(data))
                os.replace(tmp, summary)
        return c

    monkeypatch.setattr(lt, "_candidate", check_then_swap)
    run = _without_hanging(lambda: lt.load_run("swapped", results), summary if swap == "fifo" else None)
    assert run.summary is None and "changed while it was being read" in run.error
    assert not lt._parsed  # never cached under the old file's key
    monkeypatch.setattr(lt, "_candidate", real)
    if swap == "replace":
        assert lt.load_run("swapped", results).summary.verdict.headline == "the replacement"
    else:
        assert lt.load_run("swapped", results) is None  # a FIFO is not a run


def test_open_regular_refuses_symlinks_fifos_and_other_files(tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("data")
    st = f.stat()
    fh, _ = lt._open_regular(f, (st.st_dev, st.st_ino))
    with fh:
        assert fh.read() == b"data"
    link = tmp_path / "link.txt"
    link.symlink_to(f)
    assert lt._open_regular(link) is None  # a final symlink is never followed
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    assert _without_hanging(lambda: lt._open_regular(fifo), fifo) is None
    assert lt._open_regular(f, (st.st_dev, st.st_ino + 1)) is None  # not the file that was checked
    assert lt._open_regular(tmp_path / "missing") is None


# ------------------------------------------------------------------ 7. chart details
async def test_knee_band_and_its_legend_key_share_one_wash(admin_client, fixtures):
    html = (await admin_client.get(URL)).text
    band = re.search(r"<rect [^>]*data-lt-knee", html).group(0)
    key = re.search(r"<li [^>]*>(<svg (?:(?!</li>).)*?)Knee: where latency climbs</li>", html, re.S).group(1)
    assert 'fill-opacity="0.12"' in band and 'fill-opacity="0.12"' in key


def test_the_pool_size_line_sits_below_the_top_of_its_panel():
    s = lt.load_run(PASS_RUN, FIXTURES).summary
    db_panel = next(p for p in page.timeline_view(s)["panels"] if p["key"] == "db")
    assert s.server.pool_size == 40 and max(p.db_connections for p in s.timeline) == 40
    assert db_panel["y_labels"][0] == "60" and float(db_panel["ref"]["y"]) == 40.0  # 40 of 60: a third from the top


def test_end_labels_sit_beside_the_line_ends_or_not_at_all():
    assert page.end_labels([("p95", 600, 40.0), ("p50", 600, 90.0)], 600, 120) == [
        {"label": "p95", "y": "35"}, {"label": "p50", "y": "103"}]  # above the top line, below the bottom one
    assert [lab["label"] for lab in page.end_labels([("p95", 600, 60.0), ("p50", 600, 64.0)], 600, 120)] == ["p95", "p50"]
    assert page.end_labels([("p95", 500, 40.0), ("p50", 600, 90.0)], 600, 120) == []  # stops short of the edge
    assert page.end_labels([("p95", 600, 118.0), ("p50", 600, 119.0)], 600, 120) == []  # would collide: legend only
    assert page.end_labels([("rps", 600, 40.0)], 600, 120) == []  # one line: its title names it


async def test_the_latency_panel_labels_its_lines_directly(admin_client, fixtures):
    html = (await admin_client.get(URL)).text
    fig = re.search(r'<figure[^>]*aria-labelledby="lt-tl-latency-cap">(.*?)</figure>', html, re.S).group(1)
    overlay = re.search(r"<svg [^>]*data-lt-end-labels>(.*?)</svg>", fig, re.S)
    assert overlay and 'preserveAspectRatio="xMaxYMin meet"' in overlay.group(0) and 'aria-hidden="true"' in overlay.group(0)
    assert re.findall(r">(p95|p50)</text>", overlay.group(1)) == ["p95", "p50"]
    assert "Every value is in the timeline table." in fig  # the full table backs it (120 samples)
    assert fig.count("[overflow-wrap:anywhere]") == 2  # the legend (p95, p50) stays too
    assert html.count("data-lt-end-labels") == 1  # only the two-line panel


# ------------------------------------------------------------------ 8. missing data is neutral, no idle controls
async def test_missing_data_is_neutral_and_offers_no_controls(admin_client, results):
    data = small_summary("sparse", datetime(2026, 11, 5, tzinfo=UTC))
    data.update(steps=[], timeline=[], endpoints=[], classes=[], abuse=None, server=None, findings=[])
    write_run(results, "sparse", data=data)
    html = (await admin_client.get(f"{URL}?run=sparse")).text
    assert html.count("data-lt-missing") == 6
    for title in ("No load steps recorded", "No timeline recorded", "No endpoints recorded", "No user classes recorded",
                  "No abuse traffic in this run", "Server metrics not recorded"):
        tile = re.search(rf"<div [^>]*data-lt-missing>(?:(?!data-lt-missing).)*?{title}", html, re.S)
        assert tile and "bg-ok/12" not in tile.group(0) and "bg-zinc-200/70" in tile.group(0), title
    assert "No findings recorded" in html  # good news keeps its green tile
    assert "hover a chart" not in html and "No samples over 10 min" in html
    assert 'aria-label="Sort endpoints by"' not in html and "All 0 by" not in html


# ------------------------------------------------------------------ 9. times name their zone
async def test_times_name_their_zone_and_a_run_past_midnight_shows_its_end_date(admin_client, results):
    write_run(results, "daytime", data=small_summary("daytime", datetime(2026, 11, 1, 6, 0, tzinfo=UTC)))
    write_run(results, "midnight", data=small_summary("midnight", datetime(2026, 11, 1, 18, 25, tzinfo=UTC)))
    day = (await admin_client.get(f"{URL}?run=daytime")).text  # the admin is in Asia/Kolkata (UTC+5:30)
    line = re.search(r"data-lt-run-line>(.*?)</p>", day, re.S).group(1)
    assert "01 Nov 2026, 11:30–11:40 IST · 10 min" in line
    assert "01 Nov 2026, 11:30:00 IST" in day and "01 Nov 2026, 11:40:00 IST" in day  # run settings
    night = (await admin_client.get(f"{URL}?run=midnight")).text
    line = re.search(r"data-lt-run-line>(.*?)</p>", night, re.S).group(1)
    assert "01 Nov 2026, 23:55 – 02 Nov 2026, 00:05 IST · 10 min" in line
    items = re.findall(r"<li>(.*?)</li>", re.search(r"<ul[^>]*data-lt-runs>(.*?)</ul>", night, re.S).group(1), re.S)
    assert "01 Nov 2026, 23:55 IST" in items[0] and "01 Nov 2026, 11:30 IST" in items[1]
    broken = write_run(results, "zz-broken", data="{")
    os.utime(broken / "summary.json", (datetime(2026, 11, 3, 0, 0, tzinfo=UTC).timestamp(),) * 2)
    assert "changed 03 Nov 2026, 05:30 IST" in (await admin_client.get(f"{URL}?run=zz-broken")).text


# ------------------------------------------------------------------ 4. phones: long paths and tokens wrap
async def test_long_paths_and_unbroken_text_wrap_instead_of_widening_phones(admin_client, tmp_path, monkeypatch):
    deep = tmp_path / ("a-very-long-results-folder-name-" * 5) / "results"
    use_results(monkeypatch, deep)  # absolute, outside the project, missing
    html = (await admin_client.get(URL)).text
    header = re.search(r'<header class="mb-6 .*?</header>', html, re.S).group(0)  # the page's own header
    assert "Load test" in header and "results folder" in header
    assert str(deep) not in header  # the subtitle never carries a (long) path
    paths = re.findall(r'<span class="([^"]*)" data-lt-path>', html)
    assert len(paths) == 2 and all("break-all" in cls for cls in paths)
    deep.mkdir(parents=True)
    token = "x" * 150
    data = small_summary("long-text", datetime(2026, 11, 6, tzinfo=UTC))
    data["verdict"]["headline"] = token
    data["verdict"]["slos"][0].update(name="p95 " + token[:100] + " at 50 users", target="≤ 800 ms " + token[:40],
                                       actual=token[:60])
    data["capacity"]["note"] = token
    data["findings"] = [{"severity": "low", "title": token, "detail": token, "status": "open"}]
    data["abuse"] = {"requests": 10, "throttled": 9, "throttled_share": 0.9, "note": token}
    data["target"]["notes"] = token
    write_run(deep, "long-text" + "-x" * 25, data=data)  # a 59-character run id
    html = (await admin_client.get(URL)).text
    verdict = re.search(r"data-lt-verdict=.*?</section>", html, re.S).group(0)
    assert verdict.count("[overflow-wrap:anywhere]") == 2  # the headline block and the one SLO
    assert html.count('class="sa-kpi [overflow-wrap:anywhere]"') == 6
    legend = re.search(r'<figure[^>]*aria-labelledby="lt-p95-cap">.*?<ul[^>]*>(.*?)</ul>', html, re.S).group(1)
    assert all("[overflow-wrap:anywhere]" in li for li in re.findall(r"<li [^>]*>", legend))
    findings = re.search(r"data-lt-findings>.*?</section>", html, re.S).group(0)
    assert "min-w-0 [overflow-wrap:anywhere]" in findings
    assert re.search(r'<section class="[^"]*\[overflow-wrap:anywhere\][^"]*"[^>]*data-lt-settings>', html)
    assert re.search(r'<p class="[^"]*\[overflow-wrap:anywhere\][^"]*">x{100}', html)  # the abuse note
    assert csp_problems(html) == []


# ------------------------------------------------------------------ 10. a11y text
async def test_verdict_heading_and_chart_descriptions_say_enough(admin_client, fixtures):
    html = (await admin_client.get(URL)).text
    assert '<h2 id="lt-verdict" class="text-3xl font-semibold tracking-tight"><span class="sr-only">Verdict: </span>PASS</h2>' in html
    for uid in CHART_IDS:
        desc = re.search(rf'<desc id="{uid}-d">(.*?)</desc>', html, re.S).group(1)
        assert "table" in desc, uid  # every chart points to its table (the keyboard path to the values)
    assert "Every value is in the timeline table." in re.search(r'<desc id="lt-tl-rps-d">(.*?)</desc>', html, re.S).group(1)
    assert page.Fmt.ordinal(2) == "2nd" and page.Fmt.ordinal(3) == "3rd" and page.Fmt.ordinal(7) == "7th"
    assert page.Fmt.ordinal(11) == "11th" and page.Fmt.ordinal(21) == "21st" and page.Fmt.ordinal(112) == "112th"


# ------------------------------------------------------------------ 3e. SLO text reaches the SVG <desc> + legend escaped
async def test_slo_text_is_escaped_in_the_chart_description_legend_tile_and_checklist(admin_client, results):
    data = small_summary("xss-slo", datetime(2026, 11, 4, tzinfo=UTC))  # steps at 50 and 100 users
    data["verdict"]["slos"][0].update(target="<img src=x onerror=alert(1)> 800 ms",  # still a number + unit: a line
                                      name="p95 <svg onload=alert(2)> at 50 users", actual='<b>"120" ms</b>')
    write_run(results, "xss-slo", data=data)
    r = await admin_client.get(f"{URL}?run=xss-slo")
    html = r.text
    assert r.status_code == 200 and "data-lt-verdict" in html and csp_problems(html) == []
    assert "<img src=x" not in html and "<svg onload" not in html and "<b>" not in html
    target = "&lt;img src=x onerror=alert(1)&gt; 800 ms"
    desc = re.search(r'<desc id="lt-p95-d">(.*?)</desc>', html, re.S).group(1)
    assert f"The dashed line is the SLO ({target}), judged at 50 users." in desc
    legend = re.search(r'<figure[^>]*aria-labelledby="lt-p95-cap">.*?<ul[^>]*>(.*?)</ul>', html, re.S).group(1)
    assert re.search(rf"<li [^>]*>.*?SLO {re.escape(target)} · judged at 50 users</li>", legend, re.S)
    kpis = re.search(r"data-lt-kpis>(.*?)</section>", html, re.S).group(1)
    assert "p95 at 50 users" in kpis and "&lt;b&gt;&#34;120&#34; ms&lt;/b&gt;" in kpis and f"SLO {target} · met" in kpis
    checklist = re.search(r"data-lt-verdict=.*?</section>", html, re.S).group(0)
    assert "p95 &lt;svg onload=alert(2)&gt; at 50 users" in checklist and f"Target {target}" in checklist


# ------------------------------------------------------------------ never a 500: the display safety net
async def test_a_run_that_cannot_be_displayed_gets_an_error_card_not_a_500(admin_client, fixtures, monkeypatch):
    real_kpis = page.kpis

    def broken_model(*args, **kwargs):
        raise RuntimeError("a bug in the page model")

    monkeypatch.setattr(page, "kpis", broken_model)
    r = await admin_client.get(URL)
    assert r.status_code == 200 and "data-lt-error" in r.text and ">This run could not be displayed<" in r.text
    assert page.DISPLAY_ERROR in r.text and picker_runs(r.text) == [PASS_RUN, FAIL_RUN, BROKEN_RUN, INVALID_RUN]
    assert "data-lt-verdict" not in r.text and f"/{PASS_RUN}/report.md" not in r.text
    monkeypatch.setattr(page, "kpis", real_kpis)

    class BrokenFmt(page.Fmt):
        @staticmethod
        def ms(value):
            raise RuntimeError("a bug in a template helper")

    monkeypatch.setattr(page, "FMT", BrokenFmt())
    r = await admin_client.get(f"{URL}?run={FAIL_RUN}")
    assert r.status_code == 200 and ">This run could not be displayed<" in r.text and "data-lt-verdict" not in r.text
    monkeypatch.setattr(page, "FMT", page.Fmt())
    r = await admin_client.get(f"{URL}?run={BROKEN_RUN}")  # an unreadable file keeps its own wording
    assert ">This run could not be read<" in r.text and page.DISPLAY_ERROR not in r.text


# ------------------------------------------------------------------ the data volume: first, in bold (owner's rule)
async def test_the_data_volume_is_the_first_thing_the_report_shows_in_bold(admin_client, fixtures):
    html = (await admin_client.get(URL)).text
    box = re.search(r"<section[^>]*data-lt-data-volume>(.*?)</section>", html, re.S)
    assert box, "the data-volume banner is missing"
    body = box.group(1)
    assert re.search(r'<h2 id="lt-data-volume" class="[^"]*\bfont-bold\b[^"]*">380,517 database rows ', body)
    assert '<strong class="font-bold tabular-nums">24,318</strong> users' in body
    assert '<strong class="font-bold tabular-nums">186,420</strong> transactions' in body
    first = html.index("data-lt-data-volume")
    for later in ("data-lt-run-line", "data-lt-verdict=", "data-lt-kpis"):
        assert first < html.index(later), f"the data volume must come before {later}"


def test_data_volume_names_the_main_tables_in_order_and_is_absent_without_counts():
    from types import SimpleNamespace

    rows = {"metric_entries": 9, "users": 5, "security_events": 100, "products": 3, "gyms": 2}
    dv = page.data_volume(SimpleNamespace(target=SimpleNamespace(db_rows=rows)))
    assert dv == {"total": 119, "tables": 5,
                  "parts": [(5, "users"), (2, "gyms"), (3, "shop products"), (9, "height & weight readings")]}
    assert page.data_volume(SimpleNamespace(target=SimpleNamespace(db_rows={}))) is None
