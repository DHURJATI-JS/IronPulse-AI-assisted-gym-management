"""The live coach's browser side under the AI guard and the AI queue (app/static/src/js/55-live-coach.js).

The real ``00-core.js`` + ``01-ai.js`` + ``55-live-coach.js`` run in JavaScriptCore (Safari's engine) inside a small
stub browser (virtual clock and timers, DOM stubs, scripted fetch) and drive a live session:

* chat goes through ``IP.ai.request`` (feature ``live_coach``, ``X-Page-Id``); a newer message aborts the one in
  flight, a voice repeat of it is dropped;
* a refused reply (HTTP 200 + ``refused``) is spoken once, then AI chat waits ``retry_after`` seconds: messages carry
  ``"ai": false`` (the server still runs safety checks and commands) and the chat box shows the countdown; a repeated
  phrase says nothing; spam does not pause chat;
* easier / harder: one step per 1.5 s;
* discomfort reports are safety requests (never refused or aborted locally) - even while AI is paused page-wide;
* the voice (``/api/tts``): ``X-Page-Id``, no new voice requests while ``Retry-After`` runs or ``IP.ai`` pauses
  ``tts`` (the browser voice speaks meanwhile);
* a network rate limit (429 without a code): chat is held, core commands run locally, possible health problems still
  reach the server;
* pagehide aborts the message in flight and pauses the workout with a keepalive event - but not on sign-out.

Skipped where no ``jsc`` binary exists (``JSC=/path/to/jsc`` points at one); the static checks always run.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
JS_SRC = ROOT / "app" / "static" / "src" / "js"
CORE = JS_SRC / "00-core.js"
AI = JS_SRC / "01-ai.js"
LIVE = JS_SRC / "55-live-coach.js"
DIST_MIN = ROOT / "app" / "static" / "dist" / "app.min.js"
MAC_JSC = "/System/Library/Frameworks/JavaScriptCore.framework/Versions/Current/Helpers/jsc"
FORBIDDEN = ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "new Function(", "document.write(",
             'setAttribute("style"', ".cssText", 'setTimeout("', "setTimeout('", 'setInterval("', "setInterval('",
             "javascript:")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _code(path: Path) -> str:
    import rjsmin

    return rjsmin.jsmin(_read(path), keep_bang_comments=False)


def _jsc() -> str | None:
    for candidate in (os.environ.get("JSC"), shutil.which("jsc"), MAC_JSC):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


needs_jsc = pytest.mark.skipif(_jsc() is None, reason="JavaScriptCore (jsc) is not available on this machine")


# =============================================================================================== static checks
def test_live_coach_uses_the_shared_ai_layer():
    src = _read(LIVE)
    for piece in ('feature: "live_coach"', "timeout: 12000", "safety: true", "safety: unsafe", "ai: false",
                  "IP.ai.request(url, opts)", 'cooldownLeft("live_coach")', 'cooldownLeft("tts")',
                  'headers["X-Page-Id"] = IP.pageId', 'IP.parseRetryAfter(res.headers.get("retry-after"))',
                  'json: { type: "paused", state: stateFor() }, keepalive: true', "isLogoutForm(e.target)",
                  "abortMessage(); // one AI message at a time", "t - lastIntensity < 1500", "refused.retry_after > 0",
                  'refused.code === "duplicate"', "IP.ai.onCooldown("):
        assert piece in src, piece
    # discomfort reports never go through plain IP.api any more (a local AI cooldown must not hold them)
    disc = src[src.index("async function reportDiscomfort"):src.index("/* ---------------------------------------------------------- end + wake lock")]
    assert "aiRequest(urls.discomfort" in disc and "safety: true" in disc and "IP.api(" not in disc
    # the old unguarded paths are gone
    assert "IP.api(urls.message" not in src and "err.status === 503) downUntil = now() + 60000" not in src


def test_live_coach_has_no_constructs_the_csp_forbids():
    code = _code(LIVE)
    found = [token for token in FORBIDDEN if token in code]
    assert not found, found
    assert not re.search(r"\.style\s*=", code), "set CSSOM properties, never the style attribute"


def test_served_bundle_contains_the_new_live_coach():
    markers = ("Chat is back in ", "Pulse can chat again.", "Lots of messages from this network", "/auth/logout")
    missing: list[str] = []
    for _ in range(3):  # a dev watcher may be rewriting the file right now
        text = _read(DIST_MIN)
        missing = [m for m in markers if m not in text]
        if not missing:
            break
        time.sleep(1)
    assert not missing, f"app/static/dist/app.min.js is stale (missing {missing}): run python scripts/build_assets.py --js-only"


# =============================================================================================== behaviour (JavaScriptCore)
def _run(tmp_path: Path, *scripts: Path) -> str:
    harness = tmp_path / "live_harness.js"
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
    assert passed == total >= 11, out[-3000:]


@needs_jsc
def test_live_coach_client_behaviour_in_javascriptcore(tmp_path):
    _assert_all_passed(_run(tmp_path, CORE, AI, LIVE))


@needs_jsc
def test_minified_live_coach_client_behaves_the_same(tmp_path):
    import rjsmin

    bundle = "\n".join(f"/* {p.name} */\n{_read(p)}\n;" for p in (CORE, AI, LIVE))  # as scripts/build_assets.py joins
    minified = tmp_path / "live.min.js"
    minified.write_text(rjsmin.jsmin(bundle, keep_bang_comments=False), encoding="utf-8")
    _assert_all_passed(_run(tmp_path, minified))


HARNESS = r"""/* A tiny browser for 00-core.js + 01-ai.js + 55-live-coach.js in JavaScriptCore: virtual clock + timers, event
 * targets, DOM element stubs (a live-session root whose querySelector hands out one stub per selector), fetch with
 * scripted responses, AbortController, URL, Blob, Audio. Prints PASS/FAIL lines and a SUMMARY line.
 * Usage: jsc live_harness.js -- <core.js> <ai.js> <live.js>   (or one minified bundle holding all three)
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
globalThis.performance = { now: function () { return __now - 1793000000000; } }; // jsc's own runs on real time
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
globalThis.requestAnimationFrame = function (fn) { return setTimeout(function () { fn(__now); }, 16); };
globalThis.cancelAnimationFrame = globalThis.clearTimeout;
function advance(ms) {
  var end = __now + ms;
  for (var guard = 0; guard < 400000; guard++) {
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

/* ---------------------------------------------------------------- events + elements + document */
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
  if (!evt.preventDefault) evt.preventDefault = function () { evt.defaultPrevented = true; };
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
  this.nodeType = 1;
  this.attrs = {};
  this.children = [];
  this.parentNode = null;
  this.value = "";
  this.style = {};
  this.hidden = false;
  this.disabled = false;
  this.className = "";
  this.scrollTop = 0;
  this.scrollHeight = 0;
  this._text = "";
  this._q = {};
  var cls = {};
  this.classList = {
    add: function (c) { cls[c] = 1; },
    remove: function (c) { delete cls[c]; },
    toggle: function (c, on) { if (on === undefined) on = !cls[c]; if (on) cls[c] = 1; else delete cls[c]; return on; },
    contains: function (c) { return !!cls[c]; },
  };
  var self = this;
  Object.keys(attrs || {}).forEach(function (k) { self.attrs[k] = String(attrs[k]); });
}
El.prototype = Object.create(Target.prototype);
Object.defineProperty(El.prototype, "textContent", {
  get: function () { return this._text + this.children.map(function (c) { return c.textContent || ""; }).join(""); },
  set: function (v) { this._text = String(v); this.children = []; },
});
Object.defineProperty(El.prototype, "firstChild", { get: function () { return this.children[0] || null; } });
Object.defineProperty(El.prototype, "lastChild", { get: function () { return this.children[this.children.length - 1] || null; } });
El.prototype.getAttribute = function (k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; };
El.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); };
El.prototype.hasAttribute = function (k) { return Object.prototype.hasOwnProperty.call(this.attrs, k); };
El.prototype.removeAttribute = function (k) { delete this.attrs[k]; };
El.prototype.appendChild = function (c) { this.children.push(c); c.parentNode = this; return c; };
El.prototype.insertBefore = function (c, ref) {
  var i = this.children.indexOf(ref);
  if (i < 0) this.children.push(c); else this.children.splice(i, 0, c);
  c.parentNode = this;
  return c;
};
El.prototype.removeChild = function (c) { var i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1); c.parentNode = null; return c; };
El.prototype.remove = function () { if (this.parentNode) this.parentNode.removeChild(this); };
El.prototype.focus = function () {};
El.prototype.scrollIntoView = function () {};
El.prototype.contains = function () { return true; };
El.prototype.closest = function () { return null; };
/* one stub element per selector: the component finds every [data-*] hook it looks for */
El.prototype.querySelector = function (sel) { return this._q[sel] || (this._q[sel] = new El("div", { "data-sel": sel })); };
El.prototype.querySelectorAll = function () { return []; };

