"""Frontend contract of the shared AI layer (docs/ai-guard-and-queue.md section 7).

* ``app/static/src/js/00-core.js``: ``IP.pageId`` (random per document; a ``crypto.getRandomValues`` fallback for
  plain-HTTP LAN access, never sessionStorage), ``X-Page-Id`` on every ``IP.api`` request, ``opts.keepalive``,
  ``err.code`` / ``err.retryAfter``, ``/ws?page=<id>``, close code 4001 on pagehide without reconnecting, a new id and
  a fresh socket after a back/forward-cache restore, a >= 30 s back-off after 4429, and a hidden ``page_id`` field on
  same-origin POST forms.
* ``app/static/src/js/01-ai.js``: ``IP.ai`` (request, stream, describeError, cooldownLeft, noteRefusal, onCooldown,
  abortAll, pending), the "Waiting for AI" island activity and the background-task notes. ``noteRefusal(feature,
  code, seconds)`` records a refusal that came inside a 200 answer exactly like a 429 with that code and wait.

Three layers:

1. static - the sources, a bundle built exactly like ``scripts/build_assets.py`` (into tmp_path) and the served dist
   bundle carry the contract and none of the constructs the strict CSP forbids;
2. behaviour - the sources, and their rjsmin-minified bundle, run in JavaScriptCore (Safari's engine) inside a small
   stub browser (``HARNESS``: virtual clock and timers, fetch with streaming bodies, WebSocket, DOM events). Skipped
   where no ``jsc`` binary exists; ``JSC=/path/to/jsc`` points at one;
3. server - a signed-in page ships the nonce'd bundle, and routes accept the ``X-Page-Id`` header and the
   ``page_id`` form field the client now always sends.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from tests.conftest import api, login_as, post_form
from tests.factories import make_customer

ROOT = Path(__file__).resolve().parent.parent
JS_SRC = ROOT / "app" / "static" / "src" / "js"
CORE = JS_SRC / "00-core.js"
AI = JS_SRC / "01-ai.js"
DIST_MIN = ROOT / "app" / "static" / "dist" / "app.min.js"
MAC_JSC = "/System/Library/Frameworks/JavaScriptCore.framework/Versions/Current/Helpers/jsc"

# present in every build of the bundle (they survive rjsmin)
BUNDLE_MARKERS = ("X-Page-Id", "/ws?page=", "IP.ai=", "cooldown_local", "text/event-stream", "close(4001,",
                  "page-hide", "Waiting for AI", "retry-after", "retry_after", "noteRefusal:")
AI_API = ("request", "stream", "describeError", "cooldownLeft", "noteRefusal", "onCooldown", "abortAll", "pending")
# checked on comment-free code (rjsmin output), so doc comments may mention them
FORBIDDEN = ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "new Function(", "document.write(",
             'setAttribute("style"', ".cssText", 'setTimeout("', "setTimeout('", 'setInterval("', "setInterval('",
             "sessionStorage", "localStorage", "javascript:", '"visibilitychange"')
PAGE = "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed"
SCRIPT_TAG = re.compile(r'<script nonce="([^"]+)" src="(/static/dist/app\.min\.js\?v=[0-9a-f]+)" defer></script>')


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _between(src: str, start: str, end: str) -> str:
    i = src.index(start)
    return src[i:src.index(end, i)]


def _code(path: Path) -> str:
    """The file without comments (rjsmin understands strings and regex literals; a regex-based strip would not)."""
    import rjsmin

    return rjsmin.jsmin(_read(path), keep_bang_comments=False)


def _build_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    """Run the real bundler, writing into tmp_path instead of app/static/dist (a dev watcher owns that)."""
    from scripts import build_assets

    monkeypatch.setattr(build_assets, "DIST", tmp_path)
    build_assets.build_js()
    return _read(tmp_path / "app.js"), _read(tmp_path / "app.min.js")


def _jsc() -> str | None:
    for candidate in (os.environ.get("JSC"), shutil.which("jsc"), MAC_JSC):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


needs_jsc = pytest.mark.skipif(_jsc() is None, reason="JavaScriptCore (jsc) is not available on this machine")


# =============================================================================================== static checks
def test_bundle_runs_ai_after_core_and_before_the_island(tmp_path, monkeypatch):
    bundle, minified = _build_bundle(tmp_path, monkeypatch)
    order = [bundle.index(f"/* {name} */\n") for name in ("00-core.js", "01-ai.js", "05-island.js", "08-ask-ai.js",
                                                         "52-assistant.js", "55-live-coach.js", "80-shop.js")]
    assert order == sorted(order), "IP.ai must be defined after the core and before the island and every AI widget"
    missing = [m for m in BUNDLE_MARKERS if m not in minified]
    assert not missing, missing
    assert minified.index("IP.pageId=") < minified.index("IP.ai=")


def test_served_bundle_contains_the_ai_layer():
    missing: list[str] = []
    for _ in range(3):  # the dev watcher may be rewriting the file right now
        text = _read(DIST_MIN)
        missing = [m for m in BUNDLE_MARKERS if m not in text]
        if not missing:
            break
        time.sleep(1)
    assert not missing, f"app/static/dist/app.min.js is stale (missing {missing}): run python scripts/build_assets.py --js-only"


def test_core_page_id_is_random_per_document_and_kept_in_memory():
    src = _read(CORE)
    gen = _between(src, "function newPageId", "IP.pageId = newPageId();")
    assert 'typeof c.randomUUID === "function"' in gen and "c.randomUUID()" in gen
    assert "getRandomValues(new Uint8Array(16))" in gen  # http://<lan-ip> is not a secure context: no randomUUID
    restore = _between(src, 'window.addEventListener("pageshow"', "stampPageId")
    assert "e.persisted" in restore and "IP.pageId = newPageId();" in restore and "connectWs();" in restore


def test_page_id_shapes_match_the_server_rule():
    from app.ai.owner import PAGE_ID_RE, clean_page_id

    assert clean_page_id(PAGE) == PAGE  # crypto.randomUUID()
    assert clean_page_id("0123456789abcdef0123456789abcdef") is not None  # 16 random bytes as hex
    assert PAGE_ID_RE.pattern.strip("^$") in HARNESS  # the JS tests check generated ids with the same rule


def test_core_sends_the_page_id_with_every_request_and_form():
    src = _read(CORE)
    api_fn = _between(src, "IP.api = async function", "IP.escape =")
    assert 'init.headers["X-Page-Id"] = IP.pageId' in api_fn
    assert "init.keepalive = true" in api_fn
    assert "throw IP.apiError(res, data)" in api_fn
    assert '"/ws?page=" + encodeURIComponent(IP.pageId)' in src
    stamp = _between(src, "function stampPageId", "function wireGlobal")
    assert '"page_id"' in stamp and '"hidden"' in stamp and 'method !== "post"' in stamp and "sameOrigin(action)" in stamp
    wire = _between(src, "function wireGlobal", "// confirmation for destructive forms")
    assert re.search(r'addEventListener\(\s*"submit",\s*function \(e\) \{\s*stampPageId\(e\.target, e\.submitter\);\s*\},'
                     r'\s*true\s*\)', wire), "page_id must be added in the capture phase, before component handlers"


def test_core_parses_retry_after_and_codes():
    src = _read(CORE)
    err_fn = _between(src, "IP.apiError = function", "IP.api = async function")
    for piece in ('get("retry-after")', "body.retry_after", "err.status = res.status", "err.data = data",
                  "err.code =", "err.retryAfter ="):
        assert piece in err_fn, piece
    parse = _between(src, "IP.parseRetryAfter = function", "IP.apiError = function")
    assert "Date.parse" in parse  # Retry-After may be an HTTP date


def test_core_socket_lifecycle_and_close_codes():
    src = _read(CORE)
    hide = _between(src, 'window.addEventListener("pagehide"', 'window.addEventListener("pageshow"')
    assert "wsStopped = true" in hide and "closeWsForPageHide()" in hide
    close = _between(src, "function closeWsForPageHide", "/* -----")
    assert 's.close(4001, "page-hide")' in close and "__ipIntentional = true" in close
    onclose = _between(src, "sock.onclose = function", "/** Close our socket on purpose")
    assert "if (intentional || wsStopped) return;" in onclose  # no reconnect after our own close
    assert "e.code === 4401 || e.code === 4403" in onclose
    assert "e.code === 4429" in onclose and "30000 * Math.pow(2, IP.ws.floods - 1)" in onclose  # >= 30 s
    assert "}, 25000);" in src  # keep-alive ping unchanged
    for path in (CORE, AI):
        assert '"visibilitychange"' not in _read(path), f"{path.name}: never cancel on visibilitychange"


def test_ai_layer_api_and_wire_protocol():
    src = _read(AI)
    exported = _between(src, "IP.ai = {", "})();")
    for name in AI_API:
        assert re.search(rf"\b{name}:", exported), name
    for piece in ('IP.on("ai.queue"', 'IP.on("task.update"', "evt.page_id !== IP.pageId", '"ai-queue"', '"Waiting for AI"',
                  '"You are next"', '" ahead"', 'IP.emit("ai.cooldown"', '"cooldown_local"', 'Accept: "text/event-stream"',
                  '"X-CSRF-Token": IP.csrf()', 'headers["X-Page-Id"] = IP.pageId', 'credentials: "same-origin"',
                  '"queued"', '"blocked"', '"Paused while you were away - resuming now"', 'abortAll("page-hide")',
                  "{ 409: true, 422: true, 429: true, 503: true }", "IP.parseRetryAfter(evt.retry_after)",
                  "IP.apiError(res, data)", "err.status = 429", 'err.code = "cooldown_local"'):
        assert piece in src, piece
    for code in ("quota", "cooldown", "too_many", "duplicate", "spam", "busy", "disabled", "cancelled", "timeout"):
        assert re.search(rf"\b{code}\b", src), code
    assert "navigator.onLine === false" in src
    request_fn = _between(src, "function request(url, opts)", "/* -----")
    assert "IP.api(url" in request_fn and "localRefusal(key)" in request_fn  # refused locally before any fetch
    # contract section 9: safety flows are never blocked by a local cooldown nor cut by abortAll / pagehide
    assert src.count("opts.safety ? null : localRefusal(key)") == 2
    assert "!!(opts.keepalive || opts.safety)" in src


def test_island_is_resolved_lazily():
    """01-ai.js runs before 05-island.js defines IP.island: it may only look it up when it needs it."""
    code = _code(AI)
    assert "function island()" in code
    assert "IP.island." not in code and re.findall(r"IP\.island\b", code) == ["IP.island"]


@pytest.mark.parametrize("path", [CORE, AI], ids=lambda p: p.name)
def test_no_constructs_the_csp_forbids(path):
    code = _code(path)
    found = [token for token in FORBIDDEN if token in code]
    assert not found, f"{path.name}: {found}"
    assert not re.search(r"\.style\s*=", code), "set CSSOM properties, never the style attribute"


# =============================================================================================== behaviour (JavaScriptCore)
def _run_harness(tmp_path: Path, *scripts: Path) -> str:
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    proc = subprocess.run([_jsc(), str(harness), "--", *(str(s) for s in scripts)], capture_output=True, text=True,
                          timeout=240, cwd=tmp_path)
    return proc.stdout + proc.stderr


def _assert_all_passed(out: str) -> None:
    m = re.search(r"SUMMARY passed=(\d+) failed=(\d+) total=(\d+)", out)
    assert m, out[-6000:]
    passed, failed, total = map(int, m.groups())
    failures = [line for line in out.splitlines() if line.startswith("FAIL")]
    assert failed == 0 and not failures, "\n".join(failures) + "\n\n" + out[-6000:]
    assert passed == total >= 50, out[-3000:]


@needs_jsc
def test_core_and_ai_behaviour_in_javascriptcore(tmp_path):
    _assert_all_passed(_run_harness(tmp_path, CORE, AI))


@needs_jsc
def test_minified_core_and_ai_behave_the_same(tmp_path):
    import rjsmin

    bundle = "\n".join(f"/* {p.name} */\n{_read(p)}\n;" for p in (CORE, AI))  # as scripts/build_assets.py joins them
    minified = tmp_path / "core-ai.min.js"
    minified.write_text(rjsmin.jsmin(bundle, keep_bang_comments=False), encoding="utf-8")
    _assert_all_passed(_run_harness(tmp_path, minified))


@needs_jsc
def test_built_bundle_parses_in_javascriptcore(tmp_path, monkeypatch):
    _build_bundle(tmp_path, monkeypatch)
    check = tmp_path / "check.js"
    check.write_text(CHECK_SYNTAX, encoding="utf-8")
    proc = subprocess.run([_jsc(), str(check), "--", str(tmp_path / "app.js"), str(tmp_path / "app.min.js")],
                          capture_output=True, text=True, timeout=120, cwd=tmp_path)
    assert proc.returncode == 0 and proc.stdout.count("syntax ok") == 2, proc.stdout + proc.stderr


# =============================================================================================== server side
async def test_signed_in_page_ships_the_bundle_with_the_csp_nonce(client, db):
    user = await make_customer(db)
    await login_as(client, user)
    r = await client.get("/account")
    assert r.status_code == 200
    html = r.text
    tags = SCRIPT_TAG.findall(html)
    assert len(tags) == 1, "exactly one bundle <script> tag"
    nonce, src = tags[0]
    assert f"'nonce-{nonce}'" in r.headers["content-security-policy"]
    assert 'data-ws="on"' in html  # signed in: the page opens /ws?page=<IP.pageId>
    assert re.search(r'<meta name="csrf-token" content="[^"]+">', html)  # IP.ai.stream sends it as X-CSRF-Token
    assert 'data-component="island"' in html  # queue and task notes go through the island
    asset = await client.get(src)
    assert asset.status_code == 200
    missing = [m for m in BUNDLE_MARKERS if m not in asset.text]
    assert not missing, missing


async def test_guest_pages_ship_the_bundle_without_a_socket(client):
    r = await client.get("/")
    assert r.status_code == 200
    assert 'data-ws="off"' in r.text
    assert len(SCRIPT_TAG.findall(r.text)) == 1


async def test_routes_accept_the_page_id_header_and_form_field(client, db):
    user = await make_customer(db, country_code="IN", city="Bengaluru", phone="+91 98765 40000")
    await login_as(client, user)
    r = await client.get("/api/notifications?limit=1", headers={"X-Page-Id": PAGE, "Accept": "application/json"})
    assert r.status_code == 200
    r = await client.get("/api/notifications?limit=1", headers={"X-Page-Id": "not a valid id!", "Accept": "application/json"})
    assert r.status_code == 200  # an unusable id is ignored, never an error
    r = await api(client, "POST", "/api/notifications/read-all", json={}, headers={"X-Page-Id": PAGE})
    assert r.status_code == 200 and r.json()["ok"] is True
    # a real form with validation: the extra page_id field the client adds to every POST form is ignored
    form = {"full_name": "Meera Iyer", "email": user.email, "username": user.username or "", "phone": user.phone or "",
            "phone_country": "IN", "date_of_birth": user.date_of_birth.isoformat(), "country": "IN", "city": "Mysuru",
            "timezone": user.timezone, "page_id": PAGE}
    r = await post_form(client, "/account", form)
    assert r.status_code == 303 and r.headers["location"] == "/account"
    await db.refresh(user)
    assert user.full_name == "Meera Iyer" and user.city == "Mysuru"
    r = await post_form(client, "/account/notifications/read-all", {"page_id": PAGE})
    assert r.status_code == 303
    r = await post_form(client, "/auth/logout", {"page_id": PAGE})
    assert r.status_code == 303
    r = await client.get("/account")
    assert r.status_code == 303 and r.headers["location"].startswith("/auth/login")


# =============================================================================================== JavaScriptCore scripts
CHECK_SYNTAX = r"""
for (var i = 0; i < arguments.length; i++) {
  try { new Function(readFile(arguments[i])); print("syntax ok " + arguments[i]); }
  catch (e) { print("SYNTAX ERROR " + arguments[i] + ": " + e); }
}
"""

HARNESS = r"""/* Behaviour tests for 00-core.js + 01-ai.js in JavaScriptCore (Safari's engine). jsc has no DOM, fetch, WebSocket
 * or controllable timers, so this file provides a tiny browser: virtual clock + timers, event targets, a document,
 * fetch with scripted responses and streaming bodies, a WebSocket recorder, AbortController, URL, TextDecoder.
 * Usage: jsc ai_harness.js -- <core.js> <ai.js>     (prints PASS/FAIL lines and a SUMMARY line)
 */
