"""Behaviour of the two streamed Q&A widgets with the AI guard and queue (docs/ai-guard-and-queue.md section 7):

* ``app/static/src/js/08-ask-ai.js`` (plan questions, ``IP.ai.stream`` with feature "plans"): answers stream in; a
  ``blocked`` event shows its detail in the answer box, and the ask button and suggestions stay disabled - with a
  countdown - while ``IP.ai.cooldownLeft("plans") > 0`` (also when another widget paused AI); leaving the page aborts
  silently; errors go through ``IP.ai.describeError``.
* ``app/static/src/js/80-shop.js`` "shop-ask" (product questions, feature "shop"): the JSON endpoint is a second try
  only for a broken transport (network failure, a 5xx without a refusal code, a JSON reply where a stream was
  expected) and goes through ``IP.ai.request``; never after a refusal (401/403/404/409/422/429, a local cooldown, a
  ``blocked`` event), a timeout or leaving the page.

The real sources run in JavaScriptCore inside a small stub browser (virtual clock and timers, fetch with streaming
bodies, a mini DOM). Skipped where no ``jsc`` binary exists; ``JSC=/path/to/jsc`` points at one.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
JS_SRC = ROOT / "app" / "static" / "src" / "js"
SOURCES = [JS_SRC / name for name in ("00-core.js", "01-ai.js", "08-ask-ai.js", "80-shop.js")]
MAC_JSC = "/System/Library/Frameworks/JavaScriptCore.framework/Versions/Current/Helpers/jsc"


def _jsc() -> str | None:
    for candidate in (os.environ.get("JSC"), shutil.which("jsc"), MAC_JSC):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


needs_jsc = pytest.mark.skipif(_jsc() is None, reason="JavaScriptCore (jsc) is not available on this machine")


def _run(tmp_path: Path, *scripts: Path) -> str:
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
    assert passed == total >= 15, out[-3000:]


@needs_jsc
def test_ask_widgets_behaviour_in_javascriptcore(tmp_path):
    _assert_all_passed(_run(tmp_path, *SOURCES))


@needs_jsc
def test_minified_ask_widgets_behave_the_same(tmp_path):
    import rjsmin

    bundle = "\n".join(f"/* {p.name} */\n{p.read_text(encoding='utf-8')}\n;" for p in SOURCES)  # as build_assets joins
    minified = tmp_path / "widgets.min.js"
    minified.write_text(rjsmin.jsmin(bundle, keep_bang_comments=False), encoding="utf-8")
    _assert_all_passed(_run(tmp_path, minified))


def test_widgets_use_the_shared_ai_client():
    plans = (JS_SRC / "08-ask-ai.js").read_text(encoding="utf-8")
    shop = (JS_SRC / "80-shop.js").read_text(encoding="utf-8")
    ask = shop[shop.index('IP.register("shop-ask"'):shop.index('IP.register("shop-checkout"')]
    assert "fetch(" not in plans and "fetch(" not in ask  # IP.ai.stream / IP.ai.request only (X-Page-Id, cooldowns)
    assert 'IP.ai.stream(endpoint' in plans and 'feature: FEATURE' in plans and 'var FEATURE = "plans"' in plans
    assert "IP.ai.stream(streamUrl" in ask and "IP.ai.request(jsonUrl" in ask and 'var ASK_FEATURE = "shop"' in shop
    for code in (plans, ask):
        assert "innerHTML" not in code and "eval(" not in code and ".style" not in code


# =============================================================================================== JavaScriptCore harness
HARNESS = r"""/* Stub browser for 00-core.js + 01-ai.js + 08-ask-ai.js + 80-shop.js. Usage: jsc harness.js -- <file>... */
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

/* ---------------------------------------------------------------- a mini DOM */
function TextNode(t) { this.nodeType = 3; this.parentNode = null; this._t = String(t); }
Object.defineProperty(TextNode.prototype, "textContent", {
  get: function () { return this._t; },
  set: function (v) { this._t = String(v); },
});

