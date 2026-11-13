"""Tasks, reminders and the calendar month view (WP-E2).

* Times are entered in the user's local time zone and stored in UTC.
* ``build_month`` assembles a Monday-first month grid (tasks, membership renewals for customers,
  weekly shifts for staff) with a constant number of queries. It reads at most what the grid can show: the first
  ``CALENDAR_DAY_MAX`` tasks of every day (a per-day LIMIT in SQL) plus each day's true count, so a crowded day says
  "+N more" and links to that day's list (``GET /tasks?day=YYYY-MM-DD``).
* ``GET /tasks`` is paginated (``open_tasks_page``, ``TASKS_PER_PAGE``) with true totals (``open_task_stats``).
* One person holds at most ``open_task_limit()`` open tasks (TASKS_MAX_OPEN): creating - or reopening - one more
  raises ``TaskLimitError`` with a friendly message (the form shows it, the assistant answers it in words).
* ``task_reminders`` (every minute) notifies due reminders (in-app + e-mail) exactly once.
"""

from __future__ import annotations

import calendar as _calendar
import hashlib
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import DateTime, and_, bindparam, delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, joinedload

from app.config import get_settings
from app.core import caps
from app.core.deps import NotFound
from app.core.pagination import Page, paginate
from app.core.utils import local_now, safe_zone, to_local, utcnow
from app.db import session_scope
from app.models import Employee, Subscription, Task, TrainerShift, User
from app.models.enums import EmploymentStatus, NotificationCategory, Role, Severity, SubscriptionStatus, TaskKind
from app.services.batching import Chunks
from app.services.jobs import periodic

TITLE_MAX = 200
NOTES_MAX = 2000
RANGE_TASKS_MAX = 1000  # tasks loaded for one date range (the 6-week calendar grid, the assistant's "this week")
# Load-safety limits (module constants; change them here):
MAX_OPEN_TASKS = 500  # open tasks one person may hold: the fallback of the TASKS_MAX_OPEN setting (tasks_max_open)
CALENDAR_DAY_MAX = 15  # tasks one calendar day shows (grid + day panel); the rest is "+N more" -> /tasks?day=
TASKS_PER_PAGE = 50  # GET /tasks (open tasks) and GET /tasks?day= (one day's tasks): tasks per page ...
TASKS_MAX_PER_PAGE = 100  # ... and the most ?per_page= may ask for
REMINDER_BATCH = 300  # due reminders per chunk - one short transaction each
REMINDER_BUDGET_S = 45.0  # the task runs every minute: a bigger backlog carries on at the next tick
USER_KINDS: tuple[tuple[str, str], ...] = (
    ("task", "To-do"),
    ("reminder", "Reminder"),
    ("event", "Event"),
    ("workout", "Workout"),
)
KIND_LABELS = dict(USER_KINDS) | {"bedtime": "Bedtime", "renewal": "Renewal", "shift": "Shift", "more": "Full list"}
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME = re.compile(r"^(\d{1,2}):(\d{2})(?::\d{2})?$")
_DATETIME = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ](\d{1,2}:\d{2})(?::\d{2}(?:\.\d+)?)?$")
_MONTH = re.compile(r"^(\d{4})-(\d{2})$")


# ------------------------------------------------------------------ parsing helpers
def parse_date(value: str | None) -> date | None:
    value = (value or "").strip()
    if not value:
        return None
    if not _DATE.match(value):
        raise ValueError("Use the date picker (YYYY-MM-DD).")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("That date does not exist.") from exc
    if not 1970 <= parsed.year <= 2200:
        raise ValueError("Choose a realistic date.")
    return parsed


def parse_time(value: str | None) -> time | None:
    value = (value or "").strip()
    if not value:
        return None
    m = _TIME.match(value)
    if not m or not (0 <= int(m.group(1)) <= 23 and 0 <= int(m.group(2)) <= 59):
        raise ValueError("Use a valid time (HH:MM).")
    return time(int(m.group(1)), int(m.group(2)))


def local_to_utc(d: date, t: time, tz: str | None) -> datetime:
    return datetime.combine(d, t).replace(tzinfo=safe_zone(tz)).astimezone(UTC)


