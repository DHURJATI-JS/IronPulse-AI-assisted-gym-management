"""Load safety of the calendar and the task lists (approved limits; app/services/tasks.py):

* the month grid reads at most CALENDAR_DAY_MAX tasks a day (a per-day LIMIT in SQL) plus each day's true count: a
  crowded day says "+N more" and links to that day's list, ``GET /tasks?day=YYYY-MM-DD`` (paginated);
* ``GET /tasks`` is paginated (``ui.pager``) with true totals - open, due today, overdue and per group;
* one person holds at most ``open_task_limit()`` open tasks: the form answers 409 with a friendly message, reopening a
  done task counts too, the assistant answers in words (never a 500), and two requests at once can't both slip in;
* constant query counts whatever a person holds.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import event, func, insert, select

from app import db as dbmod
from app.config import Settings, get_settings
from app.core.utils import to_local, utcnow
from app.db import QueryCounter
from app.models import AssistantMessage, Task, User
from app.models.enums import TaskKind
from app.services import tasks as task_service
from tests.conftest import api, login_as, post_form
from tests.factories import make_customer

DAY = date(2027, 3, 10)  # a fixed future Wednesday: the calendar tests never depend on "today"
NOW = datetime(2027, 3, 1, 9, 0, tzinfo=UTC)


def _at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


async def _bulk(db, user: User, rows: list[dict[str, Any]]) -> None:
    await db.execute(insert(Task), [{"user_id": user.id, "kind": TaskKind.TASK, "all_day": False, **r} for r in rows])
    await db.commit()


class Loads:
    """Task rows the ORM materialises while the block runs (what a page really reads)."""

    def __init__(self) -> None:
        self.n = 0

    def _on(self, target: Any, context: Any) -> None:
        self.n += 1

    def __enter__(self) -> "Loads":
        event.listen(Task, "load", self._on)
        return self

    def __exit__(self, *exc: object) -> None:
        event.remove(Task, "load", self._on)


def _pager(text: str) -> str:
    """The pager's caption as people read it ("Page 1 of 3 · 120 tasks ..."), tags removed."""
    nav = text[text.index("data-pager"):]
    return " ".join(re.sub(r"<[^>]+>", " ", nav[:nav.index("</nav>")]).split())


async def _crowded(db, user: User, n: int = 40) -> None:
    """``n`` tasks on DAY (every 5th one done) and two on the next day."""
    await _bulk(db, user, [{"title": f"Crowded {i:02d}", "due_at": _at(DAY, 6 + i % 12, i % 60),
                            "completed_at": NOW if i % 5 == 0 else None} for i in range(n)]
                + [{"title": f"Calm {i}", "due_at": _at(DAY + timedelta(days=1), 9 + i)} for i in range(2)])


# ============================================================================================ month grid
async def test_a_crowded_day_loads_its_first_tasks_and_links_the_rest(db):
    user = await make_customer(db, timezone="UTC")
    await _crowded(db, user)
    with Loads() as loads:
        view = await task_service.build_month(db, user, f"{DAY:%Y-%m}", DAY.isoformat(), now=NOW)
    cap = task_service.CALENDAR_DAY_MAX
    assert loads.n == cap + 2  # never the whole day: CALENDAR_DAY_MAX of the crowded day + the calm day's two
    crowded = next(d for w in view.weeks for d in w if d.day == DAY)
    assert len(crowded.items) == cap and crowded.more == 40 - cap and crowded.count == 40 and view.truncated
    assert all(not i.done for i in crowded.items)  # open tasks first: the 8 done ones are the ones left out
    calm = next(d for w in view.weeks for d in w if d.day == DAY + timedelta(days=1))
    assert len(calm.items) == 2 and calm.more == 0
    days = view.days_json()
    more = days[DAY.isoformat()][-1]
    assert more["kind"] == "more" and more["link"] == f"/tasks?day={DAY.isoformat()}" and more["task_id"] is None
    assert more["title"] == f"{40 - cap} more tasks" and more["time"] == f"Showing {cap} of 40"
    assert len(days[DAY.isoformat()]) == cap + 1 and all(i["kind"] != "more" for i in days[(DAY + timedelta(days=1)).isoformat()])
    assert view.counts()["task"] == 42  # the month's chips count every task, not just the loaded ones