var window = globalThis;
var __errors = [];
var console = {
  error: function () { __errors.push(Array.prototype.map.call(arguments, String).join(" ")); },
  log: function () {},
  warn: function () {},
};

/* ---------------------------------------------------------------- virtual clock + timers */
var __now = 1793000000000; // whole second: Date.parse(toUTCString()) round-trips exactly
Date.now = function () { return __now; };
var __timers = [];
var __tid = 0;
globalThis.setTimeout = function (fn, ms) {
  var id = ++__tid;
  __timers.push({ id: id, at: __now + Math.max(0, Number(ms) || 0), fn: fn, every: 0 });
  return id;
};
globalThis.setInterval = function (fn, ms) {
  var id = ++__tid;
  __timers.push({ id: id, at: __now + ms, fn: fn, every: ms });
  return id;
};
globalThis.clearTimeout = globalThis.clearInterval = function (id) {
  if (id == null) return;
  __timers = __timers.filter(function (t) { return t.id !== id; });
};
function advance(ms) {
  var end = __now + ms;
  for (var guard = 0; guard < 200000; guard++) {
    drainMicrotasks();
    var due = null;
    for (var i = 0; i < __timers.length; i++) {
      var t = __timers[i];
      if (t.at <= end && (!due || t.at < due.at || (t.at === due.at && t.id < due.id))) due = t;
    }
    if (!due) break;
    __now = Math.max(__now, due.at);
    if (due.every) due.at += due.every;
    else __timers.splice(__timers.indexOf(due), 1);
    due.fn();
  }
  __now = end;
  drainMicrotasks();
}
function flush() { advance(0); }

/* ---------------------------------------------------------------- events, elements, document */
function Target() { this._l = {}; }
Target.prototype.addEventListener = function (type, fn, opts) {
  var capture = opts === true || !!(opts && opts.capture);
  (this._l[type] = this._l[type] || []).push({ fn: fn, capture: capture });
};
Target.prototype.removeEventListener = function (type, fn, opts) {
  var capture = opts === true || !!(opts && opts.capture);
  this._l[type] = (this._l[type] || []).filter(function (l) { return l.fn !== fn || l.capture !== capture; });
};
Target.prototype.fire = function (type, evt, phase) {
  evt = evt || {};
  evt.type = type;
  var self = this;
  (this._l[type] || []).slice().forEach(function (l) {
    if (phase === "capture" && !l.capture) return;
    if (phase === "bubble" && l.capture) return;
    l.fn.call(self, evt);
  });
  return evt;
};
window._l = {};
window.addEventListener = Target.prototype.addEventListener;
window.removeEventListener = Target.prototype.removeEventListener;
window.fire = Target.prototype.fire;

function El(tag, attrs) {
  Target.call(this);
  this.tagName = String(tag).toUpperCase();
  this.attrs = {};
  this.children = [];
  this.parentNode = null;
  this.value = "";
  this.style = {};
  var self = this;
  Object.keys(attrs || {}).forEach(function (k) { self.attrs[k] = String(attrs[k]); });
}
El.prototype = Object.create(Target.prototype);
El.prototype.getAttribute = function (k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; };
El.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); };
El.prototype.hasAttribute = function (k) { return Object.prototype.hasOwnProperty.call(this.attrs, k); };
El.prototype.removeAttribute = function (k) { delete this.attrs[k]; };
El.prototype.appendChild = function (c) { this.children.push(c); c.parentNode = this; return c; };
El.prototype.descendants = function () {
  var out = [];
  (function walk(n) { n.children.forEach(function (c) { out.push(c); if (c.children) walk(c); }); })(this);
  return out;
};
El.prototype.querySelector = function (sel) {
  var m = /^input\[name="([^"]+)"\]$/.exec(sel);
  if (!m) return null;
  return this.descendants().filter(function (c) { return c.tagName === "INPUT" && c.getAttribute("name") === m[1]; })[0] || null;
};
El.prototype.querySelectorAll = function () { return []; };

var document = new Target();
document.body = new El("body", { "data-ws": "on", "data-user-id": "7" });
document.querySelector = function (sel) {
  if (sel === 'meta[name="csrf-token"]') return { getAttribute: function () { return "csrf-abc"; } };
  return null;
};
document.querySelectorAll = function () { return []; };
document.createElement = function (tag) { return new El(tag); };
document.createTextNode = function (t) { return { nodeType: 3, textContent: String(t) }; };

/** Dispatch like a browser: document capture listeners, then the form's own, then document bubble listeners. */
function submit(form, submitter) {
  var evt = { target: form, submitter: submitter || null, defaultPrevented: false };
  evt.preventDefault = function () { evt.defaultPrevented = true; };
  document.fire("submit", evt, "capture");
  form.fire("submit", evt);
  document.fire("submit", evt, "bubble");
  return evt;
}

/* plain-HTTP LAN access (a phone on the gym Wi-Fi): no crypto.randomUUID, getRandomValues only */
var location = { protocol: "http:", host: "192.168.1.20:8000", origin: "http://192.168.1.20:8000",
                 href: "http://192.168.1.20:8000/me", pathname: "/me" };
var navigator = { onLine: true };
var crypto = { getRandomValues: function (a) { for (var i = 0; i < a.length; i++) a[i] = (i * 53 + 7) & 255; return a; } };

