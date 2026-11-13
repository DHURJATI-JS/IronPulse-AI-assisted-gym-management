"""Public load-test results page: GET /load-test (docs/load-test-page.md, "Public page").

A curated, read-only view of the newest run of the production-like Locust stress test that has a readable
summary.json, for everyone: anonymous visitors and every signed-in role (the navbar links it as "Load test results",
``app.core.nav.LOAD_TEST_ITEM``).

GET /load-test[?run=<run_id>]
    the verdict with its SLO checklist; six headline numbers (max users and requests/s within the SLOs, p95 at the SLO
    step, error rate, total requests, 5xx under random junk input); p95 latency and throughput per load step (the
    super-admin page's SVG chart macros and geometry, with the steps table behind them); how the run was tested (date,
    duration, load profile, machine, workers, PostgreSQL / Redis, data volume, AI mode, the simulated user mix with the
    junk senders and abusers) and a disclaimer. ``?run=`` picks one of the ``PUBLISHED_RUNS`` newest readable runs (the
    picker); any other value is a 404 - other run folders cannot be probed from here.

Never shown here - they stay on /superadmin/load-test: the endpoint table (owner / staff / admin routes), the timeline
(server CPU, memory, database connections), server log lines, findings, abuse and IP details, report / Locust
downloads, run folders, error messages and file paths. The template never receives the summary itself, only the
curated values ``public_view`` picks.

PUBLIC_LOADTEST_PAGE=false (``Settings.public_loadtest_page``): the page is a 404 and the navbar link is hidden.
No SQL: the page reads files only (app/services/loadtest_results.py - cached, path-safe - in a worker thread); beyond
the layout's own session lookup it never touches the database. No results yet is an empty state; a run that cannot be
read or displayed is a neutral "results unavailable" card - never a 500.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool

from app.config import get_settings, is_loopback_host
from app.core.deps import NotFound
from app.core.nav import LOAD_TEST_ITEM
from app.core.templating import render
from app.models.enums import Role
from app.routers import superadmin_loadtest as admin_page
from app.services import loadtest_results as lt

router = APIRouter()
log = logging.getLogger("ironpulse.loadtest")

PAGE_PATH = LOAD_TEST_ITEM.href  # /load-test
TEMPLATE = "public/load_test.html"
PUBLISHED_RUNS = 5  # the newest readable runs: the picker lists them, ?run= accepts nothing else
TILE_KEYS = ("users", "rps", "p95", "errors", "requests")  # of the super-admin KPI tiles (then the junk tile)
FMT = admin_page.FMT

# Locust user classes of the kit (loadtest/locustfile.py) and the fixtures -> (label, what they do); unknown names are
# split at their capitals ("BrowserUser" -> "Browser user")
USER_CLASSES: dict[str, tuple[str, str]] = {
    "anonymousvisitor": ("Anonymous visitors", "browse gyms, plans, the shop, jobs and the legal pages"),
    "publicvisitor": ("Anonymous visitors", "browse gyms, plans, the shop, jobs and the legal pages"),
    "member": ("Members", "dashboard, health, chat, the shop and checkout, a few AI questions"),
    "owner": ("Gym owners", "staff, members, finance and the shop"),
    "gymowner": ("Gym owners", "staff, members, finance and the shop"),
    "staff": ("Trainers and staff", "schedule, clients and chat"),
    "superadmin": ("Super-admins", "the platform console"),
    "abuser": ("Abusers", "floods, password guessing, scraping and scanners - expected to be throttled"),
    "junkuser": ("Junk senders", "random junk input into every form, URL and API - expected to be refused, never a 5xx"),
}
# notes / capacity note / SLO names of the kit: "AI simulated", "AI mode: simulated", "(provider: simulated)"
_AI_MODE = re.compile(r"\bAI(?:\s+mode)?\s*:?\s*((?i:simulated|off|gateway))\b")
_AI_PROVIDER = re.compile(r"\bprovider:\s*((?i:simulated|off|gateway))\b")
AI_MODES = {
    "simulated": "Simulated - no AI model was called (realistic latency, real guard and queue)",
    "off": "Off - AI features disabled",
    "gateway": "Real AI model through the AI gateway",
}
_JUNK_SLO = re.compile(r"\b5xx\b.*\bjunk input\b", re.IGNORECASE)  # the kit's "zero 5xx under random junk input"
_LEADING_5XX = re.compile(r"\s*(\d[\d,]*)\s*5xx\b", re.IGNORECASE)  # its actual: "0 5xx, 0 timeouts/... of 690"
DATA_TABLES = (("users", "users"), ("gyms", "gyms"), ("branches", "branches"), ("subscriptions", "memberships"),
               ("products", "shop products"))
UNKNOWN = "Not recorded"


# ===================================================================================== page model
def verdict_kind(passed: bool | None, headline: str) -> str:
    """"pass", "inconclusive" (a run that did not pass and whose headline says so - the kit's INCONCLUSIVE: the machine
    was busy, nothing was judged) or "fail"."""
    if passed:
        return "pass"
    return "inconclusive" if headline.strip().lower().startswith("inconclusive") else "fail"


def class_label(name: str) -> tuple[str, str]:
    known = USER_CLASSES.get(re.sub(r"[^a-z]", "", name.lower()))
    if known:
        return known
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name).replace("_", " ").split()
    text = " ".join(words)
    return (text[:1].upper() + text[1:].lower() if text else "Other users"), ""


def ai_mode(s: lt.Summary) -> str | None:
    """The run's AI mode ("simulated" / "off" / "gateway") as the kit records it, else None."""
    for text in (s.target.notes, s.capacity.note, *(slo.name for slo in s.verdict.slos)):
        m = _AI_MODE.search(text) or _AI_PROVIDER.search(text)
        if m:
            return m.group(1).lower()
    return None


