"""Behaviour of the sign-in page's account chooser (app/static/src/js/02-accounts.js) and of 00-core.js's double-submit
and "signed in elsewhere" handling, in JavaScriptCore (Safari's engine; skipped where no ``jsc`` exists).

02-accounts.js: only opaque references (and a random device key) are ever written to localStorage - never a name,
e-mail or photo from the lookup; an explicit "gone" removes an entry, an error never does; names and e-mails are
rendered as text (a name full of markup stays text); choosing an account fills the hidden reference and disables the
identifier box (the password is still asked); "Remove" revokes server-side; the sign-in hand-off cookie is moved into
localStorage and deleted; at most 5 entries. 00-core.js: X-Submit-Id on unsafe same-origin calls only, a 4409 close or
a 401 + X-Signed-Out answer leads to the sign-in page once, busy submit buttons come back.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ACCOUNTS_JS = ROOT / "app" / "static" / "src" / "js" / "02-accounts.js"
CORE_JS = ROOT / "app" / "static" / "src" / "js" / "00-core.js"
MAC_JSC = "/System/Library/Frameworks/JavaScriptCore.framework/Versions/Current/Helpers/jsc"


def _jsc() -> str | None:
    for candidate in (os.environ.get("JSC"), shutil.which("jsc"), MAC_JSC):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


needs_jsc = pytest.mark.skipif(_jsc() is None, reason="JavaScriptCore (jsc) is not available on this machine")


def _run(tmp_path: Path, harness_src: str, *scripts: Path) -> str:
    harness = tmp_path / "harness.js"
    harness.write_text(harness_src, encoding="utf-8")
    proc = subprocess.run([_jsc(), str(harness), "--", *(str(s) for s in scripts)], capture_output=True, text=True,
                          timeout=120, cwd=tmp_path)
    return proc.stdout + proc.stderr


def _assert_passed(out: str, minimum: int) -> None:
    m = re.search(r"SUMMARY passed=(\d+) failed=(\d+) total=(\d+)", out)
    assert m, out[-4000:]
    passed, failed, total = map(int, m.groups())
    failures = [line for line in out.splitlines() if line.startswith("FAIL")]
    assert failed == 0 and not failures, "\n".join(failures) + "\n\n" + out[-4000:]
    assert passed == total >= minimum, out[-2000:]


@needs_jsc
def test_account_chooser_in_javascriptcore(tmp_path):
    _assert_passed(_run(tmp_path, CHOOSER_HARNESS, ACCOUNTS_JS), 9)


@needs_jsc
def test_account_chooser_minified_behaves_the_same(tmp_path):
    import rjsmin

    minified = tmp_path / "02-accounts.min.js"
    minified.write_text(rjsmin.jsmin(ACCOUNTS_JS.read_text(encoding="utf-8"), keep_bang_comments=False), encoding="utf-8")
    _assert_passed(_run(tmp_path, CHOOSER_HARNESS, minified), 9)


def _core_harness(tests: str) -> str:
    from tests.test_ai_frontend_static import HARNESS

    prelude, _, _rest = HARNESS.partition("var IP = window.IP;")
    runner = HARNESS[HARNESS.index("/* ---------------------------------------------------------------- run */"):]
    assert prelude and runner
    return prelude + "var IP = window.IP;\n" + CORE_STUBS + tests + runner


@needs_jsc
def test_core_double_submit_and_signed_out_elsewhere_in_javascriptcore(tmp_path):
    _assert_passed(_run(tmp_path, _core_harness(CORE_TESTS), CORE_JS), 6)


@needs_jsc
def test_core_socket_closed_4409_goes_to_the_sign_in_page_in_javascriptcore(tmp_path):
    _assert_passed(_run(tmp_path, _core_harness(CORE_4409_TESTS), CORE_JS), 1)


CHOOSER_HARNESS = r"""/* A tiny browser for 02-accounts.js: elements (attributes, children, hidden, disabled, value, textContent, events),
 * a document with cookies, localStorage and an IP stub (register, h, api, newSubmitId). */