function matchCompound(el, sel) {
  if (!el || el.nodeType !== 1) return false;
  var rest = sel;
  var m = /^([a-zA-Z][a-zA-Z0-9-]*)/.exec(rest);
  if (m) {
    if (el.tagName !== m[1].toUpperCase()) return false;
    rest = rest.slice(m[0].length);
  }
  while (rest.length) {
    var a = /^\[([a-zA-Z0-9_-]+)(?:(~?=)(?:"([^"]*)"|'([^']*)'|([^\]]*)))?\]/.exec(rest);
    if (a) {
      var name = a[1];
      var op = a[2];
      var val = a[3] != null ? a[3] : a[4] != null ? a[4] : a[5];
      if (!el.hasAttribute(name)) return false;
      if (op === "=" && el.getAttribute(name) !== val) return false;
      if (op === "~=" && el.getAttribute(name).split(/\s+/).indexOf(val) === -1) return false;
      rest = rest.slice(a[0].length);
      continue;
    }
    var n = /^:not\(\[([a-zA-Z0-9_-]+)\]\)/.exec(rest);
    if (n) {
      if (el.hasAttribute(n[1])) return false;
      rest = rest.slice(n[0].length);
      continue;
    }
    throw new Error("selector not supported by the stub DOM: " + sel);
  }
  return true;
}
function matches(el, selector) {
  return String(selector).split(",").some(function (part) { return matchCompound(el, part.trim()); });
}

function El(tag, attrs) {
  Target.call(this);
  this.tagName = String(tag).toUpperCase();
  this.nodeType = 1;
  this.attrs = {};
  this.children = [];
  this.parentNode = null;
  this.value = "";
  this.scrollTop = 0;
  this.scrollHeight = 480;
  this.offsetWidth = 0;
  var self = this;
  Object.keys(attrs || {}).forEach(function (k) { self.attrs[k] = String(attrs[k]); });
}
El.prototype = Object.create(Target.prototype);
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
El.prototype.remove = function () { if (this.parentNode) this.parentNode.removeChild(this); };
El.prototype.focus = function () { document.activeElement = this; };
El.prototype.elements = function () {
  var out = [];
  (function walk(n) { n.children.forEach(function (c) { if (c.nodeType === 1) { out.push(c); walk(c); } }); })(this);
  return out;
};
El.prototype.querySelectorAll = function (sel) { return this.elements().filter(function (e) { return matches(e, sel); }); };
El.prototype.querySelector = function (sel) { return this.querySelectorAll(sel)[0] || null; };
El.prototype.closest = function (sel) {
  for (var n = this; n && n.nodeType === 1; n = n.parentNode) if (matches(n, sel)) return n;
  return null;
};
Object.defineProperty(El.prototype, "textContent", {
  get: function () { return this.children.map(function (c) { return c.textContent; }).join(""); },
  set: function (v) {
    this.children.forEach(function (c) { c.parentNode = null; });
    this.children = [];
    if (v !== "" && v != null) this.appendChild(new TextNode(v));
  },
});
Object.defineProperty(El.prototype, "className", {
  get: function () { return this.getAttribute("class") || ""; },
  set: function (v) { this.setAttribute("class", v); },
});
["hidden", "disabled"].forEach(function (prop) {
  Object.defineProperty(El.prototype, prop, {
    get: function () { return this.hasAttribute(prop); },
    set: function (v) { if (v) this.setAttribute(prop, ""); else this.removeAttribute(prop); },
  });
});
Object.defineProperty(El.prototype, "classList", {
  get: function () {
    var el = this;
    function list() { return el.className.split(/\s+/).filter(Boolean); }
    return {
      add: function () { var l = list(); for (var i = 0; i < arguments.length; i++) if (l.indexOf(arguments[i]) === -1) l.push(arguments[i]); el.className = l.join(" "); },
      remove: function () { var drop = Array.prototype.slice.call(arguments); el.className = list().filter(function (c) { return drop.indexOf(c) === -1; }).join(" "); },
      contains: function (c) { return list().indexOf(c) !== -1; },
      toggle: function (c) { if (list().indexOf(c) === -1) { this.add(c); return true; } this.remove(c); return false; },
    };
  },
});

var document = new Target();
document.documentElement = new El("html");
document.head = document.documentElement.appendChild(new El("head"));
document.head.appendChild(new El("meta", { name: "csrf-token", content: "csrf-abc" }));
document.body = document.documentElement.appendChild(new El("body", { "data-ws": "off", "data-role": "customer" }));
document.activeElement = null;
document.querySelector = function (sel) { return document.documentElement.querySelector(sel); };
document.querySelectorAll = function (sel) { return document.documentElement.querySelectorAll(sel); };
document.createElement = function (tag) { return new El(tag); };
document.createElementNS = function (ns, tag) { return new El(tag); };
document.createTextNode = function (t) { return new TextNode(t); };

function el(tag, attrs, children) {
  var e = new El(tag, attrs);
  (children || []).forEach(function (c) { e.appendChild(typeof c === "string" ? new TextNode(c) : c); });
  return e;
}
/** Dispatch like a browser: document capture listeners, the form's own, then document bubble listeners. */
function submit(form) {
  var evt = { target: form, submitter: null, defaultPrevented: false };
  evt.preventDefault = function () { evt.defaultPrevented = true; };
  document.fire("submit", evt, "capture");
  form.fire("submit", evt);
  document.fire("submit", evt, "bubble");
  return evt;
}
function click(node) { node.fire("click", { target: node }); }

/* ---------------------------------------------------------------- browser globals */
var __navigations = [];
var location = {
  protocol: "http:", host: "testserver", origin: "http://testserver", pathname: "/shop/p/7/creatine", search: "", hash: "",
  get href() { return this.origin + this.pathname + this.search + this.hash; },
  set href(v) { __navigations.push(String(v)); },
};
var navigator = { onLine: true };
var crypto = { getRandomValues: function (a) { for (var i = 0; i < a.length; i++) a[i] = (i * 29 + 3) & 255; return a; } };

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
  return new Res(status, h, JSON.stringify(obj));
}
function Body() { this.queue = []; this.waiting = null; this.failed = null; }
Body.prototype._offer = function (item) {
  if (this.waiting) { var w = this.waiting; this.waiting = null; w.ok(item); } else this.queue.push(item);
};
Body.prototype.push = function (str) { this._offer({ done: false, value: bytes(str) }); };
Body.prototype.event = function (obj) { this.push("data: " + JSON.stringify(obj) + "\n\n"); };
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
    cancel: function () { return Promise.resolve(); },
    releaseLock: function () {},
  };
};
function sse(body) { return new Res(200, { "content-type": "text/event-stream; charset=utf-8" }, body); }
function hang() { return new Promise(function () {}); }

