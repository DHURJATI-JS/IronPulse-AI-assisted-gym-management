"""Behaviour of the super-admin console's "Test providers" button (app/static/src/js/70-superadmin.js, component
``sa-ai-test``) in JavaScriptCore, Safari's engine.

The component asks through ``IP.ai.request(form.action, {form, feature: "selftest"})`` (so the AI layer's local
cooldowns, page-hide aborts and refusal codes apply), explains refusals of the AI layer in place with
``IP.ai.describeError`` (409/422/429/503, or a local cooldown), falls back to the normal form POST for other 4xx
answers (an expired CSRF token: the server renders its usual response), stays quiet while the page is being left and
always gives the button back. ``IP`` is a stub with the documented shapes of IP.ai's errors (docs/ai-guard-and-queue.md
section 7); IP.ai itself is covered by tests/test_ai_frontend_static.py. Skipped where no ``jsc`` binary exists
(``JSC=/path/to/jsc`` points at one).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SA_JS = ROOT / "app" / "static" / "src" / "js" / "70-superadmin.js"
MAC_JSC = "/System/Library/Frameworks/JavaScriptCore.framework/Versions/Current/Helpers/jsc"


def _jsc() -> str | None:
    for candidate in (os.environ.get("JSC"), shutil.which("jsc"), MAC_JSC):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


needs_jsc = pytest.mark.skipif(_jsc() is None, reason="JavaScriptCore (jsc) is not available on this machine")


def _run(tmp_path: Path, script: Path) -> str:
    harness = tmp_path / "sa_harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    proc = subprocess.run([_jsc(), str(harness), "--", str(script)], capture_output=True, text=True, timeout=120,
                          cwd=tmp_path)
    return proc.stdout + proc.stderr


def _assert_passed(out: str) -> None:
    m = re.search(r"SUMMARY passed=(\d+) failed=(\d+) total=(\d+)", out)
    assert m, out[-4000:]
    passed, failed, total = map(int, m.groups())
    failures = [line for line in out.splitlines() if line.startswith("FAIL")]
    assert failed == 0 and not failures, "\n".join(failures) + "\n\n" + out[-4000:]
    assert passed == total >= 12, out[-2000:]


@needs_jsc
def test_ai_test_button_in_javascriptcore(tmp_path):
    _assert_passed(_run(tmp_path, SA_JS))


@needs_jsc
def test_ai_test_button_minified_behaves_the_same(tmp_path):
    import rjsmin

    minified = tmp_path / "70-superadmin.min.js"
    minified.write_text(rjsmin.jsmin(SA_JS.read_text(encoding="utf-8"), keep_bang_comments=False), encoding="utf-8")
    _assert_passed(_run(tmp_path, minified))


HARNESS = r"""/* A tiny browser for 70-superadmin.js: elements with attributes, children, classList and events, a document,
 * and an IP stub (register, h, ai.request / ai.describeError, api, island). Usage: jsc sa_harness.js -- <70-superadmin.js>
 */
var window = globalThis;
var __errors = [];
var console = { error: function () { __errors.push(Array.prototype.join.call(arguments, " ")); }, log: function () {}, warn: function () {} };