def parse_local_datetime(value: str | None, tz: str | None) -> datetime | None:
    """``"2026-11-12T18:30"`` (local, e.g. from <input type=datetime-local>) -> aware UTC datetime."""
    value = (value or "").strip()
    if not value:
        return None
    m = _DATETIME.match(value)
    if not m:
        raise ValueError("Use a valid date and time.")
    d = parse_date(m.group(1))
    t = parse_time(m.group(2))
    assert d is not None and t is not None
    return local_to_utc(d, t, tz)


def parse_month(value: str | None, today: date) -> date:
    m = _MONTH.match((value or "").strip())
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        if 1970 <= y <= 2100 and 1 <= mo <= 12:
            return date(y, mo, 1)
    return today.replace(day=1)


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, 1)


# ------------------------------------------------------------------ form validation
@dataclass(slots=True)
class TaskInput:
    title: str
    notes: str | None
    kind: TaskKind
    due_at: datetime | None
    remind_at: datetime | None
    all_day: bool


def validate_task_form(form: Mapping[str, Any], tz: str | None, *, now: datetime | None = None
                       ) -> tuple[TaskInput | None, dict[str, str], dict[str, str]]:
    """Validate the task form. Returns ``(task_input | None, errors, raw_values)``."""
    now = now or utcnow()
    raw = {k: str(form.get(k) or "").strip() for k in ("title", "notes", "kind", "due_date", "due_time", "remind_at")}
    errors: dict[str, str] = {}
    title = re.sub(r"\s+", " ", raw["title"])
    if not title:
        errors["title"] = "Give the task a short title."
    elif len(title) > TITLE_MAX:
        errors["title"] = f"Keep the title under {TITLE_MAX} characters."
    notes = raw["notes"] or None
    if notes and len(notes) > NOTES_MAX:
        errors["notes"] = f"Notes can be at most {NOTES_MAX} characters."
    kind_value = raw["kind"] or "task"
    if kind_value not in dict(USER_KINDS):
        errors["kind"] = "Choose a valid type."
    due_date = due_time = None
    try:
        due_date = parse_date(raw["due_date"])
    except ValueError as exc:
        errors["due_date"] = str(exc)
    try:
        due_time = parse_time(raw["due_time"])
    except ValueError as exc:
        errors["due_time"] = str(exc)
    if due_time and not due_date and "due_date" not in errors:
        errors["due_date"] = "Pick a date for this time."
    remind_at = None
    try:
        remind_at = parse_local_datetime(raw["remind_at"], tz)
    except ValueError as exc:
        errors["remind_at"] = str(exc)
    local_today = to_local(now, tz).date()
    if due_date and not (local_today.year - 1 <= due_date.year <= local_today.year + 5):
        errors["due_date"] = "Choose a date within the next five years."
    due_at = None
    all_day = False
    if due_date and "due_date" not in errors and "due_time" not in errors:
        all_day = due_time is None
        due_at = local_to_utc(due_date, due_time or time(0, 0), tz)
    if remind_at is not None and "remind_at" not in errors:
        if remind_at < now - timedelta(minutes=1):
            errors["remind_at"] = "Pick a reminder time in the future."
        elif remind_at > now + timedelta(days=5 * 366):
            errors["remind_at"] = "Choose a reminder within the next five years."
    if kind_value == "reminder" and remind_at is None and "remind_at" not in errors:
        if due_at is not None and not all_day and due_at >= now - timedelta(minutes=1):
            remind_at = due_at
        else:
            errors["remind_at"] = "Choose when we should remind you."
    if errors:
        return None, errors, raw
    return TaskInput(title=title, notes=notes, kind=TaskKind(kind_value), due_at=due_at, remind_at=remind_at, all_day=all_day), {}, raw


# ------------------------------------------------------------------ the open-task cap
class TaskLimitError(Exception):
    """The person already holds ``open_task_limit()`` open tasks: nothing was created (or reopened)."""

    def __init__(self, limit: int, *, reopen: bool = False) -> None:
        self.limit = limit
        self.message = (f"You already have {limit:,} open tasks - the most you can keep at once. Tick off or delete "
                        f"a few you no longer need, then {'reopen' if reopen else 'add'} this one.")
        super().__init__(self.message)