var document = new Target();
document.body = new El("body", { "data-ws": "off" });
var __components = [];
document.querySelector = function (sel) {
  if (sel === 'meta[name="csrf-token"]') return { getAttribute: function () { return "csrf-abc"; } };
  return null;
};
document.querySelectorAll = function (sel) { return sel === "[data-component]" ? __components : []; };
document.createElement = function (tag) { return new El(tag); };
document.createElementNS = function (ns, tag) { return new El(tag); };
document.createTextNode = function (t) { return { nodeType: 3, textContent: String(t) }; };

function submit(form) {
  var evt = { target: form, submitter: null, defaultPrevented: false };
  evt.preventDefault = function () { evt.defaultPrevented = true; };
  document.fire("submit", evt, "capture");
  form.fire("submit", evt);
  document.fire("submit", evt, "bubble");
  return evt;
}

var location = { protocol: "http:", host: "192.168.1.20:8000", origin: "http://192.168.1.20:8000",
                 href: "http://192.168.1.20:8000/me/live/1", pathname: "/me/live/1" };
var navigator = { onLine: true, language: "en-US" };
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
var __blobs = 0;
URL.createObjectURL = function () { return "blob:" + (++__blobs); };
URL.revokeObjectURL = function () {};
function Blob(parts, opts) { this.parts = parts; this.type = (opts && opts.type) || ""; }
function Audio() { this.src = ""; this.attrs = {}; }
Audio.prototype.setAttribute = function (k, v) { this.attrs[k] = v; };
Audio.prototype.pause = function () {};
Audio.prototype.play = function () {
  var self = this;
  setTimeout(function () { if (self.onended) self.onended(); }, 500); // every line "plays" for half a second
  return Promise.resolve();
};

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
Res.prototype.blob = function () { return Promise.resolve(new Blob([this._body || ""], { type: "audio/mpeg" })); };
function json(status, obj, headers) {
  var h = { "content-type": "application/json" };
  Object.keys(headers || {}).forEach(function (k) { h[k] = headers[k]; });
  return new Res(status, h, JSON.stringify(obj));
}
function deferred() {
  var d = {};
  d.promise = new Promise(function (ok, fail) { d.resolve = ok; d.reject = fail; });
  return d;
}
function hang() { return deferred().promise; }

