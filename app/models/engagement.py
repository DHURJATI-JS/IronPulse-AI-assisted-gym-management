"""Tasks/reminders/calendar, notifications + e-mail outbox, live chat, misconduct reports, assistant log."""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.base import EMBEDDING_DIM, CreatedMixin, IdMixin, TimestampMixin, enum_col, fk
from app.models.enums import (
    EmailStatus,
    NotificationCategory,
    ReportStatus,
    Severity,
    TaskKind,
    ThreadStatus,
)

if TYPE_CHECKING:
    from app.models.gyms import Gym
    from app.models.staff import Employee
    from app.models.users import User


class Task(IdMixin, TimestampMixin, Base):
    """To-dos, reminders and calendar events (created by the user, the AI assistant or the system)."""

    __tablename__ = "tasks"
    __table_args__ = (
        # the task_reminders periodic: due reminders not yet sent (sent ones leave the index)
        Index("ix_tasks_reminder_due", "remind_at", postgresql_where=text("reminded_at IS NULL AND completed_at IS NULL")),
        # one person's OPEN tasks in /tasks order (services/tasks.py: open_tasks_page ORDER BY coalesce(due_at,
        # remind_at) NULLS LAST, id; open_task_stats; the TASKS_MAX_OPEN count): at most the cap's worth of rows,
        # never a walk over their completed history (completed tasks are kept)
        Index("ix_tasks_user_open", "user_id", text("coalesce(due_at, remind_at)"), "id",
              postgresql_where=text("completed_at IS NULL")),
        # the tasks_per_person_day cap (services/tasks.py create_task): one person's tasks of the last 24 hours - an
        # index range of at most the cap's worth of rows, never their completed history
        Index("ix_tasks_user_created", "user_id", "created_at"),
    )

    user_id: Mapped[int] = fk("users.id", "CASCADE")
    title: Mapped[str] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[TaskKind] = enum_col(TaskKind, "kind", default=TaskKind.TASK)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    remind_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    all_day: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(10), default="user", server_default="user")  # user|ai|system


class Notification(IdMixin, CreatedMixin, Base):
    """In-app notification (Dynamic Island + bell). Every notification is also e-mailed."""

    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_user_unread", "user_id", "read_at"),  # the unread badge count
        # the bell feed / notifications page / dashboards: WHERE user_id = ? ORDER BY created_at DESC, id DESC
        Index("ix_notifications_user_created", "user_id", "created_at", "id"),
        Index("ix_notifications_created_at", "created_at"),  # recent-window probes, retention
    )

    user_id: Mapped[int] = fk("users.id", "CASCADE")
    title: Mapped[str] = mapped_column(String(160))
    body: Mapped[str] = mapped_column(Text)
    category: Mapped[NotificationCategory] = enum_col(NotificationCategory, "category", default=NotificationCategory.SYSTEM)
    severity: Mapped[Severity] = enum_col(Severity, "severity", default=Severity.INFO)
    link: Mapped[str | None] = mapped_column(String(300))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    emailed: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))


class EmailOutbox(IdMixin, CreatedMixin, Base):
    """Transactional e-mail queue (sent by the background worker over SMTP, or spooled to var/mail)."""

    __tablename__ = "email_outbox"
    __table_args__ = (
        Index("ix_email_outbox_status_id", "status", "id"),  # the mailer's claim (status = queued ORDER BY id)
        Index("ix_email_outbox_status_created", "status", "created_at"),  # retention of sent / failed mail
        # the super-admin outbox search: to_email ILIKE '%q%' OR subject ILIKE '%q%' (a BitmapOr of the two)
        Index("ix_email_outbox_to_email_trgm", "to_email", postgresql_using="gin", postgresql_ops={"to_email": "gin_trgm_ops"}),
        Index("ix_email_outbox_subject_trgm", "subject", postgresql_using="gin", postgresql_ops={"subject": "gin_trgm_ops"}),
    )

    user_id: Mapped[int | None] = fk("users.id", "CASCADE", nullable=True)
    to_email: Mapped[str] = mapped_column(String(254))
    cc: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    subject: Mapped[str] = mapped_column(String(200))
    text_body: Mapped[str] = mapped_column(Text)
    html_body: Mapped[str | None] = mapped_column(Text)
    status: Mapped[EmailStatus] = enum_col(EmailStatus, "status", default=EmailStatus.QUEUED, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notification_id: Mapped[int | None] = fk("notifications.id", "SET NULL")


ACTIVITY_DESC = "(coalesce(last_message_at, created_at)) DESC"  # a conversation's last activity (the list's sort key)


class ChatThread(IdMixin, TimestampMixin, Base):
    """Live chat between a customer and a gym's staff member (``employee_id``) or its admin team (NULL).

    Owners see every thread of their gyms in the admin panel. Deleting either side keeps the thread for
    30 days (shown as "Deleted user"); the purge then removes it via ON DELETE CASCADE.
    """

    __tablename__ = "chat_threads"
    __table_args__ = (
        UniqueConstraint("gym_id", "customer_id", "employee_id", postgresql_nulls_not_distinct=True),
        # the conversation list, most recent activity first - exactly services/chat.py's thread_activity() order
        # (ORDER BY coalesce(last_message_at, created_at) DESC, id DESC): one ordered range per customer / employment /
        # gym, read LIMIT n at a time, never a sort of all of a viewer's conversations. They are also the foreign keys'
        # indexes (leading column): the single-column ones they replace are dropped by create_indexes_concurrently().
        Index("ix_chat_threads_customer_activity", "customer_id", text(ACTIVITY_DESC), text("id DESC")),
        Index("ix_chat_threads_employee_activity", "employee_id", text(ACTIVITY_DESC), text("id DESC")),
        Index("ix_chat_threads_gym_activity", "gym_id", text(ACTIVITY_DESC), text("id DESC")),
        {"info": {"superseded_indexes": {  # app.db.SUPERSEDED_INDEXES: old index -> the index that replaces it
            "ix_chat_threads_customer_id": "ix_chat_threads_customer_activity",
            "ix_chat_threads_employee_id": "ix_chat_threads_employee_activity",
            "ix_chat_threads_gym_id": "ix_chat_threads_gym_activity",
        }}},
    )

    # indexed by the activity indexes above - a narrower single-column index would only compete with them in the planner
    gym_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("gyms.id", ondelete="CASCADE"), nullable=False)
    customer_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    employee_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("employees.id", ondelete="CASCADE"),
                                                    nullable=True)
    subject: Mapped[str] = mapped_column(String(160))
    status: Mapped[ThreadStatus] = enum_col(ThreadStatus, "status", default=ThreadStatus.OPEN)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_message_preview: Mapped[str | None] = mapped_column(String(200))
    customer_unread: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    staff_unread: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    gym: Mapped[Gym] = relationship(lazy="raise")
    customer: Mapped[User] = relationship(lazy="raise", foreign_keys=[customer_id])
    employee: Mapped[Employee | None] = relationship(lazy="raise")
    messages: Mapped[list[ChatMessage]] = relationship(
        back_populates="thread", lazy="raise", passive_deletes=True, order_by="ChatMessage.id"
    )