class TaskDayLimitError(TaskLimitError):
    """The person added ``caps.limit("tasks_per_person_day")`` tasks in the last 24 hours: nothing was created. A
    ``TaskLimitError``, so every caller (forms, the assistant) already answers it in words."""

    def __init__(self, exc: caps.CapReached) -> None:
        super().__init__(exc.limit)
        self.message = str(exc)
        self.args = (self.message,)


def open_task_limit() -> int:
    """How many open tasks one person may hold: the ``tasks_max_open`` setting (TASKS_MAX_OPEN, default 500);
    ``MAX_OPEN_TASKS`` when the setting is missing or unusable."""
    try:
        return max(1, int(getattr(get_settings(), "tasks_max_open", MAX_OPEN_TASKS)))
    except (TypeError, ValueError):
        return max(1, int(MAX_OPEN_TASKS))


def _open_tasks_lock(user_id: int) -> int:
    return int.from_bytes(hashlib.blake2b(f"tasks:open:{user_id}".encode(), digest_size=8).digest(), "big", signed=True)


async def ensure_room_for_open_task(db: AsyncSession, user_id: int, *, reopen: bool = False) -> None:
    """Raise ``TaskLimitError`` when the person already holds ``open_task_limit()`` open tasks.

    The check holds a per-person advisory lock until the transaction ends, so two tabs - or a form and the assistant's
    actions - can't both slip in under the cap; the callers commit right after. The COUNT reads at most the limit's
    worth of rows."""
    limit = open_task_limit()
    await db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _open_tasks_lock(user_id)})
    held = await db.scalar(select(func.count()).select_from(
        select(Task.id).where(Task.user_id == user_id, Task.completed_at.is_(None)).limit(limit).subquery()
    ))
    if int(held or 0) >= limit:
        raise TaskLimitError(limit, reopen=reopen)


# ------------------------------------------------------------------ CRUD
async def create_task(db: AsyncSession, user: User, data: TaskInput, *, created_by: str = "user") -> Task:
    """Add a task. Raises ``TaskLimitError`` (nothing is added) when the person already holds ``open_task_limit()``
    open tasks, or ``TaskDayLimitError`` when they added the day's cap of tasks (``tasks_per_person_day``) already -
    completed tasks are kept, so the daily cap is what bounds a person's task history."""
    await ensure_room_for_open_task(db, user.id)
    try:
        await caps.enforce(db, "tasks_per_person_day", user.id,
                           select(Task.id).where(Task.user_id == user.id, Task.created_at >= caps.since()))
    except caps.CapReached as exc:
        raise TaskDayLimitError(exc) from None
    task = Task(user_id=user.id, title=data.title[:TITLE_MAX], notes=data.notes, kind=data.kind, due_at=data.due_at,
                remind_at=data.remind_at, all_day=data.all_day, created_by=created_by)
    db.add(task)
    await db.flush()
    return task


async def get_own_task(db: AsyncSession, user: User, task_id: int) -> Task:
    task = (await db.execute(select(Task).where(Task.id == task_id, Task.user_id == user.id))).scalar_one_or_none()
    if task is None:
        raise NotFound("Task not found.")
    return task


async def toggle_task(db: AsyncSession, user: User, task_id: int) -> Task:
    """Complete an open task or reopen a completed one - reopening counts against the open-task cap like a new task
    (``TaskLimitError``)."""
    task = await get_own_task(db, user, task_id)
    if task.completed_at is not None:
        await ensure_room_for_open_task(db, user.id, reopen=True)
    task.completed_at = None if task.completed_at else utcnow()
    return task


async def delete_task(db: AsyncSession, user: User, task_id: int) -> None:
    res = await db.execute(delete(Task).where(Task.id == task_id, Task.user_id == user.id))
    if not res.rowcount:
        raise NotFound("Task not found.")