var __fetches = [];
var __route = null;
function fetch(url, init) {
  var call = { url: String(url), init: init || {}, res: null };
  __fetches.push(call);
  return new Promise(function (ok, fail) {
    var signal = call.init.signal;
    if (signal && signal.aborted) { fail(domAbort()); return; }
    if (signal) {
      signal.addEventListener("abort", function () {
        fail(domAbort());
        if (call.res && call.res.body && call.res.body.fail) call.res.body.fail(domAbort());
      });
    }
    Promise.resolve(__route(call.url, call.init, call)).then(function (res) { call.res = res; ok(res); }, fail);
  });
}

/* ---------------------------------------------------------------- mini test runner */
var __tests = [];
function test(name, fn) { __tests.push({ name: name, fn: fn }); }
function show(v) { try { return JSON.stringify(v); } catch (e) { return String(v); } }
function eq(a, b, msg) { if (a !== b) throw new Error((msg ? msg + ": " : "") + "expected " + show(b) + ", got " + show(a)); }
function ok(v, msg) { if (!v) throw new Error(msg || "expected a truthy value"); }
function has(text, part, msg) { if (String(text).indexOf(part) === -1) throw new Error((msg ? msg + ": " : "") + show(text) + " does not contain " + show(part)); }

for (var __i = 0; __i < arguments.length; __i++) load(arguments[__i]);
var IP = window.IP;
document.fire("DOMContentLoaded", {});

/* ---------------------------------------------------------------- the widgets' markup (as the templates render it) */
function plansBox() {
  var provider = el("span", { "data-ask-provider": "" }, ["Gemma (gateway)"]);
  var suggest = el("button", { type: "button", class: "chip", "data-ask-suggest": "Which plan fits weight loss?" }, ["Which plan fits weight loss?"]);
  var input = el("input", { id: "ask-api-plans-ask", name: "question", "data-ask-input": "" });
  var button = el("button", { class: "btn btn-primary btn-icon shrink-0", "aria-label": "Ask" });
  var form = el("form", { class: "mt-4 flex items-end gap-2", "data-ask-form": "", "data-no-busy": "" }, [input, button]);
  var wait = el("p", { class: "mt-2 text-xs", "data-ask-wait": "", hidden: "" });
  var text = el("p", { class: "whitespace-pre-line", "data-ask-text": "" });
  var typing = el("span", { "data-ask-typing": "", hidden: "" });
  var note = el("p", { "data-ask-note": "", hidden: "" });
  var box = el("div", { class: "mt-4 hidden rounded-2xl", "data-ask-answer": "", "aria-live": "polite" }, [text, typing, note]);
  var root = el("div", { "data-component": "ask-ai", "data-endpoint": "/api/plans/ask", "data-extra": '{"gym_slug":"pulse-central"}' },
    [el("div", {}, [suggest]), form, wait, box]);
  var section = el("section", { class: "card", "aria-label": "Ask AI" }, [el("div", {}, [el("p", {}, [provider])]), root]);
  document.body.appendChild(section);
  IP.boot(section);
  return { section: section, provider: provider, suggest: suggest, input: input, button: button, form: form, wait: wait,
           text: text, typing: typing, note: note, box: box };
}

