"""No N+1 queries: EVERY GET page and GET JSON API of the app, as every role, runs the same SQL on a small world and on
a large one - and no database call runs once per row of a loop in app/** unexplained.

For each role (anonymous, member, staff/trainer, owner, super-admin) the test seeds the SMALL world of
tests/nplus_world.py (one or two rows of every collection the viewer can see), fills every route's path parameters from
it, and records the SQL of each route through the ASGI client (``app.db.QueryCounter`` + the statements themselves).
Then it GROWS the same viewers' data (``n`` more rows in every collection, nested ones included: items of the order,
messages of the conversation, branches of the gym, clients of the trainer, documents of the branch under review ...)
and records again. A route fails as an N+1 (or an unbounded per-row query) when, on the large world,

* it runs more statements in total, or
* ANY normalised statement runs at least twice more often (a per-row repeat, even when the small world happened to
  take a costlier path that hides it in the total), on the measured request - or on the FIRST view, which is compared
  separately: work that runs only once per state (marking a conversation read, a first visit) is measured cold too.

The table names the route, the role, both counts and the statements that repeat (normalised).

Routes are enumerated from ``app.main.app`` (FastAPI's included-router wrappers unwrapped, mounted sub-apps crawled,
static mounts and WebSocket routes skipped; an unknown route type fails the test), so a new route is measured the day
it is added. Skipped: the routes ``tests/n_plus_one_allowlist.json`` lists under ``skip_routes`` - each with its reason
(file downloads, static files, health, external proxies). A route the role may not see (3xx/4xx) is skipped for that
role; but every route must answer 200 for at least one role - or be listed under ``unreachable_routes`` with the reason
(a redirect to a measured page, a one-time token ...) - and a route that answers 200 on the small world must answer 200
on the large one. A bounded, explained growth is allowed only by a ``route_growth`` entry with its ``max_extra``.

Each request runs with the in-process TTL caches cleared (the cold, worst case). A larger count at the large size is
re-measured twice and the smallest is kept, so a one-off background write never fails the test. ``NPLUS_REPORT=<path>``
writes every measurement as JSON; ``NPLUS_GROW=<n>`` changes the growth (default 25).

POST routes that act on many rows of the viewer (mark all read, delete the account, check out the bag) are measured
with 2 and with 20 rows. The LOOP SCAN (scripts/find_query_loops.py) lists every database call made once per iteration
of a loop or comprehension of app/** - directly, one call deeper (a helper handed the session, a function / method /
closure that runs SQL or opens its own session), fanned out (``gather`` / ``create_task`` per row) or as an
``async for`` per outer row. Each one is a real N+1 (this test fails, printing file:line and the loop) or allowlisted
under ``loops`` with the reason it is not one, pinned to the code that reason was written for. Background work is
measured in tests/test_no_n_plus_one_jobs.py."""

from __future__ import annotations

import json
import os
import re
import textwrap
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import event, func, select
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles

from app import db as dbmod
from app.core import ttl_cache
from app.db import QueryCounter
from tests import nplus_world as world_mod
from tests.conftest import api, csrf_token, login_as, make_client, post_form

ALLOWLIST_PATH = Path(__file__).with_name("n_plus_one_allowlist.json")
ROLES = ("anonymous", "member", "staff", "owner", "superadmin")
GROW = int(os.environ.get("NPLUS_GROW") or 25)
RETRIES = 2  # extra measurements of a route whose large count is higher (the smallest count is kept)
SHAPE_REPEAT = 2  # one normalised statement running this many more times on the large world = a per-row repeat
# routes measured per role (200 on the small world) must not silently drop out: ~90 % of what each role reached on
# 2026-11-13 (anonymous 34, member 85, staff 59, owner 125, super-admin 75 URLs)
MIN_MEASURED = {"anonymous": 30, "member": 76, "staff": 53, "owner": 112, "superadmin": 67}
HEADERS = {"User-Agent": "pytest-ironpulse/1.0"}
# small-world status of every route per role (the reachability test runs after the five role crawls)
ROUTE_STATUS: dict[str, dict[str, int]] = {}
VARIANT_STATUS: dict[str, dict[str, int]] = {}  # "<route> variant <i>" per role
VARIANT_URL: dict[str, str] = {}  # "<route> variant <i>" -> its URL (the last role's)
ROLES_CRAWLED: set[str] = set()


def must_grow(role: str, w: world_mod.World) -> list[str]:
    """Pages that must render visibly more on the large world (>= 10 % more bytes): proof that the growth reaches the
    viewer - list pages, and the nested sections of single-entity pages - so a page can never pass because the large
    world is invisible to it."""
    return {
        "anonymous": ["/gyms", "/shop", "/jobs", f"/gyms/{w.gym_slug}/branches/{w.branch}"],
        "member": ["/account/notifications", "/shop/orders", "/shop/wishlist", "/tasks", "/chat", "/me/health/alerts"],
        "staff": ["/staff/clients", "/staff/schedule", "/chat", "/tasks"],
        "owner": ["/owner/customers", "/owner/staff", "/owner/shop/orders", "/owner/audit", "/owner/jobs", "/chat",
                  f"/owner/staff/{w.employee}/settlement"],
        "superadmin": ["/superadmin/users", "/superadmin/audit", "/superadmin/security", "/superadmin/reviews",
                       f"/superadmin/reviews/branches/{w.review_branch}",
                       f"/superadmin/reviews/employees/{w.review_employee}", f"/superadmin/security/ip/{w.ip}"],
    }[role]