var window = globalThis;
var __errors = [];
var console = { error: function () { __errors.push(Array.prototype.join.call(arguments, " ")); }, log: function () {}, warn: function () {} };

function Node(tag, attrs) {
  this.tagName = tag ? String(tag).toUpperCase() : null; // null: a text node
  this.attrs = {};
  this.childNodes = [];
  this.parentNode = null;
  this.listeners = {};
  this.text = "";
  this.value = "";
  this.disabled = false;
  this.required = false;
  var self = this;
  Object.keys(attrs || {}).forEach(function (k) { self.attrs[k] = String(attrs[k]); });
  if (Object.prototype.hasOwnProperty.call(this.attrs, "value")) this.value = this.attrs.value;
}
Node.prototype.getAttribute = function (k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; };
Node.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); };
Node.prototype.hasAttribute = function (k) { return Object.prototype.hasOwnProperty.call(this.attrs, k); };
Node.prototype.removeAttribute = function (k) { delete this.attrs[k]; };
Object.defineProperty(Node.prototype, "hidden", {
  get: function () { return this.hasAttribute("hidden"); },
  set: function (v) { if (v) this.attrs.hidden = ""; else delete this.attrs.hidden; },
});
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
Object.defineProperty(Node.prototype, "className", {
  get: function () { return this.attrs["class"] || ""; },
  set: function (v) { this.attrs["class"] = String(v); },
});
Node.prototype.addEventListener = function (type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); };
Node.prototype.dispatch = function (type, evt) { (this.listeners[type] || []).slice().forEach(function (fn) { fn(evt || {}); }); };
Node.prototype.click = function () { this.dispatch("click", {}); };
Node.prototype.focus = function () { __focused = this; };
Node.prototype.descendants = function () {
  var out = [];
  (function walk(n) { n.childNodes.forEach(function (c) { if (c.tagName) { out.push(c); walk(c); } }); })(this);
  return out;
};
Node.prototype.querySelectorAll = function (sel) { return this.descendants().filter(function (n) { return matches(n, sel); }); };
Node.prototype.querySelector = function (sel) { return this.querySelectorAll(sel)[0] || null; };
function textNode(t) { var n = new Node(null); n.text = String(t); return n; }
/** tag[attr][attr=value][attr="value"] - every selector the file uses */
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
var __focused = null;
var document = new Node("#document");
document.body = document.appendChild(new Node("body"));
document.createElement = function (tag) { return new Node(tag); };
document.createTextNode = function (t) { return textNode(t); };
var __cookieWrites = [];
var __cookies = "";
Object.defineProperty(document, "cookie", {
  get: function () { return __cookies; },
  set: function (v) { __cookieWrites.push(String(v)); },
});
var location = { protocol: "https:", pathname: "/auth/login" };
var __store = {};
var localStorage = {
  getItem: function (k) { return Object.prototype.hasOwnProperty.call(__store, k) ? __store[k] : null; },
  setItem: function (k, v) { __store[k] = String(v); },
  removeItem: function (k) { delete __store[k]; },
};
window.localStorage = localStorage;

var registry = {};
var calls = [];
var answer = null;
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
  api: function (url, opts) { calls.push({ url: url, opts: opts }); return answer(url, opts); },
  newSubmitId: function () { return "0123456789abcdef0123456789abcdef"; },
};
window.IP = IP;

var __tests = [];
function test(name, fn) { __tests.push({ name: name, fn: fn }); }
function eq(a, b, msg) { if (a !== b) throw new Error((msg ? msg + ": " : "") + "expected " + JSON.stringify(b) + ", got " + JSON.stringify(a)); }
function ok(v, msg) { if (!v) throw new Error(msg || "expected a truthy value"); }

var REF = function (c) { var s = ""; for (var i = 0; i < 43; i++) s += c; return s; };
var A = REF("A"), B = REF("B"), C = REF("C"), D = REF("D"), E = REF("E"), F = REF("F"), G = REF("G");