function shopBox() {
  var log = el("div", { class: "shop-ai-log", "data-ask-log": "", "aria-live": "polite" },
    [el("div", { class: "shop-bubble shop-bubble-ai" }, ["Hi Asha! Ask me anything about Creatine."])]);
  var idea = el("button", { type: "button", class: "shop-idea", "data-ask-idea": "" }, ["How should I take it?"]);
  var input = el("input", { id: "ask-q", name: "question", "data-ask-input": "" });
  var send = el("button", { type: "submit", class: "btn btn-primary btn-icon", "aria-label": "Send question", "data-ask-send": "" });
  var form = el("form", { class: "mt-4 flex items-center gap-2", "data-ask-form": "", action: "#ask", "data-no-busy": "" }, [input, send]);
  var wait = el("p", { class: "mt-2 text-xs", "data-ask-wait": "", hidden: "" });
  var count = el("span", { "data-ask-count": "" }, ["0"]);
  var root = el("div", { class: "relative mt-5", "data-component": "shop-ask", "data-stream-url": "/api/shop/products/7/ask/stream",
                         "data-url": "/api/shop/products/7/ask" },
    [log, el("div", { "data-ask-ideas": "" }, [idea]), form, wait, el("p", {}, [count, "/500"])]);
  var section = el("section", { id: "ask", class: "shop-ai" }, [root]);
  document.body.appendChild(section);
  IP.boot(section);
  return { section: section, log: log, idea: idea, input: input, send: send, form: form, wait: wait, root: root };
}

function ask(box, question) {
  box.input.value = question;
  submit(box.form);
  flush();
}
function bubbles(box) { return box.log.children.filter(function (c) { return c.nodeType === 1; }); }
function lastBubble(box) { var b = bubbles(box); return b[b.length - 1]; }
function fetchesTo(suffix) { return __fetches.filter(function (f) { return f.url.slice(-suffix.length) === suffix; }).length; }
var DONE = { type: "done", answer: "Creatine helps with strength. Check with your doctor first.", suitable_for_you: "unsure",
             safety_flags: ["Creatine and kidney disease: talk to your doctor"], suggestions: [], provider: "gateway",
             ai_available: true, busy: false, cached: false, disclaimer: "AI can make mistakes." };

/* ================================================================ plan questions (08-ask-ai.js) */
test("plans: a streamed answer - IP.ai.stream POST with the gym and the tab id, chunks, provider, note", function () {
  var body = new Body();
  __route = function () { return sse(body); };
  var box = plansBox();
  eq(box.button.disabled, false);
  ask(box, "  Which plan suits me?  ");
  eq(__fetches.length, 1);
  var call = __fetches[0];
  eq(call.url, "/api/plans/ask");
  eq(call.init.method, "POST");
  eq(call.init.headers["X-Page-Id"], IP.pageId);
  eq(call.init.headers["X-CSRF-Token"], "csrf-abc");
  eq(call.init.body, '{"question":"Which plan suits me?","gym_slug":"pulse-central"}');
  ok(!box.box.classList.contains("hidden"), "answer box shown");
  eq(box.typing.hidden, false);
  eq(box.button.disabled, true, "no second question while one streams");
  eq(box.suggest.disabled, true);
  body.event({ type: "meta", provider: "Gemma (gateway)" });
  body.event({ type: "chunk", text: "Online + Trainer " });
  flush();
  eq(box.typing.hidden, true);
  body.event({ type: "chunk", text: "suits you." });
  body.event({ type: "done", fallback: false, disclaimer: "AI can make mistakes." });
  body.end();
  flush();
  eq(box.text.textContent, "Online + Trainer suits you.");
  eq(box.provider.textContent, "Gemma (gateway)");
  eq(box.note.hidden, true);
  eq(box.button.disabled, false);
  eq(box.suggest.disabled, false);
  eq(box.wait.hidden, true);
  eq(IP.ai.pending(), 0);
});