async def test_the_month_page_says_plus_n_more_and_links_the_days_list(client, db):
    user = await make_customer(db, timezone="UTC")
    await _crowded(db, user)
    await login_as(client, user)
    r = await client.get(f"/calendar?month={DAY:%Y-%m}&day={DAY.isoformat()}")
    assert r.status_code == 200
    text = html.unescape(r.text)
    cell = text[text.index(f'data-day="{DAY.isoformat()}"'):]
    cell = cell[:cell.index("</a>")]
    assert "+37 more" in cell and "40 items" in cell  # 3 pills shown in the cell, the day holds 40
    panel = text[text.index("data-day-panel"):text.index("Quick add")]
    assert f'href="/tasks?day={DAY.isoformat()}"' in panel and "25 more tasks" in panel and "Showing 15 of 40" in panel
    assert "42 to-dos" in text
    days = json.loads(re.search(r'data-days="([^"]*)"', r.text).group(1).replace("&#34;", '"').replace("&quot;", '"')
                      .replace("&amp;", "&").replace("&#39;", "'"))
    assert days[DAY.isoformat()][-1]["link"] == f"/tasks?day={DAY.isoformat()}"


async def test_the_month_grid_issues_the_same_statements_whatever_a_day_holds(client, db):
    user = await make_customer(db, timezone="Asia/Kolkata")
    await _bulk(db, user, [{"title": "One", "due_at": _at(DAY, 4)}])
    await login_as(client, user)
    url = f"/calendar?month={DAY:%Y-%m}"
    await client.get(url)
    with QueryCounter() as small:
        assert (await client.get(url)).status_code == 200
    await _crowded(db, user, 120)
    with QueryCounter() as big, Loads() as loads:
        assert (await client.get(url)).status_code == 200
    assert small.count == big.count and loads.n <= task_service.CALENDAR_DAY_MAX + 3


# ============================================================================================ one day's list
async def test_the_days_list_pages_through_everything_on_that_day(client, db):
    user = await make_customer(db, timezone="UTC")
    await _crowded(db, user, 120)
    await login_as(client, user)
    first = await client.get(f"/tasks?day={DAY.isoformat()}")
    assert first.status_code == 200
    text = html.unescape(first.text)
    assert "Wednesday 10 March 2027" in text and "On this day" in text and 'data-day-total>120<' in text
    assert text.count('id="task-') == task_service.TASKS_PER_PAGE and "Page 1 of 3 · 120 tasks on this day" in _pager(text)
    assert f'href="/calendar?month={DAY:%Y-%m}&day={DAY.isoformat()}"' in first.text.replace("&amp;", "&")
    assert "Calm 0" not in text  # only that day
    last = html.unescape((await client.get(f"/tasks?day={DAY.isoformat()}&page=3")).text)
    assert last.count('id="task-') == 20 and "Page 3 of 3" in _pager(last)
    assert last.count("line-through") >= 20  # done tasks come last
    with QueryCounter() as qc:
        assert (await client.get(f"/tasks?day={DAY.isoformat()}&page=2")).status_code == 200
    with QueryCounter() as qc_open:
        assert (await client.get("/tasks?day=not-a-day")).status_code == 200  # junk: the open-tasks list
    assert qc.count < qc_open.count  # one statement for the day's page, three for the open list


# ============================================================================================ /tasks
async def test_the_tasks_page_is_paginated_with_true_totals(client, db):
    user = await make_customer(db, timezone="UTC")
    now = utcnow()
    today = to_local(now, "UTC").date()
    rows = ([{"title": f"Late {i}", "due_at": now - timedelta(days=3, minutes=i)} for i in range(10)]
            + [{"title": f"Now {i}", "due_at": datetime(today.year, today.month, today.day, 23, 0, tzinfo=UTC)
                if now.hour < 23 else now} for i in range(5)]
            + [{"title": f"Soon {i:03d}", "due_at": now + timedelta(days=2, minutes=i)} for i in range(100)]
            + [{"title": f"Someday {i}"} for i in range(15)]
            + [{"title": f"Done {i}", "due_at": now, "completed_at": now} for i in range(3)])
    await _bulk(db, user, rows)
    await login_as(client, user)
    await client.get("/tasks")
    with QueryCounter() as first_qc:
        first = await client.get("/tasks")
    text = html.unescape(first.text)
    assert first.status_code == 200
    stat = re.findall(r'<p class="kpi mt-2">([^<]*)</p>', text)
    assert stat[:3] == ["130", "5", "10"]  # open, due today, overdue: true counts, not what one page holds
    assert text.count('id="task-') == task_service.TASKS_PER_PAGE + 3  # one page + the 3 recently completed
    assert "Page 1 of 3 · 130 open tasks" in _pager(text) and 'rel="next"' in text
    chips = dict(re.findall(r'id="g-(\w+)"[^>]*>[^<]*</h2>\s*<span class="chip" data-group-total>([\d,]+)<', text))
    assert chips == {"overdue": "10", "today": "5", "upcoming": "100"}  # whole-list totals; page 1 holds 35 upcoming
    with QueryCounter() as last_qc:
        last = html.unescape((await client.get("/tasks?page=3")).text)
    assert last.count('id="task-') == 30 + 3 and "No date" in last and "Someday 14" in last and "Late 0" not in last
    assert first_qc.count == last_qc.count
    huge = html.unescape((await client.get("/tasks?page=99999&per_page=100000")).text)
    assert "Page 2 of 2" in _pager(huge)  # junk paging is clamped: 100 a page at most, past the end shows the last page


