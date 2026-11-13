"""The live coach hands refused replies to IP.ai (app/static/src/js/55-live-coach.js + 01-ai.js).

A reply refused by the AI guard or queue arrives as HTTP 200 with ``refused: {code, detail, retry_after}``. The live
coach records it with ``IP.ai.noteRefusal("live_coach", code, retry_after)``, which has the same effect as a 429 with
that code:

* the wait is IP.ai's "live_coach" cooldown and the coach keeps no pause timer of its own: AI chat sends
  ``"ai": false`` meanwhile, any other live_coach AI request of the page is refused locally, and chat says once that
  it is back when the wait ends - whoever started it, another AI widget of the page included;
* code "cooldown" (AI paused for the account) pauses every AI widget of the page and the coach's natural voice;
* a repeated phrase, or a refusal without a wait, records nothing;
* a refusal inside a reply is never taken for the network's rate limit (HTTP 429 without a code), which still holds
  chat as before.

The real ``00-core.js`` + ``01-ai.js`` + ``55-live-coach.js`` run in JavaScriptCore inside the stub browser of
tests/test_live_coach_client.py (copied, so each suite can change on its own). Skipped where no ``jsc`` binary exists
(``JSC=/path/to/jsc`` points at one); the static checks always run.
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


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _code(path: Path) -> str:
    """The file without comments (rjsmin understands strings and regex literals)."""
    import rjsmin

    return rjsmin.jsmin(_read(path), keep_bang_comments=False)


def _jsc() -> str | None:
    for candidate in (os.environ.get("JSC"), shutil.which("jsc"), MAC_JSC):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


needs_jsc = pytest.mark.skipif(_jsc() is None, reason="JavaScriptCore (jsc) is not available on this machine")


# =============================================================================================== static checks
def test_refused_replies_go_to_ip_ai_and_the_coach_keeps_no_pause_timer():
    src = _read(LIVE)
    assert 'IP.ai.noteRefusal("live_coach", refused.code, refused.retry_after)' in src
    pause = src[src.index("function chatPauseLeft() {"):src.index("function holdChat(refused) {")]
    assert 'return cooldownLeft("live_coach");' in pause and "Date.now()" not in pause
    code = _code(LIVE)
    for gone in ("chatUntil", "chatTimer", "pauseChat", "chatBack"):
        assert gone not in code, f"{gone}: the chat pause is IP.ai's cooldown - no private timer"


def test_served_bundle_hands_refusals_to_ip_ai():
    markers = ('noteRefusal("live_coach",', "noteRefusal:")
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
    harness = tmp_path / "live_refusals_harness.js"
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
    assert passed == total >= 8, out[-3000:]


@needs_jsc
def test_live_coach_refusal_hand_off_in_javascriptcore(tmp_path):
    _assert_all_passed(_run(tmp_path, CORE, AI, LIVE))


@needs_jsc
def test_minified_bundle_hands_refusals_off_the_same_way(tmp_path):
    import rjsmin

    bundle = "\n".join(f"/* {p.name} */\n{_read(p)}\n;" for p in (CORE, AI, LIVE))  # as scripts/build_assets.py joins
    minified = tmp_path / "live.min.js"
    minified.write_text(rjsmin.jsmin(bundle, keep_bang_comments=False), encoding="utf-8")
    _assert_all_passed(_run(tmp_path, minified))


HARNESS = r"""/* A tiny browser for 00-core.js + 01-ai.js + 55-live-coach.js in JavaScriptCore: virtual clock + timers, event
 * targets, DOM element stubs (a live-session root whose querySelector hands out one stub per selector), fetch with
 * scripted responses, AbortController, URL, Blob, Audio. Prints PASS/FAIL lines and a SUMMARY line.
 * Usage: jsc live_refusals_harness.js -- <core.js> <ai.js> <live.js>   (or one minified bundle holding all three)
 * The stub browser of tests/test_live_coach_client.py; the tests below are about refused replies.
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
var BACK = "Pulse can chat again.";
var cooldownEvents = []; // every "ai.cooldown" event of the page
IP.on("ai.cooldown", function (e) { cooldownEvents.push(e); });
function notesSaying(text) {
  return linesOf("live-msg-note").filter(function (l) { return l.indexOf(text) !== -1; }).length;
}
/** The AI reply is refused with `refused`; messages sent with "ai": false get `plain` (default: nothing to say). */
function refusingRoute(line, refused, plain) {
  return function (url, init) {
    if (url !== MSG) return defaultRoute(url);
    if (bodyOf({ init: init }).ai === false) {
      return plain ? plain() : reply("", { source: "chat_paused", chat_paused: true, emotion: "neutral" });
    }
    return reply(line, { source: refused.code === "busy" ? "fallback" : "refused", emotion: "calm", refused: refused });
  };
}