/* ---------------------------------------------------------------- DOM */
function Node(tag, attrs) {
  this.tagName = tag ? String(tag).toUpperCase() : null; // null: a text node
  this.attrs = {};
  this.childNodes = [];
  this.parentNode = null;
  this.listeners = {};
  this.disabled = false;
  this.text = "";
  var self = this;
  this.classList = {
    add: function (c) { var s = self.classes(); if (s.indexOf(c) < 0) s.push(c); self.attrs["class"] = s.join(" "); },
    remove: function (c) { self.attrs["class"] = self.classes().filter(function (x) { return x !== c; }).join(" "); },
    contains: function (c) { return self.classes().indexOf(c) >= 0; },
  };
  Object.keys(attrs || {}).forEach(function (k) { self.attrs[k] = String(attrs[k]); });
}
Node.prototype.classes = function () { return (this.attrs["class"] || "").split(/\s+/).filter(Boolean); };
Node.prototype.getAttribute = function (k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; };
Node.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); };
Node.prototype.hasAttribute = function (k) { return Object.prototype.hasOwnProperty.call(this.attrs, k); };
Node.prototype.removeAttribute = function (k) { delete this.attrs[k]; };
Node.prototype.appendChild = function (c) {
  if (c.parentNode) c.parentNode.removeChild(c);
  this.childNodes.push(c);
  c.parentNode = this;
  return c;
};
Node.prototype.removeChild = function (c) {
  var i = this.childNodes.indexOf(c);
  if (i >= 0) this.childNodes.splice(i, 1);
  c.parentNode = null;
  return c;
};
Object.defineProperty(Node.prototype, "firstChild", { get: function () { return this.childNodes[0] || null; } });
Object.defineProperty(Node.prototype, "className", {
  get: function () { return this.attrs["class"] || ""; },
  set: function (v) { this.attrs["class"] = String(v); },
});
Object.defineProperty(Node.prototype, "textContent", {
  get: function () {
    if (this.tagName === null) return this.text;
    return this.childNodes.map(function (c) { return c.textContent; }).join("");
  },
  set: function (v) {
    if (this.tagName === null) { this.text = String(v); return; }
    var self = this;
    this.childNodes.slice().forEach(function (c) { self.removeChild(c); });
    if (String(v) !== "") this.appendChild(textNode(v));
  },
});
Node.prototype.cloneNode = function (deep) {
  var n = new Node(this.tagName ? this.tagName.toLowerCase() : null, this.attrs);
  n.text = this.text;
  if (deep) this.childNodes.forEach(function (c) { n.appendChild(c.cloneNode(true)); });
  return n;
};
Node.prototype.addEventListener = function (type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); };
Node.prototype.dispatch = function (type, evt) { (this.listeners[type] || []).slice().forEach(function (fn) { fn(evt); }); };
Node.prototype.matches = function (sel) { return matches(this, sel); };
Node.prototype.closest = function (sel) { for (var n = this; n; n = n.parentNode) if (n.tagName && matches(n, sel)) return n; return null; };
Node.prototype.descendants = function () {
  var out = [];
  (function walk(n) { n.childNodes.forEach(function (c) { if (c.tagName) { out.push(c); walk(c); } }); })(this);
  return out;
};
Node.prototype.querySelectorAll = function (sel) { return this.descendants().filter(function (n) { return matches(n, sel); }); };
Node.prototype.querySelector = function (sel) { return this.querySelectorAll(sel)[0] || null; };
function textNode(t) { var n = new Node(null); n.text = String(t); return n; }

/** tag[attr][attr=value][attr="value"] - all this file's selectors need */
function matches(node, sel) {
  var m = /^([a-z]*)((?:\[[^\]]+\])*)$/i.exec(String(sel).trim());
  if (!m || !node.tagName) return false;
  if (m[1] && node.tagName !== m[1].toUpperCase()) return false;
  var conds = m[2].match(/\[[^\]]+\]/g) || [];
  return conds.every(function (c) {
    var kv = /^\[([^=\]]+)(?:=["']?([^"'\]]*)["']?)?\]$/.exec(c);
    if (!kv || !node.hasAttribute(kv[1])) return false;
    return kv[2] === undefined || node.getAttribute(kv[1]) === kv[2];
  });
}

var document = new Node("#document");
document.body = document.appendChild(new Node("body"));
document.createElement = function (tag) { return new Node(tag); };
document.createTextNode = function (t) { return textNode(t); };
window.addEventListener = function () {};
window.matchMedia = function () { return { matches: true }; };
window.requestAnimationFrame = function (fn) { fn(); };
window.setTimeout = function (fn) { fn(); return 1; };

/* ---------------------------------------------------------------- IP stub */
var registry = {};
var calls = [];
var notes = [];
var answer = null; // function(url, opts) -> Promise, the next AI answer
var IP = {
  register: function (name, fn) { registry[name] = fn; },
  h: function (tag, attrs) {
    var el = new Node(tag);
    attrs = attrs || {};
    Object.keys(attrs).forEach(function (k) {
      var v = attrs[k];
      if (v == null || v === false) return;
      if (k === "text") el.textContent = v;
      else if (k === "class") el.className = v;
      else el.setAttribute(k, v === true ? "" : v);
    });
    for (var i = 2; i < arguments.length; i++) if (arguments[i] != null) el.appendChild(arguments[i]);
    return el;
  },
  api: function (url, opts) { calls.push({ via: "api", url: url, opts: opts }); return answer(url, opts); },
  ai: {
    request: function (url, opts) { calls.push({ via: "ai", url: url, opts: opts }); return answer(url, opts); },
    describeError: function (err) {
      return "DESCRIBED " + (err.code || err.status || err.name || "error") + (err.retryAfter ? " in " + err.retryAfter + " s" : "");
    },
  },
  island: { notify: function (n) { notes.push(n); } },
};
window.IP = IP;

/* errors with the documented shapes of IP.api / IP.ai (docs/ai-guard-and-queue.md section 7) */
function apiError(status, code, retryAfter) {
  var e = new Error("Request failed (" + status + ")");
  e.status = status;
  e.code = code || null;
  e.retryAfter = retryAfter || null;
  e.data = code ? { error: "x", detail: "y", code: code, retry_after: retryAfter || 0 } : null;
  return e;
}
function localCooldown(seconds) {
  var e = apiError(429, "cooldown_local", seconds);
  e.local = true;
  e.cooldownCode = "busy";
  return e;
}
function abortError(reason) { var e = new Error("aborted"); e.name = "AbortError"; e.code = "cancelled"; e.reason = reason; return e; }