def junk_tile(s: lt.Summary) -> dict[str, Any]:
    """5xx under random junk input: the kit's SLO row of that name (its count of 5xx and whether it held)."""
    tile = {"key": "junk", "label": "5xx under random junk input", "icon": "x-circle"}
    slo = next((x for x in s.verdict.slos if _JUNK_SLO.search(x.name)), None)
    if slo is None:
        return {**tile, "value": "—", "hint": "no junk input in this run", "tone": "zinc"}
    junk = sum(c.requests for c in s.classes if "junk" in c.name.lower())
    m = _LEADING_5XX.match(slo.actual)
    sent = f"{FMT.num(junk)} junk requests · " if junk else ""
    return {**tile, "value": m.group(1) if m else (slo.actual or "—"),
            "hint": f"{sent}target {slo.target} · {'met' if slo.passed else 'missed'}",
            "tone": "green" if slo.passed else "red"}


def same_machine(host: str) -> bool:
    """The tested host is this machine (a loopback address): the app server ran beside the load generator."""
    try:
        name = urlsplit(host if "//" in host else f"//{host}").hostname
    except ValueError:
        return False
    return is_loopback_host(name)


def how_tested(s: lt.Summary) -> list[tuple[str, str]]:
    """(label, value) rows of "How it was tested" - the run's date is added by the template (viewer's time zone)."""
    t, m = s.target, s.machine
    machine = UNKNOWN
    if m is not None:
        machine = f"{m.cpus} CPU{'s' if m.cpus != 1 else ''} · {m.memory_gb:g} GB memory · {m.os}"
        if same_machine(t.host):
            machine += " - the app server and the load generator shared it"
    data = UNKNOWN
    if t.db_rows:
        total = sum(t.db_rows.values())
        parts = [f"{FMT.compact(total)} rows in {len(t.db_rows)} table{'s' if len(t.db_rows) != 1 else ''}"]
        parts += [f"{FMT.num(t.db_rows[key])} {label}" for key, label in DATA_TABLES if t.db_rows.get(key)]
        data = " · ".join(parts)
    mode = ai_mode(s)
    rows = [
        ("Duration", FMT.duration(s.duration_s)),
        ("Load profile", admin_page.plan_text(s)),
        ("Machine", machine),
        ("App server", f"{t.workers} worker process{'es' if t.workers != 1 else ''} · {t.environment} settings"),
        ("Database & cache", "PostgreSQL · Redis" if t.redis else "PostgreSQL · no Redis"),
        ("Data volume", data),
        ("AI", AI_MODES.get(mode, mode.capitalize()) if mode else UNKNOWN),
    ]
    if m is not None:
        rows.append(("Load generator", f"Locust {m.locust_version}"))
    return rows