def _moment() -> Any:
    """When a task happens: its due time, else its reminder time (NULL: no date)."""
    return func.coalesce(Task.due_at, Task.remind_at)


@dataclass(frozen=True, slots=True)
class TaskStats:
    """True counts of a person's open tasks (one aggregate statement, whatever the page shows)."""

    total: int = 0
    overdue: int = 0
    today: int = 0
    someday: int = 0  # no date

    @property
    def upcoming(self) -> int:
        return max(0, self.total - self.overdue - self.today - self.someday)

    def of(self, group: str) -> int:
        counts = {"overdue": self.overdue, "today": self.today, "upcoming": self.upcoming, "someday": self.someday}
        return counts.get(group, 0)


async def open_task_stats(db: AsyncSession, user_id: int, tz: str | None, *, now: datetime | None = None) -> TaskStats:
    """Open tasks in total, overdue (before today), due today and undated - today in the person's time zone."""
    today = to_local(now or utcnow(), tz).date()
    start, end = local_to_utc(today, time(0, 0), tz), local_to_utc(today + timedelta(days=1), time(0, 0), tz)
    moment = _moment()
    row = (await db.execute(
        select(func.count().label("total"),
               func.count().filter(moment < start).label("overdue"),
               func.count().filter(moment >= start, moment < end).label("today"),
               func.count().filter(moment.is_(None)).label("someday"))
        .where(Task.user_id == user_id, Task.completed_at.is_(None))
    )).one()
    return TaskStats(total=int(row.total or 0), overdue=int(row.overdue or 0), today=int(row.today or 0),
                     someday=int(row.someday or 0))


async def open_tasks_page(db: AsyncSession, user_id: int, *, page: int = 1,
                          per_page: int = TASKS_PER_PAGE) -> Page[Task]:
    """One page of the person's open tasks, earliest first and undated last (overdue -> today -> upcoming -> no date,
    so the page groups stay in order across pages). One statement (``count(*) OVER ()`` carries the total)."""
    stmt = (select(Task).where(Task.user_id == user_id, Task.completed_at.is_(None))
            .order_by(_moment().asc().nulls_last(), Task.id))
    return await paginate(db, stmt, page=page, per_page=per_page, max_per_page=TASKS_MAX_PER_PAGE, window_count=True)


def day_bounds(day: date, tz: str | None) -> tuple[datetime, datetime]:
    """[start, end) in UTC of one local day."""
    return local_to_utc(day, time(0, 0), tz), local_to_utc(day + timedelta(days=1), time(0, 0), tz)


async def day_tasks_page(db: AsyncSession, user_id: int, day: date, tz: str | None, *, page: int = 1,
                         per_page: int = TASKS_PER_PAGE) -> Page[Task]:
    """One page of everything on one local day (open first, then done; by time) - the calendar's "+N more" list."""
    start, end = day_bounds(day, tz)
    stmt = (select(Task)
            .where(Task.user_id == user_id,
                   or_(and_(Task.due_at >= start, Task.due_at < end),
                       and_(Task.due_at.is_(None), Task.remind_at >= start, Task.remind_at < end)))
            .order_by(Task.completed_at.is_not(None), _moment(), Task.id))
    return await paginate(db, stmt, page=page, per_page=per_page, max_per_page=TASKS_MAX_PER_PAGE, window_count=True)


async def recently_completed(db: AsyncSession, user_id: int, limit: int = 10) -> list[Task]:
    stmt = (
        select(Task)
        .where(Task.user_id == user_id, Task.completed_at.is_not(None))
        .order_by(Task.completed_at.desc())
        .limit(limit)
    )
    return list((await db.execute(stmt)).scalars().all())


async def upcoming_tasks(db: AsyncSession, user_id: int, *, days: int = 7, limit: int = 10, now: datetime | None = None) -> list[Task]:
    """Open tasks due (or reminding) between now and ``days`` from now, plus overdue ones from the last day."""
    now = now or utcnow()
    start, end = now - timedelta(days=1), now + timedelta(days=days)
    stmt = (
        select(Task)
        .where(
            Task.user_id == user_id,
            Task.completed_at.is_(None),
            or_(and_(Task.due_at >= start, Task.due_at < end),
                and_(Task.due_at.is_(None), Task.remind_at >= start, Task.remind_at < end)),
        )
        .order_by(Task.due_at.asc().nulls_last(), Task.remind_at.asc())
        .limit(limit)
    )
    return list((await db.execute(stmt)).scalars().all())