function URL(url, base) {
  url = String(url);
  var m = /^([a-zA-Z][a-zA-Z0-9+.-]*:)\/\/([^\/?#]*)([^?#]*)(\?[^#]*)?(#.*)?$/.exec(url);
  if (!m) {
    if (base === undefined) throw new TypeError("Invalid URL: " + url);
    var b = new URL(base);
    if (url.slice(0, 2) === "//") return new URL(b.protocol + url);
    var path = url.charAt(0) === "/" ? url : b.pathname.replace(/[^\/]*$/, "") + url;
    return new URL(b.protocol + "//" + b.host + path);
  }
  this.protocol = m[1].toLowerCase();
  this.host = m[2];
  this.pathname = m[3] || "/";
  this.search = m[4] || "";
  this.hash = m[5] || "";
  this.origin = this.protocol + "//" + this.host;
  this.href = this.origin + this.pathname + this.search + this.hash;
}

function domAbort() { var e = new Error("The operation was aborted."); e.name = "AbortError"; return e; }
function AbortSignal() { Target.call(this); this.aborted = false; this.reason = undefined; }
AbortSignal.prototype = Object.create(Target.prototype);
function AbortController() { this.signal = new AbortSignal(); }
AbortController.prototype.abort = function (reason) {
  if (this.signal.aborted) return;
  this.signal.aborted = true;
  this.signal.reason = reason === undefined ? domAbort() : reason;
  this.signal.fire("abort", {});
};
function FormData(form) { this.form = form; }
function TextDecoder() {}
TextDecoder.prototype.decode = function (u8) {
  if (!u8) return "";
  var s = "";
  for (var i = 0; i < u8.length; i++) s += String.fromCharCode(u8[i]);
  return s;
};
function bytes(str) {
  var a = new Uint8Array(str.length);
  for (var i = 0; i < str.length; i++) a[i] = str.charCodeAt(i) & 255;
  return a;
}

/* ---------------------------------------------------------------- fetch */
function Res(status, headers, body) {
  this.status = status;
  this.ok = status >= 200 && status < 300;
  var h = {};
  Object.keys(headers || {}).forEach(function (k) { h[k.toLowerCase()] = String(headers[k]); });
  this.headers = { get: function (k) { k = String(k).toLowerCase(); return Object.prototype.hasOwnProperty.call(h, k) ? h[k] : null; } };
  this._body = body;
  this.body = body && typeof body === "object" && body.getReader ? body : null;
}
Res.prototype.json = function () {
  var b = this._body;
  return new Promise(function (ok, fail) { try { ok(JSON.parse(b)); } catch (e) { fail(e); } });
};
Res.prototype.text = function () { return Promise.resolve(typeof this._body === "string" ? this._body : ""); };
function json(status, obj, headers) {
  var h = { "content-type": "application/json" };
  Object.keys(headers || {}).forEach(function (k) { h[k] = headers[k]; });
  return new Res(status, h, typeof obj === "string" ? obj : JSON.stringify(obj));
}
function Body() { this.queue = []; this.waiting = null; this.failed = null; this.cancelled = false; }
Body.prototype._offer = function (item) {
  if (this.waiting) { var w = this.waiting; this.waiting = null; w.ok(item); } else this.queue.push(item);
};
Body.prototype.push = function (str) { this._offer({ done: false, value: bytes(str) }); };
Body.prototype.end = function () { this._offer({ done: true, value: undefined }); };
Body.prototype.fail = function (err) {
  this.failed = err;
  if (this.waiting) { var w = this.waiting; this.waiting = null; w.fail(err); }
};
Body.prototype.getReader = function () {
  var self = this;
  return {
    read: function () {
      return new Promise(function (ok, fail) {
        if (self.failed) return fail(self.failed);
        if (self.queue.length) return ok(self.queue.shift());
        self.waiting = { ok: ok, fail: fail };
      });
    },
    cancel: function () { self.cancelled = true; return Promise.resolve(); },
    releaseLock: function () {},
  };
};
function sse(body) { return new Res(200, { "content-type": "text/event-stream; charset=utf-8" }, body); }
function deferred() {
  var d = {};
  d.promise = new Promise(function (ok, fail) { d.resolve = ok; d.reject = fail; });
  return d;
}
function hang() { return deferred().promise; }

var __fetches = [];
var __route = null;
function fetch(url, init) {
  var call = { url: String(url), init: init || {}, aborted: false, res: null };
  __fetches.push(call);
  return new Promise(function (ok, fail) {
    var signal = call.init.signal;
    if (signal && signal.aborted) { fail(domAbort()); return; }
    if (signal) {
      signal.addEventListener("abort", function () {
        call.aborted = true;
        fail(domAbort());
        if (call.res && call.res.body && call.res.body.fail) call.res.body.fail(domAbort());
      });
    }
    Promise.resolve(__route(call.url, call.init, call)).then(function (res) { call.res = res; ok(res); }, fail);
  });
}
function lastFetch() { return __fetches[__fetches.length - 1]; }

/* ---------------------------------------------------------------- WebSocket */
var __sockets = [];
function WebSocket(url) { this.url = url; this.readyState = 0; this.sent = []; this.closes = []; __sockets.push(this); }
WebSocket.prototype.send = function (d) { if (this.readyState !== 1) throw new Error("not open"); this.sent.push(String(d)); };
WebSocket.prototype.close = function (code, reason) { this.closes.push({ code: code, reason: reason }); if (this.readyState < 2) this.readyState = 2; };
function wsOpen(s) { s.readyState = 1; if (s.onopen) s.onopen({}); }
function wsClose(s, code) { s.readyState = 3; if (s.onclose) s.onclose({ code: code }); }
function wsMsg(s, obj) { if (s.onmessage) s.onmessage({ data: JSON.stringify(obj) }); }
function lastSocket() { return __sockets[__sockets.length - 1]; }

/* ---------------------------------------------------------------- island stub (05-island.js boots later) */
var isl = { notes: [], acts: [], clears: [] };
function installIsland() {
  IP.island = {
    notify: function (n) { isl.notes.push(n); },
    activity: function (k, a) { isl.acts.push({ key: k, a: a }); },
    clearActivity: function (k) { isl.clears.push(k); },
  };
}
function resetIsland() { isl.notes = []; isl.acts = []; isl.clears = []; }
function lastAct() { return isl.acts[isl.acts.length - 1]; }

/* ---------------------------------------------------------------- mini test runner */
var __tests = [];
function test(name, fn) { __tests.push({ name: name, fn: fn }); }
function show(v) {
  try { return JSON.stringify(v); } catch (e) { return String(v); } // DOM stubs have parent cycles
}
function eq(a, b, msg) {
  if (a !== b) throw new Error((msg ? msg + ": " : "") + "expected " + show(b) + ", got " + show(a));
}
function ok(v, msg) { if (!v) throw new Error(msg || "expected a truthy value"); }
function capture(p) {
  var box = { settled: false, value: undefined, error: undefined };
  p.then(function (v) { box.settled = true; box.value = v; }, function (e) { box.settled = true; box.error = e; });
  return box;
}
function okJson() { return json(200, { ok: true }); }

var CORE = arguments[0];
var AI = arguments[1];
load(CORE);
if (AI) load(AI); // a single minified bundle holds both
var IP = window.IP;
var PAGE_ID_RE = /^[A-Za-z0-9_-]{8,64}$/; // app/ai/owner.py

/* ================================================================ tests (run in order; they share one page) */
test("page id: 32 hex chars from getRandomValues on plain-HTTP LAN (no randomUUID)", function () {
  ok(/^[0-9a-f]{32}$/.test(IP.pageId), IP.pageId);
  ok(PAGE_ID_RE.test(IP.pageId), "server regex");
});

test("IP.ai exposes the documented API", function () {
  ["request", "stream", "describeError", "cooldownLeft", "noteRefusal", "onCooldown", "abortAll", "pending",
   "formatWait"].forEach(function (n) {
    eq(typeof IP.ai[n], "function", n);
  });
  eq(IP.ai.pending(), 0);
  eq(IP.ai.cooldownLeft("assistant"), 0);
  eq(typeof IP.parseRetryAfter, "function");
  eq(typeof IP.apiError, "function");
});

test("DOMContentLoaded opens one socket at /ws?page=<pageId>", function () {
  document.fire("DOMContentLoaded", {});
  installIsland();
  eq(__sockets.length, 1);
  eq(lastSocket().url, "ws://192.168.1.20:8000/ws?page=" + encodeURIComponent(IP.pageId));
});

test("the socket pings every 25 s once open", function () {
  var s = lastSocket();
  wsOpen(s);
  eq(IP.ws.connected, true);
  advance(24999);
  eq(s.sent.length, 0);
  advance(1);
  eq(s.sent[0], '{"type":"ping"}');
  advance(25000);
  eq(s.sent.length, 2);
});

test("an abnormal close reconnects (1-1.5 s) with the same page id", function () {
  var s = lastSocket();
  var closes = [];
  IP.on("ws.close", function (e) { closes.push(e); });
  wsClose(s, 1006);
  eq(IP.ws.connected, false);
  eq(closes[0].code, 1006);
  eq(closes[0].intentional, false);
  advance(999);
  eq(__sockets.length, 1, "too early");
  advance(600);
  eq(__sockets.length, 2);
  eq(lastSocket().url, s.url);
  var pings = s.sent.length;
  advance(60000);
  eq(s.sent.length, pings, "the closed socket's ping stopped");
  wsOpen(lastSocket());
});

test("4429 (flood) backs off for at least 30 s", function () {
  var s = lastSocket();
  wsClose(s, 4429);
  advance(29999);
  eq(__sockets.length, 2, "reconnected before 30 s");
  advance(5100);
  eq(__sockets.length, 3);
  eq(lastSocket().url, s.url);
  wsOpen(lastSocket());
});

test("a second 4429 doubles the back-off (>= 60 s)", function () {
  wsClose(lastSocket(), 4429);
  advance(59999);
  eq(__sockets.length, 3, "reconnected before 60 s");
  advance(5100);
  eq(__sockets.length, 4);
  wsOpen(lastSocket());
});

test("4401 stops reconnecting; IP.ws.send keeps at most 20 queued messages", function () {
  wsClose(lastSocket(), 4401);
  advance(120000);
  eq(__sockets.length, 4, "reconnected after 4401");
  for (var i = 0; i < 30; i++) IP.ws.send({ type: "chat.typing", n: i });
  eq(IP.ws.queue.length, 20);
  eq(IP.ws.queue[19].n, 29);
  eq(IP.ws.queue[0].n, 10);
});

test("pageshow (persisted) after pagehide: new page id (randomUUID when available), reconnect, flush queue", function () {
  var old = IP.pageId;
  var restored = [];
  IP.on("page.restored", function (e) { restored.push(e); });
  window.fire("pagehide", { persisted: true });
  crypto.randomUUID = function () { return "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed"; };
  window.fire("pageshow", { persisted: true });
  delete crypto.randomUUID;
  eq(IP.pageId, "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed");
  ok(IP.pageId !== old);
  ok(PAGE_ID_RE.test(IP.pageId));
  eq(__sockets.length, 5);
  eq(lastSocket().url, "ws://192.168.1.20:8000/ws?page=1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed");
  eq(restored.length, 1);
  eq(restored[0].previous, old);
  wsOpen(lastSocket());
  eq(lastSocket().sent.length, 20, "queued messages flushed on open");
  eq(IP.ws.queue.length, 0);
});

test("pagehide closes the socket with 4001 'page-hide' and never reconnects", function () {
  var s = lastSocket();
  var closes = [];
  IP.on("ws.close", function (e) { closes.push(e); });
  window.fire("pagehide", { persisted: false });
  eq(s.closes.length, 1);
  eq(s.closes[0].code, 4001);
  eq(s.closes[0].reason, "page-hide");
  eq(IP.ws.connected, false);
  var retries = IP.ws.retries;
  var timers = __timers.length;
  wsClose(s, 4001);
  eq(closes[closes.length - 1].intentional, true);
  eq(IP.ws.retries, retries, "no reconnect attempt counted");
  ok(__timers.length < timers, "no reconnect timer scheduled (and the ping stopped)");
  advance(600000);
  eq(__sockets.length, 5, "reconnected after an intentional close");
  IP.ws.send({ type: "x" });
  eq(IP.ws.queue.length, 1, "send queues instead of opening a socket");
  IP.ws.queue.length = 0;
});

test("a late close of the replaced socket changes nothing after a restore", function () {
  var before = lastSocket();
  window.fire("pageshow", { persisted: true });
  eq(__sockets.length, 6);
  var fresh = lastSocket();
  ok(fresh !== before);
  wsOpen(fresh);
  eq(IP.ws.connected, true);
  var closesBefore = before.closes.length;
  wsOpen(before); // an engine that still opens the replaced socket: it is closed at once, never used
  eq(before.closes.length, closesBefore + 1);
  eq(before.closes[before.closes.length - 1].code, 4001);
  eq(IP.ws.socket, fresh);
  wsClose(before, 1006); // the old socket's close event, delivered late
  eq(IP.ws.connected, true);
  advance(120000);
  eq(__sockets.length, 6);
  eq(IP.ws.socket, fresh);
});

test("pageshow without persisted (a normal load) changes nothing", function () {
  var id = IP.pageId;
  window.fire("pageshow", { persisted: false });
  eq(IP.pageId, id);
  eq(__sockets.length, 6);
});

test("visibilitychange never cancels requests or closes the socket", function () {
  __route = function () { return hang(); };
  var box = capture(IP.ai.request("/api/live/sessions/1/message", { json: { text: "hi" }, feature: "live_coach" }));
  flush();
  eq(IP.ai.pending(), 1);
  document.fire("visibilitychange", {});
  window.fire("visibilitychange", {});
  flush();
  eq(IP.ai.pending(), 1);
  eq(box.settled, false);
  eq(lastSocket().closes.length, 0);
  IP.ai.abortAll("test-cleanup");
  flush();
  eq(box.error.reason, "test-cleanup");
});

test("IP.api: GET carries X-Page-Id but no CSRF; POST carries both; same-origin credentials", function () {
  __route = function () { return okJson(); };
  var a = capture(IP.api("/api/notifications?limit=1"));
  flush();
  var call = lastFetch();
  eq(a.value.ok, true);
  eq(call.init.method, "GET");
  eq(call.init.credentials, "same-origin");
  eq(call.init.headers["X-Page-Id"], IP.pageId);
  eq(call.init.headers["X-CSRF-Token"], undefined);
  capture(IP.api("/api/notifications/read-all", { method: "post" }));
  flush();
  call = lastFetch();
  eq(call.init.method, "POST");
  eq(call.init.headers["X-CSRF-Token"], "csrf-abc");
  eq(call.init.headers["X-Page-Id"], IP.pageId);
  capture(IP.api("http://192.168.1.20:8000/api/x", { method: "POST", json: { a: 1 } }));
  flush();
  call = lastFetch();
  eq(call.init.headers["X-Page-Id"], IP.pageId, "absolute same-origin URL");
  eq(call.init.headers["Content-Type"], "application/json");
  eq(call.init.body, '{"a":1}');
});

test("IP.api: keepalive only when asked", function () {
  __route = function () { return okJson(); };
  capture(IP.api("/api/live/sessions/1/event", { method: "POST", json: { type: "paused" }, keepalive: true }));
  flush();
  eq(lastFetch().init.keepalive, true);
  capture(IP.api("/api/live/sessions/1/event", { method: "POST", json: { type: "paused" } }));
  flush();
  eq(lastFetch().init.keepalive, undefined);
});

test("IP.api: the page id never goes to another origin", function () {
  __route = function () { return okJson(); };
  capture(IP.api("https://tiles.example.com/x.json"));
  flush();
  eq(lastFetch().init.headers["X-Page-Id"], undefined);
  capture(IP.api("//cdn.example.com/x.json"));
  flush();
  eq(lastFetch().init.headers["X-Page-Id"], undefined);
});

test("IP.api error: status, data, code, message and the larger of Retry-After / retry_after", function () {
  __route = function () {
    return json(429, { error: "AI limit reached", detail: "You have used your AI questions.", code: "quota", retry_after: 90 }, { "Retry-After": "120" });
  };
  var box = capture(IP.api("/api/assistant/chat", { method: "POST", json: {} }));
  flush();
  var err = box.error;
  ok(err instanceof Error);
  eq(err.status, 429);
  eq(err.code, "quota");
  eq(err.retryAfter, 120);
  eq(err.message, "You have used your AI questions.");
  eq(err.data.retry_after, 90);
});

test("IP.api error: Retry-After as an HTTP date", function () {
  __route = function () { return json(503, { error: "AI is busy" }, { "Retry-After": new Date(__now + 120000).toUTCString() }); };
  var box = capture(IP.api("/api/x"));
  flush();
  eq(box.error.retryAfter, 120);
  eq(box.error.message, "AI is busy");
});

test("IP.api error: retry_after from the body only; none -> null; code null when absent", function () {
  __route = function () { return json(503, { detail: "busy", code: "busy", retry_after: 15 }); };
  var box = capture(IP.api("/api/x"));
  flush();
  eq(box.error.retryAfter, 15);
  eq(box.error.code, "busy");
  __route = function () { return json(500, { error: "Server error", detail: "Unexpected error" }); };
  box = capture(IP.api("/api/x"));
  flush();
  eq(box.error.retryAfter, null);
  eq(box.error.code, null);
  eq(box.error.status, 500);
  __route = function () { return new Res(502, { "content-type": "text/html" }, "<h1>Bad gateway</h1>"); };
  box = capture(IP.api("/api/x"));
  flush();
  eq(box.error.status, 502);
  eq(box.error.message, "Request failed (502)");
  eq(box.error.data, "<h1>Bad gateway</h1>");
});

test("IP.api error: a broken JSON error body still reports status and Retry-After", function () {
  __route = function () { return json(503, "{not json", { "Retry-After": "7" }); };
  var box = capture(IP.api("/api/x"));
  flush();
  eq(box.error.status, 503);
  eq(box.error.retryAfter, 7);
  eq(box.error.data, null);
  __route = function () { return json(200, "{not json"); };
  box = capture(IP.api("/api/x"));
  flush();
  eq(box.error.name, "SyntaxError", "a broken success body still throws like before");
});

test("IP.parseRetryAfter edge cases", function () {
  eq(IP.parseRetryAfter("0"), null);
  eq(IP.parseRetryAfter(0), null);
  eq(IP.parseRetryAfter("-5"), null);
  eq(IP.parseRetryAfter("soon"), null);
  eq(IP.parseRetryAfter(null), null);
  eq(IP.parseRetryAfter(""), null);
  eq(IP.parseRetryAfter(true), null);
  eq(IP.parseRetryAfter(2.2), 3);
  eq(IP.parseRetryAfter(" 30 "), 30);
  eq(IP.parseRetryAfter("1.5"), 2);
  eq(IP.parseRetryAfter(99999999), 604800);
  eq(IP.parseRetryAfter(new Date(__now - 5000).toUTCString()), null, "a date in the past");
});

test("POST forms get a hidden page_id before their own submit handlers run", function () {
  var form = new El("form", { method: "post", action: "/me/coach/generate" });
  var seen = null;
  form.addEventListener("submit", function () {
    var i = form.querySelector('input[name="page_id"]');
    seen = i && i.value;
  });
  submit(form);
  eq(seen, IP.pageId);
  var input = form.querySelector('input[name="page_id"]');
  eq(input.getAttribute("type"), "hidden");
  submit(form);
  eq(form.descendants().length, 1, "only one field, however often it is submitted");
  var upper = new El("form", { method: "POST" }); // no action: posts to this page
  submit(upper);
  eq(upper.querySelector('input[name="page_id"]').value, IP.pageId);
  var xhtml = new El("form", { method: "post" });
  xhtml.tagName = "form"; // XHTML documents report lower-case names
  submit(xhtml);
  eq(xhtml.querySelector('input[name="page_id"]').value, IP.pageId);
  var notForm = new El("div", { method: "post" });
  submit(notForm);
  eq(notForm.querySelector('input[name="page_id"]'), null);
});

test("GET forms, other-origin forms and formmethod=get submitters get no page_id", function () {
  var get = new El("form", { action: "/search" });
  submit(get);
  eq(get.querySelector('input[name="page_id"]'), null);
  var away = new El("form", { method: "post", action: "https://pay.example.com/checkout" });
  submit(away);
  eq(away.querySelector('input[name="page_id"]'), null);
  var mixed = new El("form", { method: "post", action: "/x" });
  submit(mixed, new El("button", { formmethod: "get" }));
  eq(mixed.querySelector('input[name="page_id"]'), null);
  var redirect = new El("form", { method: "get", action: "/x" });
  submit(redirect, new El("button", { formmethod: "post", formaction: "https://evil.example/x" }));
  eq(redirect.querySelector('input[name="page_id"]'), null);
  var promoted = new El("form", { method: "get", action: "/x" });
  submit(promoted, new El("button", { formmethod: "post" }));
  eq(promoted.querySelector('input[name="page_id"]').value, IP.pageId);
});

test("the stamped field follows a new page id; a template-provided value is kept", function () {
  var form = new El("form", { method: "post", action: "/x" });
  submit(form);
  var saved = IP.pageId;
  IP.pageId = "abcdefgh12345678";
  submit(form);
  eq(form.querySelector('input[name="page_id"]').value, "abcdefgh12345678");
  var own = new El("form", { method: "post", action: "/x" });
  var given = own.appendChild(new El("input", { type: "hidden", name: "page_id" }));
  given.value = "server-given-1234";
  submit(own);
  eq(given.value, "server-given-1234");
  var empty = new El("form", { method: "post", action: "/x" });
  var slot = empty.appendChild(new El("input", { type: "hidden", name: "page_id" }));
  submit(empty);
  eq(slot.value, "abcdefgh12345678");
  IP.pageId = saved;
});

test("request: goes through IP.api (X-Page-Id, CSRF, JSON) and counts as pending", function () {
  var d = deferred();
  __route = function () { return d.promise; };
  var box = capture(IP.ai.request("/api/assistant/chat", { json: { message: "hi" }, feature: "assistant" }));
  flush();
  eq(IP.ai.pending(), 1);
  var call = lastFetch();
  eq(call.init.method, "POST");
  eq(call.init.headers["X-Page-Id"], IP.pageId);
  eq(call.init.headers["X-CSRF-Token"], "csrf-abc");
  eq(call.init.body, '{"message":"hi"}');
  ok(call.init.signal && call.init.signal.aborted === false, "has its own signal");
  d.resolve(json(200, { reply: "Done" }));
  flush();
  eq(box.value.reply, "Done");
  eq(IP.ai.pending(), 0);
  __route = function () { return okJson(); };
  capture(IP.ai.request("/api/x", { method: "GET", feature: "x" }));
  flush();
  eq(lastFetch().init.method, "GET");
});

test("429 quota: per-feature cooldown, ai.cooldown event, local refusal without a request, never retried", function () {
  var events = [];
  var ends = [];
  var seen = [];
  IP.on("ai.cooldown", function (e) { events.push(e); });
  IP.on("ai.cooldown.end", function (e) { ends.push(e); });
  var unsubscribe = IP.ai.onCooldown(function (info) { seen.push(info); });
  __route = function () {
    return json(429, { error: "AI limit reached", detail: "Limit", code: "quota", retry_after: 120, ai_available: true }, { "Retry-After": "120" });
  };
  var n = __fetches.length;
  var box = capture(IP.ai.request("/api/assistant/chat", { json: { message: "a" }, feature: "assistant" }));
  flush();
  advance(5000);
  eq(__fetches.length, n + 1, "exactly one request (no automatic retry)");
  eq(box.error.status, 429);
  eq(box.error.code, "quota");
  eq(box.error.retryAfter, 120);
  eq(box.error.feature, "assistant");
  eq(events.length, 1);
  eq(events[0].feature, "assistant");
  eq(events[0].seconds, 120);
  eq(events[0].code, "quota");
  eq(seen.length, 1);
  eq(IP.ai.cooldownLeft("assistant"), 115);
  eq(IP.ai.cooldownLeft("plans"), 0);
  eq(IP.ai.cooldownLeft(), 0, "not a global pause");
  var again = capture(IP.ai.request("/api/assistant/chat", { json: { message: "b" }, feature: "assistant" }));
  flush();
  eq(__fetches.length, n + 1, "refused locally");
  eq(again.error.status, 429);
  eq(again.error.code, "cooldown_local");
  eq(again.error.retryAfter, 115);
  eq(again.error.cooldownCode, "quota");
  eq(again.error.message, "You've reached your AI limit for now. Try again in 2 min.");
  eq(again.error.data.code, "cooldown_local");
  eq(IP.ai.describeError(again.error), again.error.message);
  var streamed = capture(IP.ai.stream("/api/assistant/stream", { json: {}, feature: "assistant" }));
  flush();
  eq(__fetches.length, n + 1, "streams are refused locally too");
  eq(streamed.error.code, "cooldown_local");
  __route = function () { return okJson(); };
  capture(IP.ai.request("/api/plans/x", { json: {}, feature: "plans" }));
  flush();
  eq(__fetches.length, n + 2, "other features still work");
  advance(115100);
  eq(IP.ai.cooldownLeft("assistant"), 0);
  eq(ends.length, 1);
  eq(ends[0].feature, "assistant");
  eq(seen[1].ended, true);
  eq(seen[1].seconds, 0);
  var later = capture(IP.ai.request("/api/assistant/chat", { json: { message: "c" }, feature: "assistant" }));
  flush();
  eq(__fetches.length, n + 3);
  eq(later.value.ok, true);
  unsubscribe();
  __route = function () { return json(429, { code: "quota", retry_after: 5 }); };
  capture(IP.ai.request("/api/assistant/chat", { json: {}, feature: "assistant" }));
  flush();
  eq(seen.length, 2, "unsubscribed listeners are not called");
  advance(6000);
});

test("code 'cooldown' pauses every feature (global), with the account-paused wording", function () {
  __route = function () { return json(429, { error: "AI paused", detail: "Paused", code: "cooldown", retry_after: 900 }, { "Retry-After": "900" }); };
  var box = capture(IP.ai.request("/owner/customers/intake", { json: { text: "x" }, feature: "intake" }));
  flush();
  eq(box.error.code, "cooldown");
  eq(IP.ai.cooldownLeft("shop"), 900);
  eq(IP.ai.cooldownLeft(), 900);
  var n = __fetches.length;
  var other = capture(IP.ai.request("/api/shop/products/1/ask", { json: {}, feature: "shop" }));
  flush();
  eq(__fetches.length, n);
  eq(other.error.code, "cooldown_local");
  eq(other.error.cooldownCode, "cooldown");
  eq(IP.ai.describeError(other.error), "AI is paused for your account after too many requests. Try again in 15 min.");
  advance(900100);
  eq(IP.ai.cooldownLeft("shop"), 0);
  eq(IP.ai.cooldownLeft(), 0);
});

test("refusal statuses: 409/503 (+ header-only) and code-less 429 start cooldowns; no wait or 500 do not", function () {
  function refuse(feature, status, body, headers) {
    __route = function () { return json(status, body, headers); };
    var box = capture(IP.ai.request("/api/" + feature, { json: {}, feature: feature }));
    flush();
    return box.error;
  }
  eq(refuse("dup", 409, { code: "duplicate", retry_after: 20 }).code, "duplicate");
  eq(IP.ai.cooldownLeft("dup"), 0, "a duplicate refuses those words only, never the whole feature");
  eq(refuse("conflict", 409, { detail: "x", retry_after: 20 }).status, 409);
  eq(IP.ai.cooldownLeft("conflict"), 20, "any other 409 with a wait still pauses its feature");
  refuse("spam", 422, { code: "spam", retry_after: 0 });
  eq(IP.ai.cooldownLeft("spam"), 0);
  refuse("busy", 503, { code: "busy" }, { "Retry-After": "15" });
  eq(IP.ai.cooldownLeft("busy"), 15);
  refuse("boom", 500, { detail: "x" }, { "Retry-After": "30" });
  eq(IP.ai.cooldownLeft("boom"), 0);
  refuse("gate", 429, { error: "Slow down", detail: "Too many requests." }, { "Retry-After": "30" });
  eq(IP.ai.cooldownLeft("gate"), 30);
  eq(IP.ai.cooldownLeft("other"), 0, "a code-less 429 is per feature");
  refuse("fourohfour", 404, { detail: "Not here", retry_after: 30 });
  eq(IP.ai.cooldownLeft("fourohfour"), 0);
  advance(31000);
});

test("a duplicate (409) refuses only those words for that feature, for the rest of its window", function () {
  var cooldownsSeen = [];
  var unsubscribe = IP.ai.onCooldown(function (info) { cooldownsSeen.push(info); });
  var DUP = { error: "Already asked", detail: "You just asked that - give it a moment.", code: "duplicate", retry_after: 25 };
  __route = function () { return json(409, DUP, { "Retry-After": "25" }); };
  var first = capture(IP.ai.request("/api/assistant/chat", { json: { message: "show my plan" }, feature: "dupchat" }));
  flush();
  eq(first.error.status, 409);
  eq(first.error.code, "duplicate");
  eq(IP.ai.cooldownLeft("dupchat"), 0, "the feature is not paused");
  eq(cooldownsSeen.length, 0, "no ai.cooldown for a duplicate");
  var n = __fetches.length;
  __route = function () { return okJson(); };
  var other = capture(IP.ai.request("/api/assistant/chat", { json: { message: "book a class for tomorrow" }, feature: "dupchat" }));
  flush();
  eq(__fetches.length, n + 1, "a different message is sent at once");
  eq(other.value.ok, true);
  var again = capture(IP.ai.request("/api/assistant/chat", { json: { message: "  Show my PLAN?! " }, feature: "dupchat" }));
  flush();
  eq(__fetches.length, n + 1, "the same words - normalised like the server's check - are refused locally");
  eq(again.error.status, 409);
  eq(again.error.code, "duplicate");
  eq(again.error.local, true);
  eq(again.error.retryAfter, 25);
  eq(again.error.message, "You just asked that. Try again in 25 s.");
  eq(again.error.data.code, "duplicate");
  eq(again.error.data.detail, again.error.message);
  eq(IP.ai.describeError(again.error), "You just asked that. Try again in 25 s.");
  // the same words for another feature, at another URL, or with another subject are new questions
  capture(IP.ai.request("/api/plans/ask", { json: { question: "show my plan" }, feature: "dupplans" }));
  capture(IP.ai.request("/api/assistant/chat2", { json: { message: "show my plan" }, feature: "dupchat" }));
  capture(IP.ai.request("/api/assistant/chat", { json: { message: "show my plan", gym_slug: "other-gym" }, feature: "dupchat" }));
  capture(IP.ai.request("/api/assistant/chat", { json: { message: "show my plan +" }, feature: "dupchat" }));
  capture(IP.ai.request("/api/assistant/chat?about=7", { json: { message: "show my plan" }, feature: "dupchat" }));
  flush();
  eq(__fetches.length, n + 6, "only the exact words of that feature and URL are held back");
  advance(10000);
  var later = capture(IP.ai.request("/api/assistant/chat#top", { json: { message: "show my plan" }, feature: "dupchat" }));
  flush();
  eq(__fetches.length, n + 6, "still inside the server's window");
  eq(later.error.retryAfter, 15);
  advance(15100);
  var after = capture(IP.ai.request("/api/assistant/chat", { json: { message: "show my plan" }, feature: "dupchat" }));
  flush();
  eq(__fetches.length, n + 7, "asked again once the window is over");
  eq(after.value.ok, true);
  eq(cooldownsSeen.length, 0);
  unsubscribe();
});

test("a duplicate refused inside a stream ('blocked') holds back only that question about that product", function () {
  var body = new Body();
  __route = function () { return sse(body); };
  var box = capture(IP.ai.stream("/api/shop/products/7/ask/stream", { json: { question: "Is it vegan?" }, feature: "dupshop" }));
  flush();
  body.push('data: {"type":"blocked","code":"duplicate","detail":"You just asked that - give it a moment.","retry_after":30}\n\n');
  body.push('data: {"type":"done","blocked":true}\n\n');
  body.end();
  flush();
  eq(box.value.blocked.code, "duplicate");
  eq(IP.ai.cooldownLeft("dupshop"), 0);
  var n = __fetches.length;
  var same = capture(IP.ai.stream("/api/shop/products/7/ask/stream", { json: { question: "is it vegan" }, feature: "dupshop" }));
  flush();
  eq(__fetches.length, n, "the same question about the same product never reaches the network");
  eq(same.error.code, "duplicate");
  eq(same.error.status, 409);
  eq(same.error.retryAfter, 30);
  var bodies = [];
  __route = function () { var b = new Body(); bodies.push(b); return sse(b); };
  var p8 = capture(IP.ai.stream("/api/shop/products/8/ask/stream", { json: { question: "Is it vegan?" }, feature: "dupshop" }));
  var p7 = capture(IP.ai.stream("/api/shop/products/7/ask/stream", { json: { question: "How much protein?" }, feature: "dupshop" }));
  flush();
  eq(__fetches.length, n + 2, "another product, or another question, is asked at once");
  bodies.forEach(function (b) { b.push('data: {"type":"done"}\n\n'); b.end(); });
  flush();
  eq(p8.value.done.type, "done");
  eq(p7.value.done.type, "done");
  advance(31000);
});

test("noteRefusal never pauses a feature for a duplicate; safety requests are never held back", function () {
  eq(IP.ai.noteRefusal("dupcoach", "duplicate", 4), false);
  eq(IP.ai.cooldownLeft("dupcoach"), 0);
  __route = function () { return json(409, { code: "duplicate", detail: "again", retry_after: 20 }); };
  capture(IP.ai.request("/api/live/sessions/1/message", { json: { text: "my knee hurts" }, feature: "dupsafety", safety: true }));
  flush();
  var n = __fetches.length;
  __route = function () { return okJson(); };
  var again = capture(IP.ai.request("/api/live/sessions/1/message", { json: { text: "my knee hurts" }, feature: "dupsafety", safety: true }));
  flush();
  eq(__fetches.length, n + 1);
  eq(again.value.ok, true);
  advance(21000);
});

test("a shorter refusal arriving later never shortens an active cooldown", function () {
  var first = deferred();
  var second = deferred();
  var calls = 0;
  __route = function () { calls++; return calls === 1 ? first.promise : second.promise; };
  var a = capture(IP.ai.request("/api/par", { json: {}, feature: "par" }));
  var b = capture(IP.ai.request("/api/par", { json: {}, feature: "par" }));
  flush();
  eq(calls, 2, "both were sent before any cooldown existed");
  first.resolve(json(429, { code: "quota", retry_after: 120 }));
  flush();
  second.resolve(json(429, { code: "quota", retry_after: 10 }));
  flush();
  eq(a.error.retryAfter, 120);
  eq(b.error.retryAfter, 10);
  eq(IP.ai.cooldownLeft("par"), 120);
  advance(60000);
  eq(IP.ai.cooldownLeft("par"), 60);
  advance(60100);
  eq(IP.ai.cooldownLeft("par"), 0);
});

test("an abort while the response body is still arriving rejects with AbortError", function () {
  var bodyDone = deferred();
  __route = function () {
    var r = new Res(200, { "content-type": "application/json" }, "");
    r.json = function () { return bodyDone.promise; };
    return r;
  };
  var ctrl = new AbortController();
  var box = capture(IP.ai.request("/api/x", { json: {}, feature: "late", signal: ctrl.signal }));
  flush();
  ctrl.abort();
  bodyDone.resolve({ reply: "too late" });
  flush();
  eq(box.error && box.error.name, "AbortError");
  eq(box.error.reason, "caller");
  eq(IP.ai.pending(), 0);
});

test("without a feature the URL path is the cooldown bucket", function () {
  __route = function () { return json(429, { code: "quota", retry_after: 10 }); };
  capture(IP.ai.request("/api/live/sessions/5/message?x=1", { json: {} }));
  flush();
  eq(IP.ai.cooldownLeft("/api/live/sessions/5/message"), 10);
  var n = __fetches.length;
  var box = capture(IP.ai.request("/api/live/sessions/5/message", { json: {} }));
  flush();
  eq(__fetches.length, n);
  eq(box.error.code, "cooldown_local");
  advance(11000);
});

test("noteRefusal: a refusal inside a 200 answer - per-feature cooldown, ai.cooldown event, local refusals", function () {
  var events = [];
  var seen = [];
  IP.on("ai.cooldown", function (e) { if (e.feature === "coach200") events.push(e); });
  IP.on("ai.cooldown.end", function (e) { if (e.feature === "coach200") events.push(e); });
  var off = IP.ai.onCooldown(function (info) { if (info.feature === "coach200") seen.push(info); });
  var n = __fetches.length;
  eq(IP.ai.noteRefusal("coach200", "quota", 90), true);
  eq(__fetches.length, n, "recording it sends nothing");
  eq(events.length, 1);
  eq(events[0].feature, "coach200");
  eq(events[0].seconds, 90);
  eq(events[0].code, "quota");
  eq(seen.length, 1);
  eq(seen[0].seconds, 90);
  eq(IP.ai.cooldownLeft("coach200"), 90);
  eq(IP.ai.cooldownLeft("assistant"), 0, "other features keep working");
  eq(IP.ai.cooldownLeft(), 0, "a quota is not an account pause");
  var again = capture(IP.ai.request("/api/coach200/message", { json: {}, feature: "coach200" }));
  var streamed = capture(IP.ai.stream("/api/coach200/stream", { json: {}, feature: "coach200" }));
  flush();
  eq(__fetches.length, n, "refused locally, without a request");
  eq(again.error.status, 429);
  eq(again.error.code, "cooldown_local");
  eq(again.error.cooldownCode, "quota");
  eq(again.error.retryAfter, 90);
  eq(again.error.message, "You've reached your AI limit for now. Try again in 2 min.");
  eq(streamed.error.code, "cooldown_local");
  var safe = capture(IP.ai.request("/api/coach200/discomfort", { json: {}, feature: "coach200", safety: true }));
  flush();
  eq(__fetches.length, n + 1, "safety flows are never held");
  eq(safe.value.ok, true);
  advance(90100);
  eq(IP.ai.cooldownLeft("coach200"), 0);
  eq(events.length, 2);
  eq(events[1].ended, true, "ai.cooldown.end");
  eq(events[1].seconds, 0);
  eq(seen.length, 2);
  eq(seen[1].ended, true);
  eq(seen[1].code, "quota");
  off();
});

test("noteRefusal: the same wait, event and local refusal as a 429 with that code and retry_after", function () {
  var events = {};
  IP.on("ai.cooldown", function (e) { if (e.feature === "via429" || e.feature === "via200") events[e.feature] = e; });
  __route = function () { return json(429, { code: "too_many", retry_after: 45 }, { "Retry-After": "45" }); };
  capture(IP.ai.request("/api/via429", { json: {}, feature: "via429" }));
  flush();
  eq(IP.ai.noteRefusal("via200", "too_many", 45), true);
  eq(events.via200.seconds, events.via429.seconds);
  eq(events.via200.code, events.via429.code);
  eq(IP.ai.cooldownLeft("via200"), IP.ai.cooldownLeft("via429"));
  var n = __fetches.length;
  var a = capture(IP.ai.request("/api/via429", { json: {}, feature: "via429" }));
  var b = capture(IP.ai.request("/api/via200", { json: {}, feature: "via200" }));
  flush();
  eq(__fetches.length, n);
  ["status", "code", "retryAfter", "cooldownCode", "message"].forEach(function (k) { eq(b.error[k], a.error[k], k); });
  advance(45100);
  eq(IP.ai.cooldownLeft("via200"), 0);
  eq(IP.ai.cooldownLeft("via429"), 0);
});

test("noteRefusal: code 'cooldown' pauses every feature; without a feature only an account pause counts", function () {
  var ends = [];
  var off = IP.ai.onCooldown(function (info) {
    if (info.ended && (info.feature === "live_coach" || info.feature === null)) ends.push(info);
  });
  var events = [];
  IP.on("ai.cooldown", function (e) { events.push(e); });
  eq(IP.ai.noteRefusal("live_coach", "cooldown", 300), true);
  eq(events[events.length - 1].feature, "live_coach");
  eq(events[events.length - 1].code, "cooldown");
  eq(IP.ai.cooldownLeft(), 300);
  eq(IP.ai.cooldownLeft("assistant"), 300);
  eq(IP.ai.cooldownLeft("tts"), 300);
  var n = __fetches.length;
  var other = capture(IP.ai.request("/api/assistant/chat", { json: {}, feature: "assistant" }));
  flush();
  eq(__fetches.length, n, "every other AI widget of the page waits too");
  eq(other.error.cooldownCode, "cooldown");
  eq(IP.ai.describeError(other.error), "AI is paused for your account after too many requests. Try again in 5 min.");
  advance(300100);
  eq(IP.ai.cooldownLeft(), 0);
  eq(ends.length, 2, "the feature's wait and the account pause end together");
  // no feature: only an account pause can be recorded (event feature null, like the end of a global pause)
  var count = events.length;
  eq(IP.ai.noteRefusal(null, "quota", 30), false);
  eq(IP.ai.noteRefusal("", "busy", 30), false);
  eq(IP.ai.noteRefusal(undefined, null, 30), false);
  eq(events.length, count, "nothing recorded, no event");
  eq(IP.ai.cooldownLeft(), 0);
  eq(IP.ai.noteRefusal(null, "cooldown", 60), true);
  eq(events.length, count + 1);
  eq(events[count].feature, null);
  eq(events[count].code, "cooldown");
  eq(events[count].seconds, 60);
  eq(IP.ai.cooldownLeft("anything"), 60);
  advance(60100);
  eq(IP.ai.cooldownLeft(), 0);
  eq(ends.length, 3);
  eq(ends[2].feature, null);
  off();
});

test("noteRefusal: nothing without a wait; waits read like Retry-After; URLs are paths; never shortened", function () {
  var count = 0;
  var off = IP.ai.onCooldown(function () { count++; });
  [0, -5, null, undefined, "", "soon", true, NaN, "0"].forEach(function (s) {
    eq(IP.ai.noteRefusal("waitless", "quota", s), false, show(s));
  });
  eq(count, 0, "no event without a wait");
  eq(IP.ai.cooldownLeft("waitless"), 0);
  eq(IP.ai.noteRefusal("texty", "busy", "30"), true);
  eq(IP.ai.cooldownLeft("texty"), 30);
  eq(IP.ai.noteRefusal("fraction", "busy", 2.2), true);
  eq(IP.ai.cooldownLeft("fraction"), 3);
  eq(IP.ai.noteRefusal("dated", "busy", new Date(__now + 40000).toUTCString()), true);
  eq(IP.ai.cooldownLeft("dated"), 40);
  eq(IP.ai.noteRefusal("/api/live/sessions/9/message?x=1", "quota", 20), true);
  eq(IP.ai.cooldownLeft("/api/live/sessions/9/message"), 20, "a URL's path is the bucket, as in request");
  eq(IP.ai.noteRefusal("texty", "busy", 5), true);
  eq(IP.ai.cooldownLeft("texty"), 30, "a shorter wait never shortens the one running");
  advance(41000);
  eq(IP.ai.cooldownLeft("texty"), 0);
  eq(IP.ai.cooldownLeft("dated"), 0);
  off();
});

test("the caller's signal aborts the request (AbortError, code cancelled, reason caller)", function () {
  __route = function () { return hang(); };
  var ctrl = new AbortController();
  var box = capture(IP.ai.request("/api/x", { json: {}, feature: "x1", signal: ctrl.signal }));
  flush();
  var call = lastFetch();
  ctrl.abort();
  flush();
  ok(call.aborted, "fetch saw the abort");
  eq(box.error.name, "AbortError");
  eq(box.error.code, "cancelled");
  eq(box.error.reason, "caller");
  eq(IP.ai.pending(), 0);
  eq(ctrl.signal._l.abort.length, 0, "listener removed from the caller's signal after settling");
  var n = __fetches.length;
  var pre = new AbortController();
  pre.abort();
  var early = capture(IP.ai.request("/api/x", { json: {}, feature: "x1", signal: pre.signal }));
  flush();
  eq(__fetches.length, n, "an aborted signal sends nothing");
  eq(early.error.name, "AbortError");
  eq(IP.ai.pending(), 0);
});

test("timeout option aborts with code 'timeout'", function () {
  __route = function () { return hang(); };
  var box = capture(IP.ai.request("/api/x", { json: {}, feature: "slow", timeout: 5000 }));
  flush();
  advance(4999);
  eq(box.settled, false);
  advance(1);
  eq(box.error.name, "AbortError");
  eq(box.error.code, "timeout");
  eq(IP.ai.describeError(box.error), "That took too long - please try again.");
  __route = function () { return okJson(); };
  var fast = capture(IP.ai.request("/api/x", { json: {}, feature: "slow", timeout: 5000 }));
  flush();
  eq(fast.value.ok, true);
  var before = __timers.length;
  advance(6000);
  ok(__timers.length <= before, "timeout timer cleared");
});

test("abortAll(reason) aborts everything in flight and returns the count", function () {
  __route = function () { return hang(); };
  var a = capture(IP.ai.request("/api/a", { json: {}, feature: "a" }));
  var b = capture(IP.ai.request("/api/b", { json: {}, feature: "b" }));
  flush();
  eq(IP.ai.pending(), 2);
  eq(IP.ai.abortAll("sheet-closed"), 2);
  flush();
  eq(a.error.reason, "sheet-closed");
  eq(b.error.code, "cancelled");
  eq(IP.ai.pending(), 0);
  eq(IP.ai.abortAll(), 0);
});

test("pagehide aborts requests and streams (reason page-hide) but not keepalive requests", function () {
  var body = new Body();
  var keep = deferred();
  __route = function (url) {
    if (url === "/api/stream") return sse(body);
    if (url === "/api/keep") return keep.promise;
    return hang();
  };
  var r1 = capture(IP.ai.request("/api/one", { json: {}, feature: "one" }));
  var r2 = capture(IP.ai.stream("/api/stream", { json: {}, feature: "two" }));
  var r3 = capture(IP.ai.request("/api/keep", { json: { type: "paused" }, feature: "three", keepalive: true }));
  flush();
  body.push('data: {"type":"chunk","text":"partial"}\n\n');
  flush();
  eq(IP.ai.pending(), 3);
  window.fire("pagehide", { persisted: false });
  flush();
  eq(r1.error.reason, "page-hide");
  eq(r1.error.code, "cancelled");
  eq(r2.error.reason, "page-hide");
  eq(r2.error.name, "AbortError");
  eq(r3.settled, false, "keepalive request survives");
  eq(IP.ai.pending(), 1);
  eq(IP.ai.describeError(r1.error), "Stopped because you left the page.");
  keep.resolve(okJson());
  flush();
  eq(r3.value.ok, true);
  eq(IP.ai.pending(), 0);
  window.fire("pageshow", { persisted: true }); // come back for the remaining tests
  wsOpen(lastSocket());
});

test("safety: true skips local cooldowns and is never aborted by abortAll", function () {
  __route = function () { return json(429, { error: "AI paused", code: "cooldown", retry_after: 300 }); };
  capture(IP.ai.request("/api/assistant/chat", { json: {}, feature: "assistant" }));
  flush();
  eq(IP.ai.cooldownLeft("live_coach"), 300, "the account is paused");
  var d = deferred();
  __route = function () { return d.promise; };
  var n = __fetches.length;
  var box = capture(IP.ai.request("/api/live/sessions/1/discomfort", { json: { part: "knee" }, feature: "live_coach", safety: true }));
  flush();
  eq(__fetches.length, n + 1, "a safety report is sent despite the cooldown");
  eq(IP.ai.pending(), 1);
  eq(IP.ai.abortAll("sheet-closed"), 0);
  flush();
  eq(box.settled, false, "never cut");
  d.resolve(json(200, { ok: true }));
  flush();
  eq(box.value.ok, true);
  var body = new Body();
  __route = function () { return sse(body); };
  var streamed = capture(IP.ai.stream("/api/health/check", { json: {}, feature: "health_metrics", safety: true }));
  flush();
  body.push('data: {"type":"done"}\n\n');
  body.end();
  flush();
  eq(streamed.value.done.type, "done");
  var normal = capture(IP.ai.request("/api/live/sessions/1/message", { json: {}, feature: "live_coach" }));
  flush();
  eq(normal.error.code, "cooldown_local", "everything else still waits");
  advance(300100);
  eq(IP.ai.cooldownLeft("live_coach"), 0);
});

test("stream: headers, POST JSON body, split/CRLF/multi-line events, comments, onEvent order, result", function () {
  var body = new Body();
  __route = function () { return sse(body); };
  var seen = [];
  var box = capture(IP.ai.stream("/api/plans/ask", { json: { question: "hi" }, feature: "plans", onEvent: function (e) { seen.push(e); } }));
  flush();
  var call = lastFetch();
  eq(call.init.method, "POST");
  eq(call.init.credentials, "same-origin");
  eq(call.init.headers.Accept, "text/event-stream");
  eq(call.init.headers["Content-Type"], "application/json");
  eq(call.init.headers["X-CSRF-Token"], "csrf-abc");
  eq(call.init.headers["X-Page-Id"], IP.pageId);
  eq(call.init.body, '{"question":"hi"}');
  eq(IP.ai.pending(), 1);
  body.push('data: {"type":"meta","provider":"Gemma"}\n\n');
  body.push('data: {"type":"chu');
  body.push('nk","text":"Hel"}\n');
  body.push('\ndata: {"type":"chunk","text":"lo"}\r');
  body.push("\n\r\n: keep-alive\n\n");
  body.push("event: note\r"); // a CRLF split across chunks must not end the event early
  body.push('\ndata: {"a":\r\ndata: 1}\r\n\r\n');
  body.push('data:{"type":"done","fallback":false}\n\n');
  body.end();
  flush();
  eq(seen.map(function (e) { return e.type; }).join(","), "meta,chunk,chunk,note,done");
  eq(seen[1].text + seen[2].text, "Hello");
  eq(seen[3].a, 1);
  eq(box.value.done.type, "done");
  eq(box.value.done.fallback, false);
  eq(box.value.blocked, null);
  eq(IP.ai.pending(), 0);
});

test("stream: one JSON object per data: line is tolerated; a final event without a blank line is kept", function () {
  var body = new Body();
  __route = function () { return sse(body); };
  var seen = [];
  var box = capture(IP.ai.stream("/api/plans/ask", { json: {}, feature: "plans", onEvent: function (e) { seen.push(e.type); } }));
  flush();
  body.push('data: {"type":"chunk","text":"a"}\ndata: {"type":"chunk","text":"b"}\n\n');
  body.push('data: {"type":"chunk","ok":tr\ndata: ue}\n\n'); // SSE joins data lines with "\n": not valid JSON, dropped
  body.push('data: {"type":"done"}');
  body.end();
  flush();
  eq(seen.join(","), "chunk,chunk,done");
  eq(box.value.done.type, "done");
});

test("stream: 'queued' events show the island activity; the first real event clears it", function () {
  resetIsland();
  var body = new Body();
  __route = function () { return sse(body); };
  var seen = [];
  var box = capture(IP.ai.stream("/api/shop/products/1/ask/stream", { json: { question: "q" }, feature: "shop", onEvent: function (e) { seen.push(e.type); } }));
  flush();
  body.push('data: {"type":"queued","position":3,"eta":8}\n\n');
  flush();
  eq(lastAct().key, "ai-queue");
  eq(lastAct().a.label, "Waiting for AI");
  eq(lastAct().a.detail, "2 ahead");
  eq(lastAct().a.tone, "info");
  body.push('data: {"type":"queued","position":1,"eta":2}\n\n');
  flush();
  eq(lastAct().a.detail, "You are next");
  eq(isl.clears.length, 0);
  body.push('data: {"type":"start"}\n\ndata: {"type":"delta","text":"x"}\n\n');
  flush();
  eq(isl.clears.join(","), "ai-queue");
  body.push('data: {"type":"done"}\n\n');
  body.end();
  flush();
  eq(seen.join(","), "queued,queued,start,delta,done");
  eq(box.value.done.type, "done");
  eq(isl.clears.length, 1, "cleared once");
});

test("stream: a stream that ends while queued clears the activity", function () {
  resetIsland();
  var body = new Body();
  __route = function () { return sse(body); };
  var box = capture(IP.ai.stream("/api/x/stream", { json: {}, feature: "q2" }));
  flush();
  body.push('data: {"type":"queued","position":2}\n\n');
  flush();
  eq(lastAct().a.detail, "1 ahead");
  body.end();
  flush();
  eq(box.value.done, null);
  eq(isl.clears.join(","), "ai-queue");
});

test("stream: 'blocked' fills result.blocked and starts the cooldown; the next stream never reaches the network", function () {
  var body = new Body();
  __route = function () { return sse(body); };
  var events = [];
  IP.on("ai.cooldown", function (e) { if (e.feature === "shop2") events.push(e); });
  var seen = [];
  var box = capture(IP.ai.stream("/api/shop/products/2/ask/stream", { json: {}, feature: "shop2", onEvent: function (e) { seen.push(e.type); } }));
  flush();
  body.push('data: {"type":"blocked","code":"quota","detail":"Limit","retry_after":60}\n\n');
  body.push('data: {"type":"done","blocked":true}\n\n');
  body.end();
  flush();
  eq(seen.join(","), "blocked,done");
  eq(box.value.blocked.code, "quota");
  eq(box.value.done.blocked, true);
  eq(events.length, 1);
  eq(events[0].seconds, 60);
  eq(IP.ai.cooldownLeft("shop2"), 60);
  var n = __fetches.length;
  var next = capture(IP.ai.stream("/api/shop/products/2/ask/stream", { json: {}, feature: "shop2" }));
  flush();
  eq(__fetches.length, n);
  eq(next.error.code, "cooldown_local");
  advance(61000);
});

test("stream: non-2xx responses throw the same enriched error as IP.api (and start the cooldown)", function () {
  __route = function () { return json(429, { error: "Slow down", detail: "wait", code: "too_many", retry_after: 5 }, { "Retry-After": "9" }); };
  var box = capture(IP.ai.stream("/api/plans/ask", { json: {}, feature: "plans3" }));
  flush();
  eq(box.error.status, 429);
  eq(box.error.code, "too_many");
  eq(box.error.retryAfter, 9);
  eq(box.error.message, "wait");
  eq(IP.ai.cooldownLeft("plans3"), 9);
  __route = function () { return new Res(401, { "content-type": "text/html" }, "<p>sign in</p>"); };
  box = capture(IP.ai.stream("/api/plans/ask", { json: {}, feature: "plans4" }));
  flush();
  eq(box.error.status, 401);
  eq(box.error.data, "<p>sign in</p>");
  eq(IP.ai.describeError(box.error), "Your session has expired - please sign in again.");
  eq(IP.ai.pending(), 0);
  advance(10000);
});

test("stream: a JSON 200 where a stream was expected is a 'bad_stream' error", function () {
  __route = function () { return json(200, { answer: "x" }); };
  var box = capture(IP.ai.stream("/api/plans/ask", { json: {}, feature: "plans5" }));
  flush();
  eq(box.error.code, "bad_stream");
  eq(box.error.status, 200);
  eq(IP.ai.cooldownLeft("plans5"), 0);
});

test("stream: a network failure mid-stream rejects with that error; describeError knows offline", function () {
  var body = new Body();
  __route = function () { return sse(body); };
  var box = capture(IP.ai.stream("/api/plans/ask", { json: {}, feature: "plans6" }));
  flush();
  body.push('data: {"type":"chunk","text":"a"}\n\n');
  flush();
  body.fail(new TypeError("Load failed"));
  flush();
  eq(box.error.name, "TypeError");
  eq(IP.ai.pending(), 0);
  eq(IP.ai.describeError(box.error), "Couldn't reach the server - check your connection and try again.");
  navigator.onLine = false;
  eq(IP.ai.describeError(box.error), "You're offline - check your connection and try again.");
  navigator.onLine = true;
});

test("stream: an onEvent that throws is logged and the stream continues", function () {
  var body = new Body();
  __route = function () { return sse(body); };
  var calls = 0;
  var box = capture(IP.ai.stream("/api/plans/ask", { json: {}, feature: "plans7", onEvent: function () { calls++; throw new Error("widget bug"); } }));
  flush();
  body.push('data: {"type":"chunk","text":"a"}\n\ndata: {"type":"done"}\n\n');
  body.end();
  flush();
  eq(calls, 2);
  eq(box.value.done.type, "done");
  eq(__errors.length, 2);
  __errors.length = 0;
});

test("WS ai.queue: only this page's events count; queued shows, running clears", function () {
  resetIsland();
  var s = lastSocket();
  wsMsg(s, { type: "ai.queue", page_id: "another-tab-123", ticket: "t1", feature: "assistant", state: "queued", position: 2 });
  wsMsg(s, { type: "ai.queue", ticket: "t2", feature: "assistant", state: "queued", position: 2 });
  eq(isl.acts.length, 0);
  wsMsg(s, { type: "ai.queue", page_id: IP.pageId, ticket: "t9", feature: "assistant", state: "queued", position: 4, eta: 6 });
  eq(lastAct().a.detail, "3 ahead");
  wsMsg(s, { type: "ai.queue", page_id: IP.pageId, ticket: "t9", feature: "assistant", state: "queued", position: 1, eta: 1 });
  eq(lastAct().a.detail, "You are next");
  wsMsg(s, { type: "ai.queue", page_id: IP.pageId, ticket: "t9", feature: "assistant", state: "running" });
  eq(isl.clears.join(","), "ai-queue");
});

test("WS ai.queue: two waiting tickets show the best position until both are gone", function () {
  resetIsland();
  var s = lastSocket();
  wsMsg(s, { type: "ai.queue", page_id: IP.pageId, ticket: "a", feature: "tts", state: "queued", position: 5 });
  wsMsg(s, { type: "ai.queue", page_id: IP.pageId, ticket: "b", feature: "live_coach", state: "queued", position: 2 });
  eq(lastAct().a.detail, "1 ahead");
  wsMsg(s, { type: "ai.queue", page_id: IP.pageId, ticket: "b", state: "done" });
  eq(lastAct().a.detail, "4 ahead");
  eq(isl.clears.length, 0);
  wsMsg(s, { type: "ai.queue", page_id: IP.pageId, ticket: "a", state: "cancelled" });
  eq(isl.clears.join(","), "ai-queue");
});

test("WS ai.queue: an entry whose end event was lost expires by itself", function () {
  resetIsland();
  wsMsg(lastSocket(), { type: "ai.queue", page_id: IP.pageId, ticket: "lost", state: "queued", position: 2, eta: 5 });
  eq(lastAct().a.detail, "1 ahead");
  advance(59000);
  eq(isl.clears.length, 0);
  advance(2000);
  eq(isl.clears.join(","), "ai-queue");
});

test("task.update: done/failed notes, repeats ignored, paused -> resumed note, filters", function () {
  resetIsland();
  var s = lastSocket();
  wsMsg(s, { type: "task.update", task_id: 11, kind: "coach_plan", status: "running", progress: 10 });
  eq(isl.notes.length, 0);
  wsMsg(s, { type: "task.update", task_id: 11, kind: "coach_plan", status: "done", message: "Your 4-week plan is ready.", link: "/me/coach" });
  eq(isl.notes.length, 1);
  eq(isl.notes[0].tone, "success");
  eq(isl.notes[0].title, "Your training plan - done");
  eq(isl.notes[0].body, "Your 4-week plan is ready.");
  eq(isl.notes[0].link, "/me/coach");
  wsMsg(s, { type: "task.update", task_id: 11, kind: "coach_plan", status: "done", link: "/me/coach" });
  eq(isl.notes.length, 1, "repeat ignored");
  wsMsg(s, { type: "task.update", task_id: 12, kind: "verify_identity", status: "failed" });
  eq(isl.notes[1].tone, "danger");
  eq(isl.notes[1].title, "ID check - failed");
  wsMsg(s, { type: "task.update", task_id: 13, kind: "health_ai_note", status: "paused" });
  eq(isl.notes.length, 2, "paused alone is silent");
  wsMsg(s, { type: "task.update", task_id: 13, kind: "health_ai_note", status: "queued" });
  eq(isl.notes[2].tone, "info");
  eq(isl.notes[2].body, "Paused while you were away - resuming now");
  wsMsg(s, { type: "task.update", task_id: 14, kind: "coach_plan", status: "queued" });
  eq(isl.notes.length, 3, "a first queued update is silent");
  wsMsg(s, { type: "task.update", task_id: 15, kind: "health_ac_note", status: "running", resumed: true });
  eq(isl.notes[3].body, "Paused while you were away - resuming now");
  wsMsg(s, { type: "task.update", task_id: 16, kind: "coach_plan", status: "done", page_id: "another-tab-123" });
  wsMsg(s, { type: "task.update", task_id: 17, kind: "coach_plan", status: "done", notify: false });
  eq(isl.notes.length, 4, "foreign page / notify:false ignored");
  wsMsg(s, { type: "task.update", task_id: 18, kind: "mystery", status: "done", link: "https://evil.example/x" });
  eq(isl.notes[4].link, null);
  eq(isl.notes[4].title, "Background task - done");
  wsMsg(s, { type: "task.update", task_id: 19, kind: "payroll_explain", status: "done", link: "//evil.example/x" });
  eq(isl.notes[5].link, null);
  wsMsg(s, { type: "task.update", task_id: 20, kind: "payroll_explain", status: "done", page_id: IP.pageId, link: "/owner/payroll" });
  eq(isl.notes[6].link, "/owner/payroll");
  wsMsg(s, { type: "task.update", task_id: 21, kind: "coach_plan", status: "cancelled" });
  eq(isl.notes.length, 7, "cancelled is silent");
});

test("events before the island boots are ignored without errors", function () {
  var saved = IP.island;
  IP.island = undefined;
  wsMsg(lastSocket(), { type: "ai.queue", page_id: IP.pageId, ticket: "early", state: "queued", position: 2 });
  wsMsg(lastSocket(), { type: "task.update", task_id: 99, kind: "coach_plan", status: "done" });
  IP.island = saved;
  wsMsg(lastSocket(), { type: "ai.queue", page_id: IP.pageId, ticket: "early", state: "running" });
});

test("describeError: codes, statuses, waits, aborts, offline", function () {
  var d = IP.ai.describeError;
  eq(d({ code: "quota", status: 429, retryAfter: 120 }), "You've reached your AI limit for now. Try again in 2 min.");
  eq(d({ code: "quota", status: 429 }), "You've reached your AI limit for now. Try again later.");
  eq(d({ code: "cooldown", status: 429, retryAfter: 3600 }), "AI is paused for your account after too many requests. Try again in 60 min.");
  eq(d({ code: "too_many", status: 429 }), "You already have AI requests running - wait for them to finish.");
  eq(d({ code: "duplicate", status: 409, retryAfter: 20 }), "You just asked that. Try again in 20 s.");
  eq(d({ code: "duplicate", status: 409 }), "You just asked that. Wait for the answer or ask something different.");
  eq(d({ code: "spam", status: 422 }), "That message looks like spam, so it wasn't sent. Please rephrase it.");
  eq(d({ code: "busy", status: 503, retryAfter: 30 }), "The AI is very busy right now. Try again in 30 s.");
  eq(d({ code: "busy", status: 503 }), "The AI is very busy right now. Please try again in a moment.");
  eq(d({ code: "disabled", status: 403 }), "AI isn't available for your account.");
  eq(d({ code: "cancelled", status: 503 }), "The request was cancelled.");
  eq(d({ code: "cooldown_local", status: 429, retryAfter: 40 }), "Please wait a moment before asking again. Try again in 40 s.");
  eq(d({ code: "cooldown_local", status: 429, retryAfter: 40, cooldownCode: "busy" }), "The AI is very busy right now. Try again in 40 s.");
  eq(d({ status: 401 }), "Your session has expired - please sign in again.");
  eq(d({ status: 403, data: { detail: "CSRF validation failed" } }), "That request was blocked - reload the page and try again.");
  eq(d({ status: 404, data: { detail: "Product not found." } }), "Product not found.");
  eq(d({ status: 404 }), "That couldn't be found - it may have been removed.");
  eq(d({ status: 413 }), "That's too large to send.");
  eq(d({ status: 422, data: { detail: "Ask a question of 2-500 characters." } }), "Ask a question of 2-500 characters.");
  eq(d({ status: 422, data: { error: "Invalid input", fields: [] } }), "Please check what you typed and try again.");
  eq(d({ status: 429, retryAfter: 30 }), "You're going a bit fast. Try again in 30 s.");
  eq(d({ status: 429 }), "You're going a bit fast. Please wait a moment and try again.");
  eq(d({ status: 503 }), "AI is unavailable right now. Please try again in a moment.");
  eq(d({ status: 503, retryAfter: 90 }), "AI is unavailable right now. Try again in 2 min.");
  eq(d({ status: 500, data: { detail: "Unexpected error - it has been logged." } }), "Something went wrong on our side - please try again.");
  eq(d({ status: 502 }), "Something went wrong on our side - please try again.");
  eq(d({ status: 400, data: { detail: "Pick a date." } }), "Pick a date.");
  eq(d({ status: 400 }), "Something went wrong - please try again.");
  eq(d(new TypeError("Failed to fetch")), "Couldn't reach the server - check your connection and try again.");
  eq(d(domAbort()), "The request was cancelled.");
  eq(d({ name: "AbortError", reason: "page-hide" }), "Stopped because you left the page.");
  eq(d({ name: "AbortError", code: "timeout", reason: "timeout" }), "That took too long - please try again.");
  eq(d(null), "Something went wrong - please try again.");
  eq(d(undefined), "Something went wrong - please try again.");
  eq(d("Already explained"), "Already explained");
  navigator.onLine = false;
  eq(d(new TypeError("Load failed")), "You're offline - check your connection and try again.");
  eq(d({ code: "quota", status: 429, retryAfter: 60 }), "You've reached your AI limit for now. Try again in 1 min.", "codes win over offline");
  navigator.onLine = true;
});

test("formatWait", function () {
  var f = IP.ai.formatWait;
  eq(f(0), "");
  eq(f(null), "");
  eq(f(-3), "");
  eq(f(0.2), "1 s");
  eq(f(59), "59 s");
  eq(f(60), "1 min");
  eq(f(61), "2 min");
  eq(f(5399), "90 min");
  eq(f(5400), "2 h");
  eq(f(86400), "24 h");
});

test("no listener or component logged an error", function () {
  eq(__errors.length, 0, __errors.join(" | "));
});

/* ---------------------------------------------------------------- run */
(function () {
  var passed = 0;
  var failed = 0;
  for (var i = 0; i < __tests.length; i++) {
    var t = __tests[i];
    try {
      __route = function () { return okJson(); };
      t.fn();
      flush();
      passed++;
      print("PASS " + t.name);
    } catch (e) {
      failed++;
      print("FAIL " + t.name + " :: " + (e && e.message) + "\n" + (e && e.stack));
    }
  }
  print("SUMMARY passed=" + passed + " failed=" + failed + " total=" + __tests.length);
})();
"""