def allowlist() -> dict[str, Any]:
    return json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8"))


# ==================================================================================================== routes
def iter_routes(routes: Any, prefix: str = "") -> Iterator[tuple[str, Route]]:
    """(path, route) of every HTTP endpoint. FastAPI >= 0.16x wraps included routers in ``_IncludedRouter``
    (``.original_router``, its include prefix in ``.include_context``); mounted sub-apps are crawled with their mount
    path; static-file mounts and WebSocket routes have no pages. Anything else fails: a route type this crawler does
    not know could hide pages."""
    for r in routes:
        inner = getattr(r, "original_router", None)
        if inner is not None:
            extra = getattr(getattr(r, "include_context", None), "prefix", "") or ""
            yield from iter_routes(inner.routes, prefix + extra)
        elif isinstance(r, Route):
            yield prefix + r.path, r
        elif isinstance(r, WebSocketRoute):
            continue
        elif isinstance(r, Mount):
            if isinstance(r.app, StaticFiles):
                continue
            sub = list(getattr(r, "routes", None) or [])
            assert sub, (f"mounted app at {prefix + r.path} ({type(r.app).__name__}) exposes no routes to crawl - teach "
                         "tests/test_no_n_plus_one.py iter_routes() about it, or skip it with a reason")
            yield from iter_routes(sub, prefix + r.path)
        else:
            raise AssertionError(f"unknown route type {type(r).__name__} at {getattr(r, 'path', '?')}: teach "
                                 "tests/test_no_n_plus_one.py iter_routes() how to crawl it")


def get_routes(app: Any) -> list[str]:
    """Paths of every GET route (HTTP routes only: the WebSocket route has no methods)."""
    return sorted({path for path, r in iter_routes(app.routes) if "GET" in (r.methods or set())})


PARAM = re.compile(r"{([a-zA-Z_]+)(?::[a-z]+)?}")


def fill(path: str, role: str, w: world_mod.World) -> str | None:  # noqa: PLR0911, PLR0912 - one lookup table
    """The URL of ``path`` for ``role`` with every path parameter taken from the world (None: a parameter the world
    cannot fill - the test reports it)."""

    def value(name: str) -> Any:
        p = path
        if name in ("slug", "gym_slug"):
            if p.startswith("/me/videos/"):
                return w.video_slug
            if p.startswith("/shop/p/"):
                return w.product_slug
            return w.gym_slug
        if name == "gym_id":
            return w.gym
        if name == "branch_id":
            return w.review_branch if p.startswith("/superadmin/reviews/") else w.branch
        if name == "employee_id":
            return w.review_employee if p.startswith("/superadmin/reviews/") else w.employee
        if name == "job_id":
            return w.payroll_job if p.startswith("/owner/payroll/runs/") else w.job_posting
        if name == "doc_id":
            if p.startswith("/owner/branches/"):
                return w.branch_doc
            if p.startswith("/owner/staff/"):
                return w.employee_doc
            return w.identity_doc
        if name == "customer_id":
            return w.member
        if name == "statement_id":
            return w.statement
        if name == "sid":
            return w.pending_subscription
        if name == "check_id":
            return w.wellness_check
        if name == "thread_id":
            return w.staff_thread if role == "staff" else w.thread
        if name == "report_id":
            return w.report
        if name == "ip":
            return w.ip
        if name == "email_id":
            return w.email
        if name == "session_id":
            return w.workout_session
        if name == "product_id":
            return w.product
        if name == "order_id":
            if p.endswith(("/pay", "/test-gateway")):
                return w.pending_order
            return w.order
        if name == "user_id":
            return w.trainer if role == "anonymous" else w.member
        if name == "enquiry_id":
            return w.enquiry
        if name == "side":
            return "front"
        return None

    out = path
    for name in PARAM.findall(path):
        v = value(name)
        if v is None or v == "":
            return None
        out = re.sub(r"{" + name + r"(?::[a-z]+)?}", str(v), out, count=1)
    return out