function page() {
  document.body.childNodes.slice().forEach(function (c) { document.body.removeChild(c); });
  var root = IP.h("section", { "data-component": "account-chooser", "data-lookup": "/auth/remembered",
                               "data-forget": "/auth/remembered/forget", hidden: true },
                  IP.h("ul", { "data-chooser-list": "" }), IP.h("button", { "data-chooser-other": "" }));
  var form = IP.h("form", { "data-login-form": "" },
                  IP.h("input", { type: "hidden", name: "account_ref", "data-account-ref": "" }),
                  IP.h("input", { type: "hidden", name: "device_id", "data-device-id": "" }),
                  IP.h("div", { "data-chosen-account": "", hidden: true },
                       IP.h("span", { "data-chosen-avatar": "" }), IP.h("p", { "data-chosen-name": "" }),
                       IP.h("p", { "data-chosen-email": "" }), IP.h("button", { "data-chooser-switch": "" })),
                  IP.h("div", { "data-identifier-field": "" }, IP.h("input", { name: "identifier" })),
                  IP.h("input", { type: "password", name: "password" }));
  document.body.appendChild(root);
  document.body.appendChild(form);
  return { root: root, form: form, ref: form.querySelector("[data-account-ref]"),
           device: form.querySelector("[data-device-id]"), idField: form.querySelector("[data-identifier-field]"),
           idInput: form.querySelector("input[name=identifier]"), chosen: form.querySelector("[data-chosen-account]"),
           list: root.querySelector("[data-chooser-list]") };
}
function stored() { return JSON.parse(__store["ip.accounts"] || "null"); }
function seed(refs) { __store["ip.accounts"] = JSON.stringify({ device: "", refs: refs.map(function (r, i) { return { r: r, t: 1000 - i }; }) }); }
function resolved(value) { return function () { return Promise.resolve(value); }; }

load(arguments[0]);

test("the hand-off cookie moves into localStorage and is deleted; junk is ignored", function () {
  __store = {}; __cookieWrites = [];
  __cookies = "theme=dark; __Host-ip_rmb=" + A + "; ip_rmb=<script>";
  var listeners = document.listeners.DOMContentLoaded || [];
  ok(listeners.length >= 1, "a DOMContentLoaded listener");
  listeners.forEach(function (fn) { fn({}); });
  var s = stored();
  eq(s.refs.length, 1);
  eq(s.refs[0].r, A);
  ok(/^[A-Za-z0-9_-]{22,64}$/.test(s.device), "a random device key");
  eq(__cookieWrites.length, 2, "both hand-off cookies deleted");
  ok(__cookieWrites[0].indexOf("__Host-ip_rmb=; Max-Age=0; Path=/") === 0 && __cookieWrites[0].indexOf("Secure") > 0);
  eq(Object.keys(s).sort().join(","), "device,refs", "nothing else is stored");
  eq(Object.keys(s.refs[0]).sort().join(","), "r,t");
});

test("at most five references, newest first", function () {
  __store = {};
  [A, B, C, D, E, F, G].forEach(function (r) { IP.accounts.remember(r); });
  var s = stored();
  eq(s.refs.length, 5);
  eq(s.refs[0].r, G);
  eq(s.refs[4].r, C);
});

test("lookup: gone entries are removed, the others rendered as text, nothing from the answer is stored", function () {
  __store = {}; calls = [];
  seed([A, B, C]);
  var p = page();
  answer = resolved({ accounts: [
    { ref: A, state: "ok", name: "<img src=x onerror=alert(1)>", initials: "XS", email: "ab***@gmail.com",
      photo: "javascript:alert(1)" },
    { ref: B, state: "gone" },
    { ref: C, state: "ok", name: "Riya Kapoor", initials: "RK", email: null, photo: "/auth/remembered/photo/tok.en" },
  ] });
  registry["account-chooser"](p.root);
  eq(calls.length, 1);
  eq(calls[0].url, "/auth/remembered");
  eq(JSON.stringify(calls[0].opts.json), JSON.stringify({ refs: [A, B, C] }));
  ok(/^[A-Za-z0-9_-]{22,64}$/.test(p.device.value), "the device key goes with the form");
  drainMicrotasks();
  var s = stored();
  eq(s.refs.map(function (x) { return x.r; }).join(","), A + "," + C, "the gone one is off this device");
  ok(JSON.stringify(s).indexOf("Riya") < 0 && JSON.stringify(s).indexOf("gmail") < 0, "no details stored");
  eq(p.root.hidden, false);
  var picks = p.list.querySelectorAll("[data-chooser-pick]");
  eq(picks.length, 2);
  ok(picks[0].textContent.indexOf("<img src=x onerror=alert(1)>") >= 0, "markup stays text");
  eq(p.list.querySelectorAll("img").length, 1, "only our own signed photo URL becomes an image");
  eq(p.list.querySelector("img").getAttribute("src"), "/auth/remembered/photo/tok.en");
});

