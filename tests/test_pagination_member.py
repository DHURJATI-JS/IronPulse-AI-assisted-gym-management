"""Member, chat and notification lists stay bounded however much data piles up (pagination audit, member + chat +
notification items): every list is a page or a keyset window bounded in SQL (LIMIT, never sliced in Python), each
page runs a constant number of statements, counts are bounded index reads, and the island asks one cheap count.

Each test seeds more rows than one page (150) and checks what is rendered, what SQL was sent and that the statement
count does not grow with the data or with the depth of the page."""

from __future__ import annotations

import html
import re
import threading
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import event, func, insert, select, text

from app import db as dbmod
from app.core.utils import bmi, local_today, utcnow
from app.db import QueryCounter
from app.models import (
    ChatMessage,
    ChatThread,
    HealthAlert,
    HealthCondition,
    MetricEntry,
    MisconductReport,
    Notification,
    User,
)
from app.models.enums import (
    AnomalyLevel,
    ConditionKind,
    NotificationCategory,
    ReportStatus,
    Role,
    Severity,
    ThreadStatus,
)
from app.routers import account as account_router
from app.routers import customer_health as health_router
from app.services import chat as chat_service
from tests.conftest import api, login_as, make_client, post_form
from tests.factories import make_customer, make_employee, make_gym, make_owner, make_plans, make_subscription

N = 150  # more rows than any page