# ============================================================================================ the open-task cap
@pytest.fixture
def cap(monkeypatch):
    monkeypatch.setattr(get_settings(), "tasks_max_open", 3)  # TASKS_MAX_OPEN=3
    return 3


async def _open(db, user: User) -> int:
    return int(await db.scalar(select(func.count(Task.id)).where(Task.user_id == user.id, Task.completed_at.is_(None))))


async def test_the_form_refuses_the_task_past_the_cap_in_friendly_words(client, db, cap):
    user = await make_customer(db)
    await login_as(client, user)
    for i in range(cap):
        assert (await post_form(client, "/tasks", {"title": f"Task {i}"})).status_code == 303
    r = await post_form(client, "/tasks", {"title": "One too many", "next": "/calendar"})
    text = html.unescape(r.text)
    assert r.status_code == 409 and "You've reached the task limit" in text
    assert "You already have 3 open tasks - the most you can keep at once." in text and "then add this one" in text
    assert 'value="One too many"' in text and "Please fix the following" not in text  # what they typed comes back
    assert await _open(db, user) == cap
    first = (await db.execute(select(Task).where(Task.user_id == user.id).order_by(Task.id))).scalars().first()
    assert (await post_form(client, f"/tasks/{first.id}/toggle")).status_code == 303  # tick one off ...
    assert (await post_form(client, "/tasks", {"title": "One too many"})).status_code == 303  # ... and it fits
    assert await _open(db, user) == cap


async def test_reopening_a_done_task_counts_against_the_cap(client, db, cap):
    user = await make_customer(db)
    await _bulk(db, user, [{"title": f"Open {i}"} for i in range(cap)] + [{"title": "Done", "completed_at": utcnow()}])
    done = (await db.execute(select(Task).where(Task.title == "Done"))).scalar_one()
    await login_as(client, user)
    r = await post_form(client, f"/tasks/{done.id}/toggle", headers={"Accept": "application/json"})
    assert r.status_code == 409 and r.json()["code"] == "task_limit" and "then reopen this one" in r.json()["detail"]
    r = await post_form(client, f"/tasks/{done.id}/toggle", {"next": "/calendar"})
    assert r.status_code == 303 and r.headers["location"] == "/calendar"
    await db.refresh(done)
    assert done.completed_at is not None and await _open(db, user) == cap


