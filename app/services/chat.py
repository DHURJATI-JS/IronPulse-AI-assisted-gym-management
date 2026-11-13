"""WP-F live chat: customers <-> a gym's staff member (``employee_id``) or its admin team (``employee_id`` NULL).

Owners see (and can answer) every thread of their gyms; staff see threads addressed to their employment;
customers see their own threads. Deleted parties keep their history for the 30-day retention window
(shown as "Deleted user" / "Deleted staff member") and the thread becomes read-only until the purge
removes it (ON DELETE CASCADE).

Public API (also used by WP-E2's assistant)::

    thread = await get_or_create_thread(db, gym_id=..., customer_id=..., employee_id=None | id)
    message = await post_message(db, thread=thread, sender=user, body="...", sender_role="customer")

Both raise ``ChatError`` (ValueError) or ``ChatPermissionError`` (PermissionError) with a message that is
safe to show. Nothing is written before validation passes. The caller commits.

Lists are keyset windows: conversations ``THREAD_PAGE_SIZE`` at a time by (latest activity, id) with an opaque
``older`` cursor (``list_threads_page``), messages ``PAGE_SIZE`` at a time by id (``list_messages``, served by
ix_chat_messages_thread_id_id). Neither costs more the further back the reader goes.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import BigInteger, DateTime, and_, false, func, literal, or_, select, true, tuple_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, joinedload
from sqlalchemy.orm.attributes import set_committed_value

from app.core import caps
from app.core.utils import clamp, to_local, utcnow
from app.models import ChatMessage, ChatThread, Employee, Gym, Notification, Subscription, TrainerClient, User
from app.models.enums import (
    EmploymentStatus,
    NotificationCategory,
    Role,
    Severity,
    SubscriptionStatus,
    ThreadStatus,
)
from app.services import trainer_safety
from app.services.notify import notify
from app.services.realtime import hub, publish_after_commit
from app.services.subscriptions import has_active_subscription

MAX_BODY = 4000
PREVIEW_LEN = 140
PAGE_SIZE = 50
THREAD_PAGE_SIZE = 50  # conversations per window of the sidebar ("Load more conversations" fetches the next)
MAX_ID = 2**63 - 1
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_CURSOR_RE = re.compile(r"^(\d{1,17})-(\d{1,19})$")
EMAIL_THROTTLE = timedelta(minutes=15)
SENDER_ROLES = ("customer", "staff", "owner")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# Invisible format characters with no place in a chat message: bidi embeddings / overrides / isolates (they reorder what
# the others see - "pay <RLO>gnp.exe" reads "pay exe.png"), zero-width spaces, word joiners, BOMs, soft hyphens and
# invisible operators. Kept: ZWNJ / ZWJ (Indic scripts, emoji sequences), LRM / RLM / ALM (right-to-left text) and
# tag characters (flag emoji) - they only count as text next to something visible (``has_visible_text``).
_INVISIBLE = re.compile("[\u00ad\u180e\u200b\u202a-\u202e\u2060-\u2064\u2066-\u206f\ufeff\ufff9-\ufffb]")
# characters that render as nothing although they are letters or symbols (Hangul fillers, the blank Braille pattern ...)
_BLANK = frozenset("\u115f\u1160\u17b4\u17b5\u2800\u3164\uffa0")
_INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf", "Zs", "Zl", "Zp", "Mn", "Me"})


class ChatError(ValueError):
    """Invalid chat action (message safe to show)."""


class ChatPermissionError(PermissionError):
    """The user may not do this in this conversation (message safe to show)."""


# load safety (approved limits): caps of app/core/caps.py (race-safe ``caps.enforce``; ``CAPS_OVERRIDES`` tunes them).
# A member's message that mentions red-flag symptoms is never refused by any of them (app/services/trainer_safety.py).
# The gym side is capped too: per staff member / owner (every conversation) and per conversation (the member's
# protection against being flooded) - lock order: the conversation's cap, then the sender's, then the thread row.
THREADS_CAP = "chat_threads_per_member_day"
MESSAGES_CAP = "chat_messages_per_member_day"
STAFF_MESSAGES_CAP = "chat_messages_per_staff_day"
STAFF_THREAD_CAP = "chat_staff_messages_per_conversation_day"
CHAT_CAPS = (
    caps.Cap(THREADS_CAP, 20, "new conversations", "per member per day",
             "You've started {limit} new conversations in the last 24 hours. Please carry on in one of your "
             "conversations, or try again tomorrow.", window_hours=caps.DAY),
    caps.Cap(MESSAGES_CAP, 500, "chat messages", "per member per day",
             "You've sent {limit} messages in the last 24 hours - the daily limit. Please try again tomorrow. If you "
             "feel unwell, stop exercising and call 112 (or your local emergency number) or a doctor.",
             window_hours=caps.DAY),
    caps.Cap(STAFF_MESSAGES_CAP, 2000, "chat messages", "per staff member or gym owner per day",
             "You've sent {limit} chat messages in the last 24 hours - the daily limit for one account. Please try "
             "again tomorrow, or phone the member if it's urgent.", window_hours=caps.DAY),
    caps.Cap(STAFF_THREAD_CAP, 300, "messages from the gym team", "per conversation per day",
             "The gym team has sent {limit} messages in this conversation in the last 24 hours - the daily limit. "
             "Please carry on tomorrow, or phone the member if it's urgent.", window_hours=caps.DAY),
)
THREAD_TAIL_SLACK = 25  # system safety messages a conversation can hold in 24 hours (one an hour) + a margin
for _cap in CHAT_CAPS:
    caps.CAPS.setdefault(_cap.name, _cap)


class ChatLimitError(ChatError):
    """A chat cap refused the action (message safe to show; answer 429 with ``payload()``)."""

    status = 429

    def __init__(self, exc: caps.CapReached) -> None:
        super().__init__(str(exc))
        self.cap = exc.cap.name
        self.limit = exc.limit
        self.retry_after = 3600

    def payload(self) -> dict[str, Any]:
        return {"error": "Limit reached", "detail": str(self), "code": "cap_reached", "cap": self.cap,
                "limit": self.limit}


async def _enforce(db: AsyncSession, name: str, scope_id: int, rows: Any) -> None:
    try:
        await caps.enforce(db, name, scope_id, rows)
    except caps.CapReached as exc:
        raise ChatLimitError(exc) from None


# ====================================================================== helpers
def has_visible_text(text: str) -> bool:
    """Does ``text`` show anything - a character that is not whitespace, a control / format character, a lone
    combining mark or a blank look-alike?"""
    for ch in text:
        if ch in _BLANK or unicodedata.category(ch) in _INVISIBLE_CATEGORIES:
            continue
        return True
    return False


def clean_body(raw: Any) -> str:
    """The text as stored and shown: control characters, bidi overrides and zero-width junk removed, runs of blank lines
    shortened, trimmed - and "" when nothing visible is left (zero-width / bidi-only input: "Type a message first")."""
    text = str(raw or "").replace("\r\n", "\n").replace("\r", "\n")
    text = _CTRL.sub("", text)
    text = _INVISIBLE.sub("", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    text = text.strip()
    return text if has_visible_text(text) else ""


def preview_of(body: str) -> str:
    return clamp(" ".join(body.split()), PREVIEW_LEN)


def thread_options() -> tuple[Any, ...]:
    """Eager loads every page/API needs (constant queries; relationships are lazy="raise")."""
    return (
        joinedload(ChatThread.gym),
        joinedload(ChatThread.customer),
        joinedload(ChatThread.employee).joinedload(Employee.user),
    )


def active_employment_clauses(user_id: int) -> list[Any]:
    """The user's ACTIVE, not deleted employments - the only ones whose conversations they may open. A suspended, ended
    or not-yet-verified employment loses its conversations (history, safety panel and badge) at once, as it loses its
    clients; they come back when the employment is active again."""
    return [Employee.user_id == user_id, Employee.status == EmploymentStatus.ACTIVE, Employee.deleted_at.is_(None)]


def active_employment_ids(user_id: int) -> Any:
    return select(Employee.id).where(*active_employment_clauses(user_id))


def employment_active(emp: Employee | None) -> bool:
    """The staff member of a conversation can work in it: an ACTIVE, not deleted employment with a linked account."""
    return emp is not None and emp.status == EmploymentStatus.ACTIVE and emp.deleted_at is None and emp.user_id is not None


def visible_clause(user: User) -> Any:
    """Threads this user may open: customers their own, staff their ACTIVE employments', owners their gyms'."""
    if user.role == Role.CUSTOMER:
        return ChatThread.customer_id == user.id
    if user.role == Role.STAFF:
        return ChatThread.employee_id.in_(active_employment_ids(user.id))
    if user.role == Role.OWNER:
        return ChatThread.gym_id.in_(select(Gym.id).where(Gym.owner_id == user.id))
    return false()


def viewer_role(user: User, thread: ChatThread) -> str | None:
    """'customer' | 'staff' | 'owner' for a thread loaded with ``thread_options()`` (None = not a participant)."""
    if thread.customer_id == user.id:
        return "customer"
    emp = thread.employee
    if emp is not None and emp.user_id is not None and emp.user_id == user.id:
        return "staff"
    if thread.gym is not None and thread.gym.owner_id == user.id:
        return "owner"
    return None


def participant_ids(thread: ChatThread) -> set[int]:
    ids = {thread.customer_id, thread.gym.owner_id}
    if thread.employee is not None and thread.employee.user_id:
        ids.add(thread.employee.user_id)
    return {i for i in ids if i}


def employee_gone(emp: Employee | None) -> bool:
    if emp is None:
        return False
    return emp.deleted_at is not None or (emp.user is not None and emp.user.deleted_at is not None)


def purge_date(thread: ChatThread) -> datetime | None:
    dates = []
    if thread.customer is not None and thread.customer.purge_after:
        dates.append(thread.customer.purge_after)
    emp = thread.employee
    if emp is not None:
        if emp.purge_after:
            dates.append(emp.purge_after)
        if emp.user is not None and emp.user.purge_after:
            dates.append(emp.user.purge_after)
    return min(dates) if dates else None


@dataclass(slots=True)
class ReadOnly:
    kind: str  # deleted | closed | former_staff | suspended
    message: str


def read_only_state(thread: ChatThread, tz: str | None = None) -> ReadOnly | None:
    """Why nobody can post in this thread right now (None = open for posting)."""
    customer_deleted = thread.customer is not None and thread.customer.deleted_at is not None
    if customer_deleted or employee_gone(thread.employee):
        when = purge_date(thread)
        local = to_local(when, tz) if when else None
        date_label = f"{local:%d %b %Y}" if local else "the end of the 30-day retention period"
        return ReadOnly("deleted", f"This account was deleted. The conversation will be permanently deleted on {date_label}.")
    if thread.status == ThreadStatus.CLOSED:
        return ReadOnly("closed", "This conversation was closed by the gym. Start a new message to reopen it.")
    emp = thread.employee
    if emp is not None and emp.status == EmploymentStatus.TERMINATED:
        return ReadOnly("former_staff", f"{emp.display_name} no longer works at {thread.gym.name}. Message the gym team instead.")
    if emp is not None and emp.status != EmploymentStatus.ACTIVE:  # suspended / back in verification: can't work, nor chat
        return ReadOnly("suspended", f"{emp.display_name} isn't available for messages at {thread.gym.name} right now. "
                                     "Message the gym team instead.")
    return None


def staff_title(emp: Employee) -> str:
    if emp.role_title:
        return emp.role_title
    return emp.role.value.replace("_", " ").capitalize()


def counterpart(thread: ChatThread, role: str) -> dict[str, Any]:
    """Name / photo / subtitle of 'the other side' as seen by a viewer with ``role``."""
    gym = thread.gym
    if role == "customer":
        emp = thread.employee
        if emp is None:
            return {"name": f"{gym.name} team", "photo": gym.logo_key, "subtitle": "Gym admin team", "kind": "team"}
        return {"name": emp.display_name, "photo": None if employee_gone(emp) else emp.photo_key,
                "subtitle": f"{staff_title(emp)} · {gym.name}", "kind": "staff"}
    cust = thread.customer
    name = cust.display_name if cust is not None else "Deleted user"
    if role == "owner":
        emp = thread.employee
        subtitle = f"with {emp.display_name}" if emp is not None else "Admin team"
        return {"name": name, "photo": None if cust is None or cust.deleted_at else cust.photo_key,
                "subtitle": f"{subtitle} · {gym.name}", "kind": "customer"}
    return {"name": name, "photo": None if cust is None or cust.deleted_at else cust.photo_key,
            "subtitle": gym.name, "kind": "customer"}


def unread_for(thread: ChatThread, role: str) -> int:
    if role == "customer":
        return thread.customer_unread or 0
    if role == "staff" or (role == "owner" and thread.employee_id is None):
        return thread.staff_unread or 0
    return 0


def short_when(value: datetime | None, tz: str | None) -> str:
    """Compact timestamp for lists: 14:05 today, 'Yesterday', weekday this week, else 12 Mar."""
    if value is None:
        return ""
    local = to_local(value, tz)
    today = to_local(utcnow(), tz).date()
    d = local.date()
    if d == today:
        return f"{local:%H:%M}"
    if d == today - timedelta(days=1):
        return "Yesterday"
    if (today - d).days < 7:
        return f"{local:%a}"
    return f"{local:%d %b}" if d.year == today.year else f"{local:%d %b %Y}"


def thread_card(thread: ChatThread, viewer: User) -> dict[str, Any]:
    role = viewer_role(viewer, thread) or "owner"
    other = counterpart(thread, role)
    tz = getattr(viewer, "timezone", None)
    ro = read_only_state(thread, tz)
    last_at = thread.last_message_at or thread.created_at
    return {
        "when": short_when(last_at, tz),
        "id": thread.id,
        "role": role,
        "name": other["name"],
        "photo": other["photo"],
        "subtitle": other["subtitle"],
        "kind": other["kind"],
        "subject": thread.subject,
        "preview": thread.last_message_preview or "No messages yet",
        "last_at": last_at,
        "unread": unread_for(thread, role),
        "closed": thread.status == ThreadStatus.CLOSED,
        "deleted": bool(ro and ro.kind == "deleted"),
        "gym": thread.gym.name,
        "team": thread.employee_id is None,
    }


def sender_label(msg: ChatMessage, thread: ChatThread) -> tuple[str, str]:
    """(display name, role label) of a message's sender."""
    sender = msg.sender
    if msg.sender_role == "system":
        return "IronPulse", "System"
    if msg.sender_role == "staff":
        emp = thread.employee
        if emp is not None and emp.user_id == msg.sender_id:
            return emp.display_name, staff_title(emp)
        return (sender.display_name if sender is not None else "Deleted staff member"), "Staff"
    if sender is None:
        return "Deleted user", ("Admin" if msg.sender_role == "owner" else "Member")
    if msg.sender_role == "owner":
        return sender.display_name, f"{thread.gym.name} admin"
    return sender.display_name, "Member"