# ============================================================================================ helpers
class Statements:
    """Every SQL statement (and its parameters) sent while the block runs."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, Any]] = []

    def _on(self, conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        self.sent.append((statement, parameters))

    def __enter__(self) -> "Statements":
        event.listen(dbmod.get_engine().sync_engine, "before_cursor_execute", self._on)
        return self

    def __exit__(self, *exc: object) -> None:
        event.remove(dbmod.get_engine().sync_engine, "before_cursor_execute", self._on)

    def on(self, table: str) -> list[str]:
        """Statements that read ``table`` (FROM / JOIN)."""
        pat = re.compile(rf"\b(FROM|JOIN)\s+{table}\b")
        return [sql for sql, _ in self.sent if pat.search(sql)]


async def get(client, url: str, *, status: int = 200) -> tuple[str, int]:
    """(page text, statements sent) of one GET."""
    with QueryCounter() as qc:
        r = await client.get(url)
    assert r.status_code == status, (url, r.status_code, r.text[:400])
    return r.text, qc.count


def hrefs(page: str, pattern: str) -> list[str]:
    return [html.unescape(m) for m in re.findall(pattern, page)]


async def bulk_members(db, n: int, prefix: str = "Member") -> list[int]:
    stamp = utcnow().timestamp()
    rows = [{"email": f"{prefix.lower()}{i}-{stamp}@example.com", "full_name": f"{prefix} {i:03d}",
             "role": Role.CUSTOMER, "timezone": "Asia/Kolkata"} for i in range(n)]
    ids = list((await db.execute(insert(User).returning(User.id), rows)).scalars().all())
    await db.commit()
    return ids


# ============================================================================================ notifications
async def seed_notes(db, user: User, n: int = N, *, read: bool = False) -> list[int]:
    ids = list((await db.execute(insert(Notification).returning(Notification.id), [
        {"user_id": user.id, "title": f"Note {i}", "body": f"Body {i}", "category": NotificationCategory.SYSTEM,
         "severity": Severity.INFO, "read_at": utcnow() if read else None} for i in range(n)
    ])).scalars().all())
    await db.commit()
    return ids


async def test_notification_history_walks_150_in_keyset_windows_of_20(client, db):
    me = await make_customer(db)
    ids = await seed_notes(db, me)
    await login_as(client, me)

    seen: list[str] = []
    url: str | None = "/account/notifications"
    counts = []
    with Statements() as st:
        while url:
            page, n = await get(client, url)
            counts.append(n)
            shown = re.findall(r'<p class="font-semibold">(Note \d+)</p>', page)
            assert 0 < len(shown) <= account_router.PAGE_SIZE
            seen += shown
            older = hrefs(page, r'href="(/account/notifications\?before=\d+)"')
            url = older[0] if older else None
    assert seen == [f"Note {i}" for i in range(N - 1, -1, -1)]  # every note once, newest first
    assert len(counts) == -(-N // account_router.PAGE_SIZE) and len(set(counts)) == 1  # depth costs nothing extra
    windows = [s for s in st.on("notifications") if "ORDER BY" in s]
    assert windows and all("LIMIT" in s and "OFFSET" not in s for s in windows)  # keyset, never OFFSET
    assert all("LIMIT" in s for s in st.on("notifications") if "count(" in s)  # the tab counts are bounded too
    # the oldest window links back up, and the first window has no "Back to the newest"
    first, _ = await get(client, "/account/notifications")
    assert "Back to the newest" not in first and 'All <span class="ml-1 tabular-nums muted">150</span>' in first
    last, _ = await get(client, f"/account/notifications?before={ids[1]}")
    assert ">Note 0<" in last and "Back to the newest" in last and hrefs(last, r'href="(/account/notifications\?after=\d+)"')


async def test_notification_counts_stop_at_the_cap(client, db, monkeypatch):
    me = await make_customer(db)
    await seed_notes(db, me)
    monkeypatch.setattr(account_router, "COUNT_CAP", 99)
    await login_as(client, me)
    page, _ = await get(client, "/account/notifications")
    assert 'All <span class="ml-1 tabular-nums muted">99+</span>' in page and "data-unread-total>99+<" in page
    r = await client.get("/api/notifications/unread")
    assert r.json() == {"unread": 99, "capped": True}
    r = await client.get("/api/notifications?limit=5")
    assert r.json()["unread"] == 99 and r.json()["unread_capped"] is True


async def test_bell_feed_is_a_keyset_window_and_the_badge_one_bounded_count(client, db):
    me = await make_customer(db)
    other = await make_customer(db)
    await seed_notes(db, other, 30)
    ids = await seed_notes(db, me)
    await login_as(client, me)

    got: list[int] = []
    counts: list[int] = []
    url: str | None = "/api/notifications?limit=50"
    with Statements() as st:
        while url:
            with QueryCounter() as qc:
                data = (await client.get(url)).json()
            counts.append(qc.count)
            got += [i["id"] for i in data["items"]]
            assert len(data["items"]) <= 50 and data["unread"] == N and data["unread_capped"] is False
            url = f"/api/notifications?limit=50&before_id={data['next_before_id']}" if data["has_more"] else None
    assert got == ids[::-1] and len(set(counts)) == 1
    feed = [s for s in st.on("notifications") if "ORDER BY" in s]
    assert len(feed) == 3 and all("LIMIT" in s and "OFFSET" not in s for s in feed)

    # the island's page-load call: one bounded count on notifications, no feed (the old ?limit=1 call read both)
    with Statements() as st:
        r = await client.get("/api/notifications/unread")
    assert r.json() == {"unread": N, "capped": False}
    reads = st.on("notifications")
    assert len(reads) == 1 and "LIMIT" in reads[0] and "ORDER BY" not in reads[0]
    with Statements() as st:
        await client.get("/api/notifications?limit=1")
    assert len(st.on("notifications")) == 2

    for bad in ("0", "abc", str(2**63)):
        assert (await client.get(f"/api/notifications?before_id={bad}")).status_code == 422


async def test_notification_page_statement_count_is_constant(client, db):
    me = await make_customer(db)
    await login_as(client, me)
    await seed_notes(db, me, 3)
    await get(client, "/account/notifications")  # warm-up
    _, few = await get(client, "/account/notifications")
    ids = await seed_notes(db, me)
    _, many = await get(client, "/account/notifications")
    _, deep = await get(client, f"/account/notifications?before={ids[10]}")
    _, unread = await get(client, f"/account/notifications?filter=unread&after={ids[3]}")
    assert few == many == deep == unread


# ============================================================================================ island (JavaScriptCore)
ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
ISLAND_JS = ROOT / "app" / "static" / "src" / "js" / "05-island.js"


def _jsc() -> str | None:
    import os
    import shutil

    for candidate in (os.environ.get("JSC"), shutil.which("jsc"),
                      "/System/Library/Frameworks/JavaScriptCore.framework/Versions/Current/Helpers/jsc"):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


ISLAND_HARNESS = r"""
var window = globalThis;
var results = [];
function check(ok, name) { results.push((ok ? "PASS " : "FAIL ") + name); }
var store = {};
var storageBroken = false;
window.sessionStorage = {
  getItem: function (k) { if (storageBroken) throw new Error("denied"); return Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null; },
  setItem: function (k, v) { if (storageBroken) throw new Error("denied"); store[k] = String(v); },
};
window.setInterval = function () { return 0; };
window.setTimeout = function () { return 0; };
function El(tag, attrs) {
  this.tag = tag || "div"; this.attrs = attrs || {}; this.children = []; this.parent = null; this.style = {};
  this.listeners = {}; this._text = ""; this.disabled = false; this.classList = { toggle: function () {} };
}
Object.defineProperty(El.prototype, "textContent", {
  get: function () { return this._text + this.children.map(function (c) { return c.textContent; }).join(""); },
  set: function (v) { this.children.forEach(function (c) { c.parent = null; }); this.children = []; this._text = String(v); },
});
Object.defineProperty(El.prototype, "firstChild", { get: function () { return this.children[0] || null; } });
El.prototype.getAttribute = function (k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; };
El.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); };
El.prototype.removeAttribute = function (k) { delete this.attrs[k]; };
El.prototype.addEventListener = function (t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); };
El.prototype.contains = function () { return false; };
El.prototype.appendChild = function (c) { if (c) { c.parent = this; this.children.push(c); } return c; };
El.prototype.remove = function () {
  if (!this.parent) return;
  var i = this.parent.children.indexOf(this);
  if (i >= 0) this.parent.children.splice(i, 1);
  this.parent = null;
};
El.prototype.find = function (attr) {
  for (var i = 0; i < this.children.length; i++) {
    var c = this.children[i];
    if (c.getAttribute(attr) !== null) return c;
    var deep = c.find(attr);
    if (deep) return deep;
  }
  return null;
};
El.prototype.querySelector = function (sel) { var m = /^\[([a-z-]+)\]$/.exec(sel); return m ? this.find(m[1]) : null; };
var document = {
  body: new El("body", { "data-user-id": "7" }),
  documentElement: new El("html"),
  querySelectorAll: function () { return []; },
  addEventListener: function () {},
};
var calls = [];
var handlers = {};
var registered = null;
var replies = {};
var IP = {
  register: function (name, fn) { if (name === "island") registered = fn; },
  on: function (type, fn) { (handlers[type] = handlers[type] || []).push(fn); },
  api: function (url) {
    calls.push(url);
    var data = replies[url] || { unread: 3 };
    return { then: function (ok) { ok(data); return { catch: function () {} }; } };
  },
  h: function (tag, attrs) {
    var el = new El(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      var v = attrs[k];
      if (v == null || v === false) return;
      if (k === "text") el._text = String(v);
      else el.attrs[k] = v === true ? "" : String(v);
    });
    for (var i = 2; i < arguments.length; i++) if (arguments[i] != null) el.appendChild(arguments[i]);
    return el;
  },
  timeAgo: function () { return "just now"; },
};
window.IP = IP;
load(arguments[0]);