test("plans: a cached answer and the AI-down / busy fallbacks label the provider and show the note", function () {
  var body = new Body();
  __route = function () { return sse(body); };
  var box = plansBox();
  ask(box, "Cheapest plan?");
  body.event({ type: "meta", provider: "Gemma (gateway)", cached: true });
  body.event({ type: "chunk", text: "Online." });
  body.event({ type: "done", fallback: false });
  body.end();
  flush();
  eq(box.provider.textContent, "Gemma (gateway) (instant answer)");
  body = new Body();
  ask(box, "Which plan should I pick?");
  body.event({ type: "meta", provider: null, busy: true });
  body.event({ type: "chunk", text: "Suggested: Online." });
  body.event({ type: "done", fallback: true, busy: true, note: "AI is busy right now - here is a standard answer" });
  body.end();
  flush();
  eq(box.provider.textContent, "AI busy - automatic answer");
  eq(box.text.textContent, "Suggested: Online.");
  eq(box.note.hidden, false);
  eq(box.note.textContent, "AI is busy right now - here is a standard answer");
  eq(IP.ai.cooldownLeft("plans"), 0, "busy is no refusal of the member");
  body = new Body();
  ask(box, "Which plan should I pick now?");
  body.event({ type: "meta", provider: null });
  body.event({ type: "chunk", text: "Suggested: Combo." });
  body.event({ type: "done", fallback: true, note: "AI is unavailable right now - this is an automatic recommendation." });
  body.end();
  flush();
  eq(box.provider.textContent, "AI unavailable - automatic answer");
});

test("plans: 'blocked' shows the detail, disables ask + suggestions with a countdown until the cooldown ends", function () {
  var body = new Body();
  __route = function () { return sse(body); };
  var box = plansBox();
  ask(box, "One question too many?");
  body.event({ type: "blocked", code: "quota", detail: "You've used your AI questions for this minute. You can ask again in 30 seconds.", retry_after: 30 });
  body.event({ type: "done", blocked: true });
  body.end();
  flush();
  eq(box.text.textContent, "You've used your AI questions for this minute. You can ask again in 30 seconds.");
  eq(box.typing.hidden, true);
  eq(IP.ai.cooldownLeft("plans"), 30);
  eq(box.button.disabled, true);
  eq(box.suggest.disabled, true);
  eq(box.wait.hidden, false);
  eq(box.wait.textContent, "You can ask again in 30 s.");
  advance(10000);
  eq(box.wait.textContent, "You can ask again in 20 s.");
  var n = __fetches.length;
  click(box.suggest); // a chip the browser would not even let them press
  submit(box.form);
  flush();
  eq(__fetches.length, n, "a cooldown never reaches the server");
  advance(20100);
  eq(IP.ai.cooldownLeft("plans"), 0);
  eq(box.button.disabled, false);
  eq(box.suggest.disabled, false);
  eq(box.wait.hidden, true);
  eq(box.wait.textContent, "");
});

test("plans: a spam refusal (no wait) shows the detail and leaves the box usable", function () {
  var body = new Body();
  __route = function () { return sse(body); };
  var box = plansBox();
  ask(box, "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!");
  body.event({ type: "blocked", code: "spam", detail: "That message looks like spam (one character is repeated over and over). Please rephrase it.", retry_after: 0 });
  body.event({ type: "done", blocked: true });
  body.end();
  flush();
  has(box.text.textContent, "looks like spam");
  eq(box.button.disabled, false);
  eq(box.wait.hidden, true);
  eq(IP.ai.cooldownLeft("plans"), 0);
});

test("plans: a cooldown started by another widget ('cooldown' pauses all AI) disables the box too", function () {
  var box = plansBox();
  __route = function () { return json(429, { error: "AI paused", detail: "AI is paused on your account for 2 min.", code: "cooldown", retry_after: 120, ai_available: true }, { "Retry-After": "120" }); };
  IP.ai.request("/api/assistant/chat", { json: { message: "hi" }, feature: "assistant" }).catch(function () {});
  flush();
  eq(IP.ai.cooldownLeft("plans"), 120);
  eq(box.button.disabled, true);
  eq(box.wait.textContent, "You can ask again in 2 min.");
  advance(121000);
  eq(box.button.disabled, false);
  eq(box.wait.hidden, true);
});