test("an error never removes an entry and leaves the plain form", function () {
  __store = {}; calls = [];
  seed([A, B]);
  var p = page();
  answer = function () { return Promise.reject(new Error("429")); };
  registry["account-chooser"](p.root);
  drainMicrotasks();
  eq(stored().refs.length, 2);
  eq(p.root.hidden, true);
  eq(p.idField.hidden, false);
});

test("choosing an account fills the reference, hides the identifier box and asks for the password", function () {
  __store = {};
  seed([A]);
  var p = page();
  answer = resolved({ accounts: [{ ref: A, state: "ok", name: "Riya Kapoor", initials: "RK", email: "ri***@example.com", photo: null }] });
  registry["account-chooser"](p.root);
  drainMicrotasks();
  p.idInput.value = "typed@example.com";
  p.list.querySelector("[data-chooser-pick]").click();
  eq(p.ref.value, A);
  eq(p.idInput.disabled, true, "the identifier is not sent");
  eq(p.idInput.value, "");
  eq(p.idField.hidden, true);
  eq(p.chosen.hidden, false);
  eq(p.chosen.querySelector("[data-chosen-name]").textContent, "Riya Kapoor");
  eq(__focused && __focused.getAttribute("type"), "password");
  p.chosen.querySelector("[data-chooser-switch]").click();
  eq(p.ref.value, "");
  eq(p.idInput.disabled, false);
  eq(p.idInput.required, true);
  eq(p.idField.hidden, false);
  eq(p.chosen.hidden, true);
});

test("remove from this device deletes it here and revokes it on the server", function () {
  __store = {}; calls = [];
  seed([A, B]);
  var p = page();
  answer = function (url) {
    if (url === "/auth/remembered") return Promise.resolve({ accounts: [
      { ref: A, state: "ok", name: "One", initials: "O", email: null, photo: null },
      { ref: B, state: "ok", name: "Two", initials: "T", email: null, photo: null }] });
    return Promise.resolve({ ok: true });
  };
  registry["account-chooser"](p.root);
  drainMicrotasks();
  p.list.querySelector("[data-chooser-pick]").click(); // chosen, then removed: back to the plain form
  p.list.querySelector("[data-chooser-remove]").click();
  drainMicrotasks();
  eq(calls[1].url, "/auth/remembered/forget");
  eq(JSON.stringify(calls[1].opts.json), JSON.stringify({ ref: A }));
  eq(stored().refs.map(function (x) { return x.r; }).join(","), B);
  eq(p.list.querySelectorAll("[data-chooser-pick]").length, 1);
  eq(p.ref.value, "");
  eq(p.idField.hidden, false);
});

test("a failed attempt with a chosen account comes back with it selected", function () {
  __store = {};
  seed([A]);
  var p = page();
  p.ref.value = A;
  answer = resolved({ accounts: [{ ref: A, state: "ok", name: "Riya", initials: "R", email: null, photo: null }] });
  registry["account-chooser"](p.root);
  drainMicrotasks();
  eq(p.idField.hidden, true);
  eq(p.chosen.hidden, false);
  eq(p.ref.value, A);
});

test("no remembered accounts: no lookup, plain form", function () {
  __store = {}; calls = [];
  var p = page();
  registry["account-chooser"](p.root);
  drainMicrotasks();
  eq(calls.length, 0);
  eq(p.root.hidden, true);
});