async def tasks_between(db: AsyncSession, user_id: int, start: datetime, end: datetime, *, include_done: bool = True,
                        limit: int | None = RANGE_TASKS_MAX) -> list[Task]:
    """The user's tasks due (or reminding) in [start, end), earliest first - at most ``limit`` (a LIMIT in SQL: a
    user can create any number of tasks; a page or an assistant answer never loads them all)."""
    cond = [
        Task.user_id == user_id,
        or_(and_(Task.due_at >= start, Task.due_at < end), and_(Task.due_at.is_(None), Task.remind_at >= start, Task.remind_at < end)),
    ]
    if not include_done:
        cond.append(Task.completed_at.is_(None))
    stmt = select(Task).where(*cond).order_by(Task.due_at.asc().nulls_last(), Task.remind_at.asc(), Task.id)
    if limit is not None:
        stmt = stmt.limit(max(1, int(limit)))
    return list((await db.execute(stmt)).scalars().all())


# ------------------------------------------------------------------ grouping for the tasks page
def when_label(task: Task, tz: str | None, *, today: date | None = None) -> str:
    moment = task.due_at or task.remind_at
    if moment is None:
        return "No date"
    local = to_local(moment, tz)
    assert local is not None
    today = today or local_now(tz).date()
    d = local.date()
    if d == today:
        day = "Today"
    elif d == today + timedelta(days=1):
        day = "Tomorrow"
    elif d == today - timedelta(days=1):
        day = "Yesterday"
    elif today < d < today + timedelta(days=7):
        day = local.strftime("%A")
    else:
        day = local.strftime("%a %d %b" + (" %Y" if d.year != today.year else ""))
    if task.due_at is not None and task.all_day:
        return day
    return f"{day}, {local.strftime('%H:%M')}"


def when_label_for(moment: datetime, tz: str | None, *, now: datetime | None = None) -> str:
    """``today at 17:00`` / ``tomorrow at 08:00`` / ``on Fri 13 Nov at 09:00`` for a UTC moment."""
    local = to_local(moment, tz)
    assert local is not None
    today = to_local(now or utcnow(), tz).date()
    d = local.date()
    if d == today:
        day = "today"
    elif d == today + timedelta(days=1):
        day = "tomorrow"
    else:
        day = "on " + local.strftime("%a %d %b" + (" %Y" if d.year != today.year else ""))
    return f"{day} at {local:%H:%M}"


def group_tasks(tasks: list[Task], tz: str | None, *, now: datetime | None = None) -> list[tuple[str, str, list[Task]]]:
    """[(key, label, tasks)] for overdue / today / upcoming / someday."""
    now = now or utcnow()
    today = to_local(now, tz).date()
    groups: dict[str, list[Task]] = {"overdue": [], "today": [], "upcoming": [], "someday": []}
    for t in tasks:
        moment = t.due_at or t.remind_at
        if moment is None:
            groups["someday"].append(t)
            continue
        d = to_local(moment, tz).date()
        if d < today:
            groups["overdue"].append(t)
        elif d == today:
            groups["today"].append(t)
        else:
            groups["upcoming"].append(t)
    labels = {"overdue": "Overdue", "today": "Today", "upcoming": "Upcoming", "someday": "No date"}
    return [(k, labels[k], v) for k, v in groups.items() if v]