def variants(path: str, w: world_mod.World) -> list[str]:
    """Other views of a route (tabs, filters, query strings) that read other rows than its default view - measured
    like any route. Each must answer 200 for some role (the reachability test), so a typo cannot hide a view."""
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()  # the world's people live in IST
    tabs = {
        "/jobs": ["/jobs?lat=12.9716&lon=77.6412", "/jobs?lat=12.9716&lon=77.6412&view=all", "/jobs?q=coach"],
        "/shop": ["/shop?q=whey", "/shop?sort=rating", "/shop?sort=price_asc", "/shop?category=supplements"],
        "/gyms": ["/gyms?city=Bengaluru"],
        "/profile/{user_id}": [f"/profile/{w.trainer}"],
        "/chat": ["/chat?status=closed", "/chat?scope=all"],
        "/chat/{thread_id}": [f"/chat/{w.staff_thread}"],
        "/api/chat/{thread_id}/messages": [f"/api/chat/{w.staff_thread}/messages"],
        "/account/notifications": ["/account/notifications?filter=unread"],
        "/api/notifications": ["/api/notifications?limit=50"],
        "/calendar": [f"/calendar?day={today}"],
        "/me/videos": ["/me/videos?goal=general_fitness"],
        "/me/coach/status": [f"/me/coach/status?job={w.coach_job}"],
        "/api/shop/stores": ["/api/shop/stores?q=Iron"],
        "/shop/checkout": [f"/shop/checkout?gym={w.gym}"],
        "/checkout/{sid}/gateway": [f"/checkout/{w.pending_subscription}/gateway?order_id={w.checkout_orders.get('cashfree')}"],
        "/checkout/{sid}/test-gateway": [
            f"/checkout/{w.pending_subscription}/test-gateway?order_id={w.checkout_orders.get('test')}"],
        "/auth/reset": [f"/auth/reset?token={w.tokens.get('reset')}"],
        "/auth/invite": [f"/auth/invite?token={w.tokens.get('invite')}"],
        "/auth/staff-invite": [f"/auth/staff-invite?token={w.tokens.get('staff-invite')}"],
        "/owner/staff": ["/owner/staff?status=active", f"/owner/staff?gym={w.gym}", "/owner/staff?role=trainer",
                         "/owner/staff?q=Applicant"],
        "/owner/staff/{employee_id}": [f"/owner/staff/{w.employee}?tab={t}"
                                       for t in ("schedule", "clients", "documents", "profile", "activity")],
        "/owner/customers": ["/owner/customers?q=Member", "/owner/customers?sort=name", f"/owner/customers?gym={w.gym}",
                             "/owner/customers?status=active", "/owner/customers?status=expired",
                             "/owner/customers?tag=asthma"],
        "/owner/jobs": ["/owner/jobs?state=closed", f"/owner/jobs?gym={w.gym}"],
        "/owner/jobs/{job_id}/enquiries": [f"/owner/jobs/{w.job_posting}/enquiries?status=new"],
        "/owner/audit": [f"/owner/audit?gym={w.gym}", "/owner/audit?q=gym", "/owner/audit?area=staff"],
        "/owner/reports": ["/owner/reports?status=open"],
        "/owner/shop/reviews": ["/owner/shop/reviews?show=hidden"],
        "/owner/shop/orders": ["/owner/shop/orders?status=completed"],
        "/owner/shop/products": ["/owner/shop/products?status=active"],
        "/owner/finance/transactions": [f"/owner/finance/transactions?gym={w.gym}"],
        "/superadmin/users": ["/superadmin/users?q=member"],
        "/superadmin/reviews": ["/superadmin/reviews?tab=staff", "/superadmin/reviews?tab=identity"],
        "/superadmin/trust": ["/superadmin/trust?tab=violations", "/superadmin/trust?tab=ips",
                              "/superadmin/trust?tab=identities"],
        "/superadmin/security": ["/superadmin/security?triage=pending", "/superadmin/security?range=7d"],
        "/superadmin/outbox": ["/superadmin/outbox?status=sent"],
    }
    return tabs.get(path, [])


