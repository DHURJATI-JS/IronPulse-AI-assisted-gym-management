"""Regenerate the load-test fixture runs used by tests/test_superadmin_load_page.py (deterministic: seeded noise).

    .venv/bin/python tests/fixtures/loadtest/make_fixtures.py

Writes tests/fixtures/loadtest/results/<run_id>/ for four runs (newest first by started_at):

* 20261110-1800-prod-like   passing run: 6 steps, 120 timeline points (every 5 s), 40 endpoints, report + Locust HTML
                            (named locust_report.html, like the kit's; files.locust_html points at it)
* 20261109-1200-prod-like   failing run: 5 steps, 60 points (every 10 s), 20 endpoints, report only; the kit's style of
                            latency SLO ("page p95 < 1 s", steps[].slo_p95_ms) with a 300-user step past 3x the SLO
* 20261108-0900-broken      summary.json cut off mid-write (not valid JSON)
* 20261107-0900-invalid     valid JSON that breaks schema version 1 (wrong types, error_rate above 1)
"""

from __future__ import annotations

import json
import random
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"

ENDPOINTS = [  # (name, method, group, weight, base latency ms)
    ("/", "GET", "public", 9.0, 18), ("/gyms", "GET", "public", 7.0, 42), ("/gyms/[slug]", "GET", "public", 6.0, 55),
    ("/plans", "GET", "public", 3.0, 21), ("/jobs", "GET", "public", 1.5, 33), ("/about", "GET", "public", 0.8, 12),
    ("/maps/tiles/[z]/[x]/[y].png", "GET", "public", 4.0, 9), ("/static/dist/app.min.css", "GET", "static", 5.0, 4),
    ("/static/dist/app.min.js", "GET", "static", 5.0, 5), ("/healthz", "GET", "api", 1.0, 6),
    ("/api/ai/status", "GET", "api", 2.0, 7), ("/auth/login", "GET", "auth", 2.5, 16),
    ("/auth/login", "POST", "auth", 2.5, 210), ("/auth/logout", "POST", "auth", 0.6, 24),
    ("/me", "GET", "member", 6.0, 61), ("/me/coach", "GET", "member", 2.0, 74), ("/me/health", "GET", "member", 2.2, 69),
    ("/me/live", "GET", "member", 1.2, 58), ("/me/videos", "GET", "member", 1.6, 47), ("/calendar", "GET", "member", 2.4, 83),
    ("/chat", "GET", "member", 2.6, 77), ("/chat/[id]", "GET", "member", 2.0, 92), ("/chat/[id]/messages", "POST", "member", 1.4, 64),
    ("/shop", "GET", "member", 1.8, 66), ("/shop/[slug]", "GET", "member", 1.1, 58), ("/account/profile", "GET", "member", 0.9, 52),
    ("/owner", "GET", "owner", 1.6, 118), ("/owner/gyms", "GET", "owner", 0.9, 96), ("/owner/staff", "GET", "owner", 0.8, 104),
    ("/owner/customers", "GET", "owner", 1.0, 141), ("/owner/finance", "GET", "owner", 0.7, 163),
    ("/owner/finance/transactions", "GET", "owner", 0.6, 188), ("/owner/shop", "GET", "owner", 0.5, 99),
    ("/owner/reports", "GET", "owner", 0.4, 121), ("/staff", "GET", "staff", 0.9, 71), ("/staff/schedule", "GET", "staff", 0.7, 88),
    ("/staff/clients", "GET", "staff", 0.6, 93), ("/superadmin", "GET", "superadmin", 0.2, 152),
    ("/superadmin/security", "GET", "superadmin", 0.15, 171), ("/superadmin/audit", "GET", "superadmin", 0.1, 134),
]


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _r(value: float, nd: int = 1) -> float:
    return round(value, nd)