test("plans: leaving the page aborts the stream silently", function () {
  var body = new Body();
  __route = function () { return sse(body); };
  var box = plansBox();
  ask(box, "Which plan?");
  eq(IP.ai.pending(), 1);
  window.fire("pagehide", { persisted: false });
  flush();
  eq(IP.ai.pending(), 0);
  eq(box.text.textContent, "", "nothing shown for a page that is going away");
  eq(box.typing.hidden, true);
  eq(box.button.disabled, false);
});

test("plans: a stream ended by the server without 'done' says the answer was interrupted", function () {
  var body = new Body();
  __route = function () { return sse(body); };
  var box = plansBox();
  ask(box, "Which plan?");
  body.event({ type: "meta", provider: "Gemma (gateway)" });
  body.event({ type: "chunk", text: "Online + Trainer" });
  body.end();
  flush();
  eq(box.text.textContent, "Online + Trainer");
  eq(box.note.textContent, "The answer was interrupted - ask again for the full answer.");
  body = new Body();
  ask(box, "Which plan now?");
  body.end();
  flush();
  eq(box.text.textContent, "The answer was interrupted - please try again.");
});

test("plans: errors go through IP.ai.describeError (timeout, 422 detail, 401, network failure mid-answer)", function () {
  __route = function () { return sse(new Body()); };
  var box = plansBox();
  ask(box, "Which plan?");
  advance(45000);
  eq(box.text.textContent, "That took too long - please try again.");
  __route = function () { return json(422, { error: "Invalid input", detail: "Ask a question of 2-500 characters." }); };
  ask(box, "Which plan?");
  eq(box.text.textContent, "Ask a question of 2-500 characters.");
  __route = function () { return new Res(401, { "content-type": "application/json" }, '{"error":"Please sign in","detail":"Please sign in to continue."}'); };
  ask(box, "Which plan?");
  eq(box.text.textContent, "Your session has expired - please sign in again.");
  var body = new Body();
  __route = function () { return sse(body); };
  ask(box, "Which plan?");
  body.event({ type: "chunk", text: "Online" });
  flush();
  body.fail(new TypeError("Load failed"));
  flush();
  eq(box.text.textContent, "Online", "the words already shown stay");
  eq(box.note.textContent, "Couldn't reach the server - check your connection and try again.");
  eq(box.button.disabled, false);
});

/* ================================================================ product questions (80-shop.js "shop-ask") */
test("shop: deltas stream into one bubble, 'done' renders the verdict and flags - no JSON request", function () {
  var body = new Body();
  __route = function (url) { return url.indexOf("/stream") !== -1 ? sse(body) : json(500, {}); };
  var box = shopBox();
  ask(box, "Is it good for strength?");
  eq(__fetches.length, 1);
  eq(__fetches[0].url, "/api/shop/products/7/ask/stream");
  eq(__fetches[0].init.body, '{"question":"Is it good for strength?"}');
  eq(__fetches[0].init.headers["X-Page-Id"], IP.pageId);
  eq(box.input.value, "", "the question moves into the conversation");
  eq(box.send.disabled, true);
  eq(box.idea.disabled, true);
  body.event({ type: "start", blocked: false, safety: [] });
  body.event({ type: "delta", text: "Creatine helps " });
  body.event({ type: "delta", text: "with strength." });
  flush();
  eq(lastBubble(box).textContent, "Creatine helps with strength.");
  body.event(DONE);
  body.end();
  flush();
  var answer = lastBubble(box);
  has(answer.textContent, DONE.answer);
  has(answer.textContent, "Check before using");
  has(answer.textContent, "Creatine and kidney disease: talk to your doctor");
  has(answer.textContent, "Answered by AI (gateway)");
  eq(bubbles(box)[bubbles(box).length - 2].textContent, "Is it good for strength?");
  eq(fetchesTo("/ask"), 0);
  eq(box.send.disabled, false);
  ok(!box.send.hasAttribute("aria-busy"), "data-no-busy: the core never leaves the button aria-busy");
});