test("a corrupted store is ignored", function () {
  __store = { "ip.accounts": "{not json" };
  eq(IP.accounts.read().refs.length, 0);
  __store = { "ip.accounts": JSON.stringify({ device: "<x>", refs: [{ r: "short" }, { r: A, t: "x" }, "junk", { r: A }] }) };
  var s = IP.accounts.read();
  eq(s.device, "");
  eq(s.refs.length, 1);
  eq(s.refs[0].r, A);
});

test("no listener or component logged an error", function () {
  eq(__errors.length, 0, __errors.join(" | "));
});

(function () {
  var passed = 0, failed = 0;
  for (var i = 0; i < __tests.length; i++) {
    try { __tests[i].fn(); drainMicrotasks(); passed++; print("PASS " + __tests[i].name); }
    catch (e) { failed++; print("FAIL " + __tests[i].name + " :: " + (e && e.message) + "\n" + (e && e.stack)); }
  }
  print("SUMMARY passed=" + passed + " failed=" + failed + " total=" + __tests.length);
})();
"""


CORE_4409_TESTS = r"""
test("a socket closed with 4409 says why, goes to the sign-in page and never reconnects", function () {
  installIsland(); resetIsland(); __assigned = [];
  var before = __sockets.length;
  document.fire("DOMContentLoaded", {});
  eq(__sockets.length, before + 1, "DOMContentLoaded opened the socket");
  var s = lastSocket();
  wsOpen(s);
  wsClose(s, 4409);
  eq(isl.notes.length, 1);
  ok(isl.notes[0].body.indexOf("signed in on another device") > 0, isl.notes[0].body);
  advance(60000);
  eq(__sockets.length, before + 1, "no reconnect after 4409");
  eq(__assigned.length, 1);
  eq(__assigned[0], "/auth/login?elsewhere=1&next=%2Fme");
});
"""

CORE_STUBS = r"""
/* ---------------------------------------------------------------- double submit + signed out elsewhere (00-core.js) */
var __assigned = [];
location.assign = function (u) { __assigned.push(String(u)); };
Object.defineProperty(El.prototype, "childNodes", { get: function () { return this.children; } });
Object.defineProperty(El.prototype, "textContent", {
  get: function () { return this.children.map(function (c) { return c.textContent; }).join(""); },
  set: function (v) { this.children = []; if (String(v) !== "") this.children.push({ nodeType: 3, textContent: String(v) }); },
});
var __q = El.prototype.querySelector;
El.prototype.querySelectorAll = function (sel) {
  var all = this.descendants();
  if (sel.indexOf('button[type="submit"]') === 0) {
    return all.filter(function (c) {
      return (c.tagName === "BUTTON" && (c.getAttribute("type") === "submit" || !c.hasAttribute("type"))) ||
             (c.tagName === "INPUT" && c.getAttribute("type") === "submit");
    });
  }
  return [];
};
var __forms = [];
document.querySelectorAll = function (sel) {
  return sel === "form[data-ip-busy]" ? __forms.filter(function (f) { return f.hasAttribute("data-ip-busy"); }) : [];
};
"""

CORE_TESTS = r"""
test("IP.api sends X-Submit-Id on unsafe same-origin calls only (or the one the caller passes)", function () {
  installIsland();
  IP.api("/tasks", { method: "POST", json: {} });
  var id1 = lastFetch().init.headers["X-Submit-Id"];
  ok(/^[A-Za-z0-9_-]{16,64}$/.test(id1), "an id: " + id1);
  IP.api("/tasks", { method: "POST", json: {} });
  ok(/^[A-Za-z0-9_-]{16,64}$/.test(lastFetch().init.headers["X-Submit-Id"]), "one per call");
  IP.api("/tasks", { method: "POST", json: {}, submitId: "retry-0123456789abcdef" });
  eq(lastFetch().init.headers["X-Submit-Id"], "retry-0123456789abcdef");
  IP.api("/tasks");
  eq(lastFetch().init.headers["X-Submit-Id"], undefined, "never on GET");
  IP.api("https://other.example/x", { method: "POST", json: {} });
  eq(lastFetch().init.headers["X-Submit-Id"], undefined, "never to another site");
  flush();
});