/* ---------------------------------------------------------------- tests */
var __tests = [];
function test(name, fn) { __tests.push({ name: name, fn: fn }); }
var __current = null;
function check(ok, what) { if (!ok) throw new Error(what); }
function settle() { for (var i = 0; i < 25; i++) drainMicrotasks(); }

function setup() {
  calls = [];
  notes = [];
  document.body.childNodes.slice().forEach(function (c) { document.body.removeChild(c); });
  var form = new Node("form", { method: "post", "data-component": "sa-ai-test" });
  form.action = "/superadmin/ai/test";
  form.submitted = 0;
  form.submit = function () { form.submitted++; };
  var btn = new Node("button", { type: "submit", class: "btn btn-primary btn-sm", "data-busy-label": "Testing…" });
  btn.appendChild(new Node("svg"));
  btn.appendChild(textNode(" Test providers"));
  form.appendChild(btn);
  var box = new Node("div", { class: "mb-6 hidden", "data-ai-test-result": "", "aria-live": "polite" });
  document.body.appendChild(form);
  document.body.appendChild(box);
  registry["sa-ai-test"](form);
  return { form: form, btn: btn, box: box };
}
function submit(form) {
  var evt = { target: form, defaultPrevented: false };
  evt.preventDefault = function () { evt.defaultPrevented = true; };
  form.dispatch("submit", evt);
  return evt;
}
function restored(s) {
  return s.btn.disabled === false && !s.btn.hasAttribute("aria-busy") && s.btn.textContent === " Test providers" &&
    s.btn.childNodes.length === 2 && s.btn.childNodes[0].tagName === "SVG";
}
function lastNote() { return notes[notes.length - 1] || {}; }
function shown(s) { return !s.box.classList.contains("hidden") && s.box.childNodes.length === 1; }
function refusalCase(err, code) {
  return function () {
    var s = setup();
    answer = function () { return Promise.reject(err); };
    check(submit(s.form).defaultPrevented, "handled in place");
    settle();
    check(s.form.submitted === 0, "a refusal is never re-posted as a plain form");
    check(shown(s), "the result box is shown");
    check(s.box.textContent.indexOf("The provider test did not run") >= 0, "says the test did not run: " + s.box.textContent);
    check(s.box.textContent.indexOf("DESCRIBED " + code) >= 0, "explains why with IP.ai.describeError: " + s.box.textContent);
    check(s.box.firstChild.classList.contains("sa-ai-result-bad"), "styled as a problem");
    check(lastNote().title === "AI test not run" && lastNote().tone === "warning", "island: not run (warning)");
    check(restored(s), "button restored");
  };
}

var SRC = arguments[0];
load(SRC);

test("registers the component", function () {
  check(typeof registry["sa-ai-test"] === "function", "sa-ai-test registered");
  check(typeof registry["sa-bars"] === "function" && typeof registry["sa-busy"] === "function", "other components still registered");
});

test("asks through IP.ai with feature selftest and renders the answer", function () {
  var s = setup();
  var release = null;
  answer = function () {
    return new Promise(function (ok) {
      release = function () {
        ok({ ok: true, provider: "gateway", label: "Fake (gateway)", model: "fake-model", latency_ms: 12, reply: "OK",
             total_ms: 30, providers: [{ name: "gateway", label: "Fake (gateway)", status: "up", error: null }],
             disclaimer: "AI can make mistakes." });
      };
    });
  };
  check(submit(s.form).defaultPrevented, "the submit is handled in place");
  settle();
  check(s.btn.disabled === true && s.btn.getAttribute("aria-busy") === "true", "busy while the test runs");
  check(s.btn.textContent.indexOf("Testing…") >= 0, "busy label shown");
  submit(s.form);
  settle();
  check(calls.length === 1, "a second click while busy sends nothing");
  check(calls[0].via === "ai" && calls[0].url === "/superadmin/ai/test", "IP.ai.request(form.action, ...)");
  check(calls[0].opts.feature === "selftest" && calls[0].opts.form === s.form, "feature selftest + the form itself");
  release();
  settle();
  check(shown(s), "result shown");
  check(s.box.textContent.indexOf("Fake (gateway) answered in 12 ms") >= 0, "answer: " + s.box.textContent);
  check(s.box.textContent.indexOf("AI can make mistakes.") >= 0, "disclaimer kept");
  check(s.box.firstChild.classList.contains("sa-ai-result-ok"), "styled as a success");
  check(lastNote().title === "AI test passed" && lastNote().tone === "success", "island: passed");
  check(s.form.submitted === 0 && restored(s), "no fallback POST, button restored");
});