test("shop: 'blocked' is shown as the refusal, never falls back, and the cooldown disables sending", function () {
  var body = new Body();
  __route = function (url) { return url.indexOf("/stream") !== -1 ? sse(body) : json(200, DONE); };
  var box = shopBox();
  ask(box, "One question too many?");
  body.event({ type: "start", blocked: false, safety: [] });
  body.event({ type: "blocked", code: "quota", detail: "You've used your AI questions for this minute. You can ask again in 45 seconds.", retry_after: 45 });
  body.event({ type: "done", blocked: true });
  body.end();
  flush();
  eq(fetchesTo("/ask"), 0, "a refusal is never retried through the JSON endpoint");
  has(lastBubble(box).textContent, "You've used your AI questions for this minute.");
  eq(box.send.disabled, true);
  eq(box.idea.disabled, true);
  eq(box.wait.textContent, "You can ask again in 45 s.");
  box.input.value = "Typed while waiting";
  submit(box.form);
  flush();
  eq(box.input.value, "Typed while waiting", "what they typed is kept for later");
  eq(__fetches.length, 1);
  advance(46000);
  eq(box.send.disabled, false);
  eq(box.wait.hidden, true);
});

test("shop: refusals, sign-in, not-found and local cooldowns never fall back to the JSON endpoint", function () {
  var box = shopBox();
  [[409, "duplicate", "You just asked that - give it a moment."], [422, "spam", "That message looks like spam."],
   [403, null, "Cross-site request blocked."], [404, null, "Product not found."], [429, null, "Slow down"]].forEach(function (c) {
    __route = function (url) { return url.indexOf("/stream") !== -1 ? json(c[0], { error: "x", detail: c[2], code: c[1] }) : json(200, DONE); };
    ask(box, "Question " + c[0] + "?");
    eq(fetchesTo("/ask"), 0, "status " + c[0]);
    advance(61000); // let any cooldown run out
  });
  __route = function (url) { return url.indexOf("/stream") !== -1 ? json(401, { error: "Please sign in", detail: "Please sign in to continue." }) : json(200, DONE); };
  ask(box, "Question 401?");
  eq(fetchesTo("/ask"), 0);
  eq(__navigations[__navigations.length - 1], "/auth/login?next=" + encodeURIComponent("/shop/p/7/creatine#ask"));
  __route = function (url) { return url.indexOf("/stream") !== -1 ? json(429, { error: "AI limit reached", detail: "Limit.", code: "quota", retry_after: 30 }) : json(200, DONE); };
  ask(box, "Question 429 quota?");
  var n = __fetches.length;
  click(box.idea); // the idea chip is disabled in a browser; even when pressed, the local cooldown answers
  flush();
  eq(__fetches.length, n, "a running cooldown never reaches the server");
  has(lastBubble(box).textContent, "You've reached your AI limit for now.");
  eq(fetchesTo("/ask"), 0);
  advance(31000);
});

test("shop: leaving the page or a timeout never falls back", function () {
  var body = new Body();
  __route = function (url) { return url.indexOf("/stream") !== -1 ? sse(body) : json(200, DONE); };
  var box = shopBox();
  ask(box, "How should I take it?");
  var before = bubbles(box).length;
  window.fire("pagehide", { persisted: false });
  flush();
  eq(fetchesTo("/ask"), 0);
  eq(bubbles(box).length, before - 1, "the typing bubble went, no error bubble came");
  eq(box.send.disabled, false);
  __route = function (url) { return url.indexOf("/stream") !== -1 ? sse(new Body()) : json(200, DONE); };
  ask(box, "How much protein?");
  advance(40000);
  eq(fetchesTo("/ask"), 0);
  has(lastBubble(box).textContent, "That took too long - please try again.");
});

test("shop: a broken stream (network, 5xx, JSON instead of a stream) retries once through IP.ai.request", function () {
  var box = shopBox();
  var cases = [
    function () { return Promise.reject(new TypeError("Failed to fetch")); },
    function () { return json(500, { error: "Server error", detail: "Unexpected error - it has been logged." }); },
    function () { return json(200, { answer: "not a stream" }); },
  ];
  cases.forEach(function (streamReply, i) {
    var jsonCalls = fetchesTo("/ask");
    __route = function (url) { return url.indexOf("/stream") !== -1 ? streamReply() : json(200, DONE); };
    ask(box, "Fallback question " + i + "?");
    eq(fetchesTo("/ask"), jsonCalls + 1, "case " + i);
    var call = __fetches[__fetches.length - 1];
    eq(call.init.method, "POST");
    eq(call.init.body, '{"question":"Fallback question ' + i + '?"}');
    eq(call.init.headers["X-Page-Id"], IP.pageId);
    has(lastBubble(box).textContent, DONE.answer);
  });
  __route = function (url) { return url.indexOf("/stream") !== -1 ? json(503, { error: "Cancelled", detail: "Stopped.", code: "cancelled" }) : json(200, DONE); };
  var jsonCalls = fetchesTo("/ask");
  ask(box, "Cancelled question?");
  eq(fetchesTo("/ask"), jsonCalls, "a 5xx that carries a refusal / cancel code is no broken stream");
});