var __fetches = [];
var __route = null;
function fetch(url, init) {
  var call = { url: String(url), init: init || {}, aborted: false };
  __fetches.push(call);
  return new Promise(function (ok, fail) {
    var signal = call.init.signal;
    if (signal && signal.aborted) { call.aborted = true; fail(domAbort()); return; }
    if (signal) signal.addEventListener("abort", function () { call.aborted = true; fail(domAbort()); });
    Promise.resolve(__route(call.url, call.init, call)).then(ok, fail);
  });
}

/* ---------------------------------------------------------------- mini test runner */
var __tests = [];
function test(name, fn) { __tests.push({ name: name, fn: fn }); }
function show(v) { try { return JSON.stringify(v); } catch (e) { return String(v); } }
function eq(a, b, msg) { if (a !== b) throw new Error((msg ? msg + ": " : "") + "expected " + show(b) + ", got " + show(a)); }
function ok(v, msg) { if (!v) throw new Error(msg || "expected a truthy value"); }
function capture(p) {
  var box = { settled: false, value: undefined, error: undefined };
  p.then(function (v) { box.settled = true; box.value = v; }, function (e) { box.settled = true; box.error = e; });
  return box;
}

/* ---------------------------------------------------------------- the page */
for (var __i = 0; __i < arguments.length; __i++) load(arguments[__i]);
var IP = window.IP;
var MSG = "/api/live/sessions/1/message";
var EVT = "/api/live/sessions/1/event";
var DISC = "/api/live/sessions/1/discomfort";
var TTS = "/api/tts";
function item(name, reps) {
  return { exercise: name, category: "calisthenics", sets: 2, reps: reps, rest_s: 30, cues: ["Breathe out on the effort"],
           effective: { sets: 2, reps: reps, rest_s: 30 } };
}
var SESSION = { id: 1, status: "active", items: [item("Bodyweight squat", 12), item("Push-up", 10), item("Glute bridge", 15)],
                current_index: 0, total: 3, intensity: 0, intensity_label: "Standard", voice_enabled: true, ended: false,
                stopped_for_safety: false };