function island() {
  var el = new El("div", { "data-auth": "1", "data-ai-state": "online" });
  var parts = {};
  ["[data-title]", "[data-body]", "[data-icon]", "[data-unread]", "[data-clock]", "[data-list]", "[data-island-ai]", "[data-ai-text]"].forEach(function (s) { parts[s] = new El(); });
  var layers = ["idle", "expanded", "panel"].map(function (n) { return new El("div", { "data-layer": n }); });
  el.querySelector = function (s) { return parts[s] || null; };
  el.querySelectorAll = function (s) { return s === "[data-layer]" ? layers : []; };
  el.badge = parts["[data-unread]"];
  el.list = parts["[data-list]"];
  return el;
}
function boot() { calls = []; handlers = {}; var el = island(); registered(el); return el; }
function emit(type, evt) { (handlers[type] || []).forEach(function (fn) { fn(evt); }); }
function cached() { return JSON.parse(store["ip.island.unread"] || "null"); }
function notes(from, n) {
  var out = [];
  for (var i = 0; i < n; i++) out.push({ id: from - i, title: "Note " + (from - i), body: "b", severity: "info", created_at: "2026-11-10T10:00:00Z", read: false });
  return out;
}

check(typeof registered === "function", "05-island.js registers the island");
var el = boot();
check(calls.length === 1 && calls[0] === "/api/notifications/unread", "page load asks the one-count endpoint (not the feed): " + calls.join(","));
check(el.badge.textContent === "3" && el.badge.getAttribute("hidden") === null, "badge shows the server count");
var first = cached();
check(first && first.n === 3 && first.u === "7", "count cached for this tab and user");

el = boot();
check(calls.length === 0, "next page within the TTL: no request at all");
check(el.badge.textContent === "3", "badge restored from the cache");