def sender_photo(msg: ChatMessage, thread: ChatThread) -> str | None:
    from app.core.templating import media_url

    if msg.sender_role == "staff":
        emp = thread.employee
        if emp is not None and emp.user_id == msg.sender_id and not employee_gone(emp):
            return media_url(emp.photo_key)
    sender = msg.sender
    if sender is None or sender.deleted_at is not None:
        return None
    return media_url(sender.photo_key)


def message_payload(msg: ChatMessage, thread: ChatThread) -> dict[str, Any]:
    name, role_label = sender_label(msg, thread)
    return {
        "id": msg.id,
        "body": msg.body,
        "sender_id": msg.sender_id,
        "sender_name": name,
        "sender_role": msg.sender_role,
        "sender_label": role_label,
        "sender_photo": sender_photo(msg, thread),
        "created_at": msg.created_at.isoformat() if msg.created_at else utcnow().isoformat(),
    }


def day_label(value: datetime, tz: str | None, today: date | None = None) -> str:
    local = to_local(value, tz)
    if local is None:
        return ""
    today = today or to_local(utcnow(), tz).date()
    d = local.date()
    if d == today:
        return "Today"
    if d == today - timedelta(days=1):
        return "Yesterday"
    if (today - d).days < 7:
        return f"{local:%A}"
    return f"{local:%a %d %b %Y}" if d.year != today.year else f"{local:%a %d %b}"