test("the live session boots, with IP.ai.noteRefusal on the page", function () {
  __route = defaultRoute;
  document.fire("DOMContentLoaded", {});
  flush();
  ok(IP.liveCoach && IP.liveCoach.chat, "diagnostics");
  eq(typeof IP.ai.noteRefusal, "function");
  eq(IP.liveCoach.chat().paused, 0);
  eq(IP.liveCoach.chat().gate, 0);
});

test("a refused reply goes to IP.ai like a 429: the coach's chat waits, the rest of the page does not", function () {
  __fetches = [];
  var LINE = "Let's keep going - I'll be back to chat in a minute.";
  __route = refusingRoute(LINE, { code: "quota", detail: "You've used your AI chat for this minute.", retry_after: 50 });
  var before = cooldownEvents.length;
  chat("what's a good warm-up");
  flush();
  eq(caption(), LINE, "the refusal is spoken");
  eq(cooldownEvents.length, before + 1, "one ai.cooldown event for the page");
  var evt = cooldownEvents[before];
  eq(evt.feature, "live_coach");
  eq(evt.code, "quota");
  eq(evt.seconds, 50);
  eq(IP.ai.cooldownLeft("live_coach"), 50, "IP.ai keeps the wait");
  eq(IP.liveCoach.chat().paused, 50);
  eq(IP.liveCoach.chat().gate, 0, "an AI refusal is not the network's rate limit");
  eq(IP.ai.cooldownLeft("assistant"), 0, "a chat quota pauses no other feature");
  eq(IP.ai.cooldownLeft("tts"), 0);
  ok(calls(TTS).length >= 1, "the natural voice still speaks");
  chat("and a cool-down?");
  flush();
  eq(bodyOf(lastCall(MSG)).ai, false, "no AI while paused - commands and safety checks still run on the server");
  var n = __fetches.length;
  var direct = capture(IP.ai.request(MSG, { json: { text: "x" }, feature: "live_coach" }));
  flush();
  eq(__fetches.length, n, "any other live_coach AI request of the page is refused locally");
  eq(direct.error.code, "cooldown_local");
  eq(direct.error.cooldownCode, "quota");
});

test("chat is back when IP.ai's wait ends, and says so once", function () {
  var back = notesSaying(BACK);
  advance(49000);
  eq(IP.liveCoach.chat().paused, 1);
  ok(/^Chat is back in [12] s - commands still work$/.test(placeholder()), placeholder());
  eq(notesSaying(BACK), back, "not yet");
  advance(1500);
  eq(IP.liveCoach.chat().paused, 0);
  eq(placeholder(), "Ask Pulse anything");
  eq(notesSaying(BACK), back + 1);
  advance(60000);
  eq(notesSaying(BACK), back + 1, "said once");
  __route = function (url) { return url === MSG ? reply("Try leg swings.") : defaultRoute(url); };
  chat("what else for a warm-up");
  flush();
  eq(bodyOf(lastCall(MSG)).ai, undefined, "AI chat again");
});

test("code 'cooldown' in a reply pauses every AI widget of the page and the natural voice", function () {
  advance(5000);
  __fetches = [];
  var LINE = "Let's keep going - I'll be back to chat in 5 minutes.";
  __route = refusingRoute(LINE, { code: "cooldown", detail: "AI is paused on your account for 5 min.", retry_after: 300 },
                          function () { return reply("Okay, moving on.", { source: "rule" }); });
  var seen = [];
  var off = IP.ai.onCooldown(function (info) { seen.push(info); });
  var back = notesSaying(BACK);
  chat("tell me about creatine");
  flush();
  eq(caption(), LINE, "the refusal is still said ...");
  eq(calls(TTS).length, 0, "... in the browser voice: the account's voice requests would be refused as well");
  eq(IP.ai.cooldownLeft(), 300, "AI is paused for the whole page");
  eq(IP.ai.cooldownLeft("assistant"), 300);
  eq(IP.ai.cooldownLeft("tts"), 300);
  ok(seen.some(function (i) { return !i.ended && i.feature === "live_coach" && i.code === "cooldown"; }),
     "the page's widgets were told");
  var n = __fetches.length;
  var other = capture(IP.ai.request("/api/assistant/chat", { json: { message: "hi" }, feature: "assistant" }));
  flush();
  eq(__fetches.length, n, "another AI widget of the page asks nothing");
  eq(other.error.code, "cooldown_local");
  eq(other.error.cooldownCode, "cooldown");
  eq(IP.ai.describeError(other.error), "AI is paused for your account after too many requests. Try again in 5 min.");
  chat("next");
  flush();
  eq(bodyOf(lastCall(MSG)).ai, false, "the coach goes on without the AI");
  eq(caption(), "Okay, moving on.");
  eq(calls(TTS).length, 0, "no voice requests while AI is paused");
  advance(300500);
  eq(IP.ai.cooldownLeft(), 0);
  eq(notesSaying(BACK), back + 1, "two waits ended (the coach's and the account's): said once");
  __route = function (url) { return url === MSG ? reply("Creatine is well studied.") : defaultRoute(url); };
  chat("tell me about creatine");
  flush();
  eq(bodyOf(lastCall(MSG)).ai, undefined);
  eq(calls(TTS).length, 1, "the natural voice is back");
  off();
});