var CONFIG = { ai: true, first_name: "Asha", emergency: { number: "112" },
               urls: { message: MSG, event: EVT, discomfort: DISC, end: "/api/live/sessions/1/end", tts: TTS,
                       tts_status: "/api/tts/status" } };
var root = new El("div", { "data-component": "live-session", "data-session": JSON.stringify(SESSION),
                           "data-config": JSON.stringify(CONFIG) });
root.querySelector("[data-chat-input]").setAttribute("placeholder", "Ask Pulse anything");
__components.push(root);

function bodyOf(call) { return JSON.parse(call.init.body); }
function calls(url) { return __fetches.filter(function (c) { return c.url === url; }); }
function lastCall(url) { var c = calls(url); return c[c.length - 1]; }
function reply(text, extra) {
  var body = { reply: text, emotion: "encouraging", actions: [], session: SESSION, source: "ai", intent: null };
  Object.keys(extra || {}).forEach(function (k) { body[k] = extra[k]; });
  return json(200, body);
}
function defaultRoute(url) {
  if (url === TTS) return new Res(200, { "content-type": "audio/mpeg" }, "ID3");
  if (url === EVT) return json(200, { reply: "", cue: "", emotion: "calm", actions: [], session: SESSION });
  return hang();
}
function chat(text) {
  root.querySelector("[data-chat-input]").value = text;
  root.querySelector("[data-chat-form]").fire("submit", {});
}
function clickAct(act) {
  var b = root.querySelector('[data-act="' + act + '"]');
  b.attrs["data-act"] = act;
  root.fire("click", { target: { closest: function (sel) { return sel === "[data-act]" ? b : null; } } });
}
function clickChip(attr, value) {
  var chip = new El("button");
  chip.attrs[attr] = value;
  root.fire("click", { target: { closest: function (sel) { return sel === "[data-part],[data-sev]" ? chip : null; } } });
}
function lines() {
  return root.querySelector("[data-log]").children.map(function (c) { return (c.className || "") + "|" + c.textContent; });
}
function linesOf(kind) { return lines().filter(function (l) { return l.indexOf(kind) !== -1; }); }
function has(list, text) { return list.some(function (l) { return l.indexOf(text) !== -1; }); }
function caption() { return root.querySelector("[data-caption]").textContent; }
function placeholder() { return root.querySelector("[data-chat-input]").getAttribute("placeholder"); }

/* ================================================================ tests (in order: they share one page) */
test("the live session boots", function () {
  __route = defaultRoute;
  document.fire("DOMContentLoaded", {});
  flush();
  ok(IP.ai && IP.liveCoach && IP.liveCoach.chat, "IP.ai + diagnostics");
  eq(IP.liveCoach.chat().inFlight, false);
  eq(IP.liveCoach.chat().paused, 0);
});

test("a chat message goes through IP.ai (X-Page-Id, no ai flag) and the reply is shown and spoken", function () {
  __fetches = [];
  __route = function (url) { return url === MSG ? reply("Keep your chest up.") : defaultRoute(url); };
  chat("how is my form");
  eq(calls(MSG).length, 1);
  var c = calls(MSG)[0];
  eq(c.init.method, "POST");
  eq(c.init.headers["X-Page-Id"], IP.pageId);
  eq(c.init.headers["X-CSRF-Token"], "csrf-abc");
  var b = bodyOf(c);
  eq(b.text, "how is my form");
  eq(b.via, "text");
  eq(b.ai, undefined, "AI chat");
  ok(b.state && b.state.phase, "workout state");
  eq(IP.ai.pending(), 1, "tracked by IP.ai (aborted on pagehide)");
  flush();
  ok(has(linesOf("live-msg-coach"), "Keep your chest up."), show(lines()));
  eq(caption(), "Keep your chest up.");
  eq(calls(TTS).length, 1, "spoken in the natural voice");
  eq(calls(TTS)[0].init.headers["X-Page-Id"], IP.pageId);
  eq(IP.ai.pending(), 0);
  eq(IP.liveCoach.chat().pending, 0);
});