def user_mix(s: lt.Summary) -> list[dict[str, Any]]:
    out = []
    for c in sorted(s.classes, key=lambda c: (-c.weight_share, c.name.lower())):
        label, what = class_label(c.name)
        out.append({"label": label, "what": what, "share": c.weight_share})
    return out


def public_view(s: lt.Summary) -> dict[str, Any]:
    """The curated values the public template gets (never the summary itself)."""
    tiles = {k["key"]: k for k in admin_page.kpis(s)}
    return {
        "data_volume": admin_page.data_volume(s),  # shown first, in bold
        "verdict": verdict_kind(s.verdict.passed, s.verdict.headline),
        "headline": s.verdict.headline,
        "note": s.capacity.note,
        "slos": list(s.verdict.slos),
        "tiles": [tiles[key] for key in TILE_KEYS] + [junk_tile(s)],
        "steps": admin_page.steps_view(s),
        "plan": admin_page.plan_text(s),
        "knee_users": s.capacity.knee_users,
        "started_at": s.started_at,
        "ended_at": s.ended_at,
        "duration": FMT.duration(s.duration_s),
        "users_peak": s.users_peak,
        "tested": how_tested(s),
        "mix": user_mix(s),
    }


def run_url(run_id: str) -> str:
    return f"{PAGE_PATH}?{urlencode({'run': run_id})}"


def build_page(run_param: str) -> dict[str, Any] | None:
    """Everything the template needs (file work: call it in a worker thread). None: ``run_param`` is not one of the
    published runs (a 404)."""
    listing = lt.list_runs()
    published = [r for r in listing.runs if r.passed is not None][:PUBLISHED_RUNS]
    if run_param and all(r.run_id != run_param for r in published):
        return None
    run_id = run_param or (published[0].run_id if published else None)
    run = lt.load_run(run_id) if run_id else None
    view = None
    if run is not None and run.summary is not None:
        try:
            view = public_view(run.summary)
        except Exception:  # one run's data never takes the page down
            log.exception("load-test run %s could not be shown on the public page", run_id)
    state = "results" if view is not None else ("unavailable" if listing.runs else "empty")
    runs = [{"href": run_url(r.run_id), "started_at": r.started_at, "users_peak": r.users_peak,
             "verdict": verdict_kind(r.passed, r.headline), "current": r.run_id == run_id} for r in published]
    return {"state": state, "view": view, "run_id": run_id, "runs": runs, "fmt": FMT, "page_path": PAGE_PATH,
            "admin_url": None}


# ===================================================================================== route
@router.get(PAGE_PATH)
async def public_load_test_page(request: Request):
    if not get_settings().public_loadtest_page:
        raise NotFound()
    page = await run_in_threadpool(build_page, (request.query_params.get("run") or "").strip())
    if page is None:
        raise NotFound("That load-test run is not published.")
    user = getattr(request.state, "user", None)
    if user is not None and user.role == Role.SUPERADMIN:  # the full report (endpoints, server, findings, downloads)
        page["admin_url"] = admin_page.page_url(page["run_id"])
    try:
        return render(request, TEMPLATE, page)
    except Exception:
        if page["view"] is None:  # the empty / unavailable card itself failed: a real bug, let it surface
            raise
        log.exception("load-test run %s could not be rendered on the public page", page["run_id"])
        return render(request, TEMPLATE, {**page, "state": "unavailable", "view": None})