test("shop: a stream that breaks mid-answer is completed by the fallback in the same bubble", function () {
  var body = new Body();
  __route = function (url) { return url.indexOf("/stream") !== -1 ? sse(body) : json(200, DONE); };
  var box = shopBox();
  ask(box, "Is it good for strength?");
  body.event({ type: "start", blocked: false, safety: [] });
  body.event({ type: "delta", text: "Creatine helps" });
  flush();
  var count = bubbles(box).length;
  body.fail(new TypeError("Load failed"));
  flush();
  eq(fetchesTo("/ask"), 1);
  eq(bubbles(box).length, count, "no second answer bubble");
  has(lastBubble(box).textContent, DONE.answer);
});

test("shop: the fallback's own refusal is shown and starts the cooldown", function () {
  __route = function (url) {
    if (url.indexOf("/stream") !== -1) return Promise.reject(new TypeError("Failed to fetch"));
    return json(429, { error: "AI limit reached", detail: "You've used your AI questions for this minute.", code: "quota", retry_after: 20, ai_available: true }, { "Retry-After": "20" });
  };
  var box = shopBox();
  ask(box, "How should I take it?");
  eq(fetchesTo("/ask"), 1);
  has(lastBubble(box).textContent, "You've reached your AI limit for now. Try again in 20 s.");
  eq(IP.ai.cooldownLeft("shop"), 20);
  eq(box.send.disabled, true);
  advance(21000);
  eq(box.send.disabled, false);
});

test("shop: the busy and AI-down answers say where the answer came from; a cut stream says it was interrupted", function () {
  var body = new Body();
  __route = function (url) { return url.indexOf("/stream") !== -1 ? sse(body) : json(200, DONE); };
  var box = shopBox();
  ask(box, "How should I take it?");
  body.event({ type: "start", blocked: false, safety: [] });
  body.event({ type: "done", answer: "AI is busy right now, so here's what the product listing says: 3 g a day.", suitable_for_you: "unsure",
               safety_flags: [], suggestions: [], provider: null, ai_available: false, busy: true, cached: false });
  body.end();
  flush();
  has(lastBubble(box).textContent, "AI is busy right now - answer built from the product listing");
  body = new Body();
  ask(box, "Does it contain lactose?");
  body.event({ type: "done", answer: "AI is unavailable right now, so here's what the product listing says.", suitable_for_you: "unsure",
               safety_flags: [], suggestions: [], provider: null, ai_available: false, busy: false, cached: false });
  body.end();
  flush();
  has(lastBubble(box).textContent, "AI unavailable - answer built from the product listing");
  body = new Body();
  ask(box, "Is it vegan?");
  body.event({ type: "start", blocked: false, safety: [] });
  body.event({ type: "delta", text: "It is made from" });
  body.end();
  flush();
  has(lastBubble(box).textContent, "It is made from");
  has(lastBubble(box).textContent, "The answer was interrupted - ask again for the full answer.");
  eq(fetchesTo("/ask"), 0);
  body = new Body();
  ask(box, "Is it gluten free?");
  body.event({ type: "start", blocked: false, safety: [] });
  body.end();
  flush();
  has(lastBubble(box).textContent, "The answer was interrupted - please try again.");
  eq(fetchesTo("/ask"), 0);
});

test("no widget logged an error", function () {
  eq(__errors.length, 0, __errors.join(" | "));
});

/* ---------------------------------------------------------------- run */
(function () {
  var passed = 0;
  var failed = 0;
  for (var i = 0; i < __tests.length; i++) {
    var t = __tests[i];
    __fetches = [];
    try {
      __route = function () { return json(200, { ok: true }); };
      t.fn();
      flush();
      passed++;
      print("PASS " + t.name);
    } catch (e) {
      failed++;
      print("FAIL " + t.name + " :: " + (e && e.message) + "\n" + (e && e.stack));
    }
    for (var guard = 0; guard < 100 && (IP.ai.cooldownLeft("plans") || IP.ai.cooldownLeft("shop") || IP.ai.pending()); guard++) {
      advance(60000); // no cooldown or request outlives its test
    }
  }
  print("SUMMARY passed=" + passed + " failed=" + failed + " total=" + __tests.length);
})();
"""