test("one AI message at a time: a voice repeat is dropped, a newer message replaces the one in flight", function () {
  __fetches = [];
  var first = deferred();
  __route = function (url, init) {
    if (url !== MSG) return defaultRoute(url);
    return bodyOf({ init: init }).text === "first question" ? first.promise : reply("Second answer.");
  };
  var before = linesOf("live-msg-coach").length;
  chat("first question");
  flush();
  eq(calls(MSG).length, 1);
  eq(IP.liveCoach.chat().inFlight, true);
  chat("First question!"); // the recogniser heard it twice
  flush();
  eq(calls(MSG).length, 1, "the repeat is dropped");
  chat("second question");
  flush();
  eq(calls(MSG).length, 2);
  ok(calls(MSG)[0].aborted, "the first request was aborted");
  first.resolve(reply("First answer.")); // too late: nobody listens to it any more
  flush();
  var coach = linesOf("live-msg-coach").slice(before);
  eq(coach.length, 1, show(coach));
  ok(coach[0].indexOf("Second answer.") !== -1, show(coach));
  ok(!has(lines(), "didn't catch that"), "no error for the replaced message");
  eq(IP.liveCoach.chat().pending, 0);
});

test("a refused reply is spoken once; AI chat then waits retry_after with ai:false, and comes back", function () {
  __fetches = [];
  var LINE = "Let's keep going - I'll be back to chat in 2 minutes. Commands like 'next' still work.";
  __route = function (url, init) {
    if (url !== MSG) return defaultRoute(url);
    if (bodyOf({ init: init }).ai === false) return reply("", { source: "chat_paused", chat_paused: true, emotion: "neutral" });
    return reply(LINE, { source: "refused", emotion: "calm",
                         refused: { code: "cooldown", detail: "AI is paused on your account for 2 min.", retry_after: 120 } });
  };
  var before = linesOf("live-msg-coach").length;
  chat("how deep should I squat");
  flush();
  eq(linesOf("live-msg-coach").length, before + 1);
  eq(caption(), LINE);
  var left = IP.liveCoach.chat().paused;
  ok(left >= 119 && left <= 120, "paused for " + left);
  chat("tell me about protein");
  flush();
  eq(bodyOf(lastCall(MSG)).ai, false, "no AI while paused - the safety layer still sees the words");
  eq(lastCall(MSG).init.headers["X-Page-Id"], IP.pageId);
  eq(linesOf("live-msg-coach").length, before + 1, "nothing is said again");
  ok(has(linesOf("live-msg-note"), "Chat with Pulse is back in 2 min"), show(linesOf("live-msg-note")));
  chat("next");
  flush();
  eq(bodyOf(lastCall(MSG)).ai, false, "commands still go to the server");
  advance(300); // a tick paints the countdown
  ok(/^Chat is back in 2 min - commands still work$/.test(placeholder()), placeholder());
  advance(121000);
  ok(has(linesOf("live-msg-note"), "Pulse can chat again."), show(linesOf("live-msg-note")));
  eq(placeholder(), "Ask Pulse anything");
  eq(IP.liveCoach.chat().paused, 0);
  __route = function (url) { return url === MSG ? reply("Welcome back.") : defaultRoute(url); };
  chat("what should I do after this");
  flush();
  eq(bodyOf(lastCall(MSG)).ai, undefined, "AI chat again");
});

test("a repeated phrase says nothing; spam is answered but does not pause chat", function () {
  advance(2000);
  __fetches = [];
  __route = function (url, init) {
    if (url !== MSG) return defaultRoute(url);
    if (bodyOf({ init: init }).text === "again please") {
      return reply("", { source: "refused", emotion: "neutral",
                         refused: { code: "duplicate", detail: "You just asked that - give it a moment.", retry_after: 3 } });
    }
    return reply("I didn't catch that.", { source: "refused", emotion: "calm",
                                           refused: { code: "spam", detail: "That looks like spam.", retry_after: 0 } });
  };
  var before = linesOf("live-msg-coach").length;
  var said = caption();
  chat("again please");
  flush();
  eq(linesOf("live-msg-coach").length, before, "no bubble");
  eq(caption(), said, "nothing spoken");
  eq(IP.liveCoach.chat().paused, 0, "no pause for a repeat");
  chat("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa");
  flush();
  eq(linesOf("live-msg-coach").length, before + 1);
  eq(caption(), "I didn't catch that.");
  eq(IP.liveCoach.chat().paused, 0, "no pause for spam");
});