emit("notification", { title: "New", body: "x" });
check(el.badge.textContent === "4" && cached().n === 4, "a live notification bumps the badge and the cache");
check(cached().t === first.t, "local changes never extend the cache's age");

// the panel: newest window + "Show older" (keyset load-more with next_before_id)
replies["/api/notifications?limit=20"] = { items: notes(60, 20), unread: 25, has_more: true, next_before_id: 41 };
replies["/api/notifications?limit=20&before_id=41"] = { items: notes(40, 5), unread: 25, has_more: false, next_before_id: null };
IP.island.open();
check(el.list.children.length === 21 && el.badge.textContent === "25", "panel: 20 newest + a Show older button, fresh count");
var older = el.list.find("data-island-more");
check(older && older.textContent === "Show older", "Show older offered while the feed has more");
older.listeners.click[0]({ stopPropagation: function () {} });
check(calls[calls.length - 1] === "/api/notifications?limit=20&before_id=41", "Show older asks the next keyset window: " + calls[calls.length - 1]);
check(el.list.children.length === 25 && !el.list.find("data-island-more"), "older notes appended below, no button at the end of the feed");
check(el.list.children[24].textContent.indexOf("Note 36") === 0, "appended in order (newest first)");

store["ip.island.unread"] = JSON.stringify({ u: "7", n: 4, t: Date.now() - 31000 });
el = boot();
check(calls.length === 1, "an expired cache asks the server again");
store["ip.island.unread"] = JSON.stringify({ u: "8", n: 9, t: Date.now() });
el = boot();
check(calls.length === 1 && el.badge.textContent === "3", "another user's cache is ignored");
store["ip.island.unread"] = JSON.stringify({ u: "7", n: 5, t: Date.now() + 60000 });
el = boot();
check(calls.length === 1, "a cache from the future is ignored");
IP.island.setUnread(0);
check(el.badge.getAttribute("hidden") === "" && cached().n === 0, "setUnread (server-confirmed) hides the badge and refreshes the cache");
storageBroken = true;
el = boot();
check(calls.length === 1 && el.badge.textContent === "3", "storage disabled: still works, asks every time");
print(results.join("\n"));
print("SUMMARY failed=" + results.filter(function (r) { return r.indexOf("FAIL") === 0; }).length + " total=" + results.length);
"""


@pytest.mark.skipif(_jsc() is None, reason="JavaScriptCore (jsc) is not available on this machine")
def test_island_asks_one_cheap_count_and_caches_it_per_tab(tmp_path):
    import subprocess

    harness = tmp_path / "island_harness.js"
    harness.write_text(ISLAND_HARNESS, encoding="utf-8")
    proc = subprocess.run([_jsc(), str(harness), "--", str(ISLAND_JS)], capture_output=True, text=True, timeout=60)
    out = proc.stdout + proc.stderr
    m = re.search(r"SUMMARY failed=(\d+) total=(\d+)", out)
    assert m, out[-3000:]
    assert m.group(1) == "0" and int(m.group(2)) >= 17, out


# ============================================================================================ chat: conversation list
async def chat_world(db) -> dict[str, Any]:
    owner = await make_owner(db, full_name="Olivia Owner")
    gym = await make_gym(db, owner, name="Pulse Indiranagar")
    plans = await make_plans(db, gym)
    return {"owner": owner, "gym": gym, "plans": plans}


async def seed_threads(db, gym_id: int, n: int = N) -> list[int]:
    """n team threads of n members, thread i last active i minutes ago (thread 0 = most recent)."""
    members = await bulk_members(db, n)
    now = utcnow()
    ids = list((await db.execute(insert(ChatThread).returning(ChatThread.id), [
        {"gym_id": gym_id, "customer_id": uid, "employee_id": None, "subject": f"Question {i:03d}",
         "status": ThreadStatus.OPEN, "last_message_at": now - timedelta(minutes=i), "last_message_preview": f"hello {i:03d}",
         "customer_unread": 0, "staff_unread": 1} for i, uid in enumerate(members)
    ])).scalars().all())
    await db.commit()
    return ids


async def test_conversation_list_is_a_keyset_window_of_50(db):
    w = await chat_world(db)
    ids = await seed_threads(db, w["gym"].id)
    async with make_client() as c:
        await login_as(c, w["owner"])
        seen: list[int] = []
        url: str | None = "/chat"
        counts = []
        with Statements() as st:
            while url:
                page, n = await get(c, url)
                counts.append(n)
                items = [int(i) for i in re.findall(r'data-thread-item="(\d+)"', page)]
                assert 0 < len(items) <= chat_service.THREAD_PAGE_SIZE
                seen += items
                more = hrefs(page, r'href="(/chat\?older=[^"]+)" class="btn btn-secondary btn-sm w-full" data-chat-more-threads')
                url = more[0] if more else None
        assert seen == ids  # most recent activity first, each conversation once
        assert len(counts) == 3 and len(set(counts)) == 1
        lists = [s for s in st.on("chat_threads") if "ORDER BY coalesce" in s]
        assert len(lists) == 3 and all("LIMIT" in s and "OFFSET" not in s for s in lists)

        # the JSON window used by "Load more" (60-chat.js) - same cursor, same items
        first = (await c.get("/api/chat/threads")).json()
        assert [i["id"] for i in first["items"]] == ids[:50] and first["has_more"] is True
        nxt = (await c.get(first["more_api"])).json()
        assert [i["id"] for i in nxt["items"]] == ids[50:100]
        assert nxt["items"][0]["href"] == f"/chat/{ids[50]}" and nxt["items"][0]["unread"] == 1
        last = (await c.get(nxt["more_api"])).json()
        assert [i["id"] for i in last["items"]] == ids[100:] and last["has_more"] is False and last["more_api"] is None

        # opening a conversation from an older window keeps that window (and the opened thread) in the sidebar
        page, _ = await get(c, f"/chat/{ids[120]}?{first['more_api'].split('?', 1)[1]}")
        assert f'data-thread-item="{ids[120]}"' in page and "Most recent conversations" in page
        # junk cursors fall back to the newest window
        page, _ = await get(c, "/chat?older=not-a-cursor")
        assert re.findall(r'data-thread-item="(\d+)"', page)[0] == str(ids[0])


async def test_conversation_search_is_scoped_to_the_viewers_threads(db):
    w = await chat_world(db)
    await seed_threads(db, w["gym"].id, 60)
    other = await make_owner(db)
    other_gym = await make_gym(db, other)
    stranger = await make_customer(db, full_name="Member 007 Elsewhere")
    db.add(ChatThread(gym_id=other_gym.id, customer_id=stranger.id, subject="Elsewhere", status=ThreadStatus.OPEN))
    await db.commit()
    async with make_client() as c:
        await login_as(c, w["owner"])
        with Statements() as st:
            page, _ = await get(c, "/chat?q=Member+007")
        assert re.findall(r'data-thread-item="(\d+)"', page) and "Member 007" in page and "Elsewhere" not in page
        search = [s for s in st.on("chat_threads") if "ILIKE" in s]
        assert search and all("EXISTS (SELECT" in s for s in search)
        assert all(not re.search(r"customer_id IN \(SELECT users\.id", s) for s in search)  # no platform-wide scan
        assert all(not re.search(r"employee_id IN \(SELECT employees\.id", s) for s in search)
        page, _ = await get(c, "/chat?q=question+042")  # subject search still works
        assert len(re.findall(r'data-thread-item="(\d+)"', page)) == 1


async def test_conversation_list_statement_count_is_constant(db):
    w = await chat_world(db)
    async with make_client() as c:
        await login_as(c, w["owner"])
        await seed_threads(db, w["gym"].id, 3)
        await get(c, "/chat")  # warm-up
        _, few = await get(c, "/chat")
        await seed_threads(db, w["gym"].id, N)
        _, many = await get(c, "/chat")
        data = (await c.get("/api/chat/threads")).json()
        _, deep = await get(c, data["more_url"])
    assert few == many == deep


# ============================================================================================ chat: messages
async def test_message_pages_and_the_email_throttle_probe_use_their_indexes(db):
    w = await chat_world(db)
    member = await make_customer(db, full_name="Maya Member")
    await make_subscription(db, member, w["plans"][0])
    thread = await chat_service.get_or_create_thread(db, gym_id=w["gym"].id, customer_id=member.id, employee_id=None)
    await db.commit()
    quiet = 3000  # a long conversation that has gone quiet (days ago: the member's daily message cap is not reached) ...
    went_quiet = utcnow() - timedelta(days=2)
    await db.execute(insert(ChatMessage), [
        {"thread_id": thread.id, "sender_id": member.id, "sender_role": "customer", "body": f"msg {i}",
         "created_at": went_quiet + timedelta(milliseconds=i)} for i in range(quiet)
    ])
    busy_member = await make_customer(db)
    await make_subscription(db, busy_member, w["plans"][0])
    busy = await chat_service.get_or_create_thread(db, gym_id=w["gym"].id, customer_id=busy_member.id, employee_id=None)
    await db.commit()
    await db.execute(insert(ChatMessage), [  # ... behind a busier, newer one: its page must not scan past it or sort it
        {"thread_id": busy.id, "sender_id": busy_member.id, "sender_role": "customer", "body": "busy"} for _ in range(3000)
    ])
    # the owner's long notification history (all older than the 15-minute throttle window)
    await db.execute(insert(Notification), [
        {"user_id": w["owner"].id, "title": "old", "body": "b", "category": NotificationCategory.MESSAGE,
         "severity": Severity.INFO, "link": f"/chat/{thread.id}", "emailed": True} for _ in range(3000)
    ])
    await db.execute(Notification.__table__.update().where(Notification.user_id == w["owner"].id)
                     .values(created_at=utcnow() - timedelta(days=2)))
    await db.commit()
    async with dbmod.get_engine().begin() as conn:
        await conn.execute(text("ANALYZE notifications"))
        await conn.execute(text("ANALYZE chat_messages"))

    with Statements() as st:
        msgs, more = await chat_service.list_messages(db, thread.id)
        await chat_service.post_message(db, thread=thread, sender=member, body="hello again", sender_role="customer")
    await db.commit()
    assert len(msgs) == chat_service.PAGE_SIZE and more is True
    page_sql = next(sent for sent in st.sent if re.search(r"FROM chat_messages\b", sent[0]) and "ORDER BY" in sent[0])
    probe = next(sent for sent in st.sent if "FROM notifications" in sent[0] and "notifications.emailed" in sent[0])
    assert "LIMIT" in probe[0] and "notifications.created_at >=" in probe[0]

    async def plan(sql: str, params: Any) -> str:
        async with dbmod.get_engine().connect() as conn:
            raw = (await conn.get_raw_connection()).driver_connection
            async with raw.transaction():
                await raw.execute("SET LOCAL enable_seqscan = off")
                rows = await raw.fetch("EXPLAIN " + sql, *(params or ()))
        return "\n".join(r[0] for r in rows)

    probe_plan = await plan(*probe)  # either index bounds the read to the last 15 minutes
    assert "ix_notifications_user_created" in probe_plan or "ix_notifications_created_at" in probe_plan, probe_plan
    page_plan = await plan(*page_sql)  # a backward read of (thread_id, id) that stops after 51 rows
    assert "ix_chat_messages_thread_id_id" in page_plan and "Sort" not in page_plan, page_plan
    # the newest-message window really is the newest 50
    assert [m.body for m in msgs] == [f"msg {i}" for i in range(quiet - 50, quiet)]


async def test_chat_ids_beyond_bigint_are_not_found_instead_of_a_driver_error(db):
    w = await chat_world(db)
    member = await make_customer(db)
    await make_subscription(db, member, w["plans"][0])
    thread = await chat_service.get_or_create_thread(db, gym_id=w["gym"].id, customer_id=member.id, employee_id=None)
    await chat_service.post_message(db, thread=thread, sender=member, body="hi", sender_role="customer")
    await db.commit()
    huge = str(2**70)
    async with make_client() as c:
        await login_as(c, member)
        assert (await c.get(f"/chat/{huge}")).status_code == 404
        assert (await c.get(f"/api/chat/{huge}/messages")).status_code == 404
        r = await c.get(f"/api/chat/{thread.id}/messages?before={huge}")
        assert r.status_code == 200 and [m["body"] for m in r.json()["items"]] == ["hi"]
        r = await c.get(f"/api/chat/{thread.id}/messages?after={huge}")
        assert r.status_code == 200 and r.json()["items"] == []


# ============================================================================================ misconduct reports
async def report_world(db) -> dict[str, Any]:
    w = await chat_world(db)
    emp = await make_employee(db, w["gym"])
    member = await make_customer(db, full_name="Rita Reporter")
    await make_subscription(db, member, w["plans"][0])
    return {**w, "emp": emp, "member": member}


async def seed_reports(db, w: dict[str, Any], n: int) -> None:
    # answered reports: a member holds ONE open report per staff member (uq_misconduct_reports_one_open, F12)
    await db.execute(insert(MisconductReport), [
        {"gym_id": w["gym"].id, "employee_id": w["emp"].id, "reporter_id": w["member"].id, "category": "rude",
         "description": f"Report number {i:03d} about rude behaviour", "status": ReportStatus.DECLINED,
         "admin_response": "We looked into it and spoke to the trainer."} for i in range(n)
    ])
    await db.commit()


async def test_my_reports_are_paginated(db):
    w = await report_world(db)
    async with make_client() as c:
        await login_as(c, w["member"])
        await seed_reports(db, w, 2)
        await get(c, "/reports")  # warm-up
        _, few = await get(c, "/reports")
        await seed_reports(db, w, N)
        with Statements() as st:
            page, many = await get(c, "/reports")
        assert page.count("Report number") == 30 and 'aria-label="Report pages"' in page
        listing = [s for s in st.on("misconduct_reports") if "ORDER BY" in s]
        assert len(listing) == 1 and "LIMIT" in listing[0]
        last, deep = await get(c, "/reports?page=6")
        assert last.count("Report number") == 2 and "Page <span" in last
        assert few == many == deep


async def test_owner_report_search_matches_only_the_owners_own_reports(db):
    w = await report_world(db)
    await seed_reports(db, w, 3)
    rival = await make_owner(db)
    rival_gym = await make_gym(db, rival)
    zed = await make_employee(db, rival_gym)
    zed.full_name = "Zed Elsewhere"
    await db.commit()
    async with make_client() as c:
        await login_as(c, w["owner"])
        with Statements() as st:
            page, _ = await get(c, "/owner/reports?q=" + w["emp"].full_name.split()[0])
        assert page.count("Report number") == 3
        search = [s for s in st.on("misconduct_reports") if "ILIKE" in s]
        assert search and all("EXISTS (SELECT" in s and "IN (SELECT employees.id" not in s for s in search)
        page, _ = await get(c, "/owner/reports?q=Zed")
        assert "Report number" not in page


# ============================================================================================ health history
async def seed_entries(db, user: User, n: int = N) -> None:
    today = local_today(user.timezone)
    await db.execute(insert(MetricEntry), [
        {"customer_id": user.id, "recorded_on": today - timedelta(days=i), "height_cm": 175.0,
         "weight_kg": 70.0 + (i % 5) * 0.1, "bmi": bmi(175.0, 70.0 + (i % 5) * 0.1), "confirmed": True,
         "anomaly_level": AnomalyLevel.NONE, "anomaly_reasons": []} for i in range(n)
    ])
    await db.commit()


ROW = 'class="whitespace-nowrap font-medium">'  # the date cell of a History row


async def test_metric_history_is_paginated_in_sql(client, db, ai_down):
    user = await make_customer(db)
    await login_as(client, user)
    await seed_entries(db, user, 40)
    await get(client, "/me/health")  # warm-up (caches the AC advice once)
    _, small = await get(client, "/me/health")
    await db.execute(MetricEntry.__table__.delete().where(MetricEntry.customer_id == user.id))
    await db.commit()
    await seed_entries(db, user)
    with Statements() as st:
        page, big = await get(client, "/me/health")
    assert page.count(ROW) == health_router.HISTORY_PER_PAGE and "150 readings · page 1 of 5" in page
    assert 'aria-label="History pages"' in page and "/me/health?page=2#history" in page
    assert all("LIMIT" in s for s in st.on("metric_entries"))  # no unbounded read of the history
    oldest = (local_today(user.timezone) - timedelta(days=N - 1)).strftime("%d %b %Y")
    assert oldest not in page
    last, deep = await get(client, "/me/health?page=5")
    assert last.count(ROW) == 30 and oldest in last and "page 5 of 5" in last
    beyond, _ = await get(client, "/me/health?page=99")
    assert "page 5 of 5" in beyond
    assert small == big == deep


async def seed_alerts(db, user: User, n: int = N) -> list[int]:
    ids = list((await db.execute(insert(HealthAlert).returning(HealthAlert.id), [
        {"customer_id": user.id, "level": AnomalyLevel.WARNING, "title": f"Alert {i:03d}", "message": "Check again.",
         "source": "rule"} for i in range(n)
    ])).scalars().all())
    await db.commit()
    return ids


async def test_alert_history_page_is_paginated(client, db, ai_down):
    user = await make_customer(db)
    await login_as(client, user)
    await seed_alerts(db, user, 3)
    await get(client, "/me/health/alerts")  # warm-up
    _, small = await get(client, "/me/health/alerts")
    ids = await seed_alerts(db, user)
    health, _ = await get(client, "/me/health")
    assert health.count('action="/me/health/alerts/') == health_router.ALERTS_SHOWN
    assert 'href="/me/health/alerts"' in health  # "All alerts"
    with Statements() as st:
        page, big = await get(client, "/me/health/alerts")
    assert page.count('action="/me/health/alerts/') == health_router.ALERTS_PER_PAGE
    assert "All alerts · 153" in page and 'aria-label="Alert pages"' in page
    listing = [s for s in st.on("health_alerts") if "ORDER BY" in s]
    assert len(listing) == 1 and "LIMIT" in listing[0] and "OFFSET" in listing[0]
    last, deep = await get(client, "/me/health/alerts?page=8")
    assert last.count('action="/me/health/alerts/') == 13 and "Alert 000" in last
    assert small == big == deep
    # "Mark as read" returns to the same page of the history (and only to known pages)
    r = await post_form(client, f"/me/health/alerts/{ids[0]}/ack", {"next": "/me/health/alerts?page=8"})
    assert r.status_code == 303 and r.headers["location"] == "/me/health/alerts?page=8"
    r = await post_form(client, f"/me/health/alerts/{ids[1]}/ack", {"next": "https://evil.example/"})
    assert r.headers["location"] == "/me/health"
    other = await make_customer(db)
    async with make_client() as c2:
        await login_as(c2, other)
        mine, _ = await get(c2, "/me/health/alerts")
        assert "Alert 0" not in mine and "No alerts yet" in mine


# ============================================================================================ health conditions
async def seed_conditions(db, user: User, n: int, start: int = 0) -> None:
    await db.execute(insert(HealthCondition), [
        {"customer_id": user.id, "kind": ConditionKind.CONDITION, "name": f"Condition {i:03d}", "tag": None,
         "notes": "x" * 200, "source": "owner"} for i in range(start, start + n)
    ])
    await db.commit()


async def test_a_member_lists_at_most_max_conditions(client, db, ai_down):
    user = await make_customer(db)
    await login_as(client, user)
    await seed_conditions(db, user, health_router.MAX_CONDITIONS)
    r = await post_form(client, "/me/health/profile/conditions", {"kind": "allergy", "name": "Peanut allergy"})
    assert r.status_code == 400 and "the most it can hold" in html.unescape(r.text)
    held = await db.scalar(select(func.count()).select_from(HealthCondition).where(HealthCondition.customer_id == user.id))
    assert held == health_router.MAX_CONDITIONS
    # one fewer: the add goes through
    await db.execute(HealthCondition.__table__.delete().where(HealthCondition.name == "Condition 000"))
    await db.commit()
    r = await post_form(client, "/me/health/profile/conditions", {"kind": "allergy", "name": "Peanut allergy"})
    assert r.status_code == 303


async def test_condition_readers_are_bounded_and_embedding_runs_off_the_event_loop(client, db, ai_down, monkeypatch):
    user = await make_customer(db)
    await login_as(client, user)
    await seed_conditions(db, user, N)  # data from before the cap existed
    with Statements() as st:
        page, _ = await get(client, "/me/health/profile")
    assert page.count('action="/me/health/profile/conditions/') == health_router.CONDITIONS_SHOWN
    assert "Showing the first 100 items" in page and ">100+<" in page
    assert all("LIMIT" in s for s in st.on("health_conditions") if "ORDER BY" in s)
    health_page, _ = await get(client, "/me/health")
    assert health_page

    calls: list[tuple[int, bool]] = []
    real_embed = health_router.embed

    def recording_embed(text_: str) -> list[float]:
        calls.append((len(text_), threading.current_thread() is not threading.main_thread()))
        return real_embed(text_)

    monkeypatch.setattr(health_router, "embed", recording_embed)
    first = await db.scalar(select(HealthCondition.id).where(HealthCondition.customer_id == user.id)
                            .order_by(HealthCondition.id))
    with Statements() as st:
        r = await post_form(client, f"/me/health/profile/conditions/{first}/delete")
    assert r.status_code == 303
    assert calls and all(n <= health_router.CONDITIONS_TEXT_MAX and off_loop for n, off_loop in calls)
    lists = [s for s in st.on("health_conditions") if s.lstrip().upper().startswith("SELECT") and "ORDER BY" in s]
    assert lists and all("LIMIT" in s for s in lists)