def message_rows(messages: list[ChatMessage], thread: ChatThread, viewer: User) -> list[dict[str, Any]]:
    """View rows for the conversation template: day separators + 'mine' flags + grouped sender headers."""
    tz = getattr(viewer, "timezone", None)
    today = to_local(utcnow(), tz).date()
    rows: list[dict[str, Any]] = []
    last_day = None
    last_sender: tuple[Any, str] | None = None
    for m in messages:
        local = to_local(m.created_at, tz)
        d = local.date() if local else None
        if d != last_day:
            rows.append({"type": "day", "label": day_label(m.created_at, tz, today), "date": d.isoformat() if d else ""})
            last_day = d
            last_sender = None
        payload = message_payload(m, thread)
        key = (m.sender_id, m.sender_role)
        rows.append({
            "type": "message",
            **payload,
            "mine": m.sender_id is not None and m.sender_id == viewer.id,
            "system": m.sender_role == "system",
            "time": f"{local:%H:%M}" if local else "",
            "first_of_group": key != last_sender,
            "created": m.created_at,
        })
        last_sender = key
    return rows


# ====================================================================== queries
@dataclass(slots=True)
class ThreadPage:
    """One window of the conversation list, most recent activity first."""

    items: list[ChatThread]
    has_more: bool  # older conversations exist beyond this window
    next_cursor: str | None  # pass as ``older=`` to get them


