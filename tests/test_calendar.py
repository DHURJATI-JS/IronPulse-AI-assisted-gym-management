"""WP-E2 tests: calendar month grid (user time zone), tasks CRUD + ownership, validation, reminders periodic, N+1."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select

from app.core.utils import to_local, utcnow
from app.db import QueryCounter
from app.models import Notification, Task, User
from app.models.enums import Role, TaskKind
from app.services import tasks as task_service
from app.services.jobs import registered_periodic
from tests.conftest import login_as, post_form
from tests.factories import (
    make_branch,
    make_customer,
    make_employee,
    make_gym,
    make_owner,
    make_plans,
    make_shift,
    make_subscription,
    make_user,
)


async def _task(db, user: User, title: str = "Stretch", *, due_at: datetime | None = None, remind_at: datetime | None = None,
                kind: TaskKind = TaskKind.TASK, all_day: bool = False, done: bool = False) -> Task:
    t = Task(user_id=user.id, title=title, kind=kind, due_at=due_at, remind_at=remind_at, all_day=all_day,
             completed_at=utcnow() if done else None)
    db.add(t)
    await db.commit()
    return t


def _local_input(dt_utc: datetime, tz: str) -> tuple[str, str]:
    local = to_local(dt_utc, tz)
    return local.strftime("%Y-%m-%d"), local.strftime("%H:%M")


# --------------------------------------------------------------------------- calendar
async def test_month_grid_uses_user_time_zone(db):
    india = await make_customer(db, timezone="Asia/Kolkata")
    ny = await make_customer(db, timezone="America/New_York")
    moment = datetime(2026, 11, 30, 20, 0, tzinfo=UTC)  # 01:30 on 1 Dec in India, 15:00 on 30 Nov in New York
    await _task(db, india, "Physio call", due_at=moment)
    await _task(db, ny, "Physio call", due_at=moment)

    view = await task_service.build_month(db, india, "2026-12", now=datetime(2026, 12, 3, 6, 0, tzinfo=UTC))
    days = view.days_json()
    assert days["2026-12-01"][0]["time"] == "01:30" and "2026-11-30" not in days
    assert view.label == "December 2026" and view.prev_key == "2026-11" and view.next_key == "2027-01"
    assert view.weeks[0][0].day == date(2026, 11, 30) and all(len(w) == 7 for w in view.weeks)  # Monday-first grid
    assert view.selected.day == date(2026, 12, 3) and view.selected.is_today

    view_ny = await task_service.build_month(db, ny, "2026-11")
    assert view_ny.days_json()["2026-11-30"][0]["time"] == "15:00"


async def test_calendar_page_renders_for_every_role_and_handles_bad_month(client, db):
    for user in (await make_customer(db), await make_owner(db), await make_user(db, role=Role.STAFF),
                 await make_user(db, role=Role.SUPERADMIN)):
        client.cookies.clear()
        await login_as(client, user)
        r = await client.get("/calendar")
        assert r.status_code == 200 and 'data-component="calendar"' in r.text
        r = await client.get("/calendar?month=2026-13&day=garbage")
        assert r.status_code == 200
    client.cookies.clear()
    r = await client.get("/calendar")
    assert r.status_code == 303 and r.headers["location"].startswith("/auth/login")


async def test_calendar_shows_renewals_for_customers_and_shifts_for_staff(client, db):
    owner = await make_owner(db)
    gym = await make_gym(db, owner, name="Iron Temple")
    plans = await make_plans(db, gym)
    customer = await make_customer(db)
    sub = await make_subscription(db, customer, plans[4])
    await login_as(client, customer)
    r = await client.get(f"/calendar?month={sub.ends_on:%Y-%m}&day={sub.ends_on.isoformat()}")
    assert "Membership renewal · Iron Temple" in r.text

    staff_user = await make_user(db, role=Role.STAFF)
    emp = await make_employee(db, gym, user=staff_user)
    branch = await make_branch(db, gym, name="Indiranagar")
    await make_shift(db, emp, branch=branch, weekday=2, start="06:00", end="10:00")
    view = await task_service.build_month(db, staff_user, "2026-11")
    wednesdays = [d for w in view.weeks for d in w if d.day.weekday() == 2]
    assert wednesdays and all(any(i.kind == "shift" and i.time == "06:00–10:00" for i in d.items) for d in wednesdays)
    assert all(not any(i.kind == "shift" for i in d.items) for w in view.weeks for d in w if d.day.weekday() != 2)
    owner_view = await task_service.build_month(db, owner, "2026-11")
    assert not owner_view.days_json()


async def test_calendar_query_count_is_constant(client, db):
    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    staff_user = await make_user(db, role=Role.STAFF)
    emp = await make_employee(db, gym, user=staff_user)
    await make_shift(db, emp, branch=None, weekday=0)
    await login_as(client, staff_user)
    now = utcnow()
    month = to_local(now, staff_user.timezone).strftime("%Y-%m")
    for i in range(3):
        await _task(db, staff_user, f"Task {i}", due_at=now + timedelta(hours=i))
    await client.get(f"/calendar?month={month}")
    with QueryCounter() as small:
        assert (await client.get(f"/calendar?month={month}")).status_code == 200
    for i in range(30):
        await _task(db, staff_user, f"More {i}", due_at=now + timedelta(hours=i))
    await make_shift(db, emp, branch=None, weekday=3)
    with QueryCounter() as large:
        r = await client.get(f"/calendar?month={month}")
    assert r.status_code == 200 and "More 1" in r.text
    assert large.count == small.count


# --------------------------------------------------------------------------- tasks CRUD
async def test_create_task_converts_local_time_to_utc(client, db):
    user = await make_customer(db, timezone="Asia/Kolkata")
    await login_as(client, user)
    target = utcnow() + timedelta(days=2)
    d, _ = _local_input(target, user.timezone)
    r = await post_form(client, "/tasks", {"title": "  Leg day  ", "kind": "workout", "due_date": d, "due_time": "18:30",
                                           "remind_at": f"{d}T18:00", "notes": "Bring shoes", "next": f"/calendar?month={d[:7]}&day={d}"})
    assert r.status_code == 303 and r.headers["location"] == f"/calendar?month={d[:7]}&day={d}"
    t = (await db.execute(select(Task).where(Task.user_id == user.id))).scalar_one()
    assert t.title == "Leg day" and t.kind == TaskKind.WORKOUT and t.notes == "Bring shoes" and not t.all_day
    assert to_local(t.due_at, "Asia/Kolkata").strftime("%Y-%m-%d %H:%M") == f"{d} 18:30"
    assert t.due_at.astimezone(UTC).strftime("%H:%M") == "13:00"
    assert to_local(t.remind_at, "Asia/Kolkata").strftime("%H:%M") == "18:00"

    r = await post_form(client, "/tasks", {"title": "Renew gym bag", "due_date": d})
    t2 = (await db.execute(select(Task).where(Task.title == "Renew gym bag"))).scalar_one()
    assert t2.all_day and to_local(t2.due_at, "Asia/Kolkata").strftime("%H:%M") == "00:00" and r.headers["location"] == "/tasks"
    r = await post_form(client, "/tasks", {"title": "Someday", "next": "https://evil.example/x"})
    assert r.headers["location"] == "/tasks"

    page = await client.get("/tasks")
    assert page.status_code == 200 and "Leg day" in page.text and "Someday" in page.text and "No date" in page.text


async def test_task_form_validation_errors_rerender_with_400(client, db):
    user = await make_customer(db)
    await login_as(client, user)
    past = (to_local(utcnow(), user.timezone) - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M")
    cases = [
        ({"title": ""}, "Give the task a short title"),
        ({"title": "x" * 201}, "under 200 characters"),
        ({"title": "Ok", "kind": "hack"}, "Choose a valid type"),
        ({"title": "Ok", "due_date": "2026-02-30"}, "date does not exist"),
        ({"title": "Ok", "due_date": "12/11/2026"}, "date picker"),
        ({"title": "Ok", "due_time": "18:00"}, "Pick a date for this time"),
        ({"title": "Ok", "due_date": "2026-11-20", "due_time": "25:00"}, "valid time"),
        ({"title": "Ok", "remind_at": past}, "in the future"),
        ({"title": "Ok", "remind_at": "tomorrow"}, "valid date and time"),
        ({"title": "Ok", "kind": "reminder"}, "remind you"),
        ({"title": "Ok", "notes": "n" * 2001}, "at most 2000"),
        ({"title": "Ok", "due_date": "2099-01-01"}, "next five years"),
        ({"title": "Ok", "remind_at": "0001-01-01T00:00"}, "realistic date"),
        ({"title": "Ok", "due_date": "0001-01-01", "due_time": "00:00"}, "realistic date"),
    ]
    for data, message in cases:
        r = await post_form(client, "/tasks", data)
        assert r.status_code == 400, data
        assert message in r.text, (data, message)
        assert 'aria-invalid="true"' in r.text or "field-error" in r.text
    assert await db.scalar(select(func.count(Task.id))) == 0


async def test_reminder_kind_defaults_remind_at_to_due_time(client, db):
    user = await make_customer(db)
    await login_as(client, user)
    d, _ = _local_input(utcnow() + timedelta(days=1), user.timezone)
    r = await post_form(client, "/tasks", {"title": "Call coach", "kind": "reminder", "due_date": d, "due_time": "07:15"})
    assert r.status_code == 303
    t = (await db.execute(select(Task))).scalar_one()
    assert t.kind == TaskKind.REMINDER and t.remind_at == t.due_at


async def test_toggle_and_delete_own_tasks_only(client, db, anon_client_factory):
    alice = await make_customer(db)
    bob = await make_customer(db)
    bobs = await _task(db, bob, "Bob's secret task")
    mine = await _task(db, alice, "Alice task")
    await login_as(client, alice)
    page = await client.get("/tasks")
    assert "Alice task" in page.text and "Bob's secret task" not in page.text
    r = await post_form(client, f"/tasks/{bobs.id}/toggle")
    assert r.status_code == 404 and "text/html" in r.headers["content-type"] and "Task not found" in r.text  # friendly page
    assert (await post_form(client, f"/tasks/{bobs.id}/delete")).status_code == 404
    assert (await post_form(client, "/tasks/999999/toggle")).status_code == 404

    r = await post_form(client, f"/tasks/{mine.id}/toggle", {"next": "/calendar"})
    assert r.status_code == 303 and r.headers["location"] == "/calendar"
    await db.refresh(mine)
    assert mine.completed_at is not None
    r = await post_form(client, f"/tasks/{mine.id}/toggle", headers={"Accept": "application/json"})
    assert r.status_code == 200 and r.json() == {"ok": True, "id": mine.id, "completed": False}
    r = await post_form(client, f"/tasks/{mine.id}/delete")
    assert r.status_code == 303
    assert await db.scalar(select(func.count(Task.id)).where(Task.user_id == alice.id)) == 0
    await db.refresh(bobs)
    assert bobs.completed_at is None
    # CSRF is enforced
    r = await client.post(f"/tasks/{bobs.id}/delete", data={})
    assert r.status_code == 403


async def test_tasks_page_groups(client, db):
    user = await make_customer(db, timezone="UTC")
    now = utcnow()
    await _task(db, user, "Overdue thing", due_at=now - timedelta(days=3))
    await _task(db, user, "Later thing", due_at=now + timedelta(days=4))
    await _task(db, user, "Done thing", due_at=now, done=True)
    await _task(db, user, "Undated thing")
    await login_as(client, user)
    r = await client.get("/tasks")
    text = r.text
    assert r.status_code == 200
    assert text.index("Overdue") < text.index("Overdue thing") < text.index("Upcoming") < text.index("Later thing")
    assert "Recently completed" in text and "Done thing" in text and "Undated thing" in text


# --------------------------------------------------------------------------- reminders
async def test_reminder_periodic_notifies_once(db):
    assert "task_reminders" in registered_periodic()
    user = await make_customer(db)
    gone = await make_customer(db)
    now = utcnow()
    due = await _task(db, user, "Drink water", kind=TaskKind.REMINDER, remind_at=now - timedelta(minutes=1),
                      due_at=now - timedelta(minutes=1))
    future = await _task(db, user, "Later", kind=TaskKind.REMINDER, remind_at=now + timedelta(hours=1))
    done = await _task(db, user, "Already done", remind_at=now - timedelta(minutes=5), done=True)
    deleted_users = await _task(db, gone, "Ghost", remind_at=now - timedelta(minutes=5))
    gone.deleted_at = now
    gone.purge_after = now + timedelta(days=30)
    await db.commit()

    sent = await task_service.send_task_reminders(db)
    await db.commit()
    assert sent == 1
    notes = (await db.execute(select(Notification))).scalars().all()
    assert len(notes) == 1 and notes[0].user_id == user.id and notes[0].title == "Reminder: Drink water"
    assert notes[0].category.value == "reminder" and notes[0].link == "/tasks"
    for t in (due, future, done, deleted_users):
        await db.refresh(t)
    assert due.reminded_at is not None and future.reminded_at is None and done.reminded_at is None
    assert await task_service.send_task_reminders(db) == 0  # never twice
    sent_later = await task_service.send_task_reminders(db, now=now + timedelta(hours=2))
    assert sent_later == 1


async def test_reminders_query_once_for_candidates(db):
    users = [await make_customer(db) for _ in range(3)]
    now = utcnow()
    for u in users:
        await _task(db, u, "Ping", remind_at=now - timedelta(minutes=1))
    with QueryCounter() as qc:
        sent = await task_service.send_task_reminders(db)
    await db.commit()
    assert sent == 3
    # 1 candidate SELECT + per reminder: notification INSERT and e-mail INSERT (no per-row SELECTs for tasks/users)
    assert qc.count <= 1 + 3 * 3


async def test_tasks_page_query_count_is_constant(client, db):
    user = await make_customer(db)
    await login_as(client, user)
    now = utcnow()
    await _task(db, user, "One", due_at=now + timedelta(hours=1))
    await _task(db, user, "Done", done=True)
    await client.get("/tasks")
    with QueryCounter() as small:
        assert (await client.get("/tasks")).status_code == 200
    for i in range(25):
        await _task(db, user, f"Bulk {i}", due_at=now + timedelta(days=i - 5), remind_at=now + timedelta(days=i))
        await _task(db, user, f"Closed {i}", done=True)
    with QueryCounter() as large:
        r = await client.get("/tasks")
    assert r.status_code == 200 and "Bulk 24" in r.text
    assert large.count == small.count