# ------------------------------------------------------------------ calendar
@dataclass(slots=True)
class CalItem:
    kind: str
    title: str
    time: str | None = None
    tone: str = "blue"
    link: str | None = None
    task_id: int | None = None
    done: bool = False
    sort: str = ""
    detail: str | None = None

    def as_json(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("sort", None)
        d["label"] = KIND_LABELS.get(self.kind, self.kind.title())
        return d


@dataclass(slots=True)
class CalDay:
    day: date
    in_month: bool
    is_today: bool
    items: list[CalItem] = field(default_factory=list)
    more: int = 0  # tasks of this day that were not loaded (beyond CALENDAR_DAY_MAX): "+N more" -> more_link

    @property
    def iso(self) -> str:
        return self.day.isoformat()

    @property
    def count(self) -> int:
        """Everything on this day: the items shown plus the tasks not loaded."""
        return len(self.items) + self.more

    @property
    def more_link(self) -> str:
        return f"/tasks?day={self.iso}"

    def more_item(self) -> CalItem:
        """The "+N more" row of the day panel (the JS renders it like any other item: title, meta line, Open link)."""
        shown = sum(1 for i in self.items if i.task_id is not None)
        return CalItem(kind="more", title=f"{self.more} more task{'s' if self.more != 1 else ''}",
                       time=f"Showing {shown} of {shown + self.more}", tone="zinc", link=self.more_link, sort="~")


@dataclass(slots=True)
class MonthView:
    month: date
    today: date
    tz: str
    weeks: list[list[CalDay]]
    selected: CalDay
    truncated: bool = False  # some day holds more tasks than the grid loaded (see CalDay.more)
    task_counts: dict[str, int] = field(default_factory=dict)  # this month's tasks by kind (true counts, from SQL)

    @property
    def label(self) -> str:
        return self.month.strftime("%B %Y")

    @property
    def key(self) -> str:
        return self.month.strftime("%Y-%m")

    @property
    def prev_key(self) -> str:
        return add_months(self.month, -1).strftime("%Y-%m")

    @property
    def next_key(self) -> str:
        return add_months(self.month, 1).strftime("%Y-%m")

    @property
    def is_current(self) -> bool:
        return self.month == self.today.replace(day=1)

    def days_json(self) -> dict[str, list[dict[str, Any]]]:
        """Each day's items for the day panel (53-calendar.js) - a crowded day ends with its "+N more" row."""
        return {d.iso: [i.as_json() for i in d.items] + ([d.more_item().as_json()] if d.more else [])
                for week in self.weeks for d in week if d.items or d.more}

    def counts(self) -> dict[str, int]:
        """This month's items by kind: tasks from their true counts (``task_counts``), renewals and shifts as shown."""
        out: dict[str, int] = dict(self.task_counts)
        for week in self.weeks:
            for d in week:
                if d.in_month:
                    for i in d.items:
                        if i.task_id is None:
                            out[i.kind] = out.get(i.kind, 0) + 1
        return out


TASK_TONES = {"task": "blue", "reminder": "violet", "event": "green", "workout": "amber", "bedtime": "zinc"}


def _task_item(task: Task, tz: str) -> tuple[date, CalItem]:
    moment = task.due_at or task.remind_at
    local = to_local(moment, tz)
    assert local is not None
    show_time = not (task.due_at is not None and task.all_day)
    kind = task.kind.value if hasattr(task.kind, "value") else str(task.kind)
    return local.date(), CalItem(
        kind=kind, title=task.title, time=local.strftime("%H:%M") if show_time else None, tone=TASK_TONES.get(kind, "blue"),
        link="/tasks", task_id=task.id, done=task.completed_at is not None,
        sort=(local.strftime("%H:%M") if show_time else "00:00") + f"{task.id:09d}", detail=(task.notes or None) and task.notes[:160],
    )


def _in_range(start: datetime, end: datetime) -> Any:
    """Due - or, undated, reminding - in [start, end) (two plain ranges: the due_at / remind_at indexes serve them)."""
    return or_(and_(Task.due_at >= start, Task.due_at < end),
               and_(Task.due_at.is_(None), Task.remind_at >= start, Task.remind_at < end))


async def _grid_tasks(db: AsyncSession, user_id: int, day_starts: list[datetime]) -> list[tuple[int, Task, int]]:
    """``(day number, task, tasks that day)`` for the first CALENDAR_DAY_MAX tasks of each grid day (open before done,
    then by time) - ONE statement. ``day_starts`` holds the local midnights in UTC plus the end of the grid; a day's
    number is its ``width_bucket`` (computed in Python from the person's time zone, so SQL needs no zone names and
    daylight-saving days are exact). Rows come round-robin (every day's first task, then every day's second ...) under
    the overall RANGE_TASKS_MAX, so a day with tasks always reports its true count."""
    bucket = func.width_bucket(_moment(), bindparam("day_starts", day_starts, type_=ARRAY(DateTime(timezone=True))))
    grid = (select(Task, bucket.label("day_no"))
            .where(Task.user_id == user_id, _in_range(day_starts[0], day_starts[-1])).subquery("grid"))
    when = func.coalesce(grid.c.due_at, grid.c.remind_at)
    ranked = select(
        grid,
        func.row_number().over(partition_by=grid.c.day_no,
                               order_by=(grid.c.completed_at.is_not(None), when, grid.c.id)).label("rn"),
        func.count().over(partition_by=grid.c.day_no).label("n"),
    ).subquery("ranked")
    task = aliased(Task, ranked)
    rows = (await db.execute(
        select(task, ranked.c.day_no, ranked.c.n).where(ranked.c.rn <= CALENDAR_DAY_MAX)
        .order_by(ranked.c.rn, ranked.c.day_no).limit(max(1, int(RANGE_TASKS_MAX)))
    )).all()
    return [(int(day_no), t, int(n)) for t, day_no, n in rows]


async def build_month(db: AsyncSession, user: User, month_value: str | None = None, day_value: str | None = None,
                      *, now: datetime | None = None) -> MonthView:
    """Month grid in the user's time zone. Queries: tasks (1, at most CALENDAR_DAY_MAX a day) + this month's task
    counts by kind (1) + renewals (customers, 1) or shifts (staff, 1)."""
    tz = user.timezone or "UTC"
    now = now or utcnow()
    today = to_local(now, tz).date()
    month = parse_month(month_value, today)
    first_weekday, n_days = _calendar.monthrange(month.year, month.month)  # Monday = 0
    grid_start = month - timedelta(days=first_weekday)
    last = date(month.year, month.month, n_days)
    grid_end = last + timedelta(days=6 - last.weekday())  # inclusive Sunday
    days: dict[date, CalDay] = {}
    d = grid_start
    while d <= grid_end:
        days[d] = CalDay(day=d, in_month=d.month == month.month, is_today=d == today)
        d += timedelta(days=1)

    grid_days = sorted(days)
    day_starts = [local_to_utc(gd, time(0, 0), tz) for gd in grid_days]
    day_starts.append(local_to_utc(grid_end + timedelta(days=1), time(0, 0), tz))
    totals: dict[date, int] = {}
    for day_no, task, n in await _grid_tasks(db, user.id, day_starts):
        if 1 <= day_no <= len(grid_days):
            gd = grid_days[day_no - 1]
            days[gd].items.append(_task_item(task, tz)[1])
            totals[gd] = n
    for gd, n in totals.items():
        days[gd].more = max(0, n - len(days[gd].items))
    month_start, month_end = day_starts[first_weekday], day_starts[first_weekday + n_days]
    task_counts = {
        getattr(kind, "value", str(kind)): int(n)
        for kind, n in (await db.execute(
            select(Task.kind, func.count()).where(Task.user_id == user.id, _in_range(month_start, month_end))
            .group_by(Task.kind).limit(len(TaskKind))
        )).all()
    }

    if user.role == Role.CUSTOMER:
        subs = (
            await db.execute(
                select(Subscription)
                .options(joinedload(Subscription.gym), joinedload(Subscription.plan))
                .where(
                    Subscription.customer_id == user.id,
                    Subscription.status == SubscriptionStatus.ACTIVE,
                    Subscription.ends_on >= grid_start,
                    Subscription.ends_on <= grid_end,
                )
            )
        ).unique().scalars().all()
        for s in subs:
            if s.ends_on in days:
                days[s.ends_on].items.append(CalItem(
                    kind="renewal", title=f"Membership renewal · {s.gym.name}", tone="amber", link="/me/subscriptions",
                    sort="00:00", detail=f"{s.plan.name} ends today - renew to keep access.",
                ))
    elif user.role == Role.STAFF:
        shifts = (
            await db.execute(
                select(TrainerShift)
                .join(Employee, Employee.id == TrainerShift.employee_id)
                .options(joinedload(TrainerShift.branch), joinedload(TrainerShift.employee).joinedload(Employee.gym))
                .where(Employee.user_id == user.id, Employee.status == EmploymentStatus.ACTIVE, Employee.deleted_at.is_(None))
                .order_by(TrainerShift.weekday, TrainerShift.start_time)
            )
        ).unique().scalars().all()
        for cal_day in days.values():
            for sh in shifts:
                if sh.weekday == cal_day.day.weekday():
                    where = sh.branch.name if sh.branch is not None else "Online"
                    cal_day.items.append(CalItem(
                        kind="shift", title=f"Shift · {sh.employee.gym.name}", time=f"{sh.start_time:%H:%M}–{sh.end_time:%H:%M}",
                        tone="green", link="/staff/schedule", sort=f"{sh.start_time:%H:%M}", detail=where,
                    ))
    for cal_day in days.values():
        cal_day.items.sort(key=lambda i: (i.done, i.sort))
    ordered = [days[k] for k in sorted(days)]
    weeks = [ordered[i : i + 7] for i in range(0, len(ordered), 7)]
    selected_day = None
    try:
        selected_day = date.fromisoformat(day_value) if day_value else None
    except ValueError:
        selected_day = None
    if selected_day not in days:
        selected_day = today if today in days and today.month == month.month else month
    return MonthView(month=month, today=today, tz=tz, weeks=weeks, selected=days[selected_day],
                     truncated=any(cd.more for cd in days.values()), task_counts=task_counts)


# ------------------------------------------------------------------ reminders
@periodic("task_reminders", every_seconds=60, initial_delay=10)
async def send_task_reminders(db: AsyncSession, *, now: datetime | None = None) -> int:
    """Notify every due reminder exactly once, in chunks of REMINDER_BATCH (one short transaction each, oldest first).

    A chunk claims due reminders with ``FOR UPDATE OF tasks SKIP LOCKED`` (the partial index ix_tasks_reminder_due holds
    exactly the unsent, unfinished ones), notifies their owners in bulk (``notify_bulk``) and stamps ``reminded_at`` in
    the same transaction. A reminder whose owner is deleted or deactivated is stamped without a notification: its
    moment has passed and it leaves the due set instead of being skipped again every minute. Chunks repeat until the
    backlog is drained or REMINDER_BUDGET_S is spent (the next tick carries on). Returns the notifications sent."""
    from app.services.notify import Note, notify_bulk

    now = now or utcnow()
    run = Chunks(holder=db, budget_s=REMINDER_BUDGET_S)
    sent = 0
    while await run.next():
        async with session_scope() as s:
            rows = (
                await s.execute(
                    select(Task, User)
                    .join(User, User.id == Task.user_id)
                    .where(Task.remind_at <= now, Task.reminded_at.is_(None), Task.completed_at.is_(None))
                    .order_by(Task.remind_at)
                    .limit(REMINDER_BATCH)
                    .with_for_update(of=Task, skip_locked=True)
                )
            ).tuples().all()
            notes = []
            for task, user in rows:
                if user.deleted_at is not None or not user.is_active:
                    continue
                body = task.notes[:300] if task.notes else "This is the reminder you asked for."
                if task.due_at is not None and not task.all_day and task.due_at != task.remind_at:
                    body += f" Due {when_label(task, user.timezone, today=to_local(now, user.timezone).date())}."
                notes.append(Note(user, f"Reminder: {task.title}"[:160], body, link="/tasks",
                                  category=NotificationCategory.REMINDER, severity=Severity.INFO))
            if rows:
                await s.execute(update(Task).where(Task.id.in_([t.id for t, _ in rows])).values(reminded_at=now)
                                .execution_options(synchronize_session=False))
            if notes:
                sent += len(await notify_bulk(s, notes))
        run.done(len(rows), REMINDER_BATCH)
    return sent