def thread_activity() -> Any:
    """When a conversation last moved (its last message, else its creation): the list's sort key."""
    return func.coalesce(ChatThread.last_message_at, ChatThread.created_at)


def thread_cursor(thread: ChatThread) -> str:
    """Opaque keyset cursor ``<activity in µs since 1970>-<id>``: the window after it is unaffected by threads that
    move to the top meanwhile (a value, not a row lookup)."""
    at = thread.last_message_at or thread.created_at
    return f"{(at - _EPOCH) // timedelta(microseconds=1)}-{thread.id}"


def parse_thread_cursor(raw: Any) -> tuple[datetime, int] | None:
    """(activity, id) of a cursor from ``thread_cursor`` - None for anything malformed or out of range."""
    m = _CURSOR_RE.match(str(raw or "").strip())
    if not m:
        return None
    micros, thread_id = int(m.group(1)), int(m.group(2))
    if not 0 < thread_id <= MAX_ID:
        return None
    try:
        return _EPOCH + timedelta(microseconds=micros), thread_id
    except OverflowError:
        return None


def _thread_search(q: str) -> Any:
    """Name / subject / last-message search, matched only against each visible thread's own member and staff member
    (correlated primary-key lookups) - never a scan of the platform-wide users / employees tables."""
    like = "%" + re.sub(r"([\\%_])", r"\\\1", q) + "%"
    member, staff = aliased(User), aliased(Employee)
    member_match = (select(member.id).where(member.id == ChatThread.customer_id, member.deleted_at.is_(None),
                                            member.full_name.ilike(like, escape="\\")).correlate(ChatThread).exists())
    staff_match = (select(staff.id).where(staff.id == ChatThread.employee_id, staff.deleted_at.is_(None),
                                          staff.full_name.ilike(like, escape="\\")).correlate(ChatThread).exists())
    return or_(member_match, staff_match, ChatThread.subject.ilike(like, escape="\\"),
               ChatThread.last_message_preview.ilike(like, escape="\\"))


def _window_ids(user: User, conds: list[Any], n: int) -> Any:
    """Ids of one window of ``user``'s conversations, most recent activity first (a subquery). A customer's come
    straight from ix_chat_threads_customer_activity; a staff member's / an owner's are the newest ``n`` of EACH of
    their employments / gyms (LATERAL ... ORDER BY ... LIMIT n over ix_chat_threads_employee_activity /
    ix_chat_threads_gym_activity), merged - never a sort of all of their conversations."""
    activity = thread_activity()
    if user.role == Role.CUSTOMER:
        return (select(ChatThread.id).where(ChatThread.customer_id == user.id, *conds)
                .order_by(activity.desc(), ChatThread.id.desc()).limit(n))
    if user.role == Role.STAFF:
        scope = select(Employee.id.label("sid")).where(*active_employment_clauses(user.id)).subquery()
        column = ChatThread.employee_id
    elif user.role == Role.OWNER:
        scope, column = select(Gym.id.label("sid")).where(Gym.owner_id == user.id).subquery(), ChatThread.gym_id
    else:
        return select(ChatThread.id).where(false())
    newest = (select(ChatThread.id.label("id"), activity.label("at")).where(column == scope.c.sid, *conds)
              .order_by(activity.desc(), ChatThread.id.desc()).limit(n).lateral("newest"))
    return select(newest.c.id).select_from(scope).join(newest, true()).order_by(newest.c.at.desc(), newest.c.id.desc()).limit(n)


async def list_threads_page(
    db: AsyncSession, user: User, *, q: str = "", status: str = "", gym_id: int | None = None, scope: str = "",
    older: str | None = None, limit: int = THREAD_PAGE_SIZE,
) -> ThreadPage:
    """Conversations ``user`` may open, most recent activity first, one keyset window (``older`` = the
    ``next_cursor`` of the previous window). One statement whatever the number of conversations."""
    limit = max(1, min(int(limit or THREAD_PAGE_SIZE), 100))
    activity = thread_activity()
    conds: list[Any] = []
    if status == "open":
        conds.append(ChatThread.status == ThreadStatus.OPEN)
    elif status == "closed":
        conds.append(ChatThread.status == ThreadStatus.CLOSED)
    if gym_id:
        conds.append(ChatThread.gym_id == gym_id)
    if scope == "team":
        conds.append(ChatThread.employee_id.is_(None))
    elif scope == "staff":
        conds.append(ChatThread.employee_id.is_not(None))
    if q:
        conds.append(_thread_search(q))
    cursor = parse_thread_cursor(older)
    if cursor is not None:
        at, thread_id = cursor
        conds.append(tuple_(activity, ChatThread.id)
                     < tuple_(literal(at, DateTime(timezone=True)), literal(thread_id, BigInteger)))
    stmt = (select(ChatThread).options(*thread_options()).where(ChatThread.id.in_(_window_ids(user, conds, limit + 1)))
            .order_by(activity.desc(), ChatThread.id.desc()))
    rows = list((await db.execute(stmt)).unique().scalars().all())
    has_more = len(rows) > limit
    items = rows[:limit]
    return ThreadPage(items=items, has_more=has_more,
                      next_cursor=thread_cursor(items[-1]) if has_more and items else None)


async def list_threads(
    db: AsyncSession, user: User, *, q: str = "", status: str = "", gym_id: int | None = None, scope: str = "",
    older: str | None = None, limit: int = THREAD_PAGE_SIZE,
) -> list[ChatThread]:
    """The conversations of one window (see ``list_threads_page``)."""
    page = await list_threads_page(db, user, q=q, status=status, gym_id=gym_id, scope=scope, older=older, limit=limit)
    return page.items


