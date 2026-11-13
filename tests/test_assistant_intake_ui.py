"""The assistant sheet (app/static/src/js/52-assistant.js) and the owner's AI intake (the ``intake-ai`` component of
app/static/src/js/60-chat.js) on top of the shared AI layer (00-core.js + 01-ai.js, docs/ai-guard-and-queue.md
section 7).

1. static - both widgets send through ``IP.ai.request`` with their feature, word errors with the server's detail or
   ``IP.ai.describeError``, follow ``IP.ai.cooldownLeft`` / ``IP.ai.onCooldown``, and use none of the constructs the
   strict CSP forbids; the served bundle carries the new code; the sheet renders its countdown line;
2. behaviour - the real sources run in JavaScriptCore (Safari's engine) inside a small stub browser (``HARNESS``:
   virtual clock and timers, a DOM with attribute / class selectors, abortable scripted fetch). Skipped where no
   ``jsc`` binary exists; ``JSC=/path/to/jsc`` points at one.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from tests.conftest import login_as
from tests.factories import make_customer

ROOT = Path(__file__).resolve().parent.parent
JS_SRC = ROOT / "app" / "static" / "src" / "js"
CORE, AI = JS_SRC / "00-core.js", JS_SRC / "01-ai.js"
ASSISTANT, CHAT = JS_SRC / "52-assistant.js", JS_SRC / "60-chat.js"
DIST_MIN = ROOT / "app" / "static" / "dist" / "app.min.js"
MAC_JSC = "/System/Library/Frameworks/JavaScriptCore.framework/Versions/Current/Helpers/jsc"
FORBIDDEN = ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "new Function(", "document.write(",
             'setAttribute("style"', ".cssText", 'setTimeout("', "setInterval(\"", "sessionStorage", "localStorage",
             "javascript:", '"visibilitychange"')
# survive rjsmin: strings of the new code paths
BUNDLE_MARKERS = ("Basic mode · AI busy", "Automatic parser (AI busy)", "You can send again in ", "Try again in ",
                  "[data-assistant-cooldown]")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _intake_component() -> str:
    src = _read(CHAT)
    return src[src.index('IP.register("intake-ai"'):]


def _jsc() -> str | None:
    for candidate in (os.environ.get("JSC"), shutil.which("jsc"), MAC_JSC):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


needs_jsc = pytest.mark.skipif(_jsc() is None, reason="JavaScriptCore (jsc) is not available on this machine")


# =============================================================================================== static checks
def test_assistant_sends_through_the_ai_layer():
    src = _read(ASSISTANT)
    assert 'IP.ai.request(CHAT_URL, { json: { message: text }, feature: FEATURE, signal:' in src
    assert 'var FEATURE = "assistant";' in src and 'var CHAT_URL = "/api/assistant/chat";' in src
    assert 'IP.api("/api/assistant/chat"' not in src  # the chat never bypasses IP.ai any more
    assert "IP.ai.describeError(err)" in src and "IP.ai.cooldownLeft(FEATURE)" in src and "IP.ai.onCooldown(" in src
    assert "inflight.abort()" in _between(src, "function close()", "function setBusy")  # closing stops the request
    assert 'err.reason === "page-hide"' in src


def test_intake_sends_through_the_ai_layer():
    comp = _intake_component()
    assert 'IP.ai.request(url, { json: { text: value }, feature: FEATURE })' in comp and 'var FEATURE = "intake";' in comp
    assert "IP.api(" not in comp
    assert "IP.ai.describeError(err)" in comp and "IP.ai.cooldownLeft(FEATURE)" in comp and "IP.ai.onCooldown(" in comp
    assert 'err.reason === "page-hide"' in comp


@pytest.mark.parametrize("source", ["assistant", "intake"])
def test_no_constructs_the_csp_forbids(source):
    import rjsmin

    code = rjsmin.jsmin(_read(ASSISTANT) if source == "assistant" else _intake_component(), keep_bang_comments=False)
    found = [token for token in FORBIDDEN if token in code]
    assert not found, found
    assert not re.search(r"\.style\s*=", code), "set CSSOM properties, never the style attribute"


def test_served_bundle_carries_the_widget_changes():
    missing: list[str] = []
    for _ in range(3):  # a dev watcher may be rewriting the file right now
        text = _read(DIST_MIN)
        missing = [m for m in BUNDLE_MARKERS if m not in text]
        if not missing:
            break
        time.sleep(1)
    assert not missing, f"app/static/dist/app.min.js is stale (missing {missing}): run python scripts/build_assets.py --js-only"


async def test_sheet_renders_the_countdown_line(client, db):
    user = await make_customer(db)
    await login_as(client, user)
    html = (await client.get("/calendar")).text
    line = re.search(r"<p [^>]*data-assistant-cooldown[^>]*>", html)
    assert line and "hidden" in line.group(0) and 'id="assistant-cooldown"' in line.group(0)
    assert "aria-live" not in line.group(0)  # it ticks every second: not a live region
    assert re.search(r'<button type="submit"[^>]*aria-describedby="assistant-cooldown"[^>]*data-assistant-send', html)


def _between(src: str, start: str, end: str) -> str:
    i = src.index(start)
    return src[i:src.index(end, i)]


# =============================================================================================== behaviour (JavaScriptCore)
def _run_harness(tmp_path: Path, *scripts: Path) -> str:
    harness = tmp_path / "widgets_harness.js"
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
    assert passed == total >= 19, out[-3000:]


@needs_jsc
def test_widgets_behave_in_javascriptcore(tmp_path):
    _assert_all_passed(_run_harness(tmp_path, CORE, AI, ASSISTANT, CHAT))


@needs_jsc
def test_minified_widgets_behave_the_same(tmp_path):
    import rjsmin

    bundle = "\n".join(f"/* {p.name} */\n{_read(p)}\n;" for p in (CORE, AI, ASSISTANT, CHAT))  # as build_assets joins
    minified = tmp_path / "widgets.min.js"
    minified.write_text(rjsmin.jsmin(bundle, keep_bang_comments=False), encoding="utf-8")
    _assert_all_passed(_run_harness(tmp_path, minified))


HARNESS = r"""/* Behaviour tests for 52-assistant.js and the intake-ai component of 60-chat.js, on top of the real 00-core.js and
 * 01-ai.js, in JavaScriptCore. jsc has no DOM, fetch or controllable timers, so this file provides a tiny browser:
 * virtual clock + timers, event targets with capture/bubble dispatch, elements with attribute / class selectors,
 * fetch with scripted responses and abort signals, AbortController, URL.
 * Usage: jsc widgets_harness.js -- <script.js>...     (prints PASS/FAIL lines and a SUMMARY line)
 */