class ChatMessage(IdMixin, CreatedMixin, Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        CheckConstraint("char_length(body) BETWEEN 1 AND 4000", name="body_length"),
        Index("ix_chat_messages_thread_id_id", "thread_id", "id"),  # keyset pages of one thread
        # the per-member daily message cap (services/chat.py): one sender's last 24 hours only, never their history
        Index("ix_chat_messages_sender_created", "sender_id", "created_at"),
    )

    thread_id: Mapped[int] = fk("chat_threads.id", "CASCADE")
    sender_id: Mapped[int | None] = fk("users.id", "SET NULL")  # NULL after the sender is purged
    sender_role: Mapped[str] = mapped_column(String(10))  # customer | staff | owner | system
    body: Mapped[str] = mapped_column(Text)

    thread: Mapped[ChatThread] = relationship(back_populates="messages", lazy="raise")
    sender: Mapped[User | None] = relationship(lazy="raise")


# Before uq_misconduct_reports_one_open can be built: parallel submits once filed the same report several times (a race
# the per-member lock now closes). Keep the oldest open report per (member, staff member); close the copies, saying so.
DEDUPE_OPEN_REPORTS_SQL = (
    "UPDATE misconduct_reports AS r SET status = 'declined', resolved_at = now(), updated_at = now(), "
    "admin_response = 'Closed automatically: a duplicate of your report #' || ("
    "SELECT min(o.id) FROM misconduct_reports o WHERE o.status = 'open' AND o.reporter_id = r.reporter_id "
    "AND o.employee_id = r.employee_id) || ' about the same staff member, which stays open.' "
    "WHERE r.status = 'open' AND EXISTS (SELECT 1 FROM misconduct_reports o WHERE o.status = 'open' "
    "AND o.reporter_id = r.reporter_id AND o.employee_id = r.employee_id AND o.id < r.id)"
)


class MisconductReport(IdMixin, TimestampMixin, Base):
    """Customer report about an employee. The admin must answer with a reason (decline) or an apology."""

    __tablename__ = "misconduct_reports"
    __table_args__ = (
        CheckConstraint("status = 'open' OR char_length(coalesce(admin_response, '')) >= 10", name="response_required"),
        # ONE open report per member and staff member - the last line of defence behind report_submit's per-member lock
        # (app/routers/chat.py turns the IntegrityError into the friendly "already reported" error)
        Index("uq_misconduct_reports_one_open", "reporter_id", "employee_id", unique=True,
              postgresql_where=text("status = 'open'")),
        {"info": {"before_unique_index": {  # app.db.BEFORE_UNIQUE_INDEX: make existing rows fit before it is built
            "uq_misconduct_reports_one_open": DEDUPE_OPEN_REPORTS_SQL,
        }}},
    )

    gym_id: Mapped[int] = fk("gyms.id", "CASCADE")
    employee_id: Mapped[int] = fk("employees.id", "CASCADE")
    reporter_id: Mapped[int] = fk("users.id", "CASCADE")
    category: Mapped[str] = mapped_column(String(40))
    description: Mapped[str] = mapped_column(Text)
    incident_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[ReportStatus] = enum_col(ReportStatus, "status", default=ReportStatus.OPEN, index=True)
    admin_response: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by_id: Mapped[int | None] = fk("users.id", "SET NULL")

    employee: Mapped[Employee] = relationship(lazy="raise")
    reporter: Mapped[User] = relationship(lazy="raise", foreign_keys=[reporter_id])
    gym: Mapped[Gym] = relationship(lazy="raise")


class AssistantMessage(IdMixin, CreatedMixin, Base):
    """Conversation with the AI assistant (embeddings give it long-term memory via pgvector)."""

    __tablename__ = "assistant_messages"

    user_id: Mapped[int] = fk("users.id", "CASCADE")
    role: Mapped[str] = mapped_column(String(10))  # user | assistant
    content: Mapped[str] = mapped_column(Text)
    actions: Mapped[list | None] = mapped_column(JSONB)
    provider: Mapped[str | None] = mapped_column(String(40))
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