async def load_thread(db: AsyncSession, user: User, thread_id: int) -> ChatThread | None:
    """The thread if ``user`` may see it (None otherwise -> 404, so IDs cannot be probed)."""
    stmt = select(ChatThread).options(*thread_options()).where(ChatThread.id == thread_id, visible_clause(user))
    return (await db.execute(stmt)).unique().scalar_one_or_none()


async def list_messages(
    db: AsyncSession, thread_id: int, *, after: int | None = None, before: int | None = None, limit: int = PAGE_SIZE,
) -> tuple[list[ChatMessage], bool]:
    """Messages oldest -> newest. ``after``: newer than id (polling); ``before``: older page; default: latest page.
    Returns (messages, has_more_older)."""
    limit = max(1, min(int(limit or PAGE_SIZE), 100))
    # cursors outside BIGINT would make the driver fail: clamp them (a huge `before` = the latest page)
    after = None if after is None else max(0, min(int(after), MAX_ID))
    before = None if before is None else max(0, min(int(before), MAX_ID))
    stmt = select(ChatMessage).options(joinedload(ChatMessage.sender)).where(ChatMessage.thread_id == thread_id)
    if after is not None:
        rows = (await db.execute(stmt.where(ChatMessage.id > after).order_by(ChatMessage.id).limit(limit))).scalars().all()
        return list(rows), False
    if before is not None:
        stmt = stmt.where(ChatMessage.id < before)
    rows = list((await db.execute(stmt.order_by(ChatMessage.id.desc()).limit(limit + 1))).scalars().all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    rows.reverse()
    return rows, has_more


async def unread_total(db: AsyncSession, user: User) -> int:
    """Badge count for the navbar / chat list."""
    if user.role == Role.CUSTOMER:
        col = ChatThread.customer_unread
        clause = ChatThread.customer_id == user.id
    elif user.role == Role.STAFF:
        col = ChatThread.staff_unread
        clause = visible_clause(user)
    elif user.role == Role.OWNER:
        col = ChatThread.staff_unread
        clause = and_(visible_clause(user), ChatThread.employee_id.is_(None))
    else:
        return 0
    return int(await db.scalar(select(func.coalesce(func.sum(col), 0)).where(clause)) or 0)


# ====================================================================== starting conversations
@dataclass(slots=True)
class ChatTarget:
    gym: Gym
    employees: list[Employee]
    existing: dict[int | None, int]  # employee_id (None = team) -> thread id
    trainer_ids: set[int]  # the customer's assigned trainers at this gym


async def assigned_trainer_ids(db: AsyncSession, customer_id: int, gym_ids: list[int]) -> set[int]:
    if not gym_ids:
        return set()
    today = date.today()
    rows = await db.execute(
        select(Subscription.trainer_id).where(
            Subscription.customer_id == customer_id, Subscription.gym_id.in_(gym_ids),
            Subscription.status == SubscriptionStatus.ACTIVE, Subscription.trainer_id.is_not(None),
            or_(Subscription.ends_on.is_(None), Subscription.ends_on >= today),
        )
    )
    ids = {tid for (tid,) in rows.all() if tid}
    rows = await db.execute(
        select(TrainerClient.employee_id).join(Employee, Employee.id == TrainerClient.employee_id)
        .where(TrainerClient.customer_id == customer_id, TrainerClient.active.is_(True), Employee.gym_id.in_(gym_ids))
    )
    ids |= {eid for (eid,) in rows.all() if eid}
    return ids


def _reachable_employee_clause(gym_ids: list[int], trainer_ids: set[int]) -> list[Any]:
    clauses = [
        Employee.gym_id.in_(gym_ids),
        Employee.status == EmploymentStatus.ACTIVE,
        Employee.deleted_at.is_(None),
        Employee.user_id.is_not(None),
    ]
    clauses.append(or_(Employee.is_public.is_(True), Employee.id.in_(trainer_ids or {-1})))
    return clauses


async def chat_targets(db: AsyncSession, customer: User) -> list[ChatTarget]:
    """Gyms where the customer has an active membership + the people they may message there (4 queries)."""
    today = date.today()
    gyms = list((await db.execute(
        select(Gym).join(Subscription, Subscription.gym_id == Gym.id)
        .where(Subscription.customer_id == customer.id, Subscription.status == SubscriptionStatus.ACTIVE,
               or_(Subscription.ends_on.is_(None), Subscription.ends_on >= today), Gym.is_active.is_(True))
        .distinct().order_by(Gym.name)
    )).scalars().all())
    if not gyms:
        return []
    gym_ids = [g.id for g in gyms]
    trainers = await assigned_trainer_ids(db, customer.id, gym_ids)
    employees = list((await db.execute(
        select(Employee).options(joinedload(Employee.user))
        .join(User, User.id == Employee.user_id)
        .where(*_reachable_employee_clause(gym_ids, trainers), User.deleted_at.is_(None), User.is_active.is_(True))
        .order_by(Employee.is_trainer.desc(), Employee.full_name)
    )).unique().scalars().all())
    existing_rows = (await db.execute(
        select(ChatThread.gym_id, ChatThread.employee_id, ChatThread.id).where(ChatThread.customer_id == customer.id)
    )).all()
    existing: dict[int, dict[int | None, int]] = {}
    for gid, eid, tid in existing_rows:
        existing.setdefault(gid, {})[eid] = tid
    out = []
    for g in gyms:
        emps = [e for e in employees if e.gym_id == g.id]
        # assigned trainers first
        emps.sort(key=lambda e: (e.id not in trainers, not e.is_trainer, e.full_name.lower()))
        out.append(ChatTarget(gym=g, employees=emps, existing=existing.get(g.id, {}),
                              trainer_ids={e.id for e in emps if e.id in trainers}))
    return out


async def get_or_create_thread(db: AsyncSession, *, gym_id: int, customer_id: int, employee_id: int | None,
                               urgent: bool = False) -> ChatThread:
    """The (unique) thread between a customer and a gym's team / staff member, created if needed.

    New threads (and reopening a closed one) require an ACTIVE membership at the gym; staff must be an ACTIVE
    employee of that gym with a linked account who is public or the customer's assigned trainer. A NEW thread counts
    towards the member's ``THREADS_CAP`` (race-safe: the cap's lock is held until the caller commits) - unless
    ``urgent`` (the first message mentions red-flag symptoms: a safety message is never refused)."""
    stmt = select(ChatThread).where(ChatThread.gym_id == gym_id, ChatThread.customer_id == customer_id)
    stmt = stmt.where(ChatThread.employee_id.is_(None) if employee_id is None else ChatThread.employee_id == employee_id)
    existing = (await db.execute(stmt)).scalar_one_or_none()

    customer = await db.get(User, customer_id)
    if customer is None or customer.role != Role.CUSTOMER or customer.deleted_at is not None:
        raise ChatPermissionError("Only members can start conversations with a gym.")
    if existing is not None and existing.status == ThreadStatus.OPEN:
        return existing

    gym = await db.get(Gym, gym_id)
    if gym is None or not gym.is_active:
        raise ChatError("That gym isn't available.")
    if not await has_active_subscription(db, customer_id, gym_id):
        raise ChatPermissionError("You need an active membership at this gym to message its team.")
    emp: Employee | None = None
    if employee_id is not None:
        trainers = await assigned_trainer_ids(db, customer_id, [gym_id])
        emp = (await db.execute(
            select(Employee).join(User, User.id == Employee.user_id)
            .where(Employee.id == employee_id, *_reachable_employee_clause([gym_id], trainers), User.deleted_at.is_(None))
        )).scalar_one_or_none()
        if emp is None:
            raise ChatError("That staff member isn't available to message at this gym.")

    if existing is not None:  # closed thread: the member writing again reopens it
        existing.status = ThreadStatus.OPEN
        return existing

    if not urgent:
        await _enforce(db, THREADS_CAP, customer_id, select(ChatThread.id).where(
            ChatThread.customer_id == customer_id, ChatThread.created_at >= caps.since()))
    subject = f"Chat with {emp.full_name}" if emp is not None else f"Message to the {gym.name} team"
    thread = ChatThread(gym_id=gym_id, customer_id=customer_id, employee_id=employee_id, subject=subject[:160],
                        status=ThreadStatus.OPEN, customer_unread=0, staff_unread=0)
    try:
        async with db.begin_nested():
            db.add(thread)
            await db.flush()
    except IntegrityError:  # created concurrently by another request -> use that one
        again = (await db.execute(stmt)).scalar_one_or_none()
        if again is None:
            raise ChatError("Could not start the conversation - please try again.") from None
        return again
    return thread


# ====================================================================== posting
@dataclass(slots=True)
class _Parties:
    gym: Gym
    customer: User | None
    employee: Employee | None
    employee_user: User | None


async def _parties(db: AsyncSession, thread: ChatThread) -> _Parties:
    gym = await db.get(Gym, thread.gym_id)
    customer = await db.get(User, thread.customer_id)
    emp = await db.get(Employee, thread.employee_id) if thread.employee_id else None
    emp_user = await db.get(User, emp.user_id) if emp is not None and emp.user_id else None
    if gym is None:
        raise ChatError("This conversation no longer exists.")
    return _Parties(gym, customer, emp, emp_user)


def _check_sender(p: _Parties, thread: ChatThread, sender: User, role: str) -> None:
    if sender is None or sender.deleted_at is not None or not sender.is_active:
        raise ChatPermissionError("Your account can't send messages.")
    if role not in SENDER_ROLES:
        raise ChatError("Unknown sender role.")
    if role == "customer":
        ok = sender.id == thread.customer_id
    elif role == "staff":
        ok = p.employee is not None and p.employee.user_id == sender.id
    else:
        ok = p.gym.owner_id == sender.id
    if not ok:
        raise ChatPermissionError("You're not part of this conversation.")


def _staff_unavailable(p: _Parties) -> bool:
    """The conversation's staff member can't take messages: their employment is not ACTIVE (suspended, ended, back in
    verification), or it or their account was deleted."""
    if p.employee is None:
        return False
    return not employment_active(p.employee) or p.employee_user is None or p.employee_user.deleted_at is not None


def _refuse_read_only(thread: ChatThread, p: _Parties) -> None:
    """Raise why nobody may post in this conversation right now (``ChatError``, safe to show) - else return."""
    if thread.status != ThreadStatus.OPEN:
        raise ChatError("This conversation is closed.")
    customer_deleted = p.customer is None or p.customer.deleted_at is not None
    staff_deleted = p.employee is not None and (p.employee.deleted_at is not None or
                                                 (p.employee_user is not None and p.employee_user.deleted_at is not None))
    if customer_deleted or staff_deleted:
        raise ChatError("This account was deleted, so the conversation is read-only.")
    if p.employee is not None and p.employee.status == EmploymentStatus.TERMINATED:
        raise ChatError("This staff member no longer works at the gym.")
    if _staff_unavailable(p):
        raise ChatError("This staff member isn't available for messages right now. Message the gym team instead.")


def _thread_tail() -> int:
    """How many of a conversation's newest messages hold a whole day of it: the gym side's and the member's daily caps
    plus the system safety messages. (A member's red-flag messages bypass their cap: a flood of them can only make the
    gym-side ceiling count short - never refuse the gym team early.)"""
    return caps.limit(STAFF_THREAD_CAP) + caps.limit(MESSAGES_CAP) + THREAD_TAIL_SLACK


async def _enforce_caps(db: AsyncSession, thread: ChatThread, p: _Parties, sender: User, sender_role: str,
                        red: Any) -> None:
    """The daily caps of this message (race-safe: each lock is held until the caller commits)."""
    if sender_role == "customer":
        if red is None:  # a red-flag message is never refused by a cap
            await _enforce(db, MESSAGES_CAP, sender.id, select(ChatMessage.id).where(
                ChatMessage.sender_id == sender.id, ChatMessage.created_at >= caps.since()))
        return
    # the gym side: this conversation's ceiling - counted in its newest messages only (a bounded backward range of
    # ix_chat_messages_thread_id_id, never its whole history) - then the sender's own cap over all their conversations
    gym_side = sorted({i for i in (p.gym.owner_id, p.employee.user_id if p.employee is not None else None, sender.id) if i})
    tail = (select(ChatMessage.sender_id, ChatMessage.created_at).where(ChatMessage.thread_id == thread.id)
            .order_by(ChatMessage.id.desc()).limit(_thread_tail()).subquery("tail"))
    await _enforce(db, STAFF_THREAD_CAP, thread.id, select(tail.c.sender_id).where(
        tail.c.sender_id.in_(gym_side), tail.c.created_at >= caps.since()))
    await _enforce(db, STAFF_MESSAGES_CAP, sender.id, select(ChatMessage.id).where(
        ChatMessage.sender_id == sender.id, ChatMessage.created_at >= caps.since()))


async def post_message(db: AsyncSession, *, thread: ChatThread, sender: User, body: str, sender_role: str) -> ChatMessage:
    """Validate and store a message, bump the thread counters, push it live to every participant after commit
    and notify (in-app + throttled e-mail) the recipients who are not online. The caller commits.

    A member's message counts towards ``MESSAGES_CAP``; a staff member's / owner's towards ``STAFF_THREAD_CAP`` and
    ``STAFF_MESSAGES_CAP``. A member's message that mentions red-flag symptoms is never refused - not by a cap, a closed
    conversation or a staff member who can't take messages right now: a closed conversation is reopened (as the member
    writing again would) and an unavailable staff member is left out (the gym admin, who sees every conversation of the
    gym, and the member's trainers are alerted instead). The member gets emergency guidance (a safety message in the
    thread, at most one per hour, and ``message.safety_alert`` for the caller to show at once) and the gym admin, the
    staff member and the member's trainers get an urgent notification (app/services/trainer_safety.py).

    Message ids are handed out under the conversation's row lock, i.e. in commit order within a conversation: polling
    ``after=<newest id seen>`` never skips a message."""
    text = clean_body(body)
    if not text:
        raise ChatError("Type a message first.")
    if len(text) > MAX_BODY:
        raise ChatError(f"Messages can be at most {MAX_BODY:,} characters.")
    p = await _parties(db, thread)
    _check_sender(p, thread, sender, sender_role)
    red = trainer_safety.detect_red_flag(text) if sender_role == "customer" else None
    reopen = False
    if red is None:
        _refuse_read_only(thread, p)
    else:
        if _staff_unavailable(p):  # nobody to read it there but the gym admin: leave the staff member out
            p = _Parties(p.gym, p.customer, None, None)
        reopen = thread.status != ThreadStatus.OPEN
    await _enforce_caps(db, thread, p, sender, sender_role, red)

    now = utcnow()
    preview = preview_of(text)
    inc_staff = 1 if sender_role == "customer" else 0
    inc_customer = 0 if sender_role == "customer" else 1
    values: dict[str, Any] = {"last_message_at": now, "last_message_preview": preview, "updated_at": now}
    if reopen:
        values["status"] = ThreadStatus.OPEN
    # the conversation's row lock FIRST, then the message's id: ids of one conversation are handed out in commit order,
    # so a reader polling ``after=<newest id seen>`` (60-chat.js) can never miss a message that commits after a higher
    # id (e.g. a trainer's reply racing a member's red-flag message and its safety message)
    res = await db.execute(
        update(ChatThread)
        .where(ChatThread.id == thread.id)
        .values(**values, staff_unread=ChatThread.staff_unread + inc_staff,
                customer_unread=ChatThread.customer_unread + inc_customer)
        .returning(ChatThread.staff_unread, ChatThread.customer_unread)
        .execution_options(synchronize_session=False)
    )
    row = res.first()
    if row is None:  # removed meanwhile (an account purge)
        raise ChatError("This conversation no longer exists.")
    staff_unread, customer_unread = row
    for attr, value in (*values.items(), ("staff_unread", staff_unread), ("customer_unread", customer_unread)):
        set_committed_value(thread, attr, value)

    msg = ChatMessage(thread_id=thread.id, sender_id=sender.id, sender_role=sender_role, body=text, created_at=now)
    db.add(msg)
    await db.flush()
    set_committed_value(msg, "sender", sender)

    # ---- live delivery to every participant (sender's other tabs included)
    view = _ThreadView(thread, p)
    payload = message_payload(msg, view)  # type: ignore[arg-type]
    everyone = {thread.customer_id, p.gym.owner_id}
    if p.employee is not None and p.employee.user_id:
        everyone.add(p.employee.user_id)
    publish_after_commit(db, sorted(i for i in everyone if i), {
        "type": "chat.message", "thread_id": thread.id, "message": payload,
        "preview": preview, "customer_unread": customer_unread, "staff_unread": staff_unread,
    })
    if reopen:  # the others' open pages reload into the reopened conversation (the member's own keeps the guidance)
        publish_after_commit(db, sorted(i for i in everyone if i and i != sender.id),
                             {"type": "chat.status", "thread_id": thread.id, "status": ThreadStatus.OPEN.value})

    # ---- notifications for recipients who are not online (e-mail at most once per thread per 15 min)
    recipients: set[int] = set()
    if sender_role == "customer":
        recipients.add(p.employee.user_id if p.employee is not None and p.employee.user_id else p.gym.owner_id)
    else:
        recipients.add(thread.customer_id)
        if sender_role == "owner" and p.employee is not None and p.employee.user_id:
            recipients.add(p.employee.user_id)
    recipients.discard(sender.id)
    link = f"/chat/{thread.id}"
    sender_name, _ = sender_label(msg, view)  # type: ignore[arg-type]
    title = f"New message from {sender_name}"
    if sender_role != "customer":
        title = f"{sender_name} · {p.gym.name}"[:160] if sender_role == "staff" else f"Message from {p.gym.name}"
    # online on ANY worker (presence's Redis leases with several processes; at most one round trip for them all): the
    # live chat.message event reaches them, so no notification or e-mail on top
    online = await hub.online_anywhere(recipients)
    for rid in sorted(recipients):
        if not rid or rid in online:
            continue
        # e-mail throttle probe: a backward range read of ix_notifications_user_created over the last 15 minutes of
        # this recipient's notifications only - never their whole (unbounded) history
        recent = await db.scalar(
            select(Notification.id).where(
                Notification.user_id == rid, Notification.created_at >= now - EMAIL_THROTTLE,
                Notification.category == NotificationCategory.MESSAGE, Notification.link == link,
                Notification.emailed.is_(True),
            ).order_by(Notification.created_at.desc()).limit(1)
        )
        await notify(db, rid, title, preview, category=NotificationCategory.MESSAGE, severity=Severity.INFO,
                     link=link, email=recent is None)
    if red is not None:
        alert = await trainer_safety.raise_red_flag(db, thread=thread, gym=p.gym, member=sender, employee=p.employee,
                                                    red=red)
        if alert.alerted:
            await _post_system_message(db, thread, p, view, alert.guidance, sorted(i for i in everyone if i),
                                       customer_unread=customer_unread, staff_unread=staff_unread)
        msg.safety_alert = alert  # type: ignore[attr-defined]  # transient: the routes show the guidance at once
    return msg


async def _post_system_message(db: AsyncSession, thread: ChatThread, p: _Parties, view: Any, body: str,
                               recipients: list[int], *, customer_unread: int, staff_unread: int) -> ChatMessage:
    """An IronPulse message in the conversation (sender NULL, role "system"): pushed live to every participant; it
    neither counts as unread nor replaces the conversation's preview."""
    sys_msg = ChatMessage(thread_id=thread.id, sender_id=None, sender_role="system", body=clean_body(body)[:MAX_BODY],
                          created_at=utcnow())
    db.add(sys_msg)
    await db.flush()
    set_committed_value(sys_msg, "sender", None)
    publish_after_commit(db, recipients, {
        "type": "chat.message", "thread_id": thread.id, "message": message_payload(sys_msg, view),
        "preview": thread.last_message_preview, "customer_unread": customer_unread, "staff_unread": staff_unread,
    })
    return sys_msg


class _ThreadView:
    """Minimal stand-in exposing ``gym``/``employee`` for payload helpers when the thread's relationships
    are not loaded (the assistant passes a bare ChatThread)."""

    __slots__ = ("employee", "gym", "id")

    def __init__(self, thread: ChatThread, p: _Parties) -> None:
        self.id = thread.id
        self.gym = p.gym
        self.employee = p.employee
        if p.employee is not None:
            set_committed_value(p.employee, "user", p.employee_user)


# ====================================================================== read state / status
async def mark_read(db: AsyncSession, thread: ChatThread, user: User) -> bool:
    """Reset the viewer's unread counter (owners only clear admin-team threads). Returns True if changed."""
    role = viewer_role(user, thread)
    if role == "customer":
        if not thread.customer_unread:
            return False
        await db.execute(update(ChatThread).where(ChatThread.id == thread.id).values(customer_unread=0)
                         .execution_options(synchronize_session=False))
        set_committed_value(thread, "customer_unread", 0)
        return True
    if role == "staff" or (role == "owner" and thread.employee_id is None):
        if not thread.staff_unread:
            return False
        await db.execute(update(ChatThread).where(ChatThread.id == thread.id).values(staff_unread=0)
                         .execution_options(synchronize_session=False))
        set_committed_value(thread, "staff_unread", 0)
        return True
    return False


async def set_status(db: AsyncSession, thread: ChatThread, actor: User, status: ThreadStatus) -> None:
    """Owner / assigned staff close or reopen a conversation (customer notified). The caller commits."""
    role = viewer_role(actor, thread)
    if role == "staff" and not employment_active(thread.employee):  # suspended / former staff: not the gym team now
        role = None
    if role not in ("owner", "staff"):
        raise ChatPermissionError("Only the gym team can close or reopen conversations.")
    if status == ThreadStatus.OPEN:
        ro = read_only_state(thread)
        if ro is not None and ro.kind == "deleted":
            raise ChatError("This conversation is read-only because an account was deleted.")
    if thread.status == status:
        return
    thread.status = status
    verb = "closed" if status == ThreadStatus.CLOSED else "reopened"
    publish_after_commit(db, sorted(participant_ids(thread)), {"type": "chat.status", "thread_id": thread.id,
                                                               "status": status.value})
    if thread.customer is not None and thread.customer.deleted_at is None and actor.id != thread.customer_id:
        await notify(db, thread.customer, f"Conversation {verb}",
                     f"{thread.gym.name} {verb} the conversation “{thread.subject}”.",
                     category=NotificationCategory.MESSAGE, severity=Severity.INFO, link=f"/chat/{thread.id}",
                     email=False)


def typing_targets(thread: ChatThread, user_id: int) -> list[int]:
    return sorted(i for i in participant_ids(thread) if i != user_id)


__all__ = [
    "CHAT_CAPS", "MESSAGES_CAP", "STAFF_MESSAGES_CAP", "STAFF_THREAD_CAP", "THREADS_CAP", "ChatError", "ChatLimitError",
    "ChatPermissionError", "ChatTarget", "MAX_BODY", "PAGE_SIZE", "THREAD_PAGE_SIZE", "ThreadPage",
    "active_employment_clauses", "active_employment_ids", "chat_targets", "clean_body", "counterpart",
    "employment_active", "get_or_create_thread", "has_visible_text", "list_messages", "list_threads",
    "list_threads_page", "load_thread", "mark_read", "message_payload", "message_rows", "parse_thread_cursor",
    "participant_ids", "post_message", "read_only_state", "set_status", "thread_activity", "thread_card",
    "thread_cursor", "thread_options", "typing_targets", "unread_total", "viewer_role", "visible_clause",
]