test("a real failed test (every provider down) keeps its wording", function () {
  var s = setup();
  answer = function () { return Promise.resolve({ ok: false, error: "gateway: simulated outage", providers: [] }); };
  submit(s.form);
  settle();
  check(s.box.textContent.indexOf("AI unavailable - every provider failed") >= 0, s.box.textContent);
  check(s.box.textContent.indexOf("gateway: simulated outage") >= 0, "the server's error");
  check(lastNote().title === "AI test failed" && lastNote().tone === "danger", "island: failed");
  check(restored(s), "button restored");
});

test("429 too_many is explained in place", refusalCase(apiError(429, "too_many", 5), "too_many"));
test("503 busy (AI queue full) is explained in place", refusalCase(apiError(503, "busy", 5), "busy"));
test("409 duplicate is explained in place", refusalCase(apiError(409, "duplicate", 3), "duplicate"));
test("422 spam is explained in place", refusalCase(apiError(422, "spam"), "spam"));
test("a 429 without a code (the gate's rate limit) is explained, not re-posted", refusalCase(apiError(429, null, 30), "429"));
test("a local cooldown never reaches the network and is explained", refusalCase(localCooldown(4), "cooldown_local"));
test("503 cancelled (the request was stopped) is explained", refusalCase(apiError(503, "cancelled"), "cancelled"));

test("other 4xx answers fall back to the normal form POST", function () {
  [403, 401, 400, 404].forEach(function (status) {
    var s = setup();
    answer = function () { return Promise.reject(apiError(status)); };
    submit(s.form);
    settle();
    check(s.form.submitted === 1, status + ": the server renders its normal response");
    check(s.box.classList.contains("hidden") && s.box.childNodes.length === 0, status + ": nothing rendered in place");
    check(restored(s), status + ": button restored");
  });
});

test("a server error is a failed test with a friendly message", function () {
  var s = setup();
  answer = function () { return Promise.reject(apiError(500)); };
  submit(s.form);
  settle();
  check(s.form.submitted === 0, "not re-posted");
  check(s.box.textContent.indexOf("The provider test failed") >= 0, s.box.textContent);
  check(s.box.textContent.indexOf("DESCRIBED 500") >= 0, "describeError");
  check(lastNote().tone === "danger", "island: danger");
});

test("a network failure is a failed test with a friendly message", function () {
  var s = setup();
  answer = function () { return Promise.reject(new TypeError("Failed to fetch")); };
  submit(s.form);
  settle();
  check(s.form.submitted === 0 && s.box.textContent.indexOf("The provider test failed") >= 0, s.box.textContent);
});

test("leaving the page (pagehide abort) shows nothing", function () {
  var s = setup();
  answer = function () { return Promise.reject(abortError("page-hide")); };
  submit(s.form);
  settle();
  check(s.form.submitted === 0 && s.box.classList.contains("hidden") && notes.length === 0, "quiet while leaving");
  check(restored(s), "button restored");
});

test("another abort (timeout) is explained", function () {
  var s = setup();
  var err = abortError("timeout");
  err.code = "timeout";
  answer = function () { return Promise.reject(err); };
  submit(s.form);
  settle();
  check(s.box.textContent.indexOf("DESCRIBED timeout") >= 0, s.box.textContent);
});

test("without IP.ai the plain IP.api POST still works", function () {
  var keep = IP.ai;
  IP.ai = undefined;
  try {
    var s = setup();
    answer = function () { return Promise.resolve({ ok: true, provider: "gateway", label: "G", model: "m", latency_ms: 5, reply: "OK", total_ms: 6, providers: [] }); };
    submit(s.form);
    settle();
    check(calls.length === 1 && calls[0].via === "api" && calls[0].opts.method === "POST" && calls[0].opts.form === s.form, "IP.api POST with the form");
    check(s.box.textContent.indexOf("G answered in 5 ms") >= 0, s.box.textContent);
  } finally {
    IP.ai = keep;
  }
});

var passed = 0;
var failed = 0;
__tests.forEach(function (t) {
  try {
    t.fn();
    settle();
    if (__errors.length) throw new Error("console.error: " + __errors.join(" | "));
    passed++;
    print("PASS " + t.name);
  } catch (e) {
    failed++;
    print("FAIL " + t.name + ": " + (e && e.message ? e.message : e));
  }
  __errors = [];
});
print("SUMMARY passed=" + passed + " failed=" + failed + " total=" + __tests.length);
"""
