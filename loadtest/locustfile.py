"""IronPulse - production-like load test (Locust 2.x, FastHttpUser).

Real-browser behaviour per simulated user: its own client address (X-Forwarded-For from 198.18.0.0/15, the load
generator plays the TLS proxy with X-Forwarded-Proto: https), a real User-Agent, cookies handled like a browser
(Secure / __Host- cookies, see ltlib/browser.py), redirects followed hop by hop (a page view that lands on another page
is a failure), CSRF tokens taken from the page's <meta name="csrf-token"> / hidden form field exactly as 00-core.js
does, static assets from the browser cache (revalidated with If-None-Match once heuristically stale), the notification
badge polled like the island (30 s cache), a NEW WebSocket per signed-in page view (like the browser), think times.
Anonymous visitors come and go: a session lasts ~6 page views (geometric), then 70 % of the visitors are replaced by a
new one (new address, empty cookies and cache) and 30 % come back with their cache.

User classes (weights, % of the users at any load step):
  AnonymousVisitor 100 (~46 %)  Member 60 (~28 %)  Owner 20 (~9 %)  Staff 10 (~5 %)  JunkUser 22 (~10 %: random junk
  input, own address block / own accounts, stats names prefixed "JUNK")  Abuser 5 (~2.3 %, own address block, stats
  names prefixed "ABUSE")  Superadmin: a FIXED 3 (platform admins do not scale with visitors; 1.5 % at the 200-user
  SLO step)

One session per account (the app's SINGLE_SESSION_PER_ACCOUNT): every signed-in simulated user has an account of its
own (members, owners, staff, super-admins lt_superadmin_01..10, junk testers lt_junk_*, abusers); "signed in
elsewhere" on a user's own session is a failure (ltlib/browser.py, ltlib/ws.py). Forms carry their one-time submit_id,
API calls a new X-Submit-Id each (the double-submit guard).

Profiles (LT_PROFILE): stress10m (default, the 10-minute step + spike + recovery shape below) | smoke50 (<= 50 users,
3 minutes: the shape compressed) | smoke (fixed 1-3 users per class, short waits, every task once first) | calibrate
(ramp to 600 users: measures server CPU per request) | soak (30 min at 200 users: the memory-leak check) | custom
(LT_STEPS="end_s:users:spawn_rate,...").

    .venv-loadtest/bin/locust -f loadtest/locustfile.py --host http://127.0.0.1:12000 --headless --processes 4 \
        --csv loadtest/results/<run>/locust --csv-full-history --html loadtest/results/<run>/locust_report.html
"""

from __future__ import annotations

import html as htmllib
import itertools
import json
import math
import os
import random
import re
import secrets
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import gevent  # noqa: E402
import websocket  # noqa: E402
from locust import FastHttpUser, LoadTestShape, between, events, task  # noqa: E402
from locust.runners import MasterRunner, WorkerRunner  # noqa: E402

import ltenv  # noqa: E402
from ltlib import junk as J  # noqa: E402
from ltlib.browser import ELSEWHERE, HTML_ACCEPT, USER_AGENTS, Browser, parse_form, sse_events  # noqa: E402
from ltlib.data import accounts, allocator, catalogue, near_point  # noqa: E402
from ltlib.reqlog import reqlog  # noqa: E402
from ltlib.ws import CLOSE_LABELS, Presence, close_code, ws_flood  # noqa: E402

PROFILE = os.environ.get("LT_PROFILE", "stress10m")
SMOKE = PROFILE == "smoke"
QUICK = PROFILE in ("smoke", "smoke50")  # functional runs: shorter waits, AI asks allowed more often
WAIT_MIN = float(os.environ.get("LT_WAIT_MIN", "0.5" if SMOKE else ("1" if QUICK else "2")))
WAIT_MAX = float(os.environ.get("LT_WAIT_MAX", "1.5" if SMOKE else ("4" if QUICK else "8")))
WS_MODE = os.environ.get("LT_WS_MODE", "per-page")  # per-page (browser) | hold (one socket per user) | off
AI_MIN_INTERVAL = float(os.environ.get("LT_AI_MIN_INTERVAL", "20" if QUICK else "90"))  # s between one member's AI asks
# smoke: every user runs each of its tasks once first; smoke50: the first user of each class in each process does
TOUR_PER_CLASS = int(os.environ.get("LT_TOUR_PER_CLASS", "999" if SMOKE else ("1" if PROFILE == "smoke50" else "0")))
VISITOR_SESSION_VIEWS = float(os.environ.get("LT_VISITOR_SESSION_VIEWS", "6"))  # mean page views of a visit
VISITOR_NEW_SHARE = float(os.environ.get("LT_VISITOR_NEW_SHARE", "0.7"))  # visits by a NEW visitor (no cache/cookies)
ABUSE_FRESH_IP_SHARE = float(os.environ.get("LT_ABUSE_FRESH_IP_SHARE", "0.5"))  # bursts from a fresh (botnet) address
SUPERADMINS = int(os.environ.get("LT_SUPERADMINS", "3"))
if SUPERADMINS > ltenv.N_SUPERADMINS:  # one account each (one session per account): the seeder makes N_SUPERADMINS
    print(f"[locustfile] LT_SUPERADMINS={SUPERADMINS} > the {ltenv.N_SUPERADMINS} seeded admin accounts - using "
          f"{ltenv.N_SUPERADMINS}", flush=True)
    SUPERADMINS = ltenv.N_SUPERADMINS
JUNK_WEIGHT = int(os.environ.get("LT_JUNK_WEIGHT", "22"))  # ~10 % of the users (22 / (195 + 22)); 0 = no junk users
JUNK_SIGNED_IN_SHARE = float(os.environ.get("LT_JUNK_SIGNED_IN_SHARE", "0.5"))  # junk users signed in (own account)
JUNK_AI_INTERVAL = float(os.environ.get("LT_JUNK_AI_INTERVAL", "45" if QUICK else "150"))  # s between a junk user's
# well-formed AI asks (the AI slots are shared with the members; malformed asks are refused before the AI anyway)

# --------------------------------------------------------------------------------------------- load shape
# Derived from measurements, not guessed (README "Load shape"). The two smokes of this kit (per-page WebSockets,
# visitor churn, checkout, AI) measured 6.3-11 ms of worker CPU and 3-6 ms of PostgreSQL CPU per normal request and
# ~1.4-1.6 requests/s per user at 1-4 s think time, i.e. ~0.75 requests/s per user at the real 2-8 s: 4 workers
# saturate near 4 / (6.3..11 ms) = 360-630 requests/s = ~450-850 users (both smokes ran next to the other teams'
# work, so the true figure may be higher). Locust needs ~0.7 ms per request, so --processes 4 drives this easily.
# The steps sit at ~25 / 50 / 100 / 150 % of that estimate above the 200-user SLO step (judged at 3-5 min), the
# spike adds x1.5 within ~11 s, and the run ends with 70 s back at the SLO load to show the recovery. After the other
# teams finish, a calibration ramp (LT_PROFILE=calibrate, 3 min) re-measures on a quiet machine: analyze.py prints the
# saturation estimate at the 2-8 s think time - move the upper steps (LT_STEPS) when it differs by more than ~25 %.
STEPS = {
    "stress10m": [(60, 50, 2), (180, 100, 5), (300, 200, 10), (370, 350, 15), (440, 700, 25), (500, 1050, 30),
                  (530, 1600, 50), (600, 200, 200)],
    # the real shape compressed into 3 minutes and <= 50 users (functional check of stats, monitor and report)
    "smoke50": [(30, 10, 2), (60, 20, 2), (90, 30, 2), (120, 40, 2), (150, 50, 5), (180, 10, 10)],
    "calibrate": [(180, 600, 5)],
    "soak": [(1800, 200, 10)],
}
SMOKE_COUNTS = {"AnonymousVisitor": 2, "Member": 3, "Owner": 1, "Staff": 1, "Superadmin": 2, "Abuser": 1, "JunkUser": 4}
SMOKE_SECONDS = int(os.environ.get("LT_SMOKE_SECONDS", "120"))


def active_steps() -> list[tuple[int, int, float]]:
    raw = os.environ.get("LT_STEPS", "").strip()
    if raw:
        out = []
        for part in raw.split(","):
            end, users, rate = part.split(":")
            out.append((int(end), int(users), float(rate)))
        return out
    if SMOKE:
        return [(SMOKE_SECONDS, sum(SMOKE_COUNTS.values()), 3)]
    return STEPS.get(PROFILE, STEPS["stress10m"])


class StepLoadShape(LoadTestShape):
    """The active profile's steps: (end second, users, spawn rate); the run stops after the last one."""

    def tick(self) -> tuple[int, float] | None:
        t = self.get_run_time()
        for end, users, rate in active_steps():
            if t < end:
                return users, rate
        return None


# --------------------------------------------------------------------------------------------- run bookkeeping
def run_dir() -> Path | None:
    if os.environ.get("LT_RUN_DIR"):
        return Path(os.environ["LT_RUN_DIR"])
    current = HERE / ".current_run"
    if current.is_file():
        return HERE / "results" / current.read_text(encoding="utf-8").strip()
    return None


PREFLIGHT: dict[str, Any] = {}


@events.init.add_listener
def _init(environment: Any, **_: Any) -> None:
    allocator.configure(environment)
    if not isinstance(environment.runner, MasterRunner):
        rd = run_dir()
        if rd is not None:
            reqlog.open(rd)
    accounts()
    catalogue()
    if not isinstance(environment.runner, WorkerRunner):  # master or local runner: is the shared machine quiet?
        from ltlib.preflight import check  # noqa: PLC0415

        PREFLIGHT.update(check())
        PREFLIGHT["forced"] = os.environ.get("LT_FORCE") == "1"
        if not PREFLIGHT["ok"]:
            for reason in PREFLIGHT["reasons"]:
                print(f"[locustfile] preflight BUSY: {reason}", flush=True)
            if not PREFLIGHT["forced"] and not QUICK:
                print("[locustfile] refusing to load-test a busy machine (the numbers would show the other teams' work "
                      "as app limits) - wait, or set LT_FORCE=1 (the report marks the run)", flush=True)
                gevent.spawn_later(0.5, environment.runner.quit)


@events.request.add_listener
def _on_request(request_type: str, name: str, response_time: float, response_length: int, exception: Any = None,
                context: Any = None, response: Any = None, **_: Any) -> None:
    reqlog.write(request_type, name, response_time, response_length, exception, context, response)