async def test_the_assistant_answers_the_cap_in_words_and_keeps_nothing_of_that_message(client, db, fake_ai, cap):
    user = await make_customer(db, timezone="UTC")
    await _bulk(db, user, [{"title": f"Open {i}"} for i in range(cap - 1)])
    fake_ai(responses=[json.dumps({"reply": "Added both.", "actions": [
        {"type": "create_task", "title": "Buy protein", "due_at": None},
        {"type": "create_reminder", "title": "Stretch", "remind_at": (utcnow() + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M")},
    ]})])
    await login_as(client, user)
    r = await api(client, "POST", "/api/assistant/chat", json={"message": "add buy protein and remind me to stretch"})
    assert r.status_code == 200
    data = r.json()
    assert data["reply"].startswith("I couldn't add that. You already have 3 open tasks")
    assert data["actions"] == [{"type": "create_task", "ok": False, "detail": data["actions"][0]["detail"], "link": "/tasks"}]
    assert "Tick off or delete a few" in data["actions"][0]["detail"]
    assert await _open(db, user) == cap - 1  # the first action's task was not kept either: the message ran nothing
    assert await db.scalar(select(func.count(AssistantMessage.id))) == 0


async def test_two_requests_at_once_never_slip_past_the_cap(db, cap):
    user = await make_customer(db)
    await _bulk(db, user, [{"title": f"Open {i}"} for i in range(cap - 1)])
    data = task_service.TaskInput(title="Racer", notes=None, kind=TaskKind.TASK, due_at=None, remind_at=None, all_day=False)
    async with dbmod.get_sessionmaker()() as first, dbmod.get_sessionmaker()() as second:
        member_a, member_b = await first.get(User, user.id), await second.get(User, user.id)
        await task_service.create_task(first, member_a, data)  # holds the person's lock until it commits
        racer = asyncio.create_task(task_service.create_task(second, member_b, data))
        await asyncio.sleep(0.3)
        assert not racer.done()  # waits for the first request's transaction ...
        await first.commit()
        with pytest.raises(task_service.TaskLimitError):  # ... then counts its task
            await racer
        await second.rollback()
    assert await _open(db, user) == cap


def test_the_cap_is_the_tasks_max_open_setting(monkeypatch):
    assert Settings.model_fields["tasks_max_open"].default == task_service.MAX_OPEN_TASKS == 500
    with pytest.raises(ValidationError):
        Settings(tasks_max_open=0)  # TASKS_MAX_OPEN=0 would lock everyone out: refused at startup
    monkeypatch.setattr(get_settings(), "tasks_max_open", 7)
    assert task_service.open_task_limit() == 7
    monkeypatch.setattr(task_service, "get_settings", lambda: SimpleNamespace(tasks_max_open="lots"))
    assert task_service.open_task_limit() == task_service.MAX_OPEN_TASKS
    monkeypatch.setattr(task_service, "get_settings", lambda: SimpleNamespace())
    assert task_service.open_task_limit() == task_service.MAX_OPEN_TASKS


# ============================================================================================ the open-tasks index
async def test_open_task_reads_use_the_partial_index_never_the_completed_history(client, db):
    """Completed tasks are kept, so a long-time member's tasks grow without end; every read of their OPEN tasks - the
    /tasks page (in its own order), its totals and the TASKS_MAX_OPEN count - goes through ix_tasks_user_open
    (user_id, coalesce(due_at, remind_at), id) WHERE completed_at IS NULL: at most the cap's worth of rows, read in
    page order, whatever history sits next to them."""
    user = await make_customer(db, timezone="UTC")
    others = [await make_customer(db) for _ in range(3)]
    now = utcnow()
    # a long-time member: the completed history was created over the years (the tasks_per_person_day cap counts
    # only the last 24 hours - through ix_tasks_user_created, never this history)
    await _bulk(db, user, [{"title": f"Done {i}", "due_at": now - timedelta(days=i % 900), "completed_at": now,
                            "created_at": now - timedelta(days=1 + i % 900)} for i in range(8000)]
                + [{"title": f"Open {i}", "due_at": None if i % 7 == 0 else now + timedelta(hours=i)} for i in range(120)])
    for other in others:
        await _bulk(db, other, [{"title": f"Theirs {i}", "due_at": now + timedelta(hours=i),
                                 "completed_at": now if i % 2 else None} for i in range(1000)])
    async with dbmod.get_engine().connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.exec_driver_sql("VACUUM ANALYZE tasks")
    sent: list[tuple[str, Any]] = []

    def on_execute(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        sent.append((statement, parameters))

    await login_as(client, user)
    event.listen(dbmod.get_engine().sync_engine, "before_cursor_execute", on_execute)
    try:
        assert (await client.get("/tasks")).status_code == 200
        assert (await post_form(client, "/tasks", {"title": "One more"})).status_code == 303  # the cap's count
    finally:
        event.remove(dbmod.get_engine().sync_engine, "before_cursor_execute", on_execute)
    reads = [(s, p) for s, p in sent if re.search(r"FROM tasks\b", s) and "tasks.completed_at IS NULL" in s
             and "tasks.user_id = " in s]
    page = [x for x in reads if "OVER ()" in x[0]]
    totals = [x for x in reads if "FILTER (WHERE" in x[0]]
    count = [x for x in reads if re.search(r"SELECT count\(\*\) AS count_1\s+FROM \(SELECT tasks\.id", x[0])]
    assert len(page) == len(totals) == len(count) == 1, [s[:160] for s, _ in reads]
    held = int(await db.scalar(select(func.count(Task.id)).where(Task.user_id == user.id, Task.completed_at.is_(None))))
    assert held == 121
    for statement, params in page + totals + count:  # read-only statements: EXPLAIN ANALYZE shows what they really read
        async with dbmod.get_engine().connect() as conn:
            raw = (await conn.exec_driver_sql("EXPLAIN (ANALYZE, FORMAT JSON) " + statement, params)).scalar()
            await conn.rollback()
        nodes: list[dict[str, Any]] = []

        def walk(node: dict[str, Any]) -> None:
            nodes.append(node)
            for sub in node.get("Plans", []):
                walk(sub)

        walk((json.loads(raw) if isinstance(raw, str) else raw)[0]["Plan"])
        assert any(n.get("Index Name") == "ix_tasks_user_open" for n in nodes), raw
        for n in nodes:
            assert n["Node Type"] != "Seq Scan", raw
            if n.get("Relation Name") == "tasks" or n.get("Index Name") == "ix_tasks_user_open":
                # the 121 open tasks at most - never the 8,000 completed ones next to them
                assert n.get("Actual Rows", 0) * n.get("Actual Loops", 1) <= held, raw
                assert n.get("Rows Removed by Filter", 0) == 0, raw