def urls_for(role: str, w: world_mod.World, paths: list[str], skip: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """{url: route path} to measure for ``role``, and the routes whose parameters the world cannot fill."""
    urls: dict[str, str] = {}
    unfilled: list[str] = []
    for path in paths:
        if path in skip:
            continue
        url = fill(path, role, w)
        if url is None:
            unfilled.append(path)
            continue
        urls[url] = path
        for extra in variants(path, w):
            urls[extra] = path
    return urls, unfilled


# ==================================================================================================== measuring
_capture: ContextVar[list[str] | None] = ContextVar("nplus_capture", default=None)


def _record(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
    box = _capture.get()
    if box is not None:
        box.append(statement)


@dataclass
class Sample:
    status: int
    count: int = 0
    statements: list[str] = field(default_factory=list)
    location: str | None = None
    seconds: float = 0.0
    size: int = 0  # bytes of the body (the large world renders more rows: the growth is visible)


def normalise(sql: str) -> str:
    """One line, placeholders / literals / IN lists / multi-row VALUES folded - equal for every run of the same
    statement, whatever the number of rows it carries."""
    s = " ".join(sql.split())
    s = re.sub(r"\$\d+(::[A-Za-z_ ]+(\[\])?)?", "?", s)
    s = re.sub(r"'(?:[^']|'')*'", "'S'", s)
    s = re.sub(r"\b\d+(\.\d+)?\b", "N", s)
    s = re.sub(r"\(\?(?:, \?)*\)", "(?...)", s)  # IN (?), IN (?, ?) ...: one shape
    s = re.sub(r"(?:\?, )+\?", "?...", s)
    s = re.sub(r"(\((?:\?\.\.\.|\?)(?:, N)?\))(?:, \((?:\?\.\.\.|\?)(?:, N)?\))+", r"\1, ...", s)  # VALUES (..), (..)
    return s


def clear_caches() -> None:
    for cache in list(ttl_cache._caches.values()):  # noqa: SLF001 - every in-process TTL cache: the cold case
        cache.clear()


async def recorded(fn: Callable[[], Awaitable[Any]]) -> tuple[Any, int, list[str]]:
    """Run ``fn`` and return (its result, the statements counted, the statements' SQL)."""
    box: list[str] = []
    token = _capture.set(box)
    try:
        with QueryCounter() as qc:
            out = await fn()
    finally:
        _capture.reset(token)
    return out, qc.count, list(box)


async def sample(client: Any, url: str) -> Sample:
    clear_caches()
    started = time.perf_counter()
    r, count, statements = await recorded(lambda: client.get(url, headers=HEADERS))
    return Sample(r.status_code, count, statements, r.headers.get("location"), time.perf_counter() - started,
                  len(r.content))


async def measure(client: Any, url: str) -> tuple[Sample, Sample]:
    """(the FIRST view - cold: first-view work such as mark-as-read runs here -, then the measured request)."""
    first = await sample(client, url)
    if first.status != 200:
        return first, Sample(first.status, location=first.location)
    return first, await sample(client, url)


def repeats(small: Sample, large: Sample, top: int = 4) -> list[str]:
    """The normalised statements the large request ran more often than the small one (+delta, most first)."""
    a, b = Counter(map(normalise, small.statements)), Counter(map(normalise, large.statements))
    grown = sorted(((b[s] - a[s], s) for s in b if b[s] > a[s]), reverse=True)
    return [f"+{d} x {s[:260]}" for d, s in grown[:top]]


def shape_growth(small: list[str], large: list[str], at_least: int = SHAPE_REPEAT) -> list[str]:
    """The normalised statements the large run made at least ``at_least`` more times: a per-row repeat, even when the
    total is masked (the small world took a costlier path, or another statement went away)."""
    a, b = Counter(map(normalise, small)), Counter(map(normalise, large))
    grown = sorted(((b[s] - a[s], s) for s in b if b[s] - a[s] >= at_least), reverse=True)
    return [f"+{d} x {s[:260]}" for d, s in grown]


def grows(s: Sample, g: Sample, extra: int = 0) -> bool:
    return g.count > s.count + extra or bool(shape_growth(s.statements, g.statements, max(SHAPE_REPEAT, extra + 1)))


def allowance(path: str, role: str) -> tuple[int, str | None]:
    entry = allowlist().get("route_growth", {}).get(f"{path} [{role}]") or allowlist().get("route_growth", {}).get(path)
    if not entry:
        return 0, None
    return int(entry["max_extra"]), entry["reason"]


def table(rows: list[tuple[str, str, int, int, list[str]]], title: str) -> str:
    out = [f"{'route':60} {'role':10} {'small':>5} {'large':>5}  statements that repeat (normalised)"]
    for url, r, a, b, rep in sorted(rows, key=lambda x: x[3] - x[2], reverse=True):
        out.append(f"{url:60} {r:10} {a:5d} {b:5d}  " + ("\n" + " " * 84).join(rep or ["(no single statement repeats)"]))
    return title + "\n" + "\n".join(out)


# ==================================================================================================== the GET crawl
def test_crawler_finds_the_routes(app):
    paths = get_routes(app)
    assert len(paths) > 100, paths  # guard against a vacuous crawl
    assert {"/", "/gyms", "/owner", "/me", "/superadmin", "/staff", "/api/notifications"} <= set(paths)
    lists = allowlist()
    for section in ("skip_routes", "unreachable_routes"):
        entries = lists.get(section, {})
        assert all(isinstance(reason, str) and len(reason) >= 10 for reason in entries.values()), \
            f"every {section} entry needs a reason"
        stale = sorted(set(entries) - set(paths))
        assert not stale, f"{section} entries that match no GET route any more (remove them): {stale}"


def test_the_crawler_knows_every_route_type():
    """Mounted sub-apps are crawled with their prefix, static mounts skipped, unknown route types fail."""
    from fastapi import APIRouter, FastAPI

    sub = FastAPI()

    @sub.get("/inner")
    async def inner() -> dict[str, int]:
        return {}

    router = APIRouter()

    @router.get("/routed")
    async def routed() -> dict[str, int]:
        return {}

    top = FastAPI()
    top.include_router(router, prefix="/pre")
    top.mount("/sub", sub)
    top.mount("/files", StaticFiles(directory=str(Path(__file__).parent)))
    assert {"/sub/inner", "/pre/routed"} <= set(get_routes(top))

    class Odd:
        path = "/odd"

    with pytest.raises(AssertionError, match="unknown route type"):
        list(iter_routes([Odd()]))


@pytest.mark.parametrize("role", ROLES)
async def test_no_route_runs_more_statements_on_a_larger_world(app, db, role):
    w = await world_mod.build(db)
    skip = allowlist()["skip_routes"]
    paths = get_routes(app)
    urls, unfilled = urls_for(role, w, paths, skip)
    engine = dbmod.get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", _record)
    client = make_client()
    small: dict[str, Sample] = {}
    large: dict[str, Sample] = {}
    cold_small: dict[str, Sample] = {}
    cold_large: dict[str, Sample] = {}
    try:
        if role != "anonymous":
            await login_as(client, await db.get(world_mod.User, w.users[role]))
        await client.get("/healthz")  # CSRF cookie
        for url in urls:
            cold_small[url], small[url] = await measure(client, url)
        for url, s in cold_small.items():  # what this role reached (the reachability test reads it)
            ROUTE_STATUS.setdefault(urls[url], {})[role] = 200 if 200 in (s.status, small[url].status) else s.status
        for path in {urls[u] for u in urls}:  # keyed by position: a variant's ids differ from world to world
            for i, v in enumerate(variants(path, w)):
                VARIANT_STATUS.setdefault(f"{path} variant {i + 1}", {})[role] = cold_small[v].status if v in cold_small else 0
                VARIANT_URL[f"{path} variant {i + 1}"] = v
        ROLES_CRAWLED.add(role)
        await world_mod.grow(db, w, GROW)
        for url, s in small.items():
            if s.status != 200:
                continue
            cold_large[url], got = await measure(client, url)
            extra, _ = allowance(urls[url], role)
            for _ in range(RETRIES):
                if got.status != 200 or not grows(s, got, extra):
                    break
                again = await sample(client, url)
                if again.status == 200 and (again.count, len(shape_growth(s.statements, again.statements))) < (
                        got.count, len(shape_growth(s.statements, got.statements))):
                    got = again
            large[url] = got
    finally:
        event.remove(engine, "before_cursor_execute", _record)
        await client.aclose()

    rows, broken = [], []
    for url, s in small.items():
        if s.status != 200 or url not in large:
            continue
        g = large[url]
        path = urls[url]
        if g.status != 200:
            broken.append(f"{url} [{role}]: 200 on the small world, {g.status} on the large one "
                          f"({g.location or 'no redirect'}) - the large world hides the page from the comparison")
            continue
        extra, _ = allowance(path, role)
        if grows(s, g, extra):
            rows.append((url, role, s.count, g.count,
                         shape_growth(s.statements, g.statements, max(SHAPE_REPEAT, extra + 1)) or repeats(s, g)))
        cs, cl = cold_small.get(url), cold_large.get(url)
        if cs and cl and cs.status == cl.status == 200:
            first = shape_growth(cs.statements, cl.statements, max(SHAPE_REPEAT, extra + 1))
            if first:
                rows.append((f"{url} (first view)", role, cs.count, cl.count, first))

    report = {"role": role, "grow": GROW, "unfilled": unfilled,
              "routes": {url: {"route": urls[url], "small_status": s.status, "small": s.count,
                               "large_status": large[url].status if url in large else None,
                               "large": large[url].count if url in large else None,
                               "first_small": cold_small[url].count, "first_large": cold_large[url].count
                               if url in cold_large else None,
                               "location": s.location or cold_small[url].location,
                               "repeats": repeats(s, large[url]) if url in large else [],
                               "first_repeats": shape_growth(cold_small[url].statements, cold_large[url].statements, 1)
                               if url in cold_large else [],
                               "small_bytes": s.size, "large_bytes": large[url].size if url in large else None}
                         for url, s in small.items()}}
    if os.environ.get("NPLUS_REPORT"):
        out = Path(os.environ["NPLUS_REPORT"])
        data = json.loads(out.read_text()) if out.exists() else {}
        data[role] = report
        out.write_text(json.dumps(data, indent=2, default=str))

    measured = sum(1 for u in small if small[u].status == 200)
    assert measured >= MIN_MEASURED[role], (
        f"[{role}] only {measured} routes answered 200 (at least {MIN_MEASURED[role]} expected) - the world or the "
        "login is broken: " + ", ".join(f"{u}={s.status}" for u, s in small.items()))
    problems = []
    if unfilled:
        problems.append(f"routes whose path parameters the world cannot fill (add them to tests/test_no_n_plus_one.py "
                        f"fill() or skip_routes with a reason): {unfilled}")
    invisible = [f"{u}: {small[u].size} -> {large[u].size} bytes" if u in large else f"{u}: not measured "
                 f"(small {small[u].status if u in small else 'not requested'})"
                 for u in must_grow(role, w)
                 if u not in large or large[u].status != 200 or large[u].size < small[u].size * 1.1]
    if invisible:
        problems.append(f"[{role}] the large world does not show on these pages (tests/nplus_world.py grow() no longer "
                        f"reaches the viewer, so the comparison would be vacuous): {invisible}")
    problems += broken
    if rows:
        problems.append(table(rows, f"N+1: statements grow with the data (small world -> +{GROW} rows per collection; "
                                    f"'(first view)': the cold first request, compared on its own):"))
    assert not problems, "\n\n".join(problems)


def test_every_get_route_is_measured_or_explained(app):
    """Runs after the five role crawls above: a route that never answered 200 for any role is not measured at all, so
    it must be listed under ``unreachable_routes`` with the reason (and leave the list once it is measured); every
    view of ``variants()`` must answer 200 for some role."""
    if set(ROLES) - ROLES_CRAWLED:
        pytest.skip("needs the five role crawls of this module in the same run (run the whole file)")
    lists = allowlist()
    skip, unreachable = lists["skip_routes"], lists.get("unreachable_routes", {})
    never = sorted(p for p in get_routes(app) if p not in skip
                   and not any(st == 200 for st in ROUTE_STATUS.get(p, {}).values()))
    missing = [f"{p} {ROUTE_STATUS.get(p) or '(never requested)'}" for p in never if p not in unreachable]
    stale = [p for p in unreachable if p not in never]
    dead = sorted(v for v, by_role in VARIANT_STATUS.items() if not any(st == 200 for st in by_role.values()))
    problems = []
    if missing:
        problems.append("GET routes that answered 200 for NO role, so nothing measured them - give the world what they "
                        "need (tests/nplus_world.py / fill() / variants()) or list them under unreachable_routes in "
                        "tests/n_plus_one_allowlist.json with the reason:\n  " + "\n  ".join(missing))
    if stale:
        problems.append(f"unreachable_routes entries that ARE measured now (remove them): {stale}")
    if dead:
        problems.append("variants() views that answered 200 for no role (a typo, or a filter the page dropped):\n  "
                        + "\n  ".join(f"{v}: {VARIANT_URL.get(v)} {VARIANT_STATUS[v]}" for v in dead))
    assert not problems, "\n\n".join(problems)


# ==================================================================================================== POST routes
SMALL_ROWS, LARGE_ROWS = 2, 20


@dataclass
class PostCase:
    name: str
    seed: Callable[[Any, int], Awaitable[tuple[Any, dict[str, Any]]]]  # (the acting user, context) with n rows
    send: Callable[[Any, dict[str, Any]], Awaitable[Any]]  # the request
    handled: Callable[[Any, Any, dict[str, Any]], Awaitable[int]]  # rows the request really handled (no vacuous pass)


async def _unread_member(db: Any, n: int) -> tuple[Any, dict[str, Any]]:
    from app.models import Notification
    from tests.factories import make_customer

    user = await make_customer(db)
    await world_mod.put(db, Notification, [{"user_id": user.id, "title": f"Notice {i}", "body": "Hello"} for i in range(n)])
    await db.commit()
    return user, {"user": user.id}


async def _read_count(db: Any, r: Any, ctx: dict[str, Any]) -> int:
    from app.models import Notification

    unread = await db.scalar(select(func.count(Notification.id)).where(Notification.user_id == ctx["user"],
                                                                         Notification.read_at.is_(None)))
    total = await db.scalar(select(func.count(Notification.id)).where(Notification.user_id == ctx["user"]))
    return int(total or 0) - int(unread or 0) if r.status_code in (200, 303) else -1


async def _staff_with_jobs(db: Any, n: int) -> tuple[Any, dict[str, Any]]:
    from app.models.enums import Role
    from tests.factories import make_employee, make_gym, make_owner, make_user

    user = await make_user(db, role=Role.STAFF)
    for _ in range(n):  # n employments at n gyms (no cap on a person's employments)
        await make_employee(db, await make_gym(db, await make_owner(db)), user=user)
    return user, {"user": user.id}


async def _deleted_employments(db: Any, r: Any, ctx: dict[str, Any]) -> int:
    from app.models import Employee

    if r.status_code != 303:
        return -1
    return int(await db.scalar(select(func.count(Employee.id)).where(Employee.user_id == ctx["user"],
                                                                      Employee.deleted_at.is_not(None))) or 0)


async def _bag(db: Any, n: int) -> tuple[Any, dict[str, Any]]:
    from app.models import CartItem, Product
    from app.models.shop import ProductCategory
    from tests.factories import make_branch, make_customer, make_gym, make_owner

    gym = await make_gym(db, await make_owner(db))
    branch = await make_branch(db, gym)
    user = await make_customer(db)
    k = time.monotonic_ns()
    products = await world_mod.put(db, Product, [{
        "gym_id": gym.id, "name": f"Bag item {i}", "slug": f"bag-item-{k}-{i}", "category": ProductCategory.ACCESSORIES,
        "description": "A bag item.", "price": Decimal("100"), "currency": gym.currency, "tax_rate": Decimal("18"),
        "stock": 50, "is_active": True, "fulfillment": "both"} for i in range(n)], ids=True)
    await world_mod.put(db, CartItem, [{"user_id": user.id, "product_id": p, "quantity": 1} for p in products])
    await db.commit()
    return user, {"user": user.id, "gym": gym.id, "branch": branch.id}


async def _ordered_items(db: Any, r: Any, ctx: dict[str, Any]) -> int:
    from app.models import ShopOrder, ShopOrderItem

    if r.status_code != 303:
        return -1
    return int(await db.scalar(select(func.count(ShopOrderItem.id)).join(ShopOrder, ShopOrder.id == ShopOrderItem.order_id)
                               .where(ShopOrder.customer_id == ctx["user"])) or 0)


POST_CASES = [
    PostCase("/account/notifications/read-all (n unread)", _unread_member,
             lambda c, ctx: post_form(c, "/account/notifications/read-all", {}), _read_count),
    PostCase("/api/notifications/read-all (n unread)", _unread_member,
             lambda c, ctx: api(c, "POST", "/api/notifications/read-all"), _read_count),
    PostCase("/account/delete (staff with n employments)", _staff_with_jobs,
             lambda c, ctx: post_form(c, "/account/delete", {"password": "Str0ng!Passw0rd", "confirm_text": "DELETE",
                                                             "confirm_identity": "1", "reason": "other"}),
             _deleted_employments),
    PostCase("/shop/checkout (a bag of n items)", _bag,
             lambda c, ctx: post_form(c, "/shop/checkout", {"gym": ctx["gym"], "fulfillment": "pickup",
                                                            "branch_id": ctx["branch"]}), _ordered_items),
]


@pytest.mark.parametrize("case", POST_CASES, ids=lambda c: c.name.split(" (")[0])
async def test_post_routes_do_not_grow_with_their_rows(db, case: PostCase):
    """A POST that acts on all of the viewer's rows runs the same statements for 2 rows and for 20."""
    engine = dbmod.get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", _record)
    got: dict[int, tuple[int, list[str]]] = {}
    try:
        for n in (SMALL_ROWS, LARGE_ROWS):
            user, ctx = await case.seed(db, n)
            client = make_client()
            try:
                await login_as(client, user)
                await client.get("/account", headers=HEADERS)  # first-use costs of the session, outside the measurement
                await csrf_token(client)
                clear_caches()
                r, count, statements = await recorded(lambda: case.send(client, ctx))
            finally:
                await client.aclose()
            handled = await case.handled(db, r, ctx)
            assert handled == n, (f"{case.name}: the request handled {handled} of {n} rows ({r.status_code} "
                                  f"{r.headers.get('location', '')} {r.text[:300]}) - the measurement would be vacuous")
            got[n] = (count, statements)
    finally:
        event.remove(engine, "before_cursor_execute", _record)
    (small, small_sql), (large, large_sql) = got[SMALL_ROWS], got[LARGE_ROWS]
    grown = shape_growth(small_sql, large_sql)
    if os.environ.get("NPLUS_REPORT"):  # every measurement as JSON (key "posts")
        out = Path(os.environ["NPLUS_REPORT"])
        data = json.loads(out.read_text()) if out.exists() else {}
        data.setdefault("posts", {})[case.name] = {"small_rows": SMALL_ROWS, "large_rows": LARGE_ROWS, "small": small,
                                                   "large": large, "repeats": grown}
        out.write_text(json.dumps(data, indent=2, default=str))
    assert large <= small and not grown, (
        f"N+1 in {case.name}: {small} statements for {SMALL_ROWS} rows, {large} for {LARGE_ROWS}. Statements that "
        "repeat (normalised):\n" + "\n".join(grown or repeats(Sample(200, small, small_sql), Sample(200, large, large_sql))
                                             or ["(none repeats - new statements appeared)"]))


# ==================================================================================================== the model
def test_every_relationship_raises_on_implicit_lazy_load():
    """The convention every N+1 check here builds on: an implicit lazy load raises (``lazy="raise"``), so per-row
    loads can only be explicit queries - which the crawl, the job measurements and the loop scan see."""
    import app.models  # noqa: F401 - every model is mapped

    mappers = list(dbmod.Base.registry.mappers)
    assert len(mappers) > 20, "the mapper registry is (almost) empty - is app.models still imported?"
    lazy = sorted(f"{m.class_.__name__}.{rel.key}: lazy={rel.lazy!r}" for m in mappers for rel in m.relationships
                  if rel.lazy not in ("raise", "raise_on_sql"))
    assert not lazy, ("relationships that load lazily (an N+1 waiting to happen in any loop over the rows) - declare "
                      "them lazy='raise' and load explicitly (selectinload / joinedload / one IN query):\n  "
                      + "\n  ".join(lazy))


# ==================================================================================================== the loop scan
def test_no_database_call_inside_a_loop_is_unexplained():
    from scripts.find_query_loops import changed, reason_of, scan, stale, unexplained

    findings = scan(indirect=True)
    allowed = allowlist()["loops"]
    assert len(findings) > 50, "the scan found almost nothing - is it still reading app/**?"
    problems = []
    short = sorted(fp for fp, e in allowed.items() if len(reason_of(e).strip()) < 10)
    if short:
        problems.append(f"loops entries without a reason: {short}")
    todo = unexplained(findings, allowed)
    if todo:
        problems.append(
            f"{len(todo)} database call(s) made once per row of a loop - an N+1 unless the loop is bounded. Fix it (one "
            "set-based statement, an IN list, a join, a bulk INSERT, notify_bulk / enqueue_many) or, when it is not an "
            "N+1, add its fingerprint to tests/n_plus_one_allowlist.json \"loops\" with the reason (then run "
            "`.venv/bin/python scripts/find_query_loops.py --refresh <fingerprint>` to pin it):\n\n"
            + "\n\n".join(f"{f.describe()}\n    fingerprint {f.fingerprint}" for f in todo))
    again = changed(findings, allowed)
    if again:
        problems.append(
            f"{len(again)} allowlisted loop(s) whose code changed since the reason was written (the function, the "
            "helpers of its module it calls, or a module constant they read): check that each loop is STILL bounded "
            "as the reason says - if not, it is an N+1: fix it and drop the entry - then re-pin them with "
            "`.venv/bin/python scripts/find_query_loops.py --refresh <fingerprint> ...`:\n\n"
            + "\n\n".join(f"{f.describe(reason_of(allowed[f.fingerprint]))}\n    fingerprint {f.fingerprint}"
                          for f in again))
    gone = stale(findings, allowed)
    if gone:
        problems.append("loops entries that match no loop any more (the loop was fixed or rewritten: remove them, or "
                        "run `.venv/bin/python scripts/find_query_loops.py --refresh` after re-reviewing):\n  "
                        + "\n  ".join(gone))
    assert not problems, "\n\n".join(problems)


SCANNER_CASES = textwrap.dedent('''
    import asyncio

    import httpx
    from app.db import session_scope

    LIMIT = 20


    async def helper(order):
        async with session_scope() as s:
            await s.execute("UPDATE")


    async def notify(db, user):
        db.add(user)


    async def rows_for(db, a):                       # an async generator: one query each time it is iterated
        for r in await db.scalars("SELECT rows"):
            yield r


    async def _admins(db):
        return (await db.scalars("SELECT admins LIMIT :n", {"n": LIMIT})).all()


    class Repo:
        def __init__(self, db):
            self._db = db

        async def load(self, i):                     # a method that runs SQL
            return await self._db.get(object, i)

        async def page(self, ids):
            for i in ids:
                await self._db.get(object, i)        # direct: the session held in self._db


    async def page(db, ids, groups):
        rows = await db.execute("SELECT")            # not in a loop
        for r in await db.scalars("SELECT ids"):     # the iterable runs once
            await db.get(object, r)                  # direct: per row
        while await db.scalar("SELECT next"):        # a while condition runs every time
            pass
        got = [await db.get(object, i) for i in ids] # a comprehension
        for i in ids:
            await notify(db, i)                      # indirect: handed the session
            await helper(i)                          # indirect: opens its own session
            await other(i)                           # unknown function: not a finding
        async with httpx.AsyncClient() as session:
            for i in ids:
                await session.get("http://x")        # an HTTP client named session: not a finding
        for i in ids:
            s = {}
            s.get(i)                                 # a dict's get, not awaited: not a finding


    async def fan_gather(db, ids):
        return await asyncio.gather(*(helper(i) for i in ids))                # fan-out over a generator


    async def fan_map(db, ids):
        return await asyncio.gather(*map(helper, ids))                        # fan-out through map


    async def fan_tasks(db, ids):
        async with asyncio.TaskGroup() as tg:
            for i in ids:
                tg.create_task(helper(i))                                     # fan-out: a task per row


    async def fan_later(db, ids):
        coros = [helper(i) for i in ids]                                      # fan-out: gathered later
        return await asyncio.gather(*coros)


    async def closure(db, ids):
        async def one(i):
            return await db.get(object, i)
        for i in ids:
            await one(i)                                                      # indirect: a closure on the session


    async def method(db, ids):
        repo = Repo(db)
        for i in ids:
            await repo.load(i)                                                # indirect: a method that runs SQL


    async def nested_async_for(db, groups):
        for a in groups:
            async for r in rows_for(db, a):                                   # async-for per outer row
                pass


    async def other_name(database, ids):
        for i in ids:
            await database.execute("SELECT 1")                                # direct: another session name


    async def wrapped(db, ids):
        for i in ids:
            await asyncio.wait_for(db.execute("SELECT"), 5)                   # direct: wrapped in wait_for


    async def notify_admins(db):
        """The admins (bounded by LIMIT) get a notice."""
        for a in await _admins(db):
            await notify(db, a)                                               # indirect: per admin


    async def unrelated():
        return 1
''')


def _scan_source(tmp_path: Path, src: str, *, indirect: bool = True) -> list[Any]:
    from scripts.find_query_loops import scan

    (tmp_path / "mod.py").write_text(src)
    return scan(tmp_path, indirect=indirect)


def test_the_loop_scanner_finds_queries_in_loops(tmp_path):
    """The scanner itself: what counts as once per row - inside a loop, one call deeper, fanned out, an async for."""
    found = {(f.kind, f.function, f.call.split("(")[0]) for f in _scan_source(tmp_path, SCANNER_CASES)}
    assert found == {
        ("direct", "Repo.page", "self._db.get"),
        ("direct", "page", "db.get"), ("direct", "page", "db.scalar"),
        ("indirect", "page", "notify"), ("indirect", "page", "helper"),
        ("fan-out", "fan_gather", "helper"), ("fan-out", "fan_map", "map"), ("fan-out", "fan_tasks", "helper"),
        ("fan-out", "fan_later", "helper"),
        ("indirect", "closure", "one"), ("indirect", "method", "repo.load"),
        ("async-for", "nested_async_for", "rows_for"),
        ("direct", "other_name", "database.execute"), ("direct", "wrapped", "db.execute"),
        ("indirect", "notify_admins", "notify"),
    }, found
    direct = _scan_source(tmp_path, SCANNER_CASES, indirect=False)
    assert {(f.kind, f.function) for f in direct} == {("direct", "Repo.page"), ("direct", "page"),
                                                      ("direct", "other_name"), ("direct", "wrapped")}
    assert len([f for f in direct if f.call.startswith("db.get")]) == 2  # the for body and the comprehension


def test_the_loop_scanner_pins_the_reviewed_code(tmp_path):
    """An allowlisted loop must be reviewed again when the code its reason rests on changes: the function, the module's
    helpers it calls (the bound often lives there) and the module constants they read - not when unrelated code or a
    docstring changes. Stale entries and changed ones are reported; ``refresh`` re-pins after the review."""
    from scripts.find_query_loops import changed, refresh, stale, unexplained

    def sha(src: str) -> str:
        (f,) = [f for f in _scan_source(tmp_path, src) if f.function == "notify_admins"]
        return f.fn_sha

    base = sha(SCANNER_CASES)
    assert sha(SCANNER_CASES.replace("async def unrelated():\n    return 1", "async def unrelated():\n    return 2")) == base
    assert sha(SCANNER_CASES.replace("The admins (bounded by LIMIT) get a notice.", "Docs only.")) == base
    assert sha(SCANNER_CASES.replace("LIMIT = 20", "LIMIT = 20000")) != base  # the bound
    assert sha(SCANNER_CASES.replace('"SELECT admins LIMIT :n", {"n": LIMIT}', '"SELECT every account"')) != base
    assert sha(SCANNER_CASES.replace("for a in await _admins(db):", "for a in await db.scalars('SELECT *'):")) != base

    findings = _scan_source(tmp_path, SCANNER_CASES)
    target = next(f for f in findings if f.function == "notify_admins")
    lists = tmp_path / "allow.json"
    lists.write_text(json.dumps({"loops": {target.fingerprint: "at most LIMIT (20) admins per event",
                                           "mod.py::gone::db.get(x)": {"reason": "a loop that was removed", "fn_sha": "x"}}}))
    allowed = json.loads(lists.read_text())["loops"]
    assert target not in unexplained(findings, allowed)
    assert [f.fingerprint for f in changed(findings, allowed)] == [target.fingerprint]  # never pinned
    assert stale(findings, allowed) == ["mod.py::gone::db.get(x)"]
    refresh(findings, lists)
    allowed = json.loads(lists.read_text())["loops"]
    assert allowed == {target.fingerprint: {"reason": "at most LIMIT (20) admins per event", "fn_sha": target.fn_sha}}
    assert not changed(findings, allowed) and not stale(findings, allowed)
    moved = _scan_source(tmp_path, SCANNER_CASES.replace("LIMIT = 20", "LIMIT = 20000"))
    assert [f.fingerprint for f in changed(moved, allowed)] == [target.fingerprint]