var window = globalThis;
var __errors = [];
var console = {
  error: function () { __errors.push(Array.prototype.map.call(arguments, String).join(" ")); },
  log: function () {},
  warn: function () {},
};

/* ---------------------------------------------------------------- virtual clock + timers */
var __now = 1793000000000;
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
globalThis.requestAnimationFrame = function (fn) { return setTimeout(fn, 16); };
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

/* ---------------------------------------------------------------- events */
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
function Event(type, init) { this.type = type; this.bubbles = !!(init && init.bubbles); }

/** Browser-like dispatch: document capture listeners, then the target and its ancestors, then document bubble ones. */
function dispatch(target, type, init) {
  var evt = init || {};
  evt.type = type;
  evt.target = target;
  evt.defaultPrevented = false;
  evt.preventDefault = function () { evt.defaultPrevented = true; };
  evt.stopPropagation = function () {};
  document.fire(type, evt, "capture");
  for (var n = target; n && n.nodeType === 1; n = n.parentNode) n.fire(type, evt);
  document.fire(type, evt, "bubble");
  return evt;
}

/* ---------------------------------------------------------------- DOM */
function parseSel(sel) {
  return String(sel).split(",").map(function (part) {
    var s = part.trim();
    var alt = { tag: null, attrs: [], classes: [], nots: [], ok: true };
    var m = /^[a-zA-Z][a-zA-Z0-9-]*/.exec(s);
    if (m) { alt.tag = m[0].toUpperCase(); s = s.slice(m[0].length); }
    while (s.length) {
      if ((m = /^\[([^\]="~\s]+)(?:="([^"]*)")?\]/.exec(s))) alt.attrs.push([m[1], m[2] === undefined ? null : m[2]]);
      else if ((m = /^\.([A-Za-z0-9_-]+)/.exec(s))) alt.classes.push(m[1]);
      else if ((m = /^:not\(\[([^\]="~\s]+)(?:="([^"]*)")?\]\)/.exec(s))) alt.nots.push([m[1], m[2] === undefined ? null : m[2]]);
      else { alt.ok = false; break; } // combinators and the like: never match (the widgets don't depend on them here)
      s = s.slice(m[0].length);
    }
    return alt;
  });
}
function attrMatch(el, pair) { return el.hasAttribute(pair[0]) && (pair[1] === null || el.getAttribute(pair[0]) === pair[1]); }
function matchesAny(el, alts) {
  if (!el || el.nodeType !== 1) return false;
  return alts.some(function (a) {
    if (!a.ok || (a.tag && el.tagName !== a.tag)) return false;
    for (var i = 0; i < a.attrs.length; i++) if (!attrMatch(el, a.attrs[i])) return false;
    for (var j = 0; j < a.classes.length; j++) if (!el.classList.contains(a.classes[j])) return false;
    for (var k = 0; k < a.nots.length; k++) if (attrMatch(el, a.nots[k])) return false;
    return true;
  });
}

function TextNode(t) { this.nodeType = 3; this.textContent = String(t); this.parentNode = null; }
TextNode.prototype.remove = function () { if (this.parentNode) this.parentNode.removeChild(this); };

function El(tag) {
  Target.call(this);
  this.nodeType = 1;
  this.tagName = String(tag).toUpperCase();
  this.attrs = {};
  this.children = [];
  this.parentNode = null;
  this.value = "";
  this.style = {};
  this.hidden = false;
  this.disabled = false;
  this.scrollTop = 0;
  this.scrollHeight = 120;
  this._text = "";
  var self = this;
  this.classList = {
    _get: function () { return (self.attrs["class"] || "").split(/\s+/).filter(Boolean); },
    _set: function (list) { self.attrs["class"] = list.join(" "); },
    contains: function (c) { return this._get().indexOf(c) !== -1; },
    add: function () {
      var list = this._get();
      for (var i = 0; i < arguments.length; i++) if (list.indexOf(arguments[i]) === -1) list.push(arguments[i]);
      this._set(list);
    },
    remove: function () {
      var drop = Array.prototype.slice.call(arguments);
      this._set(this._get().filter(function (c) { return drop.indexOf(c) === -1; }));
    },
    toggle: function (c, force) {
      var want = force === undefined ? !this.contains(c) : !!force;
      if (want) this.add(c); else this.remove(c);
      return want;
    },
  };
}
El.prototype = Object.create(Target.prototype);
Object.defineProperty(El.prototype, "className", {
  get: function () { return this.attrs["class"] || ""; },
  set: function (v) { this.attrs["class"] = String(v); },
});
Object.defineProperty(El.prototype, "textContent", {
  get: function () { return this._text + this.children.map(function (c) { return c.textContent; }).join(""); },
  set: function (v) {
    this.children.forEach(function (c) { c.parentNode = null; });
    this.children = [];
    this._text = v == null ? "" : String(v);
  },
});
Object.defineProperty(El.prototype, "firstChild", { get: function () { return this.children[0] || null; } });
El.prototype.getAttribute = function (k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; };
El.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); };
El.prototype.hasAttribute = function (k) { return Object.prototype.hasOwnProperty.call(this.attrs, k); };
El.prototype.removeAttribute = function (k) { delete this.attrs[k]; };
El.prototype.appendChild = function (c) {
  if (c.parentNode) c.parentNode.removeChild(c);
  this.children.push(c);
  c.parentNode = this;
  return c;
};
El.prototype.removeChild = function (c) {
  var i = this.children.indexOf(c);
  if (i !== -1) this.children.splice(i, 1);
  c.parentNode = null;
  return c;
};
El.prototype.insertBefore = function (c, ref) {
  if (c.parentNode) c.parentNode.removeChild(c);
  var i = ref ? this.children.indexOf(ref) : -1;
  if (i === -1) this.children.push(c); else this.children.splice(i, 0, c);
  c.parentNode = this;
  return c;
};
El.prototype.insertAdjacentElement = function (where, c) {
  if (where !== "afterend" || !this.parentNode) throw new Error("insertAdjacentElement(" + where + ") not supported");
  var p = this.parentNode;
  if (c.parentNode) c.parentNode.removeChild(c);
  p.children.splice(p.children.indexOf(this) + 1, 0, c);
  c.parentNode = p;
  return c;
};
El.prototype.remove = function () { if (this.parentNode) this.parentNode.removeChild(this); };
El.prototype.contains = function (n) { for (; n; n = n.parentNode) if (n === this) return true; return false; };
El.prototype.descendants = function () {
  var out = [];
  (function walk(n) {
    n.children.forEach(function (c) { if (c.nodeType === 1) { out.push(c); walk(c); } });
  })(this);
  return out;
};
El.prototype.querySelectorAll = function (sel) {
  var alts = parseSel(sel);
  return this.descendants().filter(function (e) { return matchesAny(e, alts); });
};
El.prototype.querySelector = function (sel) { return this.querySelectorAll(sel)[0] || null; };
El.prototype.closest = function (sel) {
  var alts = parseSel(sel);
  for (var n = this; n && n.nodeType === 1; n = n.parentNode) if (matchesAny(n, alts)) return n;
  return null;
};
El.prototype.focus = function () { document.activeElement = this; };
El.prototype.setSelectionRange = function () {};
El.prototype.scrollIntoView = function () {};
El.prototype.dispatchEvent = function (evt) { dispatch(this, evt.type, {}); return true; };
El.prototype.click = function () { if (!this.disabled) dispatch(this, "click", {}); };

var document = new Target();
document.nodeType = 9;
document.root = new El("html");
document.body = document.root.appendChild(new El("body"));
document.body.setAttribute("data-ws", "off");
document.activeElement = document.body;
document.querySelector = function (sel) {
  if (sel === 'meta[name="csrf-token"]') return { getAttribute: function () { return "csrf-abc"; } };
  return document.root.querySelector(sel);
};
document.querySelectorAll = function (sel) { return document.root.querySelectorAll(sel); };
document.getElementById = function (id) { return document.root.querySelector('[id="' + id + '"]'); };
document.createElement = function (tag) { return new El(tag); };
document.createElementNS = function (ns, tag) { return new El(tag); };
document.createTextNode = function (t) { return new TextNode(t); };
document.contains = function (n) { return document.root.contains(n); };
window.matchMedia = function () { return { matches: false, addEventListener: function () {} }; };

var location = { protocol: "https:", host: "gym.example", origin: "https://gym.example",
                 href: "https://gym.example/calendar", pathname: "/calendar" };
var navigator = { onLine: true };
var crypto = { randomUUID: function () { return "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed"; } };
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
function AbortSignal() { Target.call(this); this.aborted = false; }
AbortSignal.prototype = Object.create(Target.prototype);
function AbortController() { this.signal = new AbortSignal(); }
AbortController.prototype.abort = function () {
  if (this.signal.aborted) return;
  this.signal.aborted = true;
  this.signal.fire("abort", {});
};
function FormData(form) { this.form = form; }

/* ---------------------------------------------------------------- fetch */
function Res(status, headers, body) {
  this.status = status;
  this.ok = status >= 200 && status < 300;
  var h = {};
  Object.keys(headers || {}).forEach(function (k) { h[k.toLowerCase()] = String(headers[k]); });
  this.headers = { get: function (k) { k = String(k).toLowerCase(); return Object.prototype.hasOwnProperty.call(h, k) ? h[k] : null; } };
  this._body = body;
  this.body = null;
}
Res.prototype.json = function () {
  var b = this._body;
  return new Promise(function (ok, fail) { try { ok(JSON.parse(b)); } catch (e) { fail(e); } });
};
Res.prototype.text = function () { return Promise.resolve(typeof this._body === "string" ? this._body : ""); };
function json(status, obj, headers) {
  var h = { "content-type": "application/json" };
  Object.keys(headers || {}).forEach(function (k) { h[k] = headers[k]; });
  return new Res(status, h, JSON.stringify(obj));
}
function refusal(status, code, detail, retryAfter) {
  var titles = { quota: "AI limit reached", cooldown: "AI paused", too_many: "Too many AI requests",
                 duplicate: "Already asked", spam: "Message blocked", busy: "AI is busy" };
  return json(status, { error: titles[code], detail: detail, code: code, retry_after: retryAfter, ai_available: true },
              retryAfter ? { "Retry-After": String(retryAfter) } : {});
}
function hang() { return new Promise(function () {}); }

var __fetches = [];
var __route = null;
function fetch(url, init) {
  var call = { url: String(url), init: init || {}, aborted: false, method: ((init && init.method) || "GET").toUpperCase() };
  __fetches.push(call);
  return new Promise(function (ok, fail) {
    var signal = call.init.signal;
    if (signal && signal.aborted) { call.aborted = true; fail(domAbort()); return; }
    if (signal) signal.addEventListener("abort", function () { call.aborted = true; fail(domAbort()); });
    Promise.resolve(__route(call.url, call.init, call)).then(ok, fail);
  });
}
function calls(path, method) {
  return __fetches.filter(function (c) { return c.url.split("?")[0] === path && (!method || c.method === method); });
}
function lastCall(path, method) { var list = calls(path, method); return list[list.length - 1] || null; }

var HISTORY = [];
function defaultRoute(url, init) {
  if (url === "/api/assistant/history") return json(200, { items: HISTORY });
  return json(200, { ok: true });
}
/** Route POST /api/assistant/chat and /owner/customers/intake to `chat` / `intake`, the rest as by default. */
function route(chat, intake) {
  __route = function (url, init, call) {
    if (url === "/api/assistant/chat" && chat) return chat(call);
    if (url === "/owner/customers/intake" && intake) return intake(call);
    return defaultRoute(url, init);
  };
}

/* ---------------------------------------------------------------- the page: assistant sheet + intake form */
function h(tag, attrs, kids) {
  var e = new El(tag);
  Object.keys(attrs || {}).forEach(function (k) { e.setAttribute(k, attrs[k]); });
  (kids || []).forEach(function (c) { e.appendChild(c); });
  return e;
}
var fab = h("button", { type: "button", "class": "fab", "data-assistant-open": "" });
var backdrop = h("div", { "class": "sheet-backdrop", "data-assistant-backdrop": "", "data-open": "0" });
backdrop.hidden = true;
var statusEl = h("span", { "data-assistant-status": "" });
statusEl.textContent = "Gemma (gateway)";
var dotEl = h("span", { "class": "ai-dot", "data-assistant-dot": "", "data-state": "online" });
var closeBtn = h("button", { type: "button", "data-assistant-close": "" });
var intro = h("div", { "data-assistant-intro": "" });
var logEl = h("div", { "data-assistant-log": "" }, [intro]);
var chip = h("button", { type: "button", "class": "suggestion-chip", "data-assistant-suggest": "Show my tasks" });
var cooldownEl = h("p", { id: "assistant-cooldown", "data-assistant-cooldown": "" });
cooldownEl.hidden = true;
var input = h("textarea", { name: "message", "data-assistant-input": "" });
var sendBtn = h("button", { type: "submit", "data-assistant-send": "" });
var form = h("form", { "data-assistant-form": "", "data-no-busy": "" }, [input, sendBtn]);
var sheet = h("section", { id: "assistant-sheet", "class": "sheet", "data-component": "assistant", "data-open": "0",
                           "data-ai-available": "1", inert: "" },
              [statusEl, dotEl, closeBtn, logEl, chip, cooldownEl, form]);

var itext = h("textarea", { "data-intake-text": "" });
var ilabel = h("span", { "data-intake-label": "" });
ilabel.textContent = "Fill with AI";
var irun = h("button", { type: "button", "class": "btn", "data-intake-run": "" }, [ilabel]);
var iprovider = h("span", { "class": "chip", "data-intake-provider": "" });
var isummary = h("p", { "data-intake-summary": "" });
var inotes = h("ul", { "data-intake-notes": "" });
var idisc = h("span", { "data-intake-disclaimer": "" });
var iresult = h("div", { "data-intake-result": "" }, [iprovider, isummary, inotes, idisc]);
iresult.hidden = true;
var ierror = h("p", { "class": "hidden text-bad", "data-intake-error": "" });
var ibox = h("section", { "data-component": "intake-ai", "data-url": "/owner/customers/intake", "data-form": "register-form",
                          "data-min": "10" }, [itext, irun, iresult, ierror]);
var fullName = h("input", { name: "full_name" });
var source = h("input", { type: "hidden", name: "intake_source", value: "owner_manual", "data-intake-source": "" });
var regForm = h("form", { id: "register-form", method: "post", action: "/owner/customers/new" }, [fullName, source]);
regForm.elements = { namedItem: function (name) { return regForm.querySelector('[name="' + name + '"]'); } };
[fab, backdrop, sheet, ibox, regForm].forEach(function (n) { document.body.appendChild(n); });

/* ---------------------------------------------------------------- page helpers */
function isOpen() { return sheet.getAttribute("data-open") === "1"; }
function openSheet() { if (!isOpen()) dispatch(fab, "click", {}); flush(); advance(100); }
function closeSheet() { if (isOpen()) dispatch(closeBtn, "click", {}); flush(); }
function type(text) { input.value = text; }
function submit() { dispatch(form, "submit", {}); flush(); }
function pressEnter(el) { dispatch(el || input, "keydown", { key: "Enter", shiftKey: false, isComposing: false }); flush(); }
function typing() { return logEl.children.some(function (c) { return c.nodeType === 1 && c.getAttribute("role") === "status"; }); }
/** The conversation as [{role, text, meta}] (typing dots excluded). */
function messages() {
  return logEl.children.filter(function (w) {
    return w.nodeType === 1 && w.getAttribute("role") !== "status" && w.querySelector(".bubble");
  }).map(function (w) {
    var bubble = w.querySelector(".bubble");
    var meta = w.children.filter(function (c) { return c.nodeType === 1 && c.tagName === "SPAN"; })[0];
    return { role: bubble.classList.contains("bubble-user") ? "user" : "ai", text: bubble.textContent,
             meta: meta ? meta.textContent : null };
  });
}
function lastMessage() { var m = messages(); return m[m.length - 1] || null; }
function answer(body) { return function () { return json(200, body); }; }
var OK_REPLY = { reply: "On it!", actions: [], provider: "gateway", ai_available: true };
function intakeData(extra) {
  var d = { ok: true, source: "ai", provider: "Fake (gateway)", ai_available: true, busy: false, found: ["full_name"],
            disclaimer: "AI can make mistakes.", fields: { full_name: "Riya Kapoor" }, lists: {}, missing: ["id_front"],
            missing_labels: ["ID card (front photo)"], invalid: {}, dropped: [], notes: [], age: null, minor: null };
  Object.keys(extra || {}).forEach(function (k) { d[k] = extra[k]; });
  return d;
}
function runIntake(note) { itext.value = note; irun.click(); flush(); }
function waitOut() { advance(10 * 60 * 1000); } // longer than any wait used below

/* ---------------------------------------------------------------- mini test runner */
var __tests = [];
function test(name, fn) { __tests.push({ name: name, fn: fn }); }
function show(v) { try { return JSON.stringify(v); } catch (e) { return String(v); } }
function eq(a, b, msg) { if (a !== b) throw new Error((msg ? msg + ": " : "") + "expected " + show(b) + ", got " + show(a)); }
function ok(v, msg) { if (!v) throw new Error(msg || "expected a truthy value"); }

for (var a = 0; a < arguments.length; a++) load(arguments[a]);
var IP = window.IP;
__route = defaultRoute;
document.fire("DOMContentLoaded", {});
flush();

/* ================================================================ assistant sheet */
test("boot: both widgets are wired, nothing is waiting", function () {
  eq(typeof IP.assistant, "object");
  eq(typeof IP.assistant.send, "function");
  eq(sendBtn.disabled, false);
  eq(cooldownEl.hidden, true);
  eq(irun.disabled, false);
  eq(ilabel.textContent, "Fill with AI");
  eq(IP.ai.pending(), 0);
});

test("a message goes through IP.ai: feature bucket, page id, CSRF, JSON body; the answer is shown", function () {
  route(answer({ reply: "Added it.", actions: [{ type: "create_task", ok: true, detail: "Added “Buy protein”.", link: "/tasks" }],
                 provider: "gateway", ai_available: true }));
  var changed = [];
  IP.on("tasks.changed", function (e) { changed.push(e); });
  openSheet();
  eq(calls("/api/assistant/history", "GET").length, 1, "history loads once on the first open");
  type("add buy protein tomorrow");
  submit();
  var c = lastCall("/api/assistant/chat", "POST");
  ok(c, "the chat request was sent");
  eq(c.init.headers["X-Page-Id"], IP.pageId);
  eq(c.init.headers["X-CSRF-Token"], "csrf-abc");
  eq(c.init.body, JSON.stringify({ message: "add buy protein tomorrow" }));
  ok(c.init.signal && typeof c.init.signal.addEventListener === "function", "abortable");
  var m = messages();
  eq(m.length, 2);
  eq(m[0].role + ":" + m[0].text, "user:add buy protein tomorrow");
  eq(m[0].meta, null);
  eq(m[1].role + ":" + m[1].text, "ai:Added it.");
  eq(m[1].meta, null);
  eq(input.value, "");
  eq(sendBtn.disabled, false);
  eq(typing(), false);
  eq(changed.length, 1);
  eq(IP.ai.pending(), 0);
});

test("quota refusal: the server's words, 'Not sent', the words back, no sending until the wait is over", function () {
  var detail = "You've used your AI chat messages for this minute. You can send more in 45 seconds.";
  route(function () { return refusal(429, "quota", detail, 45); });
  var before = messages().length;
  type("remind me to stretch at 7pm");
  submit();
  var m = messages();
  eq(m.length, before + 2);
  eq(m[before].text, "remind me to stretch at 7pm");
  eq(m[before].meta, "Not sent");
  eq(m[before + 1].text, detail, "the server's own explanation");
  eq(input.value, "remind me to stretch at 7pm", "the words are back in the input");
  eq(IP.ai.cooldownLeft("assistant"), 45);
  eq(sendBtn.disabled, true);
  eq(cooldownEl.hidden, false);
  eq(cooldownEl.textContent, "You can send again in 45 s.");
  advance(10000);
  eq(cooldownEl.textContent, "You can send again in 35 s.", "the line counts down");
  var sent = calls("/api/assistant/chat", "POST").length;
  pressEnter();
  dispatch(chip, "click", {});
  flush();
  eq(calls("/api/assistant/chat", "POST").length, sent, "Enter and the chips don't send while waiting");
  eq(input.value, "remind me to stretch at 7pm");
  eq(messages().length, before + 2);
  advance(35500);
  eq(IP.ai.cooldownLeft("assistant"), 0);
  eq(sendBtn.disabled, false, "re-enabled when the wait ends");
  eq(cooldownEl.hidden, true);
  eq(cooldownEl.textContent, "");
  route(answer(OK_REPLY));
  submit();
  eq(calls("/api/assistant/chat", "POST").length, sent + 1, "sending works again");
  eq(lastMessage().text, "On it!");
  eq(input.value, "");
});

test("an account-wide pause (code 'cooldown') says so in the countdown line", function () {
  route(function () { return refusal(429, "cooldown", "AI is paused on your account for 2 min because of too many requests in a short time.", 120); });
  type("what should I eat today?");
  submit();
  eq(lastMessage().text, "AI is paused on your account for 2 min because of too many requests in a short time.");
  eq(cooldownEl.textContent, "AI is paused for your account. You can send again in 2 min.");
  eq(sendBtn.disabled, true);
  eq(IP.ai.cooldownLeft("intake"), 120, "a pause covers every AI feature on the page");
  eq(irun.disabled, true, "the intake waits too");
  waitOut();
  eq(sendBtn.disabled, false);
  eq(cooldownEl.hidden, true);
  eq(irun.disabled, false);
  eq(ilabel.textContent, "Fill with AI");
  input.value = "";
});

test("duplicate (409): the server's words; only those words wait, anything else is sent", function () {
  route(function () { return refusal(409, "duplicate", "You just asked that - give it a moment.", 18); });
  type("show my tasks");
  submit();
  eq(lastMessage().text, "You just asked that - give it a moment.");
  eq(messages()[messages().length - 2].meta, "Not sent");
  eq(cooldownEl.hidden, true, "a duplicate pauses nothing");
  eq(sendBtn.disabled, false);
  var sent = calls("/api/assistant/chat", "POST").length;
  input.value = "";
  type("Show my tasks!");
  submit();
  eq(calls("/api/assistant/chat", "POST").length, sent, "the same words wait out the server's window locally");
  eq(lastMessage().text, "You just asked that. Try again in 18 s.");
  route(answer(OK_REPLY));
  input.value = "";
  type("plan my week");
  submit();
  eq(calls("/api/assistant/chat", "POST").length, sent + 1, "other words are sent at once");
  waitOut();
  eq(sendBtn.disabled, false);
  input.value = "";
});

test("spam (422, no wait): the server's words, sending stays possible", function () {
  route(function () { return refusal(422, "spam", "That message looks like spam (one character is repeated over and over). Please rephrase it.", 0); });
  type("zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz");
  submit();
  eq(lastMessage().text, "That message looks like spam (one character is repeated over and over). Please rephrase it.");
  eq(input.value, "zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz", "back in the input to rephrase");
  eq(IP.ai.cooldownLeft("assistant"), 0);
  eq(sendBtn.disabled, false);
  eq(cooldownEl.hidden, true);
  input.value = "";
});

test("too many at once (429 too_many): the server's words", function () {
  route(function () { return refusal(429, "too_many", "You already have several AI requests in progress - please wait for them to finish.", 5); });
  type("plan my week");
  submit();
  eq(lastMessage().text, "You already have several AI requests in progress - please wait for them to finish.");
  eq(cooldownEl.textContent, "You can send again in 5 s.");
  waitOut();
  input.value = "";
});

test("other failures use IP.ai.describeError, and nothing claims the message was not sent", function () {
  var before = messages().length;
  route(function () { return Promise.reject(new TypeError("Load failed")); });
  type("hello there");
  submit();
  eq(lastMessage().text, "Couldn't reach the server - check your connection and try again.");
  eq(messages()[before].meta, null, "unknown outcome: no 'Not sent'");
  eq(input.value, "");
  route(function () { return json(500, { error: "Server error", detail: "Unexpected error - it has been logged." }); });
  type("hello again");
  submit();
  eq(lastMessage().text, "Something went wrong on our side - please try again.");
  route(function () { return json(401, { error: "Please sign in", detail: "Please sign in" }); });
  type("and again");
  submit();
  eq(lastMessage().text, "Your session has expired - please sign in again.");
  route(function () { return json(503, { error: "Cancelled", detail: "The AI request was stopped because the page was closed or the connection was lost.", code: "cancelled" }); });
  type("one more");
  submit();
  eq(lastMessage().text, "The request was cancelled.");
  eq(messages()[messages().length - 2].meta, "Not sent", "the server stopped it: nothing was stored");
  eq(input.value, "one more");
  eq(sendBtn.disabled, false);
  input.value = "";
});

test("busy answer: 'Basic mode · AI busy' and the status line keeps saying AI is online", function () {
  statusEl.textContent = "Gemma (gateway)";
  route(answer({ reply: "AI is busy right now - I handled that with basic commands.", busy: true, ai_available: false,
                 provider: null, actions: [{ type: "create_reminder", ok: true, detail: "Reminder set.", link: "/calendar" }] }));
  type("remind me to drink water in 20 minutes");
  submit();
  eq(lastMessage().text, "AI is busy right now - I handled that with basic commands.");
  eq(lastMessage().meta, "Basic mode · AI busy");
  eq(statusEl.textContent, "Gemma (gateway)");
  eq(dotEl.getAttribute("data-state"), "online");
  eq(sheet.getAttribute("data-ai-available"), "1");
});

test("offline answer: 'Basic mode · AI offline' and the status line says so", function () {
  route(answer({ reply: "AI is unavailable right now - I handled that with basic commands.", ai_available: false,
                 provider: null, actions: [] }));
  type("show my tasks please");
  submit();
  eq(lastMessage().meta, "Basic mode · AI offline");
  eq(statusEl.textContent, "AI unavailable - basic commands still work");
  eq(dotEl.getAttribute("data-state"), "offline");
  route(answer(OK_REPLY));
  type("and now?");
  submit();
  eq(statusEl.textContent, "Gemma (gateway)", "a real answer brings the status back");
});

test("closing the sheet aborts the request: no error, the words come back, the next open reloads the history", function () {
  route(function () { return hang(); });
  type("message my trainer: running late");
  submit();
  var c = lastCall("/api/assistant/chat", "POST");
  eq(typing(), true);
  eq(IP.ai.pending(), 1);
  var before = messages().length;
  closeSheet();
  eq(c.aborted, true, "the fetch was aborted - the server stops the AI work");
  eq(IP.ai.pending(), 0);
  eq(typing(), false);
  eq(messages().length, before, "no error bubble");
  eq(input.value, "message my trainer: running late");
  eq(sendBtn.disabled, false);
  eq(isOpen(), false);
  HISTORY = [{ id: 1, role: "user", content: "earlier question", actions: [] },
             { id: 2, role: "assistant", content: "earlier answer", actions: [] }];
  var loads = calls("/api/assistant/history", "GET").length;
  openSheet();
  eq(calls("/api/assistant/history", "GET").length, loads + 1, "the history is fetched again");
  var m = messages();
  eq(m.length, 2, "the log is the server's history");
  eq(m[0].text + "|" + m[1].text, "earlier question|earlier answer");
  eq(input.value, "message my trainer: running late");
  closeSheet();
  openSheet();
  eq(calls("/api/assistant/history", "GET").length, loads + 1, "only once");
  input.value = "";
});

test("leaving the page (pagehide) aborts quietly; a back/forward-cache restore shows the real history", function () {
  route(function () { return hang(); });
  type("what's on today?");
  submit();
  var c = lastCall("/api/assistant/chat", "POST");
  var before = messages().length;
  window.fire("pagehide", { persisted: true });
  flush();
  eq(c.aborted, true);
  eq(messages().length, before, "an AbortError with reason 'page-hide' is not reported");
  eq(typing(), false);
  eq(sendBtn.disabled, false);
  HISTORY = [{ id: 3, role: "user", content: "what's on today?", actions: [] },
             { id: 4, role: "assistant", content: "Leg day.", actions: [] }];
  var loads = calls("/api/assistant/history", "GET").length;
  window.fire("pageshow", { persisted: true });
  flush();
  eq(calls("/api/assistant/history", "GET").length, loads + 1, "the sheet was open: history reloaded at once");
  eq(lastMessage().text, "Leg day.");
  input.value = "";
});

test("closing the sheet while the history is still loading sends nothing", function () {
  // an aborted request makes the next open reload the history: hold that reload back
  route(function () { return hang(); });
  type("first try");
  submit();
  closeSheet();
  eq(input.value, "first try");
  var release = null;
  __route = function (url) {
    if (url === "/api/assistant/history") {
      return new Promise(function (done) { release = function () { done(json(200, { items: [] })); }; });
    }
    return json(200, OK_REPLY);
  };
  openSheet();
  ok(release, "the history reload is in flight");
  input.value = "hello";
  submit(); // waits for the history first ...
  closeSheet(); // ... and the sheet closes meanwhile
  release();
  flush();
  var asked = calls("/api/assistant/chat", "POST").filter(function (c) { return c.init.body === JSON.stringify({ message: "hello" }); });
  eq(asked.length, 0, "nothing was asked");
  eq(input.value, "hello");
  eq(sendBtn.disabled, false);
  eq(messages().length, 0, "the log is the server's (empty) history");
  input.value = "";
  HISTORY = [];
});

/* ================================================================ intake */
test("intake: sent through IP.ai with feature 'intake' and the page id; the form is filled", function () {
  route(null, answer(intakeData()));
  runIntake("New member Riya Kapoor, phone 98765 43210, has asthma.");
  var c = lastCall("/owner/customers/intake", "POST");
  ok(c, "intake request sent");
  eq(c.init.headers["X-Page-Id"], IP.pageId);
  eq(c.init.body, JSON.stringify({ text: "New member Riya Kapoor, phone 98765 43210, has asthma." }));
  eq(fullName.value, "Riya Kapoor");
  eq(source.value, "owner_ai");
  eq(iresult.hidden, false);
  eq(iprovider.textContent, "Fake (gateway)");
  eq(iprovider.children[0].getAttribute("data-state"), "online");
  eq(irun.disabled, false);
  eq(ilabel.textContent, "Fill with AI");
  eq(ierror.textContent, "");
  eq(ierror.classList.contains("hidden"), true);
});

test("intake: a full AI queue is answered by the parser, labelled 'AI busy'", function () {
  route(null, answer(intakeData({ source: "fallback", provider: null, ai_available: false, busy: true,
                                 notes: ["AI is busy right now - details were extracted by the automatic parser."] })));
  runIntake("New member Riya Kapoor, phone 98765 43210, has asthma. Again.");
  eq(iprovider.textContent, "Automatic parser (AI busy)");
  eq(iprovider.children[0].getAttribute("data-state"), "degraded");
  route(null, answer(intakeData({ source: "fallback", provider: null, ai_available: false })));
  runIntake("New member Riya Kapoor, phone 98765 43210, has asthma. Offline.");
  eq(iprovider.textContent, "Automatic parser (AI unavailable)");
  eq(iprovider.children[0].getAttribute("data-state"), "offline");
});

test("intake quota refusal: the server's words, the button waits with a countdown, then works again", function () {
  var detail = "You've used your AI generations for this minute. You can generate more in 45 seconds.";
  route(null, function () { return refusal(429, "quota", detail, 45); });
  runIntake("Name is Kabir Rao, 30 years old, phone 98450 12345.");
  eq(ierror.textContent, detail);
  eq(ierror.classList.contains("hidden"), false);
  eq(irun.disabled, true);
  eq(ilabel.textContent, "Try again in 45 s");
  advance(20000);
  eq(ilabel.textContent, "Try again in 25 s");
  var sent = calls("/owner/customers/intake", "POST").length;
  dispatch(itext, "keydown", { key: "Enter", metaKey: true });
  flush();
  irun.click();
  flush();
  eq(calls("/owner/customers/intake", "POST").length, sent, "no request while waiting");
  eq(sendBtn.disabled, false, "the assistant has its own bucket");
  advance(26000);
  eq(irun.disabled, false);
  eq(ilabel.textContent, "Fill with AI");
  route(null, answer(intakeData()));
  runIntake("Name is Kabir Rao, 30 years old, phone 98450 12345.");
  eq(calls("/owner/customers/intake", "POST").length, sent + 1);
  eq(ierror.classList.contains("hidden"), true, "the old error is cleared");
});

test("intake duplicate / spam / validation errors show the server's words", function () {
  route(null, function () { return refusal(409, "duplicate", "You just asked that - give it a moment.", 15); });
  runIntake("Name is Kabir Rao, 30 years old, phone 98450 12345.");
  eq(ierror.textContent, "You just asked that - give it a moment.");
  eq(ilabel.textContent, "Fill with AI", "a duplicate pauses nothing: only that note waits");
  eq(irun.disabled, false);
  waitOut();
  route(null, function () { return refusal(422, "spam", "That message looks like spam (it is mostly symbols). Please rephrase it.", 0); });
  runIntake("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!");
  eq(ierror.textContent, "That message looks like spam (it is mostly symbols). Please rephrase it.");
  eq(irun.disabled, false);
  route(null, function () { return json(400, { error: "Invalid input", detail: "Keep the description under 4,000 characters." }); });
  runIntake("A very long note, really.");
  eq(ierror.textContent, "Keep the description under 4,000 characters.");
  eq(irun.disabled, false);
  eq(ilabel.textContent, "Fill with AI");
});

test("intake: leaving the page aborts the request without an error", function () {
  ierror.textContent = "";
  ierror.classList.add("hidden");
  route(null, function () { return hang(); });
  runIntake("Name is Leela Nair, email leela@example.com.");
  var c = lastCall("/owner/customers/intake", "POST");
  eq(irun.disabled, true);
  eq(ilabel.textContent, "Reading your note…");
  window.fire("pagehide", {});
  flush();
  eq(c.aborted, true);
  eq(ierror.textContent, "");
  eq(ierror.classList.contains("hidden"), true);
  eq(irun.disabled, false);
  eq(ilabel.textContent, "Fill with AI");
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
      __route = defaultRoute;
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