test("a 401 with X-Signed-Out: elsewhere says why and goes to the sign-in page once", function () {
  installIsland(); resetIsland(); __assigned = [];
  __route = function () { return json(401, { error: "Please sign in", detail: "You were signed out because your account signed in on another device (Chrome on Windows, 13 Nov 2026 at 01:30 IST)." }, { "X-Signed-Out": "elsewhere" }); };
  var box = capture(IP.api("/api/notifications"));
  flush();
  ok(box.error && box.error.status === 401 && box.error.code === "signed_out_elsewhere");
  eq(isl.notes.length, 1);
  ok(isl.notes[0].body.indexOf("Chrome on Windows") > 0);
  advance(1300);
  eq(__assigned.length, 1);
  eq(__assigned[0], "/auth/login?elsewhere=1&next=%2Fme");
  capture(IP.api("/api/notifications"));
  advance(2000);
  eq(__assigned.length, 1, "only once");
  eq(isl.notes.length, 1);
});

test("submitting disables the form's submit buttons with a busy label; formDone puts them back", function () {
  document.fire("DOMContentLoaded", {});
  var form = new El("form", { method: "post", action: "/tasks" });
  var btn = new El("button", { type: "submit", "data-busy-label": "Saving..." });
  btn.appendChild({ nodeType: 3, textContent: "Add task" });
  var other = new El("button", {});
  form.appendChild(btn); form.appendChild(other);
  __forms.push(form);
  submit(form, btn);
  flush();
  eq(btn.disabled, true); eq(other.disabled, true);
  eq(btn.getAttribute("aria-busy"), "true");
  eq(btn.textContent, "Saving...");
  IP.formDone(form);
  eq(btn.disabled, false); eq(other.disabled, false);
  eq(btn.getAttribute("aria-busy"), null);
  eq(btn.textContent, "Add task", "the original label is back");
});

test("busy buttons come back after the timeout and on a back/forward cache return", function () {
  var form = new El("form", { method: "post", action: "/tasks" });
  var btn = new El("button", { type: "submit" });
  btn.appendChild({ nodeType: 3, textContent: "Save" });
  form.appendChild(btn);
  __forms.push(form);
  submit(form, btn);
  flush();
  eq(btn.textContent, "Please wait…");
  advance(15001);
  eq(btn.disabled, false);
  submit(form, btn);
  flush();
  eq(btn.disabled, true);
  window.fire("pageshow", { persisted: true });
  eq(btn.disabled, false);
  eq(btn.textContent, "Save");
});

test("data-no-busy forms are left alone; a component-handled submit gets a fresh submit id", function () {
  var form = new El("form", { method: "post", action: "/chat/1/messages", "data-no-busy": "" });
  var btn = new El("button", { type: "submit" });
  var sid = new El("input", { type: "hidden", name: "submit_id" });
  sid.value = "rendered-0123456789abcd";
  form.appendChild(btn); form.appendChild(sid);
  form.addEventListener("submit", function (e) { e.preventDefault(); }); // sent with fetch by its component
  submit(form, btn);
  flush();
  ok(!btn.disabled, "not disabled");
  ok(sid.value !== "rendered-0123456789abcd" && /^[A-Za-z0-9_-]{16,64}$/.test(sid.value), "renewed: " + sid.value);
  var plain = new El("form", { method: "post", action: "/tasks" });
  var sid2 = new El("input", { type: "hidden", name: "submit_id" });
  sid2.value = "rendered-0123456789abcd";
  plain.appendChild(new El("button", { type: "submit" })); plain.appendChild(sid2);
  submit(plain, null);
  flush();
  eq(sid2.value, "rendered-0123456789abcd", "a real navigation keeps it: a double click stays one request");
});

test("no listener or component logged an error (double submit tests)", function () {
  eq(__errors.length, 0, __errors.join(" | "));
});
"""