test("easier / harder: one step per 1.5 s, however fast the buttons are tapped", function () {
  advance(2000);
  __fetches = [];
  __route = function (url) { return url === MSG ? reply("Okay, adjusting.", { source: "rule" }) : defaultRoute(url); };
  clickAct("easier");
  clickAct("easier");
  clickAct("harder");
  flush();
  eq(calls(MSG).length, 1);
  eq(bodyOf(calls(MSG)[0]).text, "easier");
  advance(1400);
  clickAct("harder");
  flush();
  eq(calls(MSG).length, 1, "still within 1.5 s");
  advance(200);
  clickAct("harder");
  flush();
  eq(calls(MSG).length, 2);
  eq(bodyOf(calls(MSG)[1]).text, "harder");
  ok(has(linesOf("live-msg-note"), "Intensity"), "shown as a note, not as chat");
});

test("voice: a 429 means the browser voice until Retry-After; downloaded lines still play", function () {
  advance(5000);
  __fetches = [];
  var n = 0;
  var refuse = true;
  __route = function (url) {
    if (url === TTS) {
      if (refuse) return json(429, { code: "quota", retry_after: 30, fallback: "browser" }, { "Retry-After": "30" });
      return new Res(200, { "content-type": "audio/mpeg" }, "ID3");
    }
    return url === MSG ? reply("Voice line " + (++n) + ".") : defaultRoute(url);
  };
  chat("question a");
  flush();
  eq(calls(TTS).length, 1);
  eq(calls(TTS)[0].init.headers["X-Page-Id"], IP.pageId);
  eq(caption(), "Voice line 1.", "the browser voice says it instead");
  advance(1000);
  chat("question b");
  flush();
  eq(calls(TTS).length, 1, "no voice requests while Retry-After runs");
  eq(caption(), "Voice line 2.");
  advance(31000);
  refuse = false;
  chat("question c");
  flush();
  eq(calls(TTS).length, 2, "after Retry-After the natural voice is asked again");
});

test("AI paused page-wide: discomfort reports and chat (ai:false) still reach the server, the voice waits", function () {
  advance(5000);
  __fetches = [];
  __route = function (url) {
    if (url === "/api/assistant/chat") {
      return json(429, { error: "AI paused", detail: "AI is paused on your account for 5 min.", code: "cooldown",
                         retry_after: 300, ai_available: true }, { "Retry-After": "300" });
    }
    if (url === DISC) {
      return json(200, { reply: "Sorry to hear that - let's swap that exercise.", emotion: "empathetic",
                         actions: [{ type: "pause" }], session: SESSION, source: "rule" });
    }
    if (url === MSG) return reply("", { source: "chat_paused", chat_paused: true, emotion: "neutral" });
    return defaultRoute(url);
  };
  var refused = capture(IP.ai.request("/api/assistant/chat", { json: { message: "hi" }, feature: "assistant" }));
  flush();
  eq(refused.error && refused.error.code, "cooldown");
  ok(IP.ai.cooldownLeft("live_coach") >= 299 && IP.ai.cooldownLeft("tts") >= 299, "every AI feature waits");
  chat("tell me about recovery");
  flush();
  eq(calls(MSG).length, 1, "not refused locally");
  eq(bodyOf(calls(MSG)[0]).ai, false);
  clickAct("discomfort");
  clickChip("data-part", "knee");
  clickAct("report");
  flush();
  eq(calls(DISC).length, 1, "the safety report went out");
  eq(bodyOf(calls(DISC)[0]).body_part, "knee");
  eq(calls(DISC)[0].init.headers["X-Page-Id"], IP.pageId);
  ok(has(linesOf("live-msg-coach"), "Sorry to hear that"), show(lines()));
  eq(calls(TTS).length, 0, "no voice requests while AI is paused");
  eq(IP.ai.abortAll("test"), 0, "nothing abortable left over");
  advance(301000);
  eq(IP.ai.cooldownLeft("live_coach"), 0);
});