@events.test_start.add_listener
def _test_start(environment: Any, **_: Any) -> None:
    if environment.runner is not None and not isinstance(environment.runner, WorkerRunner):  # master or local runner
        rd = run_dir()
        if rd is not None:
            rd.mkdir(parents=True, exist_ok=True)
            now = time.time()
            ist = datetime.fromtimestamp(now, timezone(timedelta(hours=5, minutes=30)))
            try:  # what the launcher started the server with (AI mode etc.), recorded with the run
                srv = json.loads((rd / "server_config.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                srv = {}
            meta = {"start": now, "start_ist": ist.strftime("%Y-%m-%d %H:%M:%S IST"), "profile": PROFILE,
                    "ai_mode": srv.get("ai_mode") or os.environ.get("LT_AI") or "unknown (no server_config.json)",
                    "server": {k: srv.get(k) for k in ("ENVIRONMENT", "workers", "DB_POOL_SIZE", "DB_MAX_OVERFLOW",
                                                       "smtp_mode", "backlog", "forced")} if srv else None,
                    "steps": active_steps(), "host": environment.host,
                    "processes": getattr(environment.parsed_options, "processes", None),
                    "weights": {c.__name__: getattr(c, "weight", 1) for c in environment.user_classes},
                    "fixed": {c.__name__: getattr(c, "fixed_count", 0) for c in environment.user_classes},
                    "wait": [WAIT_MIN, WAIT_MAX], "ws_mode": WS_MODE, "ai_min_interval_s": AI_MIN_INTERVAL,
                    "tour_per_class": TOUR_PER_CLASS, "visitor_session_views": VISITOR_SESSION_VIEWS,
                    "visitor_new_share": VISITOR_NEW_SHARE, "abuse_fresh_ip_share": ABUSE_FRESH_IP_SHARE,
                    "preflight": PREFLIGHT, "locust_version": _locust_version()}
            (rd / "run_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")


def _locust_version() -> str:
    try:
        import locust  # noqa: PLC0415

        return str(locust.__version__)
    except Exception:  # noqa: BLE001
        return "?"


@events.quitting.add_listener
def _quit(**_: Any) -> None:
    reqlog.close()


@events.test_stop.add_listener
def _stop(**_: Any) -> None:
    if reqlog.fh is not None:
        reqlog.fh.flush()


# --------------------------------------------------------------------------------------------- AI outcome checks
def _blocked(evs: list[dict[str, Any]], done: dict[str, Any]) -> str | None:
    if done.get("blocked") or any(e.get("type") == "blocked" for e in evs):
        return str(next((e.get("code") for e in evs if e.get("type") == "blocked"), "") or "?")
    return None


def plan_outcome(resp: Any) -> tuple[str | None, str]:
    """POST /api/plans/ask (SSE: meta, chunk*, done | blocked, done)."""
    evs = sse_events(resp.text or "")
    done = next((e for e in reversed(evs) if e.get("type") == "done"), None)
    if done is None:
        return "SSE stream without a done event", "incomplete"
    code = _blocked(evs, done)
    if code:
        return f"AI guard refused a normal member ({code})", f"blocked:{code}"
    if done.get("busy"):
        return "AI busy - the member got the standard answer", "busy"
    if done.get("fallback"):
        return None, "fallback"
    meta = next((e for e in evs if e.get("type") == "meta"), {})
    return None, "cached" if meta.get("cached") else "answer"


def product_outcome(resp: Any) -> tuple[str | None, str]:
    """POST /api/shop/products/{id}/ask/stream (SSE: start, delta*, done | blocked, done)."""
    evs = sse_events(resp.text or "")
    done = next((e for e in reversed(evs) if e.get("type") == "done"), None)
    if done is None:
        return "SSE stream without a done event", "incomplete"
    code = _blocked(evs, done)
    if code:
        return f"AI guard refused a normal member ({code})", f"blocked:{code}"
    if done.get("busy"):
        return "AI busy - the member got the listing answer", "busy"
    if done.get("cached"):
        return None, "cached"
    return None, "answer" if done.get("ai_available") else "fallback"


def assistant_outcome(resp: Any) -> tuple[str | None, str]:
    """POST /api/assistant/chat (JSON: reply, actions, provider, ai_available, busy?)."""
    try:
        data = json.loads(resp.text or "")
    except ValueError:
        return "assistant answer is not JSON", "invalid"
    if not isinstance(data, dict) or "reply" not in data:
        return "assistant answer without a reply", "invalid"
    if data.get("busy"):
        return "AI busy - the member got the basic-commands answer", "busy"
    return None, "answer" if data.get("ai_available") else "fallback"


# --------------------------------------------------------------------------------------------- base user
MEMBER_QUESTIONS = ["Which plan suits me if I train three times a week?", "Is the online plan enough for weight loss?",
                    "Do I need a trainer as a beginner?", "Which plan is best for building muscle?",
                    "Can I switch plans later?", "What does the AI coach add to the gym plan?"]
ASSISTANT_MESSAGES = ["Remind me to drink water at 4 pm", "What's on my calendar this week?",
                      "Add a task: stretch for 10 minutes tonight", "How many workouts did I log this week?",
                      "Plan a light recovery day for tomorrow", "Move my leg day to Friday morning"]
PRODUCT_QUESTIONS = ["How should I take it?", "Is it good for muscle gain?", "Does it contain lactose?",
                     "Is it beginner friendly?", "How long does it last?", "Is it safe with asthma?"]
CHAT_LINES = ["Thanks! See you tomorrow at 7.", "Can we move my session to the evening?", "Got it, will do.",
              "Is the gym open on Sunday morning?", "My shoulder feels better this week.",
              "Could you share a stretching routine?"]
LAST_ID = re.compile(r'data-last-id="(\d+)"')
_TOURED: dict[str, int] = {}


class BrowserUser(FastHttpUser):
    abstract = True
    wait_time = between(WAIT_MIN, WAIT_MAX)
    network_timeout = 60.0
    connection_timeout = 15.0
    kind = "visitor"

    def context(self) -> dict[str, Any]:
        return {"cls": type(self).__name__}

    def client_ip(self) -> str:
        return ltenv.random_ip(ltenv.VISITOR_NET, self.rng)

    def on_start(self) -> None:
        self.rng = random.Random()
        self.b = Browser(self, self.client_ip(), self.rng.choice(USER_AGENTS), expect_429=self.kind == "abuser")
        self.presence: Presence | None = None
        self.cat = catalogue()
        self.sign_in()
        name = type(self).__name__
        if _TOURED.get(name, 0) < TOUR_PER_CLASS:
            _TOURED[name] = _TOURED.get(name, 0) + 1
            self.tour()

    def on_stop(self) -> None:
        if self.presence is not None:
            self.presence.close(wait=True)

    def sign_in(self) -> None:  # signed-in classes override
        return None

    # ---- helpers
    def view(self, url: str, name: str, **kw: Any) -> Any:
        resp = self.b.page(url, name, **kw)
        if self.presence is not None and WS_MODE == "per-page":
            self.presence.open()  # the new page's socket; the previous one closes in the background (pagehide)
        return resp

    def login(self, email: str, password: str, home: str, remembered: tuple[str, str] | None = None) -> bool:
        """Like the browser: GET the sign-in page (CSRF cookie + token, the form's one-time submit_id, the device key
        02-accounts.js puts into the form), POST the form with "Remember this account" left ticked (its default), follow
        the 303 to the role's home page and move the remembered-account reference out of its hand-off cookie.
        ``remembered`` = (reference, device key) of a browser that remembers this account: the page's account chooser
        looks it up (POST /auth/remembered), shows the photo, and the form signs in with account_ref - no e-mail."""
        b = self.b
        page = self.view("/auth/login", "GET /auth/login", forms=True)
        _action, fields, boxes = parse_form(page.text if page is not None and page.status_code == 200 else "",
                                            action="/auth/login")
        fields.pop("csrf_token", None)  # form_post stamps the current token like 00-core.js
        if "remember" in boxes:
            fields["remember"] = boxes["remember"]  # ticked by default - left ticked
        if remembered:
            b.device_id = remembered[1]
        fields.update({"identifier": email, "password": password, "device_id": b.device_id})
        fields.setdefault("next", "")
        if remembered:
            r = b.api_post_json("/auth/remembered", {"refs": [remembered[0]]}, "POST /auth/remembered (account chooser)")
            try:
                acc = (json.loads(r.text or "{}").get("accounts") or [{}])[0] if r is not None and r.status_code == 200 else {}
            except (ValueError, AttributeError, IndexError):
                acc = {}
            if isinstance(acc, dict) and acc.get("state") == "ok" and acc.get("ref") == remembered[0]:
                if str(acc.get("photo") or "").startswith("/auth/remembered/photo/"):
                    b.request("GET", acc["photo"], name="GET /auth/remembered/photo/[token]",
                              headers=b.headers(accept="image/avif,image/webp,*/*"), html=False, expect=(200, 404))
                fields["account_ref"] = remembered[0]
                fields.pop("identifier", None)  # the chosen account: the e-mail box is disabled, so not sent
                reqlog.note(type(self).__name__, "sign-in", "account chooser")
        resp = b.form_post("/auth/login", fields, name="POST /auth/login")
        # from here on every redirect to /auth/login counts as a failure - also when this sign-in failed, so a
        # broken sign-in never turns a member into a silent anonymous visitor
        b.signed_in = True
        b.take_handoff()
        if resp is None or resp.status_code != 200 or not b._path(str(resp.url or "")).startswith(home):
            return False
        b.subresources(resp.text or "")
        if WS_MODE != "off":
            self.presence = Presence(self, b)
            self.presence.open()
        return True

    def login_admin(self, email: str, password: str) -> bool:
        """A platform admin signs in on its own page (/superadmin/login - the only sign-in super-admins have; the
        member sign-in refuses their credentials like an unknown account's): GET the page (CSRF cookie + token, the
        form's one-time submit_id), POST it, follow the 303 to /superadmin. No account chooser for admins."""
        b = self.b
        page = self.view("/superadmin/login", "GET /superadmin/login", forms=True)
        _action, fields, _boxes = parse_form(page.text if page is not None and page.status_code == 200 else "",
                                             action="/superadmin/login")
        fields.pop("csrf_token", None)  # form_post stamps the current token like 00-core.js
        fields.update({"identifier": email, "password": password})
        fields.setdefault("next", "")
        resp = b.form_post("/superadmin/login", fields, name="POST /superadmin/login")
        b.signed_in = True
        if resp is None or resp.status_code != 200 or not b._path(str(resp.url or "")).startswith("/superadmin"):
            return False
        b.subresources(resp.text or "")
        if WS_MODE != "off":
            self.presence = Presence(self, b)
            self.presence.open()
        return True

    def tour(self) -> None:
        """Functional coverage: run every task of the class once (in a fixed order) before the random mix starts."""
        self._touring = True
        seen: set[str] = set()
        for fn in self.tasks:
            name = getattr(fn, "__name__", str(fn))
            if name in seen:
                continue
            seen.add(name)
            started = time.perf_counter()
            try:
                fn(self)
                exc = None
            except Exception as e:  # noqa: BLE001 - a task that crashes is a kit bug: make it visible
                exc = e
            if exc is not None:
                self.environment.events.request.fire(request_type="TASK", name=f"{type(self).__name__}.{name}",
                                                     response_time=(time.perf_counter() - started) * 1000,
                                                     response_length=0, exception=exc, context=self.context(),
                                                     response=None)
            gevent.sleep(self.rng.uniform(0.2, 0.6))
        self._touring = False

    def tiles(self, lat: float, lon: float) -> None:
        """The branch page's map: a few tiles around the pin (the server answers a local transparent PNG - no
        Geoapify key and MAP_OSM_FALLBACK=false, so nothing leaves the machine)."""
        z = 15
        n = 2 ** z
        x = int((lon + 180.0) / 360.0 * n)
        y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
        for dx, dy in ((0, 0), (1, 0), (0, 1), (1, 1)):
            path = f"/maps/tiles/{z}/{x + dx}/{y + dy}.png"
            if path in self.b.cache:
                continue
            self.b.cache.add(path)
            self.b.request("GET", path, name="GET /maps/tiles/[z]/[x]/[y].png", headers=self.b.headers(accept="image/*"),
                           html=False)


# --------------------------------------------------------------------------------------------- anonymous visitors
class AnonymousVisitor(BrowserUser):
    """Browses the public site: home, gym directory (filters, pages, near me), gym / branch pages, shop (pages,
    categories, searches with typos, store filter + typeahead), products, store pages, jobs (all + near me with radius
    chips), job pages, trainer profiles, legal pages. Visits end after ~6 page views; most visitors are then replaced by
    a new one (new address, no cookies, empty cache), some return with their cache."""

    weight = 100

    def on_start(self) -> None:
        super().on_start()
        reqlog.note("AnonymousVisitor", "visit", "new")

    def view(self, url: str, name: str, **kw: Any) -> Any:
        resp = super().view(url, name, **kw)
        if self.rng.random() < 1.0 / max(1.0, VISITOR_SESSION_VIEWS):  # the visit ends after this page
            if self.rng.random() < VISITOR_NEW_SHARE:
                self.b = Browser(self, self.client_ip(), self.rng.choice(USER_AGENTS))
                reqlog.note("AnonymousVisitor", "visit", "new")
            else:
                reqlog.note("AnonymousVisitor", "visit", "returning")
        return resp

    def gym(self) -> dict[str, Any]:
        """Popular gyms get more views: weight n_branches ** 0.7 (the biggest chains are the worst-case pages)."""
        gyms = self.cat["gyms"]
        if not hasattr(AnonymousVisitor, "_gym_w"):
            AnonymousVisitor._gym_w = [max(1, g["n_branches"]) ** 0.7 for g in gyms]
        return self.rng.choices(gyms, weights=AnonymousVisitor._gym_w, k=1)[0]

    def branch_gym(self) -> dict[str, Any]:
        gyms = self.cat["gyms_with_branches"]
        if not hasattr(AnonymousVisitor, "_branch_w"):
            AnonymousVisitor._branch_w = [min(30, g["n_branches"]) for g in gyms]
        return self.rng.choices(gyms, weights=AnonymousVisitor._branch_w, k=1)[0]

    @task(30)
    def home(self) -> None:
        self.view("/", "GET /")

    @task(15)
    def gyms(self) -> None:
        page = self.rng.choice([1, 1, 1, 2, 3, self.rng.randint(4, 25)])
        self.view("/gyms" + (f"?page={page}" if page > 1 else ""), "GET /gyms")

    @task(15)
    def gyms_filtered(self) -> None:
        r = self.rng.random()
        if r < 0.45:
            city = self.rng.choice(self.cat["cities"][:25])["name"]
            self.view(f"/gyms?city={city.replace(' ', '+')}", "GET /gyms?city=")
        elif r < 0.7:
            self.view(f"/gyms?mode={self.rng.choice(['online', 'offline'])}", "GET /gyms?mode=")
        else:
            q = self.rng.choice(["iron", "fit", "pulse", "strength", "club", "yoga", "forge", "titan", "zen"])
            self.view(f"/gyms?q={q}", "GET /gyms?q=")

    @task(15)
    def gyms_near(self) -> None:
        lat, lon = near_point(self.rng)
        extra = f"&page={self.rng.randint(2, 4)}" if self.rng.random() < 0.2 else ""
        self.view(f"/gyms?lat={lat}&lon={lon}{extra}", "GET /gyms?lat&lon")

    @task(25)
    def gym_page(self) -> None:
        g = self.gym()
        if self.rng.random() < 0.3:
            lat, lon = near_point(self.rng)
            self.view(f"/gyms/{g['slug']}?lat={lat}&lon={lon}", "GET /gyms/[slug]")
        else:
            self.view(f"/gyms/{g['slug']}", "GET /gyms/[slug]")

    @task(8)
    def gym_branches(self) -> None:
        g = self.branch_gym()
        page = self.rng.randint(2, max(2, (g["n_branches"] + 11) // 12)) if g["n_branches"] > 12 and self.rng.random() < 0.5 else 1
        self.view(f"/gyms/{g['slug']}/branches" + (f"?page={page}" if page > 1 else ""), "GET /gyms/[slug]/branches")

    @task(25)
    def branch_page(self) -> None:
        g = self.branch_gym()
        bid = self.rng.choice(g["branches"])
        self.view(f"/gyms/{g['slug']}/branches/{bid}", "GET /gyms/[slug]/branches/[id]")
        lat, lon = near_point(self.rng)
        self.tiles(lat, lon)

    @task(4)
    def plans(self) -> None:
        self.view("/plans", "GET /plans")

    @task(20)
    def shop(self) -> None:
        r = self.rng.random()
        if r < 0.4:
            self.view("/shop", "GET /shop")
        elif r < 0.75:
            self.view(f"/shop?category={self.rng.choice(self.cat['categories'])}", "GET /shop?category=")
        else:
            self.view(f"/shop?page={self.rng.choice([2, 3, 5, 10, self.rng.randint(11, 400)])}", "GET /shop?page=")

    @task(15)
    def shop_search(self) -> None:
        q = self.rng.choice(self.cat["typos"] if self.rng.random() < 0.3 else self.cat["search"])
        sort = self.rng.choice(["", "", "", "&sort=price_asc", "&sort=rating", "&in_stock=1"])
        self.view(f"/shop?q={q.replace(' ', '+')}{sort}", "GET /shop?q=")

    @task(8)
    def shop_store_filter(self) -> None:
        prefix = self.rng.choice(self.cat["store_queries"])
        for i in range(2, min(len(prefix), 4) + 1):  # the typeahead fires as the visitor types (debounced)
            self.b.api_get(f"/api/shop/stores?q={prefix[:i]}", "GET /api/shop/stores?q=")
        self.view(f"/shop?gym={self.rng.choice(self.cat['store_ids'])}", "GET /shop?gym=")

    @task(25)
    def product(self) -> None:
        pid, slug, _ = self.rng.choice(self.cat["products"])
        self.view(f"/shop/p/{pid}/{slug}", "GET /shop/p/[id]/[slug]")

    @task(8)
    def store_page(self) -> None:
        g = self.rng.choice(self.cat["store_ids"])
        slug = self.cat["gym_by_id"][g]["slug"] if g in self.cat["gym_by_id"] else None
        if slug:
            self.view(f"/shop/gym/{slug}", "GET /shop/gym/[slug]")

    @task(8)
    def jobs(self) -> None:
        r = self.rng.random()
        if r < 0.5:
            self.view("/jobs", "GET /jobs")
        elif r < 0.75:
            self.view(f"/jobs?page={self.rng.randint(2, 20)}", "GET /jobs?page=")
        else:
            self.view(f"/jobs?role={self.rng.choice(['trainer', 'receptionist', 'manager', 'nutritionist'])}", "GET /jobs?role=")

    @task(12)
    def jobs_near(self) -> None:
        lat, lon = near_point(self.rng)
        r = self.rng.random()
        if r < 0.45:
            self.view(f"/jobs?lat={lat}&lon={lon}", "GET /jobs?lat&lon")
        elif r < 0.8:
            self.view(f"/jobs?lat={lat}&lon={lon}&km={self.rng.choice([5, 10, 20])}", "GET /jobs?lat&lon&km=")
        else:
            self.view(f"/jobs?lat={lat}&lon={lon}&view=all", "GET /jobs?lat&lon&view=all")

    @task(8)
    def job_page(self) -> None:
        self.view(f"/jobs/{self.rng.choice(self.cat['jobs'])}", "GET /jobs/[id]")

    @task(4)
    def trainer(self) -> None:
        if self.cat["trainers"]:
            self.view(f"/profile/{self.rng.choice(self.cat['trainers'])}", "GET /profile/[id]")

    @task(3)
    def legal(self) -> None:
        path = self.rng.choice(["/about", "/terms", "/privacy"])
        self.view(path, f"GET {path}")

    @task(3)
    def login_page(self) -> None:
        self.view("/auth/login", "GET /auth/login")


# --------------------------------------------------------------------------------------------- members
class Member(BrowserUser):
    """Signs in once, then: dashboard, health (+history, alerts, logging a reading), notifications page + island feed,
    chat list / thread / messages API / posting, shop browsing, cart add/remove, wishlist on/off, CHECKOUT (test-mode
    payment), orders, calendar, tasks, account pages, a SMALL share of AI asks (assistant with its history, streamed plan
    and product questions; >= AI_MIN_INTERVAL apart) whose outcome (answer / fallback / busy / blocked) is recorded."""

    weight = 60
    kind = "member"

    def sign_in(self) -> None:
        _, self.acc = allocator.take("members", accounts()["members"])
        self.b.ip = self.acc["ip"]
        self.last_ai = time.monotonic() - self.rng.uniform(0, AI_MIN_INTERVAL)
        remembered = (self.acc["rr"], self.acc["rd"]) if self.acc.get("rr") and self.acc.get("rd") else None
        self.login(self.acc["e"], accounts()["password"], "/me", remembered=remembered)

    def product_url(self) -> tuple[int, str] | None:
        own = self.cat["products_by_gym"].get(self.acc["gid"]) or []
        if own and self.rng.random() < 0.8:
            return self.rng.choice(own)
        pid, slug, _ = self.rng.choice(self.cat["products"])
        return pid, slug

    def ai_allowed(self, name: str) -> bool:  # noqa: ARG002
        if getattr(self, "_touring", False):  # the coverage tour asks each AI question once
            return True
        if time.monotonic() - self.last_ai < AI_MIN_INTERVAL * self.rng.uniform(1.0, 1.5):
            return False
        self.last_ai = time.monotonic()
        return True

    @task(30)
    def dashboard(self) -> None:
        self.view("/me", "GET /me")

    @task(15)
    def health(self) -> None:
        self.view("/me/health", "GET /me/health")

    @task(5)
    def health_history(self) -> None:
        self.view(f"/me/health?page={self.rng.choice([2, 2, 3])}", "GET /me/health?page=")

    @task(5)
    def health_alerts(self) -> None:
        self.view("/me/health/alerts", "GET /me/health/alerts")

    @task(4)
    def log_metric(self) -> None:
        self.view("/me/health", "GET /me/health", forms=True)
        w = round(float(self.acc["w"]) + self.rng.uniform(-0.2, 0.2), 1)
        self.b.form_post("/me/health/metrics", {"height_cm": self.acc["h"], "weight_kg": w, "next": "/me/health",
                                                "source": "manual"}, name="POST /me/health/metrics")

    @task(12)
    def notifications(self) -> None:
        self.view("/account/notifications", "GET /account/notifications")

    @task(8)
    def island_feed(self) -> None:
        self.b.api_get("/api/notifications?limit=20", "GET /api/notifications?limit=20")

    @task(2)
    def read_all(self) -> None:
        self.b.api_post_json("/api/notifications/read-all", {}, "POST /api/notifications/read-all")

    @task(10)
    def chat_list(self) -> None:
        self.view("/chat", "GET /chat")

    def open_thread(self) -> int | None:
        threads = self.acc.get("t") or []
        if not threads:
            self.view("/chat", "GET /chat")
            return None
        tid = self.rng.choice(threads)
        resp = self.view(f"/chat/{tid}", "GET /chat/[id]")
        last = 0
        if resp is not None and resp.text:
            m = LAST_ID.search(resp.text)
            last = int(m.group(1)) if m else 0
        self.b.api_get(f"/api/chat/{tid}/messages?after={last}&limit=100", "GET /api/chat/[id]/messages?after=")
        self.b.api_post_json(f"/api/chat/{tid}/read", {}, "POST /api/chat/[id]/read")
        return tid

    @task(10)
    def chat_thread(self) -> None:
        self.open_thread()

    @task(3)
    def chat_post(self) -> None:
        tid = self.open_thread()
        if tid is not None:
            self.b.api_post_json(f"/api/chat/{tid}/messages", {"body": self.rng.choice(CHAT_LINES)},
                                 "POST /api/chat/[id]/messages", expect=(201, 409))

    @task(12)
    def shop_browse(self) -> None:
        if self.rng.random() < 0.5:
            self.view(f"/shop/gym/{self.acc['g']}", "GET /shop/gym/[slug]")
        else:
            self.view(f"/shop?category={self.rng.choice(self.cat['categories'])}", "GET /shop?category=")

    @task(10)
    def product(self) -> None:
        pick = self.product_url()
        if pick:
            self.view(f"/shop/p/{pick[0]}/{pick[1]}", "GET /shop/p/[id]/[slug]")

    @task(5)
    def cart_add_remove(self) -> None:
        pick = self.product_url()
        if not pick:
            return
        url = f"/shop/p/{pick[0]}/{pick[1]}"
        self.view(url, "GET /shop/p/[id]/[slug]")
        # 200 = added; 400 = the shop refused it (out of stock, 18+ for a minor ...) - both are normal shop answers
        self.b.api_post_form("/shop/cart/add", {"product_id": pick[0], "quantity": 1, "next": url, "page_id": self.b.page_id},
                             "POST /shop/cart/add", expect=(200, 400))
        self.view("/shop/cart", "GET /shop/cart", forms=True)
        self.b.form_post("/shop/cart/remove", {"product_id": pick[0], "next": "/shop/cart"}, name="POST /shop/cart/remove")

    @task(2)
    def checkout(self) -> None:
        """The shop's main write path: product -> bag -> checkout form -> place order (stock check, order + items,
        audit) -> test-mode payment page -> approve (payment, stock decrement, owner notification) -> order page."""
        own = self.cat["products_by_gym"].get(self.acc["gid"]) or []
        if not own:
            return self.cart()
        pid, slug = self.rng.choice(own)
        url = f"/shop/p/{pid}/{slug}"
        self.view(url, "GET /shop/p/[id]/[slug]")
        added = self.b.api_post_form("/shop/cart/add", {"product_id": pid, "quantity": 1, "next": url,
                                                        "page_id": self.b.page_id}, "POST /shop/cart/add", expect=(200, 400))
        if added is None or added.status_code != 200:
            return  # out of stock / age-restricted for a minor: the shop said no, the visitor moves on
        page = self.view(f"/shop/checkout?gym={self.acc['gid']}", "GET /shop/checkout", allow_redirect_to=("/shop/cart",))
        if page is None or page.status_code != 200 or 'id="checkout-form"' not in (page.text or ""):
            return
        _action, fields, boxes = parse_form(page.text or "", form_id="checkout-form")
        if "safety_ack" in boxes:
            fields["safety_ack"] = boxes["safety_ack"]  # the member reads and ticks the safety notes
        if fields.get("fulfillment") == "delivery":
            fields.update({k: v for k, v in {
                "ship_name": self.acc["e"].split("@")[0].replace("_", " ").title(),
                "ship_phone": f"+91 98{self.rng.randint(10000000, 99999999)}",
                "ship_line1": f"{self.rng.randint(1, 400)}, 5th Cross, Indiranagar", "ship_city": "Bengaluru",
                "ship_postal": "560038"}.items() if not fields.get(k)})
        fields.pop("csrf_token", None)  # form_post stamps the current token like 00-core.js

        def placed(resp: Any) -> tuple[str | None, str]:
            path = self.b._path(str(resp.url or ""))
            if "/test-gateway" in path or path.startswith("/shop/orders/"):
                return None, "placed"
            return None, "refused"  # 400: re-rendered checkout (sold out meanwhile, address problem ...)

        resp = self.b.form_post("/shop/checkout", fields, name="POST /shop/checkout", expect=(200, 400), check=placed)
        if resp is None or resp.status_code != 200 or "/test-gateway" not in self.b._path(str(resp.url or "")):
            return
        gw = self.b._path(str(resp.url or ""))
        _a, pay_fields, _b = parse_form(resp.text or "", action=gw.split("?")[0])
        pay_fields.pop("csrf_token", None)
        pay_fields.setdefault("decision", "approve")
        self.b.form_post(gw.split("?")[0], pay_fields, name="POST /shop/orders/[id]/test-gateway")

    @task(3)
    def wishlist_toggle(self) -> None:
        pick = self.product_url()
        if not pick:
            return
        url = f"/shop/p/{pick[0]}/{pick[1]}"
        self.view(url, "GET /shop/p/[id]/[slug]")
        for _ in range(2):  # save, then remove again: the wishlist stays as seeded
            self.b.api_post_form(f"/shop/wishlist/{pick[0]}/toggle", {"next": url}, "POST /shop/wishlist/[id]/toggle",
                                 expect=(200, 400))

    @task(5)
    def cart(self) -> None:
        self.view("/shop/cart", "GET /shop/cart")

    @task(3)
    def wishlist(self) -> None:
        self.view("/shop/wishlist", "GET /shop/wishlist")

    @task(6)
    def orders(self) -> None:
        self.view("/shop/orders", "GET /shop/orders")

    @task(5)
    def order_detail(self) -> None:
        orders = self.acc.get("o") or []
        if orders:
            self.view(f"/shop/orders/{self.rng.choice(orders)}", "GET /shop/orders/[id]")
        else:
            self.view("/shop/orders", "GET /shop/orders")

    @task(5)
    def calendar(self) -> None:
        self.view("/calendar", "GET /calendar")

    @task(5)
    def tasks_page(self) -> None:
        self.view("/tasks", "GET /tasks")

    @task(3)
    def account(self) -> None:
        self.view("/account", "GET /account")

    @task(3)
    def subscriptions(self) -> None:
        self.view("/me/subscriptions", "GET /me/subscriptions")

    @task(3)
    def coach(self) -> None:
        self.view("/me/coach", "GET /me/coach")

    @task(3)
    def videos(self) -> None:
        self.view("/me/videos", "GET /me/videos")

    @task(2)
    def wellness(self) -> None:
        self.view("/me/wellness", "GET /me/wellness")

    @task(4)
    def own_gym(self) -> None:
        self.view(f"/gyms/{self.acc['g']}", "GET /gyms/[slug]")

    # ---- AI (a small share; with the simulated provider the guard / fair queue / slots run for real)
    @task(3)
    def ai_assistant(self) -> None:
        if not self.ai_allowed("assistant"):
            return self.dashboard()
        self.b.api_get("/api/assistant/history", "GET /api/assistant/history")  # 52-assistant.js loads it first
        self.b.api_post_json("/api/assistant/chat", {"message": self.rng.choice(ASSISTANT_MESSAGES)},
                             "POST /api/assistant/chat", check=assistant_outcome)

    @task(2)
    def ai_plan(self) -> None:
        if not self.ai_allowed("plan"):
            return self.own_gym()
        self.view(f"/gyms/{self.acc['g']}", "GET /gyms/[slug]")
        self.b.api_post_json("/api/plans/ask", {"question": self.rng.choice(MEMBER_QUESTIONS), "gym_slug": self.acc["g"]},
                             "POST /api/plans/ask (SSE)", accept="text/event-stream", check=plan_outcome)

    @task(2)
    def ai_product(self) -> None:
        if not self.ai_allowed("product"):
            return self.product()
        pick = self.product_url()
        if pick:
            self.view(f"/shop/p/{pick[0]}/{pick[1]}", "GET /shop/p/[id]/[slug]")
            self.b.api_post_json(f"/api/shop/products/{pick[0]}/ask/stream",
                                 {"question": self.rng.choice(PRODUCT_QUESTIONS)},
                                 "POST /api/shop/products/[id]/ask/stream (SSE)", accept="text/event-stream",
                                 check=product_outcome)


# --------------------------------------------------------------------------------------------- owners
class Owner(BrowserUser):
    """Gym owner: dashboard, staff (pages, filters, detail), customers (pages, search, detail), shop dashboard / orders /
    products, finance overview + transactions, audit, payroll, gyms, jobs + their enquiries, team chat; rarely the CSV
    export. The 3 owners of the biggest chains are handed out first (accounts.json order)."""

    weight = 20
    kind = "owner"

    def sign_in(self) -> None:
        _, self.acc = allocator.take("owners", accounts()["owners"])
        self.b.ip = self.acc["ip"]
        self.login(self.acc["e"], accounts()["password"], "/owner")

    def gym_q(self) -> str:
        return f"?gym={self.rng.choice(self.acc['gyms'])}" if len(self.acc["gyms"]) > 1 and self.rng.random() < 0.5 else ""

    @task(25)
    def dashboard(self) -> None:
        self.view("/owner", "GET /owner")

    @task(10)
    def staff(self) -> None:
        r = self.rng.random()
        if r < 0.5:
            self.view("/owner/staff", "GET /owner/staff")
        elif r < 0.75:
            self.view(f"/owner/staff?page=2{self.gym_q().replace('?', '&')}", "GET /owner/staff?page=")
        else:
            self.view(f"/owner/staff?role={self.rng.choice(['trainer', 'receptionist', 'manager'])}", "GET /owner/staff?role=")

    @task(4)
    def staff_detail(self) -> None:
        if self.acc["employees"]:
            self.view(f"/owner/staff/{self.rng.choice(self.acc['employees'])}", "GET /owner/staff/[id]")

    @task(10)
    def customers(self) -> None:
        r = self.rng.random()
        if r < 0.4:
            self.view("/owner/customers", "GET /owner/customers")
        elif r < 0.6:
            self.view(f"/owner/customers?page={self.rng.randint(2, 6)}", "GET /owner/customers?page=")
        elif r < 0.8:
            self.view(f"/owner/customers?q={self.rng.choice(['a', 'sh', 'ra', 'lt_member', 'kumar', 'iyer'])}",
                      "GET /owner/customers?q=")
        else:
            self.view(f"/owner/customers?status=active{self.gym_q().replace('?', '&')}", "GET /owner/customers?status=")

    @task(5)
    def customer_detail(self) -> None:
        if self.acc["customers"]:
            self.view(f"/owner/customers/{self.rng.choice(self.acc['customers'])}", "GET /owner/customers/[id]")

    @task(6)
    def shop_dashboard(self) -> None:
        self.view("/owner/shop", "GET /owner/shop")

    @task(10)
    def shop_orders(self) -> None:
        r = self.rng.random()
        if r < 0.5:
            self.view("/owner/shop/orders", "GET /owner/shop/orders")
        elif r < 0.8:
            self.view(f"/owner/shop/orders?status={self.rng.choice(['paid', 'processing', 'completed', 'refund_requested'])}",
                      "GET /owner/shop/orders?status=")
        else:
            self.view(f"/owner/shop/orders?page={self.rng.randint(2, 5)}", "GET /owner/shop/orders?page=")

    @task(5)
    def order_detail(self) -> None:
        if self.acc["orders"]:
            self.view(f"/owner/shop/orders/{self.rng.choice(self.acc['orders'])}", "GET /owner/shop/orders/[id]")

    @task(6)
    def shop_products(self) -> None:
        r = self.rng.random()
        if r < 0.6:
            self.view("/owner/shop/products", "GET /owner/shop/products")
        else:
            self.view(f"/owner/shop/products?q={self.rng.choice(['whey', 'gloves', 'mat', 'band', 'bar'])}",
                      "GET /owner/shop/products?q=")

    @task(10)
    def finance(self) -> None:
        self.view("/owner/finance" + self.gym_q(), "GET /owner/finance")

    @task(8)
    def transactions(self) -> None:
        r = self.rng.random()
        if r < 0.5:
            self.view("/owner/finance/transactions", "GET /owner/finance/transactions")
        elif r < 0.75:
            self.view(f"/owner/finance/transactions?page={self.rng.randint(2, 8)}", "GET /owner/finance/transactions?page=")
        else:
            self.view(f"/owner/finance/transactions?direction={self.rng.choice(['incoming', 'outgoing'])}",
                      "GET /owner/finance/transactions?direction=")

    @task(5)
    def audit(self) -> None:
        self.view("/owner/audit", "GET /owner/audit")

    @task(3)
    def payroll(self) -> None:
        self.view("/owner/payroll", "GET /owner/payroll")

    @task(3)
    def gyms(self) -> None:
        self.view("/owner/gyms", "GET /owner/gyms")

    @task(3)
    def jobs(self) -> None:
        self.view("/owner/jobs", "GET /owner/jobs")

    @task(2)
    def job_enquiries(self) -> None:
        jobs = self.acc.get("jobs") or []
        if not jobs:
            return self.jobs()
        status = self.rng.choice(["", "", "new", "reviewed"])
        self.view(f"/owner/jobs/{self.rng.choice(jobs)}/enquiries" + (f"?status={status}" if status else ""),
                  "GET /owner/jobs/[id]/enquiries")

    @task(5)
    def chat(self) -> None:
        self.view("/chat", "GET /chat")
        if self.acc.get("threads"):
            self.view(f"/chat/{self.rng.choice(self.acc['threads'])}", "GET /chat/[id]")

    @task(4)
    def notifications(self) -> None:
        self.view("/account/notifications", "GET /account/notifications")

    @task(1)
    def csv_export(self) -> None:
        # a streamed CSV of this year's transactions; 303 back to the list when it would exceed EXPORT_MAX_ROWS
        year = time.gmtime().tm_year
        self.b.request("GET", f"/owner/finance/transactions.csv?from={year}-01-01", name="GET /owner/finance/transactions.csv",
                       headers=self.b.headers(accept="text/csv,*/*"), html=False)


# --------------------------------------------------------------------------------------------- staff
class Staff(BrowserUser):
    """Staff / trainer: portal, schedule, clients (+detail), chat (+thread, a reply now and then), notifications.
    The 4 trainers with 200 clients are handed out first."""

    weight = 10
    kind = "staff"

    def sign_in(self) -> None:
        _, self.acc = allocator.take("staff", accounts()["staff"])
        self.b.ip = self.acc["ip"]
        self.login(self.acc["e"], accounts()["password"], "/staff")

    @task(20)
    def portal(self) -> None:
        self.view("/staff", "GET /staff")

    @task(10)
    def schedule(self) -> None:
        self.view("/staff/schedule", "GET /staff/schedule")

    @task(10)
    def clients(self) -> None:
        self.view("/staff/clients", "GET /staff/clients")

    @task(6)
    def client_detail(self) -> None:
        if self.acc.get("clients"):
            self.view(f"/staff/clients/{self.rng.choice(self.acc['clients'])}", "GET /staff/clients/[id]")
        else:
            self.view("/staff/clients", "GET /staff/clients")

    @task(8)
    def chat(self) -> None:
        self.view("/chat", "GET /chat")
        if self.acc.get("threads"):
            self.view(f"/chat/{self.rng.choice(self.acc['threads'])}", "GET /chat/[id]")

    @task(2)
    def chat_reply(self) -> None:
        threads = self.acc.get("threads") or []
        if not threads:
            return self.chat()
        tid = self.rng.choice(threads)
        self.view(f"/chat/{tid}", "GET /chat/[id]")
        # 201 posted; 409 = the thread is read-only right now (closed by the gym) - a normal answer of the API
        self.b.api_post_json(f"/api/chat/{tid}/messages", {"body": self.rng.choice(CHAT_LINES)},
                             "POST /api/chat/[id]/messages", expect=(201, 409))

    @task(5)
    def notifications(self) -> None:
        self.view("/account/notifications", "GET /account/notifications")

    @task(3)
    def account(self) -> None:
        self.view("/account", "GET /account")


# --------------------------------------------------------------------------------------------- platform super-admin
class Superadmin(BrowserUser):
    """Platform admin - each simulated admin with an account of its own (lt_superadmin_01..10: one session per
    account, a shared account would sign the others out): overview, security centre, trust & safety, users (search,
    pages), audit, bans, AI page, review queue, outbox, the load-test page. Never /superadmin/selfcheck (it probes
    external services). A FIXED number of admins (LT_SUPERADMINS, default 3, at most 10) - admins do not scale with
    traffic."""

    weight = 3
    fixed_count = 0 if SMOKE else SUPERADMINS
    kind = "superadmin"

    def sign_in(self) -> None:
        pool = accounts().get("superadmins") or []
        if not pool:
            raise RuntimeError("accounts.json has no 'superadmins' (seeded by an older kit) - re-seed: "
                               ".venv/bin/python loadtest/seed_loadtest.py")
        _, self.acc = allocator.take("superadmins", pool)  # wraps around past 10 admins: then they share (a failure)
        self.b.ip = self.acc["ip"]
        self.login_admin(self.acc["e"], accounts()["password"])  # /superadmin/login: the admins' only sign-in

    @task(10)
    def overview(self) -> None:
        self.view("/superadmin", "GET /superadmin")

    @task(6)
    def security(self) -> None:
        self.view("/superadmin/security", "GET /superadmin/security")

    @task(6)
    def users(self) -> None:
        r = self.rng.random()
        if r < 0.4:
            self.view("/superadmin/users", "GET /superadmin/users")
        elif r < 0.7:
            self.view(f"/superadmin/users?q=lt_member_{self.rng.randint(0, 199):03d}", "GET /superadmin/users?q=")
        else:
            self.view(f"/superadmin/users?page={self.rng.randint(2, 30)}", "GET /superadmin/users?page=")

    @task(5)
    def audit(self) -> None:
        self.view("/superadmin/audit", "GET /superadmin/audit")

    @task(3)
    def bans(self) -> None:
        self.view("/superadmin/bans", "GET /superadmin/bans")

    @task(3)
    def ai(self) -> None:
        self.view("/superadmin/ai", "GET /superadmin/ai")

    @task(2)
    def reviews(self) -> None:
        self.view("/superadmin/reviews", "GET /superadmin/reviews")

    @task(2)
    def outbox(self) -> None:
        self.view("/superadmin/outbox", "GET /superadmin/outbox")

    @task(3)
    def trust(self) -> None:
        self.view("/superadmin/trust", "GET /superadmin/trust")

    @task(1)
    def load_test_page(self) -> None:
        self.view("/superadmin/load-test", "GET /superadmin/load-test")


# --------------------------------------------------------------------------------------------- abusers
ABUSE_OK = (400, 401, 403, 409, 422, 429)  # refusals are the EXPECTED outcome for abusers; 5xx and timeouts are not
SCAN_PATHS = ["/wp-login.php", "/.env", "/wp-admin/setup-config.php", "/phpmyadmin/index.php", "/.git/config",
              "/backup.sql", "/config.php.bak", "/admin/login.asp", "/server-status", "/.aws/credentials", "/vendor/phpunit",
              "/old/index.php", "/test.php", "/api/v1/debug", "/actuator/env", "/cgi-bin/luci", "/.DS_Store", "/db.zip"]
SCANNER_PROBES = ["/gyms?q=1%27%20UNION%20SELECT%20NULL,NULL,NULL--", "/shop?q=%3Cscript%3Ealert(document.cookie)%3C/script%3E",
                  "/jobs?q=..%2F..%2F..%2Fetc%2Fpasswd", "/shop?q=1%20AND%20SLEEP(5)--", "/gyms?city=%24%7Bjndi:ldap://x%7D"]


class Abuser(BrowserUser):
    """Abuse from addresses of its own block (198.19.240.0/20 - never a normal user's). Each pattern has a fixed address
    (it shows the 200 -> 429 -> 403 progression and then stays banned) and about half of the bursts come from a FRESH
    address instead (a botnet: the rate limiter, ban queue, security flush and triage keep working under load):
    password brute force against a non-existent account, a flood of one repeated assistant message (signed in with its
    own lt_abuser account), rapid identical catalogue requests, a 404 directory scan, requests with scanner signatures,
    and a WebSocket flood (50 messages in 10 s: the server closes with 4429). Expected: 400/401/403/404/429 (rate
    limits, lockout, bans); a successful sign-in with a wrong password, any 5xx, or an unstopped WebSocket flood is a
    failure. All stats names start with "ABUSE" so the report keeps them apart from the normal users."""

    weight = 5
    kind = "abuser"
    wait_time = between(5, 15) if not QUICK else between(2, 5)

    def client_ip(self) -> str:
        return ltenv.random_ip(ltenv.ABUSER_NET, self.rng)

    def sign_in(self) -> None:
        _, self.acc = allocator.take("abusers", accounts()["abusers"])
        self.ips: dict[str, str] = {}
        self.session: dict[str, str] = {}  # the flood's signed-in cookies (the other attacks run without a session)
        self.session_csrf = ""

    def use(self, pattern: str, *, session: bool) -> str:
        """Switch to the address of ``pattern`` (or, for ~half the bursts, a fresh botnet address) and to the matching
        cookie jar + CSRF token (bots don't share sessions; the flood keeps its signed-in jar between bursts)."""
        fresh = self.rng.random() < ABUSE_FRESH_IP_SHARE and pattern not in ("flood", "wsflood")
        key = "flood" if pattern == "wsflood" else pattern  # the signed-in attacks share one address and session
        self.b.ip = self.client_ip() if fresh else self.ips.setdefault(key, self.client_ip())
        self.b.cookies = dict(self.session) if session else {}
        self.b.csrf = self.session_csrf if session else ""
        self.b.signed_in = session and bool(self.session)
        reqlog.note("Abuser", f"burst {pattern}", "fresh-ip" if fresh else "fixed-ip")
        return "fresh" if fresh else "fixed"

    def burst(self, n: int, fn: Any, pause: float = 0.05) -> None:
        for _ in range(n):
            fn()
            gevent.sleep(pause)

    def ensure_session(self) -> bool:
        if self.session:
            return True
        self.b.request("GET", "/auth/login", name="ABUSE GET /auth/login", expect=(200, 403, 429))
        resp = self.b.form_post("/auth/login", {"identifier": self.acc["e"], "password": accounts()["password"],
                                                "next": ""}, name="ABUSE POST /auth/login (own account)",
                                expect=(200, 403, 429))
        if resp is not None and resp.status_code == 200:
            self.session, self.session_csrf = dict(self.b.cookies), self.b.csrf
            self.b.signed_in = True
            return True
        return False

    @task(2)
    def brute_force(self) -> None:
        self.use("brute", session=False)
        self.b.request("GET", "/auth/login", name="ABUSE GET /auth/login", expect=(200, 403, 429))
        ghost = f"ghost.{uuid.uuid4().hex[:10]}@{ltenv.EMAIL_DOMAIN}"

        def attempt() -> None:
            body = {"identifier": ghost, "password": uuid.uuid4().hex[:12], "next": ""}
            self.b.form_post("/auth/login", body, name="ABUSE POST /auth/login (unknown account)", expect=ABUSE_OK)

        self.burst(self.rng.randint(15, 30), attempt, 0.1)

    @task(1)
    def ai_flood(self) -> None:
        self.use("flood", session=True)
        self.ensure_session()
        message = "Give me a full 12 week plan right now " + "!" * self.rng.randint(1, 3)
        self.burst(self.rng.randint(25, 40),
                   lambda: self.b.api_post_json("/api/assistant/chat", {"message": message},
                                                "ABUSE POST /api/assistant/chat (flood)", expect=(200, *ABUSE_OK)), 0.05)
        if self.session:
            self.session = dict(self.b.cookies)

    @task(2)
    def rapid_identical(self) -> None:
        self.use("burst", session=False)
        url = f"/shop?q={self.rng.choice(['protein', 'whey', 'creatine'])}"

        def hit() -> None:
            self.b.request("GET", url, name="ABUSE GET /shop?q= (identical burst)", expect=(200, 403, 429))

        self.burst(self.rng.randint(80, 150), hit, 0.02)

    @task(1)
    def scan_404(self) -> None:
        self.use("scan404", session=False)

        def probe() -> None:
            path = self.rng.choice(SCAN_PATHS)
            if self.rng.random() < 0.5:
                path = f"/{uuid.uuid4().hex[:8]}{path}"
            self.b.request("GET", path, name="ABUSE GET [404 directory scan]", expect=(404, 403, 429), html=False)

        self.burst(self.rng.randint(30, 45), probe, 0.05)

    @task(1)
    def scanner_signature(self) -> None:
        self.use("scanner", session=False)
        ua = self.b.ua
        self.b.ua = self.rng.choice(["sqlmap/1.8.2#stable (https://sqlmap.org)", "Nikto/2.5.0", ua])

        def probe() -> None:
            self.b.request("GET", self.rng.choice(SCANNER_PROBES), name="ABUSE GET [scanner signature]",
                           expect=(200, 400, 403, 404, 429))

        self.burst(self.rng.randint(5, 12), probe, 0.2)
        self.b.ua = ua

    @task(1)
    def websocket_flood(self) -> None:
        self.use("wsflood", session=True)
        if not self.ensure_session():
            return
        ws_flood(self, self.b, messages=50, seconds=10.0)
        self.session = dict(self.b.cookies)


# --------------------------------------------------------------------------------------------- random junk input
JUNK_EXCLUDED_ACTIONS = ("/auth/logout", "/account/delete", "/account/sessions/revoke-others")  # would end the session
JUNK_AI_PREFIXES = ("/me/coach/generate", "/me/live/start", "/api/assistant/chat", "/api/plans/ask", "/api/tts",
                    "/api/live/sessions", "/api/shop/products/")  # well-formed asks reach the (shared) AI slots
OPEN_REDIRECTS = ["//evil.example/x", "https://evil.example/", "/\\evil.example", "\\\\evil.example", "javascript:alert(1)",
                  "%2F%2Fevil.example", "/%09/evil.example", "https:evil.example", "//127.0.0.1@evil.example", "/ /x"]
BANNED_TEXT = "Access blocked"  # the security gate's ban page / JSON (app/security/gate.py)
_JROUTE_SAFE = re.compile(r"[a-z0-9_.-]{1,40}")


def jroute(path: str) -> str:
    """Stats name of a junk request's path: ids -> [id], slugs -> [slug], anything junk -> [junk] (bounded names)."""
    p = path.split("?", 1)[0]
    for prefix, label in (("/static/", "/static/[file]"), ("/media/", "/media/[key]"), ("/maps/tiles/", "/maps/tiles/[z]/[x]/[y].png")):
        if p.startswith(prefix):
            return label
    segs = p.split("/")[1:]
    out: list[str] = []
    for i, s in enumerate(segs):
        prev = segs[i - 1] if i else ""
        if s.isdigit():
            out.append("[id]")
        elif (i == 1 and prev in ("gyms", "subscribe")) or (i == 2 and segs[:2] == ["shop", "gym"]) or \
                (i == 3 and segs[:2] == ["shop", "p"] and s != "review"):
            out.append("[slug]")
        elif prev == "photo" and i >= 2 and segs[i - 2] == "remembered":
            out.append("[token]")
        elif prev == "ip" and segs[:2] == ["superadmin", "security"]:
            out.append("[ip]")
        elif not _JROUTE_SAFE.fullmatch(s):
            out.append("[junk]")
        else:
            out.append(s)
    return "/" + "/".join(out)


class JunkLog:
    """Per-process JSON lines of every junk request that FAILED (5xx, timeout / connection error, IDOR, open redirect,
    unescaped markup): <run dir>/junk_failures_<pid>.jsonl - the input that did it, for analyze.py's junk section."""

    def __init__(self) -> None:
        self.fh: Any = None
        self.lock = threading.Lock()

    def write(self, record: dict[str, Any]) -> None:
        with self.lock:
            if self.fh is None:
                rd = run_dir()
                if rd is None:
                    return
                rd.mkdir(parents=True, exist_ok=True)
                self.fh = open(rd / f"junk_failures_{os.getpid()}.jsonl", "a", encoding="utf-8")  # noqa: SIM115
            self.fh.write(json.dumps(record, ensure_ascii=True, default=str) + "\n")
            self.fh.flush()


junk_log = JunkLog()


def _preview(body: Any, limit: int = 3000) -> str:
    if body is None:
        return ""
    if isinstance(body, bytes):
        return body[:limit].decode("utf-8", "backslashreplace") + (f"... ({len(body)} bytes)" if len(body) > limit else "")
    s = str(body)
    return s[:limit] + (f"... ({len(s)} chars)" if len(s) > limit else "")


class JunkUser(BrowserUser):
    """Random junk into every door it can reach (~10 % of the users: weight 22 of 217). Half of them are anonymous
    browsers of their own address block (198.19.128.0/18; half of those "browser-like" with Sec-Fetch headers, half
    tool-like, which the gate may ban - they then come back from a fresh address), half sign in with an account of
    their own (lt_junk_*, online members of one gym). What they send: random and missing query parameters, negative /
    huge / float / NaN / Infinity numbers, oversized strings, unicode and control characters, NUL bytes, malformed
    JSON, wrong content types, junk bytes uploaded as images, random paths, odd methods and headers, other users'
    object ids (IDOR), junk frames on the WebSocket - into public pages, search, sign-in / sign-up / recovery, every
    form of their pages (parsed like a browser: csrf_token and the form's submit_id kept) and the JSON APIs.

    Expected: any 2xx / 3xx / 4xx (validation errors, 401 / 403 / 404, 409 caps, 413, 422, 429 / 403 throttling and
    bans). FAILURE: any 5xx, a timeout / connection error, a 2xx on another user's object (IDOR), a redirect to another
    site (open redirect), junk markup reflected unescaped, 'signed in elsewhere' on its own session. Every stats name
    starts with "JUNK"; failures are written with their input to <run>/junk_failures_<pid>.jsonl (analyze.py)."""

    weight = JUNK_WEIGHT
    kind = "junk"
    _n = itertools.count()

    def client_ip(self) -> str:
        return ltenv.random_ip(ltenv.JUNK_ANON_NET, self.rng)

    def context(self) -> dict[str, Any]:
        return {"cls": "JunkUser"}

    # ---------------------------------------------------------------------------------------------- set-up
    def sign_in(self) -> None:
        k = next(JunkUser._n)
        x = allocator.worker_index + k  # alternates across processes too: a 4-user smoke gets both kinds
        self.signed = math.floor((x + 1) * JUNK_SIGNED_IN_SHARE) > math.floor(x * JUNK_SIGNED_IN_SHARE)
        self.browser_like = self.signed or self.rng.random() < 0.5
        self.attack = 0.25 if self.signed else 1.0  # HTML / SQL-looking junk: rarer from a fixed (bannable) address
        self.banned = False
        self.acc: dict[str, Any] | None = None
        self.last_ai = time.monotonic() - self.rng.uniform(0, JUNK_AI_INTERVAL)
        self.foreign = self.cat.get("foreign") or {}
        if self.signed:
            pool = accounts().get("junk") or []
            if not pool:
                print("[locustfile] accounts.json has no 'junk' accounts (older seed) - junk users stay anonymous",
                      flush=True)
                self.signed = False
            else:
                _, self.acc = allocator.take("junk", pool)
                self.b.ip = self.acc["ip"]
                self.junk_login()
        reqlog.note("JunkUser", "mode", "signed-in" if self.signed else
                    ("anonymous browser" if self.browser_like else "anonymous tool"))

    def junk_login(self) -> None:
        """Its own account, like a browser (no junk in this one request pair: the junk starts once signed in)."""
        b = self.b
        page = self.jx("GET", "/auth/login", "/auth/login (own account)", follow=False)
        _a, fields, boxes = parse_form(page.text if page is not None and page.status_code == 200 else "",
                                       action="/auth/login")
        fields.update({"identifier": self.acc["e"], "password": accounts()["password"], "device_id": b.device_id,
                       "csrf_token": b.csrf})
        if "remember" in boxes:
            fields["remember"] = boxes["remember"]
        fields.setdefault("submit_id", b.new_submit_id())
        self.jx("POST", "/auth/login", "/auth/login (own account)", body=J.urlencode_pairs(list(fields.items())),
                ctype="application/x-www-form-urlencoded")
        b.take_handoff()
        b.signed_in = any(k.endswith("ip_session") for k in b.cookies)
        if not b.signed_in:
            self.signed = False  # refused (rate limit / ban): carries on as an anonymous junk sender

    # ---------------------------------------------------------------------------------------------- the one sender
    def fresh_identity(self) -> None:
        """Banned (the gate's 403 "Access blocked"): like an attacker, come back from a fresh address - an anonymous
        sender as a new browser of its block, a signed-in one with the same session from the junk senders' rotation
        block (198.19.160.0/19) - so the junk keeps reaching the forms and APIs for the whole run."""
        self.banned = False
        if self.signed:
            self.b.ip = ltenv.random_ip(ltenv.JUNK_ROTATE_NET, self.rng)
            reqlog.note("JunkUser", "fresh address after a ban", "signed-in, same session")
        else:
            self.b = Browser(self, self.client_ip(), self.rng.choice(USER_AGENTS))
            reqlog.note("JunkUser", "fresh address after a ban", "anonymous, new browser")

    def jx(self, method: str, url: str, route: str, *, body: Any = None, ctype: str | None = None,
           headers: dict[str, str | None] | None = None, what: str = "", idor: str | None = None, follow: bool = True,
           api: bool = False, accept: str | None = None, marker: str | None = None, suffix: str = "") -> Any:
        """Send one junk request like the browser would (cookies, CSRF, Sec-Fetch for browser-like senders) and judge
        it: < 500 is fine, 5xx / transport error / IDOR / open redirect / unescaped reflection is a FAILURE (logged with
        its input). A 3xx is followed once (a GET), like the browser after a form post."""
        if self.banned:
            self.fresh_identity()
        b = self.b
        name = f"JUNK {method} {route}{suffix}"
        h = b.headers(accept=accept or ("application/json" if api else HTML_ACCEPT))
        unsafe = method not in ("GET", "HEAD", "OPTIONS", "TRACE")
        if self.browser_like:
            h.update({"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors" if api else "navigate",
                      "Sec-Fetch-Dest": "empty" if api else "document"})
        if unsafe:
            h["Origin"] = b.origin
            if api:
                h.update({"X-CSRF-Token": b.csrf, "X-Submit-Id": b.new_submit_id(), "X-Page-Id": b.page_id})
        if ctype:
            h["Content-Type"] = ctype
        for k, v in (headers or {}).items():
            if v is None:
                h.pop(k, None)
            else:
                h[k] = v
        if isinstance(body, str):
            body = body.encode("utf-8", "surrogatepass")
        location = ""
        sent = False
        try:
            with self.client.request(method, url, name=name, data=body, headers=h, catch_response=True,
                                     allow_redirects=False) as resp:
                sent = True
                b._store_cookies(resp)
                status = resp.status_code or 0
                why = self.junk_verdict(resp, status, method, url, idor, marker)
                location = (resp.headers.get("location") or "") if resp.headers and 300 <= status < 400 else ""
                if idor and location:  # where a refused IDOR probe was sent (report.md lists them for review)
                    ctx = resp.request_meta.get("context") if hasattr(resp, "request_meta") else None
                    if isinstance(ctx, dict):
                        ctx["note"] = f"-> {b._path(location).split('?', 1)[0][:56]}"
                if why:
                    resp.failure(why)
                    self.log_failure(name, method, url, h, body, what, status, why, resp)
                else:
                    resp.success()
        except Exception as exc:  # noqa: BLE001 - the client could not even send it (a kit limit, not an app answer)
            if sent:
                raise  # a bug of this kit while judging the answer: the task crash is reported (TASK / exceptions)
            reqlog.note("JunkUser", "unsendable junk request", f"{name}: {exc.__class__.__name__}")
            return None
        if follow and location and status in (301, 302, 303, 307, 308):
            target = b._path(location)
            parts = urlsplit(location)
            if target.startswith("/") and not target.startswith("//") and (not parts.netloc or parts.hostname in
                                                                         ("127.0.0.1", "localhost")):
                return self.jx("GET", target, jroute(target), follow=False, what=f"redirect after {name}")
        return resp

    def junk_verdict(self, resp: Any, status: int, method: str, url: str, idor: str | None,
                     marker: str | None) -> str | None:
        b = self.b
        if status == 0:
            return f"transport error under junk input: {getattr(resp, 'error', None)!r}"[:160]
        if status >= 500:
            return f"{status} server error under junk input"
        headers = resp.headers or {}
        if status == 401 and headers.get("x-signed-out") == "elsewhere" and self.signed:
            return f"{ELSEWHERE} (401 X-Signed-Out: elsewhere)"
        if idor and 200 <= status < 300:
            return f"IDOR: another user's {idor} answered {status}"
        if 300 <= status < 400:
            loc = headers.get("location") or ""
            parts = urlsplit(loc)
            if (parts.netloc and parts.hostname not in ("127.0.0.1", "localhost")) or loc.startswith(("//", "/\\")):
                return f"open redirect to {loc[:80]}"
            if parts.path.startswith("/auth/login") and self.signed and b.signed_in and method == "GET":
                why = b._diagnose_sign_out(b._path(loc))
                if why:
                    return why
        ctype = (headers.get("content-type") or "").lower()
        if status == 200 and "text/html" in ctype:
            text = resp.text or ""
            if marker and marker in text:
                return f"junk markup reflected unescaped (XSS): {marker}"
            m = next((x for x in J.XSS_MARKERS if x in text), None)
            if m:
                return f"unescaped junk markup in the page (XSS): {m}"
            b._after_html(text, url.split("?", 1)[0] if url.startswith("/") else "/")
        elif status == 403 and BANNED_TEXT in (resp.text or "")[:3000]:
            self.banned = True
        return None

    def log_failure(self, name: str, method: str, url: str, h: dict[str, str], body: Any, what: str, status: int,
                    why: str, resp: Any) -> None:
        try:
            snippet = (resp.text or "")[:600] if status else ""
        except Exception:  # noqa: BLE001
            snippet = ""
        junk_log.write({"t": round(time.time(), 3), "name": name, "method": method, "url": url[:4000],
                        "mode": "signed-in" if self.signed else ("anonymous browser" if self.browser_like else "anonymous tool"),
                        "ip": self.b.ip, "account": (self.acc or {}).get("e"), "status": status, "error": why,
                        "what": what[:500], "content_type": h.get("Content-Type", ""),
                        "headers": {k: v[:200] for k, v in h.items() if k.lower() not in ("cookie", "x-csrf-token")},
                        "body": _preview(body), "response": snippet})

    # ---------------------------------------------------------------------------------------------- helpers
    def ai_ok(self) -> bool:
        if getattr(self, "_touring", False):
            return True
        if time.monotonic() - self.last_ai < JUNK_AI_INTERVAL * self.rng.uniform(1.0, 1.5):
            return False
        self.last_ai = time.monotonic()
        return True

    def reflect(self) -> tuple[str, str]:
        """(payload, marker): markup that passes the gate's signatures; seen raw in a page = reflected unescaped."""
        tok = secrets.token_hex(4)
        return f"\"'><b data-jx=\"{tok}\">jx{tok}</b>", f'<b data-jx="{tok}">'

    MAX_ID_TABLE = {"orders": "shop_orders", "threads": "chat_threads", "tasks": "tasks", "notifications": "notifications",
                    "customers": "users", "products": "products", "jobs": "job_postings", "branches": "branches",
                    "employees": "employees", "subscriptions": "subscriptions"}

    def fid(self, key: str) -> tuple[str, bool]:
        """(id, foreign) for an IDOR / detail probe: mostly another user's real object (``foreign``: the seeder's sample,
        which no lt_junk_* account owns - only these are judged as IDOR), sometimes any number, sometimes junk."""
        r = self.rng.random()
        pool = self.foreign.get(key) or []
        if pool and r < 0.65:
            return str(self.rng.choice(pool)), True
        if r < 0.85:
            top = int((self.foreign.get("max_ids") or {}).get(self.MAX_ID_TABLE.get(key, key), 0) or 50_000)
            return str(self.rng.randint(1, int(top * 1.2) + 10)), False
        return self.rng.choice(["0", "-1", "999999999999", str(2**63), "1e3", "abc", "%2e%2e", "1.5", "%D9%A1%D9%A2%D9%A3",
                                "NaN", "%F0%9F%98%80", "1%00"]), False

    def product(self) -> tuple[int, str]:
        pid, slug, _ = self.rng.choice(self.cat["products"])
        return pid, slug

    def gym_slug(self) -> str:
        return self.rng.choice(self.cat["gyms"])["slug"]

    def private_ok(self) -> bool:
        return self.signed and self.b.signed_in

    # ---------------------------------------------------------------------------------------------- tasks
    @task(14)
    def public_query(self) -> None:
        """A page or API with random / missing / junk query parameters (and sometimes markup that must not come back
        unescaped)."""
        if self.banned:
            self.fresh_identity()
        pid, slug = self.product()
        g = self.gym_slug()
        routes: list[tuple[str, str, list[str], bool]] = [
            ("/", "/", ["ref", "utm_source", "q"], False),
            ("/gyms", "/gyms", ["page", "city", "mode", "q", "lat", "lon", "sort", "km", "view"], False),
            ("/gyms/[slug]", f"/gyms/{g}", ["lat", "lon", "plan", "tab"], False),
            ("/gyms/[slug]/branches", f"/gyms/{g}/branches", ["page", "city", "q"], False),
            ("/shop", "/shop", ["q", "category", "page", "sort", "gym", "in_stock", "min", "max", "brand"], False),
            ("/shop/gym/[slug]", f"/shop/gym/{g}", ["q", "category", "page", "sort"], False),
            ("/shop/p/[id]/[slug]", f"/shop/p/{pid}/{slug}", ["ref", "variant", "qty"], False),
            ("/jobs", "/jobs", ["page", "role", "lat", "lon", "km", "view", "q", "type", "city"], False),
            ("/jobs/[id]", f"/jobs/{self.rng.choice(self.cat['jobs'])}", ["ref"], False),
            ("/plans", "/plans", ["gym", "mode"], False),
            ("/api/shop/stores", "/api/shop/stores", ["q", "limit"], True),
            ("/auth/signup", "/auth/signup", ["role", "next", "email", "invite"], False),
            ("/auth/login", "/auth/login", ["next", "elsewhere", "email"], False),
            ("/api/geo/search", "/api/geo/search", ["q"], True),
            ("/api/geo/reverse", "/api/geo/reverse", ["lat", "lon"], True),
        ]
        if self.private_ok():
            routes += [
                ("/me/health", "/me/health", ["page", "metric", "range"], False),
                ("/account/notifications", "/account/notifications", ["page", "filter", "before"], False),
                ("/api/notifications", "/api/notifications", ["limit", "before", "after", "unread"], True),
                ("/api/chat/threads", "/api/chat/threads", ["limit", "before", "q"], True),
                ("/tasks", "/tasks", ["view", "date", "page", "filter"], False),
                ("/calendar", "/calendar", ["month", "year", "date", "view"], False),
                ("/shop/orders", "/shop/orders", ["page", "status"], False),
                ("/chat", "/chat", ["page", "q", "status"], False),
                ("/me/videos", "/me/videos", ["category", "q", "page"], False),
                ("/shop/checkout", "/shop/checkout", ["gym"], False),
                ("/api/assistant/history", "/api/assistant/history", ["before", "limit"], True),
                ("/me/health/alerts", "/me/health/alerts", ["page", "level"], False),
                ("/me/subscriptions", "/me/subscriptions", ["page"], False),
                ("/api/shop/cart/count", "/api/shop/cart/count", ["gym"], True),
            ]
        route, path, keys, api = self.rng.choice(routes)
        q = J.query(self.rng, keys, attack=self.attack)
        marker = None
        if not api and self.rng.random() < 0.3:
            payload, marker = self.reflect()
            q += ("&" if q else "") + f"{J.qenc(self.rng.choice(keys))}={J.qenc(payload)}"
        if route == "/auth/login" and self.rng.random() < 0.5:  # where does a signed-in sign-in page send you?
            q = f"next={J.qenc(self.rng.choice(OPEN_REDIRECTS))}"
        self.jx("GET", path + ("?" + q if q else ""), route, api=api, marker=marker, what=f"junk query: {q[:400]}")

    @task(6)
    def random_path(self) -> None:
        """A random path - or a known route with junk where its id / slug goes."""
        if self.banned:
            self.fresh_identity()
        seg = lambda: J.path_segment(self.rng)  # noqa: E731
        pid, slug = self.product()
        shapes = [
            ("[random path]", lambda: "/" + "/".join(seg() for _ in range(self.rng.randint(1, 4)))),
            ("/gyms/[junk]", lambda: f"/gyms/{seg()}"), ("/shop/p/[junk]", lambda: f"/shop/p/{seg()}/{seg()}"),
            ("/shop/p/[id]/[junk]", lambda: f"/shop/p/{pid}/{seg()}"), ("/jobs/[junk]", lambda: f"/jobs/{seg()}"),
            ("/profile/[junk]", lambda: f"/profile/{seg()}"), ("/shop/gym/[junk]", lambda: f"/shop/gym/{seg()}"),
            ("/gyms/[slug]/branches/[junk]", lambda: f"/gyms/{self.gym_slug()}/branches/{seg()}"),
            ("/media/[junk]", lambda: f"/media/{seg()}/{seg()}"), ("/static/[junk]", lambda: f"/static/{seg()}"),
            ("/maps/tiles/[junk]", lambda: f"/maps/tiles/{seg()}/{seg()}/{seg()}.png"),
            ("/me/videos/[junk]", lambda: f"/me/videos/{seg()}"), ("/api/[junk]", lambda: f"/api/{seg()}/{seg()}"),
            ("/subscribe/[junk]", lambda: f"/subscribe/{seg()}"), ("/checkout/[junk]", lambda: f"/checkout/{seg()}"),
            ("/auth/remembered/photo/[junk]", lambda: f"/auth/remembered/photo/{seg()}"),
            ("/superadmin/security/ip/[junk]", lambda: f"/superadmin/security/ip/{seg()}"),
            ("/owner/payroll/[junk]", lambda: f"/owner/payroll/{seg()}"),
        ]
        route, build = self.rng.choice(shapes)
        url = build()
        if self.rng.random() < 0.2:
            url += "?" + J.query(self.rng, ["page", "q", "id"], attack=self.attack)
        self.jx("GET", url, route, what=f"junk path: {url[:400]}", api=route.startswith("/api/"))

    def _junk_identifier(self) -> str:
        """Junk that can never be a real account's e-mail, @username, phone or member ID (a junk guess must never
        lock or e-mail a simulated user): always at least one character none of them can contain."""
        s = J.value(self.rng, 120, nul=self.rng.random() < 0.1, attack=self.attack)
        if re.fullmatch(r"[A-Za-z0-9_.@+()\s-]*", s):
            s += self.rng.choice(["😀", "<", "ﷺ", "‮", "'", "»"])
        return s

    @task(8)
    def auth_forms(self) -> None:
        """Sign-in, sign-up, password / username recovery, one-time links, the account chooser - all with junk."""
        if self.banned:
            self.fresh_identity()
        v = lambda n=200: J.value(self.rng, n, nul=self.rng.random() < 0.15, attack=self.attack)  # noqa: E731
        form = "application/x-www-form-urlencoded"
        kind = self.rng.choice(["login", "login", "signup", "signup_owner", "forgot", "forgot_username", "reset",
                                "links", "chooser", "chooser"])
        if kind in ("login", "signup", "signup_owner", "forgot", "forgot_username", "reset"):
            page = {"login": "/auth/login", "signup": "/auth/signup", "signup_owner": "/auth/signup?role=owner",
                    "forgot": "/auth/forgot", "forgot_username": "/auth/forgot-username",
                    "reset": f"/auth/reset?token={J.qenc(v(80))}"}[kind]
            r = self.jx("GET", page, jroute(page), follow=False, what=f"junk link: {page[:300]}")
            action = page.split("?", 1)[0]
            forms = J.parse_forms(r.text) if r is not None and r.status_code == 200 else []
            f = next((x for x in forms if x.method == "post" and J.same_origin_path(htmllib.unescape(x.action), action)
                      == action), None)
            pairs = self.fuzz_pairs(f, action, files=False) if f is not None else [("csrf_token", self.b.csrf)]
            # an identifier box never carries anything that could be a real account (a junk guess must never lock,
            # e-mail or sign in a simulated user - one session per account)
            pairs = [(k, self._junk_identifier() if k in ("identifier", "email", "contact", "phone", "username",
                                                            "account_ref") and val not in ("", None) else val)
                     for k, val in pairs]
            if kind == "login" and self.rng.random() < 0.4:
                pairs.append(("next", self.rng.choice(OPEN_REDIRECTS)))
            if kind == "signup" and self.rng.random() < 0.1:  # sometimes a well-formed address: junk in the rest
                pairs = [(k, f"junk.{secrets.token_hex(5)}@{ltenv.EMAIL_DOMAIN}" if k == "email" else val) for k, val in pairs]
            body = J.urlencode_pairs(pairs)
            self.jx("POST", action, action, body=body, ctype=form, what=f"junk {kind} form: {body[:600]}")
        elif kind == "links":
            page = self.rng.choice(["/auth/verify-email", "/auth/invite", "/auth/staff-invite", "/auth/reset"])
            self.jx("GET", f"{page}?token={J.qenc(v(self.rng.choice([10, 64, 3000])))}", page, what="junk token")
        else:  # the sign-in page's account chooser (JSON, CSRF header like IP.api)
            if not self.b.csrf:
                self.jx("GET", "/auth/login", "/auth/login", follow=False)
            path = self.rng.choice(["/auth/remembered", "/auth/remembered", "/auth/remembered/forget"])
            if self.rng.random() < 0.5:
                body, what = J.json_body(self.rng, {"refs": "list"} if path == "/auth/remembered" else {"ref": "text"},
                                         attack=self.attack)
            else:  # well-formed references nobody holds (and one too many)
                refs = [secrets.token_urlsafe(32) for _ in range(self.rng.choice([1, 3, 6]))]
                body, what = json.dumps({"refs": refs} if path == "/auth/remembered" else {"ref": refs[0]}).encode(), \
                    f"{len(refs)} unknown references"
            self.jx("POST", path, path, body=body, ctype=self.rng.choice(["application/json"] * 4 + ["text/plain"]),
                    api=True, what=what)

    def fuzz_pairs(self, f: J.Form, action: str, *, files: bool = True) -> list[tuple[str, Any]]:
        """The form's fields as a browser would send them - csrf_token, page_id and submit_id kept (sometimes junked
        too) - every other field kept, junked, emptied, dropped or doubled; select / checkbox / radio values out of
        range; file inputs get junk bytes; sometimes extra fields."""
        rng = self.rng
        out: list[tuple[str, Any]] = []
        for name, val in f.fields:
            if name in ("csrf_token", "page_id", "submit_id"):
                r = rng.random()
                if name == "csrf_token" and r < 0.03:
                    continue  # no token: 403 expected
                out.append((name, J.value(rng, 64, nul=False) if r > 0.98 else (val or self.b.page_id if name == "page_id" else val)))
                continue
            r = rng.random()
            if r < 0.22:
                out.append((name, val))
            elif r < 0.30:
                continue
            elif r < 0.35:
                out += [(name, val), (name, J.value(rng, 60, attack=self.attack))]
            else:
                out.append((name, J.value(rng, f.maxlen.get(name, rng.choice([120, 300, 2000])),
                                          nul=rng.random() < 0.12, attack=self.attack)))
        for name, options in f.selects.items():
            r = rng.random()
            if r < 0.4 and options:
                out.append((name, rng.choice(options)))
            elif r < 0.85:
                out.append((name, J.value(rng, 60, nul=False, attack=self.attack)))
        for name, val, checked in f.checks:
            r = rng.random()
            if r < 0.4:
                out.append((name, val))
            elif r < 0.6:
                out.append((name, J.value(rng, 30, nul=False, attack=self.attack)))
        if files:
            for name in f.files:
                fname, ctype, data, _what = J.junk_file(rng)
                out.append((name, (fname, ctype, data)))
        for _ in range(rng.choice([0, 0, 0, 1, 2])):
            out.append((rng.choice(J.EXTRA_KEYS) or "x", J.value(rng, 40, nul=False, attack=self.attack)))
        return out

    def submit_form(self, f: J.Form, page_url: str, *, label: str = "") -> None:
        action = J.same_origin_path(htmllib.unescape(f.action), page_url.split("?", 1)[0])
        if action is None or action.split("?", 1)[0].startswith(JUNK_EXCLUDED_ACTIONS):
            return
        route = jroute(action)
        if action.startswith(JUNK_AI_PREFIXES) and not self.ai_ok():
            return  # a well-formed AI form would take one of the members' AI slots - not this time
        if f.method != "post":  # a GET form (search, filters): junk in its query string
            pairs = self.fuzz_pairs(f, action, files=False)
            q = J.urlencode_pairs([(k, v) for k, v in pairs if not isinstance(v, tuple)])
            self.jx("GET", action.split("?", 1)[0] + "?" + q, route, what=f"junk GET form {label}: {q[:500]}")
            return
        pairs = self.fuzz_pairs(f, action)
        has_files = any(isinstance(v, tuple) for _k, v in pairs)
        r = self.rng.random()
        if has_files or "multipart" in f.enctype or r < 0.07:
            body, ctype = J.multipart(pairs, self.rng)
        elif r < 0.11:
            body, ctype = json.dumps({k: v for k, v in pairs}, ensure_ascii=False).encode("utf-8", "surrogatepass"), \
                "application/json"
        elif r < 0.14:
            body, ctype = J.urlencode_pairs(pairs).encode(), self.rng.choice(["text/plain", "application/xml", ""])
        else:
            body, ctype = J.urlencode_pairs(pairs).encode(), "application/x-www-form-urlencoded"
        what = f"junk form {label} ({ctype or 'no content type'}): " + _preview(body, 600)
        resp = self.jx("POST", action, route, body=body, ctype=ctype or None, what=what)
        if resp is not None and self.rng.random() < 0.12 and 200 <= (resp.status_code or 0) < 400:
            # the double-submit guard: the very same request (same submit_id) again - replayed or 409, never 5xx
            self.jx("POST", action, route, body=body, ctype=ctype or None, what="resubmitted: " + what,
                    suffix=" (resubmit)", follow=False)

    def form_pages(self) -> list[str]:
        pid, slug = self.product()
        job = self.rng.choice(self.cat["jobs"])
        public = ["/auth/login", "/auth/signup", "/auth/forgot", "/auth/forgot-username", f"/jobs/{job}",
                  f"/shop/p/{pid}/{slug}", "/gyms", "/shop", "/jobs", f"/subscribe/{self.gym_slug()}"]
        if not self.private_ok():
            return public
        own = (self.acc or {}).get("g") or self.gym_slug()
        return public[4:] + ["/account", "/account", "/me/health", "/me/health", "/me/health/profile",
                             "/me/health/profile/fitness", "/me/wellness", "/tasks", "/tasks", "/calendar", "/chat/new",
                             "/reports/new", "/shop/cart", "/account/identity", "/account/notifications",
                             "/me/health/alerts", "/me/subscriptions", "/shop/wishlist", "/chat", "/me/coach",
                             f"/subscribe/{own}", f"/shop/checkout?gym={(self.acc or {}).get('gid') or ''}",
                             "/me/health/profile/ai"]

    @task(16)
    def forms(self) -> None:
        """Every form of its pages (parsed like a browser), submitted with junk."""
        if self.banned:
            self.fresh_identity()
        page = self.rng.choice(self.form_pages())
        r = self.jx("GET", page, jroute(page), follow=False)
        if r is None or r.status_code != 200 or "text/html" not in (r.headers.get("content-type") or ""):
            return
        forms = [f for f in J.parse_forms(r.text) if f.fields or f.files or f.selects or f.checks]
        posts = [f for f in forms if f.method == "post"]
        pick = posts if posts and self.rng.random() < 0.85 else forms
        if pick:
            self.submit_form(self.rng.choice(pick), page, label=jroute(page))

    @task(4)
    def uploads(self) -> None:
        """Junk bytes as images into the upload forms (profile photo, ID card, health device photo)."""
        if not self.private_ok():
            return self.forms()
        page = self.rng.choice(["/account", "/account/identity", "/me/health"])
        r = self.jx("GET", page, page, follow=False)
        if r is None or r.status_code != 200:
            return
        with_files = [f for f in J.parse_forms(r.text) if f.files and f.method == "post"]
        if with_files:
            self.submit_form(self.rng.choice(with_files), page, label=f"upload on {page}")

    @task(12)
    def json_api(self) -> None:
        """The JSON APIs with junk bodies: malformed JSON, wrong types, huge / NaN numbers, NUL bytes, deep nesting,
        wrong content types, missing CSRF header - and junk ids in their paths."""
        if self.banned:
            self.fresh_identity()
        if not self.b.csrf:  # IP.api reads the token from the page's meta tag
            self.jx("GET", "/shop" if not self.private_ok() else "/me", "/shop" if not self.private_ok() else "/me",
                    follow=False)
        pid, _slug = self.product()
        apis: list[tuple[str, str, dict[str, str]]] = [
            ("/api/shop/products/[id]/ask", f"/api/shop/products/{pid}/ask", {"question": "text"}),
            ("/api/plans/ask", "/api/plans/ask", {"question": "text", "gym_slug": "text"}),
            ("/auth/remembered", "/auth/remembered", {"refs": "list"}),
        ]
        if self.private_ok():
            apis += [
                ("/api/assistant/chat", "/api/assistant/chat", {"message": "text"}),
                ("/api/me/location", "/api/me/location", {"lat": "number", "lon": "number", "accuracy": "number",
                                                          "timezone": "text"}),
                ("/api/notifications/read-all", "/api/notifications/read-all", {}),
                ("/api/notifications/[id]/read", f"/api/notifications/{self.fid('notifications')[0]}/read", {}),
                ("/api/chat/[id]/messages", f"/api/chat/{self.fid('threads')[0]}/messages", {"body": "text"}),
                ("/api/chat/[id]/read", f"/api/chat/{self.fid('threads')[0]}/read", {}),
                ("/api/memory/confirm", "/api/memory/confirm", {"token": "text"}),
                ("/api/memory/undo", "/api/memory/undo", {"token": "text"}),
                ("/api/live/sessions", "/api/live/sessions", {"plan_id": "id", "day_index": "number", "quick": "text",
                                                              "voice_enabled": "bool"}),
                ("/api/tts", "/api/tts", {"text": "text"}),
                ("/shop/cart/add", "/shop/cart/add", {"product_id": "id", "quantity": "number", "next": "text"}),
                ("/shop/wishlist/[id]/toggle", f"/shop/wishlist/{self.fid('products')[0]}/toggle", {"next": "text"}),
            ]
        route, path, fields = self.rng.choice(apis)
        body, what = J.json_body(self.rng, fields, attack=self.attack)
        if path.startswith(JUNK_AI_PREFIXES) and not self.ai_ok():
            body, what = self.rng.choice([(b"{", "malformed JSON"), (b'{"question": 7, "message": 7, "text": 7}',
                                                                      "wrong types"), (b"[]", "a list")])
        ctype = self.rng.choice(["application/json"] * 6 + ["text/plain", "application/x-www-form-urlencoded",
                                                            "application/json; charset=utf-16", "multipart/form-data", ""])
        headers: dict[str, str | None] = {}
        if self.rng.random() < 0.05:
            headers["X-CSRF-Token"] = None  # no token: 403 expected
        accept = "text/event-stream" if "/ask" in path and "stream" in path or path == "/api/plans/ask" else None
        self.jx("POST", path, route, body=body, ctype=ctype or None, api=True, headers=headers, accept=accept,
                what=f"{what} as {ctype or 'no content type'}: " + _preview(body, 600), follow=False)

    @task(10)
    def idor(self) -> None:
        """Other users' orders, threads, tasks, documents, staff, payroll ... by id: 403 / 404 expected, a 2xx is an
        IDOR. Anonymous senders try them too (sign-in redirect / 401 expected)."""
        if self.banned:
            self.fresh_identity()
        gets = [("/shop/orders/[id]", "/shop/orders/{}", "orders"), ("/shop/orders/[id]/receipt", "/shop/orders/{}/receipt", "orders"),
                ("/shop/orders/[id]/pay", "/shop/orders/{}/pay", "orders"),
                ("/shop/orders/[id]/test-gateway", "/shop/orders/{}/test-gateway", "orders"),
                ("/chat/[id]", "/chat/{}", "threads"), ("/api/chat/[id]/messages", "/api/chat/{}/messages?after=0&limit=50", "threads"),
                ("/me/wellness/[id]", "/me/wellness/{}", "wellness"), ("/identity/[id]/front", "/identity/{}/front", "identity_docs"),
                ("/checkout/[id]", "/checkout/{}", "subscriptions"), ("/owner/customers/[id]", "/owner/customers/{}", "customers"),
                ("/owner/staff/[id]", "/owner/staff/{}", "employees"), ("/owner/shop/orders/[id]", "/owner/shop/orders/{}", "orders"),
                ("/owner/shop/products/[id]", "/owner/shop/products/{}", "products"),
                ("/owner/branches/[id]", "/owner/branches/{}", "branches"),
                ("/owner/jobs/[id]/enquiries", "/owner/jobs/{}/enquiries", "jobs"),
                ("/owner/enquiries/[id]/resume", "/owner/enquiries/{}/resume", "enquiries"),
                ("/owner/payroll/[id]", "/owner/payroll/{}", "statements"), ("/owner/identity/[id]", "/owner/identity/{}", "identity_docs"),
                ("/owner/reports/[id]", "/owner/reports/{}", "reports"), ("/staff/clients/[id]", "/staff/clients/{}", "customers"),
                ("/superadmin/outbox/[id]", "/superadmin/outbox/{}", "emails"), ("/superadmin/users", "/superadmin/users", ""),
                ("/api/owner/branches/[id]/status", "/api/owner/branches/{}/status", "branches"),
                ("/owner/staff/[id]/documents", "/owner/staff/{}/documents", "employees")]
        posts = [("/tasks/[id]/toggle", "/tasks/{}/toggle", "tasks"), ("/tasks/[id]/delete", "/tasks/{}/delete", "tasks"),
                 ("/api/notifications/[id]/read", "/api/notifications/{}/read", "notifications"),
                 ("/account/notifications/[id]/read", "/account/notifications/{}/read", "notifications"),
                 ("/me/health/alerts/[id]/ack", "/me/health/alerts/{}/ack", "alerts"),
                 ("/me/health/profile/conditions/[id]/delete", "/me/health/profile/conditions/{}/delete", "conditions"),
                 ("/shop/orders/[id]/cancel", "/shop/orders/{}/cancel", "orders"),
                 ("/shop/orders/[id]/refund-request", "/shop/orders/{}/refund-request", "orders"),
                 ("/api/chat/[id]/messages", "/api/chat/{}/messages", "threads"), ("/api/chat/[id]/read", "/api/chat/{}/read", "threads"),
                 ("/chat/[id]/close", "/chat/{}/close", "threads"), ("/chat/[id]/messages", "/chat/{}/messages", "threads"),
                 ("/me/subscriptions/[id]/cancel", "/me/subscriptions/{}/cancel", "subscriptions"),
                 ("/owner/shop/orders/[id]/status", "/owner/shop/orders/{}/status", "orders"),
                 ("/owner/shop/refunds/[id]/approve", "/owner/shop/refunds/{}/approve", "refunds"),
                 ("/owner/shop/reviews/[id]/toggle", "/owner/shop/reviews/{}/toggle", "reviews"),
                 ("/owner/enquiries/[id]/status", "/owner/enquiries/{}/status", "enquiries"),
                 ("/owner/staff/[id]/suspend", "/owner/staff/{}/suspend", "employees"),
                 ("/superadmin/users/[id]/delete", "/superadmin/users/{}/delete", "customers"),
                 ("/account/identity/[id]/recheck", "/account/identity/{}/recheck", "identity_docs"),
                 ("/checkout/[id]/pay", "/checkout/{}/pay", "subscriptions")]
        if self.rng.random() < 0.55:
            route, tpl, key = self.rng.choice(gets)
            oid, foreign = self.fid(key) if key else ("", True)
            path = tpl.format(oid) if key else tpl
            api = path.startswith("/api/")
            self.jx("GET", path, route, idor=(key or route) if foreign else None, suffix=" (IDOR)", follow=False,
                    api=api, what=f"another user's {key or route}: {path}")
            return
        route, tpl, key = self.rng.choice(posts)
        oid, foreign = self.fid(key)
        path = tpl.format(oid)
        if not self.b.csrf:
            self.jx("GET", "/me" if self.private_ok() else "/", "/me" if self.private_ok() else "/", follow=False)
        if path.startswith("/api/"):
            body, ctype = json.dumps({"body": J.text(self.rng, 200, attack=self.attack)}).encode(), "application/json"
            api = True
        else:
            pairs = [("csrf_token", self.b.csrf), ("page_id", self.b.page_id), ("submit_id", self.b.new_submit_id()),
                     ("status", self.rng.choice(["completed", "cancelled", J.value(self.rng, 30, nul=False)])),
                     ("reason", J.text(self.rng, 120, attack=self.attack)), ("note", J.text(self.rng, 200, attack=self.attack)),
                     ("next", self.rng.choice(["/", "/tasks", self.rng.choice(OPEN_REDIRECTS)]))]
            body, ctype, api = J.urlencode_pairs(pairs).encode(), "application/x-www-form-urlencoded", False
        self.jx("POST", path, route, body=body, ctype=ctype, idor=key if foreign else None, suffix=" (IDOR)",
                follow=False, api=api, what=f"POST on another user's {key}: {path}")

    @task(4)
    def headers_methods(self) -> None:
        """Odd methods and headers: PUT / DELETE / PATCH / OPTIONS / TRACE / PROPFIND / HEAD on known routes; junk Accept,
        Accept-Language, Cookie, Content-Type, Referer, Host, X-Page-Id, X-Submit-Id, If-None-Match, Range. Never the
        X-Forwarded-For / -Proto the proxy sets (that is the sender's identity)."""
        if self.banned:
            self.fresh_identity()
        pid, slug = self.product()
        paths = ["/", "/gyms", "/shop", f"/shop/p/{pid}/{slug}", "/jobs", "/auth/login", "/api/shop/stores?q=fit",
                 "/api/notifications/unread", "/tasks", "/static/dist/app.min.css", "/me", "/healthz"]
        path = self.rng.choice(paths)
        route = jroute(path)
        r = self.rng.random()
        if r < 0.4:
            method = self.rng.choice(["PUT", "DELETE", "PATCH", "OPTIONS", "TRACE", "PROPFIND", "HEAD", "CONNECT"])
            body = J.value(self.rng, 200, attack=self.attack) if method in ("PUT", "PATCH", "DELETE") else None
            self.jx(method, path, route, body=body, ctype="application/x-www-form-urlencoded" if body else None,
                    follow=False, what=f"{method} on {path}")
            return
        junk_headers: dict[str, str | None] = {}
        choices = {
            "Accept": lambda: self.rng.choice(["*/*;q=NaN", "text/html;q=2", "application/json, */*;q=-1", "😀/😀",
                                               "a" * 3000, ""]),
            "Accept-Language": lambda: self.rng.choice(["xx-YY;q=NaN", "*;q=1e9", ",,,,", "en-" + "x" * 500, "日本"]),
            "Cookie": lambda: self.rng.choice(["=;;=;", "__Host-ip_session=" + "A" * 4000, "a=\"unterminated",
                                               "__Host-ip_csrf=junk; __Host-ip_csrf=other", "x=" + "é" * 50, ";" * 200]),
            "Referer": lambda: self.rng.choice(["javascript:alert(1)", "//evil.example", "https://evil.example/" + "x" * 2000, "😀"]),
            "Host": lambda: self.rng.choice(["evil.example", "127.0.0.1:12000@evil.example", "[::1]", "a" * 300, ""]),
            "X-Page-Id": lambda: J.value(self.rng, 80, nul=False),
            "If-None-Match": lambda: self.rng.choice(['"junk"', "*", "W/" + "a" * 500, '"\\"']),
            "Range": lambda: self.rng.choice(["bytes=-1", "bytes=999999999-", "bytes=0-0,0-0,0-0", "bytes=a-b", "bytes=1-0"]),
            "Content-Type": lambda: self.rng.choice(["application/json; charset=junk", "multipart/form-data", "text/😀"]),
            "Accept-Encoding": lambda: self.rng.choice(["br;q=NaN", "identity;q=0, *;q=0", "x" * 1000]),
            "User-Agent": lambda: J.text(self.rng, 300, attack=0),
        }
        for key in self.rng.sample(list(choices), k=self.rng.randint(1, 3)):
            junk_headers[key] = choices[key]()
        method = "GET"
        body = None
        if self.rng.random() < 0.25:
            method, path, route = "POST", "/api/notifications/read-all", "/api/notifications/read-all"
            junk_headers["X-Submit-Id"] = self.rng.choice(["short", "x" * 65, "😀" * 20, "../../", "a b c d e f g h i j k"])
            body = b"{}"
        for k, val in list(junk_headers.items()):  # one line of latin-1 without control characters: what HTTP can carry
            if val is not None:
                junk_headers[k] = re.sub(r"[\x00-\x08\x0a-\x1f\x7f]", " ", val.encode("utf-8").decode("latin-1"))
        self.jx(method, path, route, body=body, ctype="application/json" if body else None, headers=junk_headers,
                api=path.startswith("/api/"), follow=False, what=f"junk headers {sorted(junk_headers)}: "
                + json.dumps(junk_headers)[:600])

    @task(2)
    def websocket_junk(self) -> None:
        """Junk frames on its own /ws socket: broken JSON, wrong types, binary, oversized text. The server may ignore
        them or close (1003 / 1007 / 1008 / 1009 / 4xxx); 1011 (internal error) or a dropped socket is a FAILURE."""
        if not self.private_ok():
            return self.public_query()
        b = self.b
        started = time.perf_counter()
        frames = ["{", "null", "[]", '{"type": 123}', '{"type": "ping", "x": ' + json.dumps(J.text(self.rng, 200)) + "}",
                  '{"type": "' + "p" * 5000 + '"}', "\x00\x01", J.text(self.rng, 2000), "A" * 70_000,
                  '{"type": "subscribe", "channel": "../../admin"}', '{"type": "ping"}' * 50]
        sent = self.rng.sample(frames, k=self.rng.randint(3, 6))
        code: int | None = None
        error: str | None = None
        try:
            ws = websocket.create_connection(
                f"ws://{b.host}/ws?page={J.qenc(self.rng.choice([b.page_id, J.text(self.rng, 80, attack=0)]))}",
                timeout=10, header=[f"User-Agent: {b.ua}", f"X-Forwarded-For: {b.ip}", "X-Forwarded-Proto: https"],
                cookie=b.cookie_header(), origin=b.origin, enable_multithread=True)
        except Exception as exc:  # noqa: BLE001 - a refused handshake (4xx) is a fine answer to an odd page id
            status = int(getattr(exc, "status_code", 0) or 0)
            why = f"handshake answered {status}" if status >= 500 else (
                None if status else f"handshake failed: {exc.__class__.__name__}: {str(exc)[:80]}")
            self.environment.events.request.fire(request_type="WS", name="JUNK WS /ws (junk frames)",
                                                 response_time=(time.perf_counter() - started) * 1000, response_length=0,
                                                 exception=RuntimeError(why) if why else None, context=self.context(),
                                                 response=None)
            return
        try:
            ws.settimeout(2.0)
            for frame in sent:
                try:
                    if self.rng.random() < 0.2:
                        ws.send_binary(frame.encode("utf-8", "surrogatepass"))
                    else:
                        ws.send(frame)
                except Exception:  # noqa: BLE001 - closed by the server mid-way: read the close code below
                    break
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                try:
                    op, data = ws.recv_data(control_frame=True)
                except websocket.WebSocketTimeoutException:
                    break
                except Exception as exc:  # noqa: BLE001
                    code, error = 1006, f"socket dropped without a close frame: {exc.__class__.__name__}"
                    break
                if op == websocket.ABNF.OPCODE_CLOSE:
                    code = close_code(data)
                    break
        finally:
            try:
                ws.close(status=4001, reason=b"page-hide", timeout=1)
            except Exception:  # noqa: BLE001
                pass
        bad = code in (1011, 1006) or (code == 4409)
        why = None
        if bad:
            why = (f"{ELSEWHERE} (4409)" if code == 4409 else error or f"closed by server: {code} internal error")
            junk_log.write({"t": round(time.time(), 3), "name": "JUNK WS /ws (junk frames)", "method": "WS", "url": "/ws",
                            "mode": "signed-in", "ip": b.ip, "account": (self.acc or {}).get("e"), "status": code,
                            "error": why, "what": "junk frames", "body": json.dumps([f[:300] for f in sent]),
                            "response": ""})
        self.environment.events.request.fire(request_type="WS", name="JUNK WS /ws (junk frames)",
                                             response_time=(time.perf_counter() - started) * 1000, response_length=0,
                                             exception=RuntimeError(why) if why else None, context=self.context(),
                                             response=None)
        if code is not None:
            reqlog.note("JunkUser", "ws close", f"{code} {CLOSE_LABELS.get(code, '')}".strip())


# --------------------------------------------------------------------------------------------- smoke: fixed counts
def _smoke_counts() -> None:  # a function: a module-level loop variable would look like one more User class to Locust
    for cls in (AnonymousVisitor, Member, Owner, Staff, Superadmin, Abuser, JunkUser):
        cls.fixed_count = SMOKE_COUNTS[cls.__name__]


if SMOKE:
    _smoke_counts()
elif JUNK_WEIGHT <= 0:
    JunkUser.abstract = True  # LT_JUNK_WEIGHT=0: a run without junk senders