def build_run(*, run_id: str, started: datetime, users: list[int], step_s: int, every_s: int, step_rps: list[float],
              p50: list[float], p95: list[float], p99: list[float], err: list[float], passed: list[bool],
              n_endpoints: int, seed: int, verdict: dict, capacity: dict, server: dict, abuse: dict,
              findings: list[dict], target: dict, files: dict, cpu_peak: float, rss_start: float,
              rss_growth: float, pool: int, slo_p95: list[float] | None = None) -> dict:
    rnd = random.Random(seed)
    duration = step_s * len(users)
    steps = []
    for i, u in enumerate(users):
        steps.append({"users": u, "start_s": i * step_s, "end_s": (i + 1) * step_s, "rps": step_rps[i],
                      "error_rate": err[i], "p50_ms": p50[i], "p95_ms": p95[i], "p99_ms": p99[i], "passed": passed[i]})
        if slo_p95 is not None:  # p95 of the requests the latency SLO judges (the kit: page requests only)
            steps[-1]["slo_p95_ms"] = slo_p95[i]
    timeline = []
    top_rps = max(step_rps)
    for t in range(every_s, duration + 1, every_s):
        i = min(len(users) - 1, (t - 1) // step_s)
        into = t - i * step_s
        prev_users = users[i - 1] if i else 0
        ramp = min(1.0, into / 10.0)  # spawn rate: the step's users are all running ~10 s after it starts
        u = round(prev_users + (users[i] - prev_users) * ramp)
        frac = u / users[i]
        rps = step_rps[i] * frac * (1 + rnd.uniform(-0.04, 0.04))
        lat_k = 1 + rnd.uniform(-0.08, 0.1)
        cpu = min(97.0, cpu_peak * (rps / top_rps) ** 0.9 + rnd.uniform(-2.5, 2.5))
        rss = rss_start + rss_growth * t / 60 + rnd.uniform(-4, 4) + (25 if i >= len(users) - 1 else 0) * min(1, into / 60)
        dbc = min(pool, max(1, round(pool * (rps / top_rps) ** 1.4 + rnd.uniform(-1.5, 1.5))))
        timeline.append({
            "t_s": t, "users": u, "rps": _r(max(0.0, rps)), "p50_ms": _r(p50[i] * lat_k), "p95_ms": _r(p95[i] * lat_k),
            "failures_per_s": _r(rps * err[i] * (1 + rnd.uniform(-0.3, 0.3)), 2), "cpu_pct": _r(max(1.0, cpu)),
            "rss_mb": _r(rss), "db_connections": dbc,
        })
    total_requests = int(sum(r * step_s for r in step_rps))
    weights = [e[3] for e in ENDPOINTS[:n_endpoints]]
    wsum = sum(weights)
    endpoints = []
    total_failures = 0
    knee_k = p95[-1] / p95[len(p95) // 2]
    for (name, method, group, weight, base), _w in zip(ENDPOINTS[:n_endpoints], weights, strict=True):
        req = int(total_requests * weight / wsum)
        fail_rate = max(0.0, (sum(err) / len(err)) * (3.2 if group in ("owner", "auth") and method != "GET" else
                                                     2.4 if group == "owner" else 0.6) * rnd.uniform(0.6, 1.4))
        if group == "static":
            fail_rate = 0.0
        fails = int(req * fail_rate)
        total_failures += fails
        k = rnd.uniform(0.9, 1.15)
        e50 = base * k * (1.2 if group == "owner" else 1.0)
        endpoints.append({
            "name": name, "method": method, "group": group, "requests": req, "failures": fails,
            "p50_ms": _r(e50), "p95_ms": _r(e50 * 3.1 * min(knee_k, 4.0) ** 0.5), "p99_ms": _r(e50 * 6.4 * min(knee_k, 4.0) ** 0.6),
            "max_ms": _r(e50 * 18 * min(knee_k, 4.0) ** 0.7), "rps": _r(req / duration, 2),
        })
    classes_spec = [("PublicVisitor", 0.34, ("public", "static", "api")), ("Member", 0.30, ("member", "auth")),
                    ("GymOwner", 0.14, ("owner",)), ("Staff", 0.10, ("staff",)), ("SuperAdmin", 0.02, ("superadmin",)),
                    ("Abuser", 0.10, ())]
    classes = []
    for cname, share, groups in classes_spec:
        rows = [e for e in endpoints if e["group"] in groups]
        if cname == "Abuser":
            classes.append({"name": cname, "weight_share": share, "requests": abuse["requests"],
                            "failures": abuse["requests"] - abuse["throttled"], "p95_ms": 31.0})
            continue
        req = sum(e["requests"] for e in rows)
        fails = sum(e["failures"] for e in rows)
        p95c = max((e["p95_ms"] for e in rows), default=None)
        classes.append({"name": cname, "weight_share": share, "requests": req, "failures": fails, "p95_ms": p95c})
    totals = {
        "requests": total_requests, "failures": total_failures, "error_rate": round(total_failures / total_requests, 5),
        "rps": _r(total_requests / duration), "p50_ms": _r(sorted(p50)[len(p50) // 2] * 1.1),
        "p95_ms": _r(p95[-2] * 1.25), "p99_ms": _r(p99[-1] * 0.8), "max_ms": _r(p99[-1] * 2.6),
    }
    return {
        "schema": 1, "run_id": run_id, "started_at": _iso(started), "ended_at": _iso(started + timedelta(seconds=duration)),
        "duration_s": duration, "target": target,
        "machine": {"cpus": 10, "memory_gb": 32, "os": "macOS 27.2 (arm64)", "python": "3.15.2", "locust_version": "2.43.1"},
        "verdict": verdict, "capacity": capacity, "totals": totals, "steps": steps, "timeline": timeline,
        "endpoints": endpoints, "classes": classes, "abuse": abuse, "server": server, "findings": findings,
        "files": files,
    }


def passing() -> dict:
    return build_run(
        run_id="20261110-1800-prod-like", started=datetime(2026, 11, 10, 18, 0, tzinfo=UTC),
        users=[25, 50, 100, 200, 300, 400], step_s=100, every_s=5,
        step_rps=[52.4, 104.1, 207.3, 405.2, 588.0, 641.7], p50=[38, 41, 47, 68, 118, 410], p95=[96, 104, 131, 236, 512, 1450],
        p99=[180, 196, 240, 470, 910, 2380], err=[0.0, 0.0, 0.0002, 0.0006, 0.0021, 0.0185],
        passed=[True, True, True, True, True, False], n_endpoints=40, seed=1810,
        verdict={"passed": True, "headline": "Holds 300 concurrent users within every SLO; past 400 users latency climbs "
                 "sharply (the knee) as the database pool saturates.", "slos": [
                     {"name": "p95 latency at 200 users", "target": "≤ 800 ms", "actual": "236 ms", "passed": True, "users": 200},
                     {"name": "Error rate at 200 users", "target": "< 1 %", "actual": "0.06 %", "passed": True, "users": 200},
                     {"name": "Throughput at 200 users", "target": "≥ 300 req/s", "actual": "405 req/s", "passed": True},
                     {"name": "Memory growth", "target": "< 5 MB/min", "actual": "1.8 MB/min", "passed": True},
                     {"name": "Abusive traffic throttled", "target": "≥ 95 %", "actual": "98.7 %", "passed": True}]},
        capacity={"max_users_within_slo": 300, "max_rps_within_slo": 588.0, "knee_users": 400,
                  "note": "Between 300 and 400 users p95 latency almost triples while throughput gains 9 % - all 40 pooled "
                          "database connections are busy and requests queue for one."},
        server={"peak_cpu_pct": 91.4, "peak_rss_mb": 611.8, "rss_growth_mb_per_min": 1.8, "peak_db_connections": 40,
                "pool_size": 40, "errors_5xx": 37, "timeouts": 4,
                "log_errors": ["sqlalchemy.exc.TimeoutError: QueuePool limit of size 10 overflow 4 reached (31 times)",
                               "503 Taking too long: statement timeout on GET /owner/finance/transactions (6 times)"]},
        abuse={"requests": 8420, "throttled": 8311, "throttled_share": 0.987,
               "note": "Login floods and scraping were answered with 429 by the rate limiter; 109 requests got through "
                       "before the per-address bucket filled."},
        findings=[
            {"severity": "high", "title": "N+1 queries on /owner/customers",
             "detail": "Each member row loaded its subscription separately (42 queries per page). Eager-loaded in one IN query.",
             "status": "fixed"},
            {"severity": "medium", "title": "Database pool saturates at 400 users",
             "detail": "All 40 pooled connections are busy at the 400-user step; requests wait for a connection.",
             "status": "open"},
            {"severity": "low", "title": "Static files are served by the app server",
             "detail": "Fine at this size; put a CDN or nginx in front before a bigger launch.", "status": "accepted"}],
        target={"host": "http://127.0.0.1:12000", "workers": 4, "environment": "production", "redis": True,
                "notes": "Production settings, seeded with loadtest/seed_loadtest.py; AI calls answered by the fake gateway.",
                "db_rows": {"users": 24318, "gyms": 120, "branches": 342, "subscriptions": 21877, "transactions": 186420,
                            "security_events": 51230, "chat_messages": 96210}},
        files={"report_md": "report.md", "locust_html": "locust_report.html"},
        cpu_peak=91.4, rss_start=486.0, rss_growth=1.8, pool=40)


def failing() -> dict:
    return build_run(
        run_id="20261109-1200-prod-like", started=datetime(2026, 11, 9, 12, 0, tzinfo=UTC),
        users=[25, 50, 100, 200, 300], step_s=120, every_s=10,
        step_rps=[49.8, 97.6, 171.2, 214.9, 221.3], p50=[61, 72, 188, 640, 1320], p95=[190, 248, 905, 1240, 3480],
        p99=[420, 610, 1900, 3010, 7210], err=[0.0, 0.0011, 0.0084, 0.0231, 0.0612],
        passed=[True, True, False, False, False], n_endpoints=20, seed=1209,
        verdict={"passed": False, "headline": "Fails the 200-user SLOs: page p95 1,380 ms and 2.3 % errors - Redis was off "
                 "and every request waited for a database session.", "slos": [
                     {"name": "page p95 < 1 s at 200 users", "target": "< 1 s", "actual": "1,380 ms", "passed": False},
                     {"name": "Error rate at 200 users", "target": "< 1 %", "actual": "2.31 %", "passed": False, "users": 200},
                     {"name": "Throughput at 200 users", "target": "≥ 300 req/s", "actual": "215 req/s", "passed": False},
                     {"name": "Abusive traffic throttled", "target": "≥ 95 %", "actual": "62.0 %", "passed": False}]},
        capacity={"max_users_within_slo": 50, "max_rps_within_slo": 97.6, "knee_users": 100,
                  "note": "Latency rises steeply from 100 users; throughput stops growing at ~220 req/s."},
        server={"peak_cpu_pct": 64.2, "peak_rss_mb": 702.5, "rss_growth_mb_per_min": 9.6, "peak_db_connections": 20,
                "pool_size": 20, "errors_5xx": 812, "timeouts": 96,
                "log_errors": ["sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 0 reached (788 times)",
                               "asyncpg.exceptions.QueryCanceledError: canceling statement due to statement timeout (24 times)"]},
        abuse={"requests": 6120, "throttled": 3794, "throttled_share": 0.62,
               "note": "RATE_LIMIT_WHITELIST still contained 127.0.0.1, so most abusive requests were never throttled."},
        findings=[
            {"severity": "high", "title": "Sessions read from PostgreSQL on every request",
             "detail": "REDIS_URL was empty, so each request held a pooled connection for the session lookup.", "status": "open"},
            {"severity": "high", "title": "Rate limiter bypassed for the load generator",
             "detail": "Remove 127.0.0.1 from RATE_LIMIT_WHITELIST for production-like runs.", "status": "open"},
            {"severity": "medium", "title": "Memory grows 9.6 MB per minute", "detail": "Investigate the live-coach cache.",
             "status": "open"}],
        target={"host": "http://127.0.0.1:12000", "workers": 2, "environment": "production", "redis": False,
                "notes": "First attempt: Redis off, DB_POOL_SIZE=5.", "db_rows": {"users": 24318, "gyms": 120}},
        files={"report_md": "report.md", "locust_html": None},
        cpu_peak=64.2, rss_start=520.0, rss_growth=9.6, pool=20, slo_p95=[205, 270, 980, 1380, 3920])


def write(run: dict, *, report: bool, html: bool) -> Path:  # html: Locust's report, under files.locust_html
    folder = RESULTS / run["run_id"]
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "summary.json").write_text(json.dumps(run, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    if report:
        verdict = "PASS" if run["verdict"]["passed"] else "FAIL"
        (folder / "report.md").write_text(
            f"# Load test {run['run_id']}\n\n**{verdict}** - {run['verdict']['headline']}\n\n"
            f"* Requests: {run['totals']['requests']:,}\n* Error rate: {run['totals']['error_rate'] * 100:.2f} %\n",
            encoding="utf-8")
    if html:  # Locust's own report carries inline scripts: the page must only ever offer it as a download
        (folder / run["files"]["locust_html"]).write_text(
            "<!doctype html><html><head><title>Locust</title><script>window.templateArgs = {};</script></head>"
            "<body><h1>Locust report</h1><script>document.title = 'Locust report';</script></body></html>\n",
            encoding="utf-8")
    return folder


def main() -> None:
    if RESULTS.exists():
        shutil.rmtree(RESULTS)
    write(passing(), report=True, html=True)
    write(failing(), report=True, html=False)
    broken = RESULTS / "20261108-0900-broken"
    broken.mkdir(parents=True)
    text = json.dumps({"schema": 1, "run_id": broken.name, "started_at": "2026-11-08T09:00:00Z",
                       "ended_at": "2026-11-08T09:10:00Z", "duration_s": 600, "target": {"host": "http://127.0.0.1:12000"}},
                      indent=1)
    (broken / "summary.json").write_text(text[: len(text) - 40], encoding="utf-8")  # cut off mid-write
    invalid = passing()
    invalid.update(run_id="20261107-0900-invalid", started_at="2026-11-07T09:00:00Z", ended_at="2026-11-07T09:10:00Z")
    invalid["verdict"] = {"passed": "maybe", "headline": "Half-finished analysis"}
    invalid["totals"] = {**invalid["totals"], "error_rate": 3.5}
    invalid["files"] = {"report_md": "../../../secrets.txt"}
    folder = RESULTS / invalid["run_id"]
    folder.mkdir(parents=True)
    (folder / "summary.json").write_text(json.dumps(invalid, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {len(list(RESULTS.iterdir()))} runs to {RESULTS}")


if __name__ == "__main__":
    main()