test("a network rate limit (429, no code): chat is held, core commands run here, health words still go out", function () {
  __fetches = [];
  __route = defaultRoute;
  clickAct("begin");
  flush();
  ok(IP.liveCoach.state.begun, "the workout runs");
  __route = function (url, init) {
    if (url !== MSG) return defaultRoute(url);
    if (bodyOf({ init: init }).ai === false) return reply("Sorry to hear that - let's ease off.", { source: "rule" });
    return json(429, { error: "Slow down", detail: "Too many requests. Please wait a moment and try again." },
                { "Retry-After": "30" });
  };
  chat("tell me a joke");
  flush();
  eq(calls(MSG).length, 1);
  ok(IP.liveCoach.chat().gate >= 29, "gate " + IP.liveCoach.chat().gate);
  ok(has(linesOf("live-msg-coach"), "catching my breath. Try again in 30 seconds."), show(lines()));
  chat("tell me another joke");
  flush();
  eq(calls(MSG).length, 1, "held: nothing more from this network");
  ok(has(linesOf("live-msg-note"), "Lots of messages from this network"), show(linesOf("live-msg-note")));
  var events = calls(EVT).length;
  chat("next");
  flush();
  eq(calls(MSG).length, 1, "next ran locally");
  ok(calls(EVT).length > events && bodyOf(lastCall(EVT)).type === "skipped", "through the progress event");
  chat("my knee hurts");
  flush();
  eq(calls(MSG).length, 2, "a possible health problem is still sent");
  eq(bodyOf(lastCall(MSG)).ai, false);
  advance(31000);
  eq(IP.liveCoach.chat().gate, 0);
});

test("pagehide aborts the message in flight and pauses the workout (keepalive) - but not when signing out", function () {
  __fetches = [];
  __route = function (url) { return url === MSG ? hang() : defaultRoute(url); };
  ok(["ready", "work", "rest"].indexOf(IP.liveCoach.state.phase) !== -1, IP.liveCoach.state.phase);
  submit(new El("form", { method: "post", action: "/auth/logout" })); // signing out ...
  window.fire("pagehide", { persisted: false }); // ... unloads the page: the session cookie is already gone
  flush();
  eq(calls(EVT).length, 0, "no request that would fail CSRF");
  window.fire("pageshow", { persisted: true }); // the sign-out did not happen after all (bfcache restore)
  flush();
  ok(["ready", "work", "rest"].indexOf(IP.liveCoach.state.phase) !== -1, "still running");
  chat("are you still there");
  flush();
  eq(IP.liveCoach.chat().inFlight, true);
  var pendingMsg = lastCall(MSG);
  window.fire("pagehide", { persisted: true });
  flush();
  ok(pendingMsg.aborted, "the AI message was aborted");
  var paused = calls(EVT).filter(function (c) { return bodyOf(c).type === "paused"; });
  eq(paused.length, 1);
  eq(paused[0].init.keepalive, true);
  eq(bodyOf(paused[0]).state.phase, "paused");
  eq(IP.liveCoach.state.phase, "paused");
  eq(IP.liveCoach.chat().inFlight, false);
});

test("helpers: repeat check, health words, local commands, spoken waits, sign-out forms", function () {
  var u = IP.liveCoachUtil;
  eq(u.normText("  What muscles   does this work?! "), "what muscles does this work");
  ["my chest feels tight", "I feel dizzy", "ouch", "I can't breathe", "my knee hurts", "I think I'm going to faint",
   "something feels wrong", "help"].forEach(function (t) { ok(u.looksUnsafe(t), t); });
  ["tell me a joke", "next", "what's next", "how many reps"].forEach(function (t) { ok(!u.looksUnsafe(t), t); });
  eq(u.gateCommand("Next!"), "next");
  eq(u.gateCommand("hold on"), "pause");
  eq(u.gateCommand("let's go"), "resume");
  eq(u.gateCommand("done"), "done");
  eq(u.gateCommand("tell me a joke"), null);
  eq(u.spokenWait(45), "45 seconds");
  eq(u.spokenWait(120), "2 minutes");
  eq(u.spokenWait(3600), "an hour");
  eq(u.spokenWait(86400), "24 hours");
  ok(u.isLogoutForm(new El("form", { action: "/auth/logout" })));
  ok(!u.isLogoutForm(new El("form", { action: "/account" })));
  ok(!u.isLogoutForm(new El("div", { action: "/auth/logout" })));
});

/* ---------------------------------------------------------------- run */
var __passed = 0;
var __failed = 0;
__tests.forEach(function (t) {
  __errors = [];
  try {
    t.fn();
    if (__errors.length) throw new Error("console.error: " + __errors.join(" | "));
    __passed++;
    print("PASS " + t.name);
  } catch (e) {
    __failed++;
    print("FAIL " + t.name + ": " + (e && e.message ? e.message : e) + (e && e.stack ? "\n" + e.stack : ""));
  }
});
print("SUMMARY passed=" + __passed + " failed=" + __failed + " total=" + __tests.length);
"""