test("an account pause started by another AI widget holds chat too, and chat says when it is back", function () {
  advance(5000);
  __fetches = [];
  __route = function (url) {
    if (url === "/api/assistant/chat") {
      return json(429, { error: "AI paused", detail: "AI is paused on your account for 2 min.", code: "cooldown",
                         retry_after: 120, ai_available: true }, { "Retry-After": "120" });
    }
    if (url === MSG) return reply("", { source: "chat_paused", chat_paused: true, emotion: "neutral" });
    return defaultRoute(url);
  };
  var back = notesSaying(BACK);
  capture(IP.ai.request("/api/assistant/chat", { json: { message: "hi" }, feature: "assistant" }));
  flush();
  eq(IP.liveCoach.chat().paused, 120);
  eq(IP.liveCoach.chat().gate, 0);
  advance(300);
  ok(/^Chat is back in 2 min - commands still work$/.test(placeholder()), placeholder());
  chat("how many sets are left");
  flush();
  eq(bodyOf(lastCall(MSG)).ai, false);
  advance(120000);
  eq(IP.liveCoach.chat().paused, 0);
  eq(placeholder(), "Ask Pulse anything");
  eq(notesSaying(BACK), back + 1, "the coach says chat is back, whoever paused it");
});

test("a repeated phrase and a refusal without a wait record nothing", function () {
  advance(5000);
  __fetches = [];
  __route = function (url, init) {
    if (url !== MSG) return defaultRoute(url);
    if (bodyOf({ init: init }).text === "again please") {
      return reply("", { source: "refused", emotion: "neutral",
                         refused: { code: "duplicate", detail: "You just asked that - give it a moment.", retry_after: 4 } });
    }
    return reply("I didn't catch that.", { source: "refused", emotion: "calm",
                                           refused: { code: "spam", detail: "That looks like spam.", retry_after: 0 } });
  };
  var before = cooldownEvents.length;
  chat("again please");
  flush();
  chat("zzzzzzzzzzzzzzzzzzzzzzzzzzzz");
  flush();
  eq(calls(MSG).length, 2);
  eq(cooldownEvents.length, before, "no ai.cooldown: the first answer is on its way, and spam asks for no wait");
  eq(IP.ai.cooldownLeft("live_coach"), 0);
  eq(IP.liveCoach.chat().paused, 0);
  eq(caption(), "I didn't catch that.");
});

test("a refusal inside a reply is never taken for the network's rate limit - not even without a code", function () {
  advance(5000);
  __fetches = [];
  var refused = { code: "busy", detail: "The AI is very busy right now.", retry_after: 20 };
  __route = function (url, init) {
    return refusingRoute("Busy moment - keep going, you're doing great.", refused)(url, init); // reads `refused` now
  };
  chat("motivate me");
  flush();
  eq(IP.liveCoach.chat().gate, 0);
  eq(IP.ai.cooldownLeft("live_coach"), 20);
  chat("tell me a joke");
  flush();
  eq(calls(MSG).length, 2, "sent (the server runs commands and safety checks), not held as if rate limited");
  eq(bodyOf(lastCall(MSG)).ai, false);
  advance(21000);
  refused = { detail: "Please wait.", retry_after: 15 }; // a malformed refusal: no code
  chat("hello there");
  flush();
  eq(calls(MSG).length, 3);
  eq(IP.liveCoach.chat().gate, 0, "still not the network's limit");
  eq(IP.ai.cooldownLeft("live_coach"), 15, "but its wait is kept");
  advance(16000);
  eq(IP.liveCoach.chat().paused, 0);
});

test("the network's rate limit (429 without a code) still holds chat, and chat says when it is back", function () {
  advance(5000);
  __fetches = [];
  __route = function (url) {
    if (url !== MSG) return defaultRoute(url);
    return json(429, { error: "Slow down", detail: "Too many requests. Please wait a moment and try again." },
                { "Retry-After": "30" });
  };
  var back = notesSaying(BACK);
  chat("tell me a joke");
  flush();
  ok(IP.liveCoach.chat().gate >= 29, "gate " + IP.liveCoach.chat().gate);
  ok(has(linesOf("live-msg-coach"), "catching my breath"), show(lines()));
  chat("tell me another joke");
  flush();
  eq(calls(MSG).length, 1, "held: nothing more from this network");
  advance(31000);
  eq(IP.liveCoach.chat().gate, 0);
  eq(IP.liveCoach.chat().paused, 0);
  eq(notesSaying(BACK), back + 1);
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
