"""Security events, IP bans, audit trail, AI call log and the background job queue.

Indexes follow the readers: time-window aggregates and the nightly retention (app/services/retention.py) range-scan
``created_at`` / ``(status, finished_at)``; the order and staff timelines look audit rows up by entity. New indexes on
large existing tables: build them CONCURRENTLY before deploying (ARCHITECTURE.md §3), sync_schema then skips them."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import CreatedMixin, IdMixin, enum_col, fk
from app.models.enums import JobStatus, ThreatCategory


class SecurityEvent(IdMixin, CreatedMixin, Base):
    __tablename__ = "security_events"
    __table_args__ = (
        Index("ix_security_events_ip_created", "ip", "created_at"),
        Index("ix_security_events_created_at", "created_at"),  # 24 h / 7 d / 30 d windows, retention
        Index("ix_security_events_category_created", "category", "created_at"),  # category + day filters
    )

    ip: Mapped[str] = mapped_column(String(45))
    user_id: Mapped[int | None] = fk("users.id", "SET NULL")
    method: Mapped[str] = mapped_column(String(10))
    path: Mapped[str] = mapped_column(String(512))
    user_agent: Mapped[str | None] = mapped_column(String(300))
    category: Mapped[ThreatCategory] = enum_col(ThreatCategory, "category", index=True)
    severity: Mapped[int] = mapped_column(SmallInteger)  # 1 (noise) .. 5 (critical)
    detail: Mapped[str | None] = mapped_column(String(500))
    action: Mapped[str] = mapped_column(String(10), default="logged")  # logged | blocked | banned
    ai_reviewed: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), index=True)
    ai_verdict: Mapped[str | None] = mapped_column(Text)


class IpBan(IdMixin, CreatedMixin, Base):
    """IP bans. ``banned_until`` NULL = permanent (repeat offenders without a subscription)."""

    __tablename__ = "ip_bans"
    __table_args__ = (
        Index("ix_ip_bans_ip_active", "ip", "lifted_at"),
        # ban sync (every 30 s): lifted_at IS NULL AND (banned_until IS NULL OR banned_until > now); retention of
        # expired bans: lifted_at IS NULL AND banned_until < cutoff
        Index("ix_ip_bans_unlifted_until", "banned_until", postgresql_where=text("lifted_at IS NULL")),
        Index("ix_ip_bans_lifted_at", "lifted_at", postgresql_where=text("lifted_at IS NOT NULL")),  # retention: lifted
    )

    ip: Mapped[str] = mapped_column(String(45))
    user_id: Mapped[int | None] = fk("users.id", "SET NULL")
    reason: Mapped[str] = mapped_column(String(300))
    severity: Mapped[int] = mapped_column(SmallInteger)
    banned_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(20), default="auto")  # auto | ai | superadmin
    offense_count: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    had_subscription: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    lifted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lifted_by_id: Mapped[int | None] = fk("users.id", "SET NULL")


class AuditLog(IdMixin, CreatedMixin, Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        # an order's / an employee's timeline: WHERE entity_type = ? AND entity_id = ? ORDER BY created_at
        Index("ix_audit_logs_entity", "entity_type", "entity_id", "created_at"),
        Index("ix_audit_logs_created_at", "created_at"),  # retention
        # the owner audit page, newest first (ORDER BY created_at DESC, id DESC): one gym's rows / one actor's rows.
        # They are also the foreign keys' indexes (leading column), replacing the single-column ones (dropped by
        # create_indexes_concurrently()): no extra index on this insert-heavy table, and no narrower competitor
        Index("ix_audit_logs_gym_created", "gym_id", "created_at", "id"),
        Index("ix_audit_logs_actor_created", "actor_id", "created_at", "id"),
        {"info": {"superseded_indexes": {  # app.db.SUPERSEDED_INDEXES: old index -> the index that replaces it
            "ix_audit_logs_actor_id": "ix_audit_logs_actor_created",
            "ix_audit_logs_gym_id": "ix_audit_logs_gym_created",
        }}},
    )

    # indexed by ix_audit_logs_actor_created / ix_audit_logs_gym_created above
    actor_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    gym_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("gyms.id", ondelete="SET NULL"), nullable=True)
    action: Mapped[str] = mapped_column(String(80), index=True)
    entity_type: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    ip: Mapped[str | None] = mapped_column(String(45))


class AICallLog(IdMixin, CreatedMixin, Base):
    __tablename__ = "ai_call_logs"
    __table_args__ = (
        Index("ix_ai_call_logs_user_created", "user_id", "created_at"),
        Index("ix_ai_call_logs_created_at", "created_at"),  # the super-admin 24 h aggregates, retention
    )

    feature: Mapped[str] = mapped_column(String(40), index=True)
    provider: Mapped[str] = mapped_column(String(20))
    model: Mapped[str] = mapped_column(String(80))
    ok: Mapped[bool] = mapped_column(Boolean)
    latency_ms: Mapped[int] = mapped_column(Integer)  # provider time only (queue wait is queue_ms)
    error: Mapped[str | None] = mapped_column(String(500))
    # who spent the compute (app/ai/owner.py) - NULL for system work
    user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    owner_kind: Mapped[str | None] = mapped_column(String(10))  # user | job | system
    queue_ms: Mapped[int | None] = mapped_column(Integer)  # time spent waiting in the AI queue
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))


class Job(IdMixin, CreatedMixin, Base):
    """Durable background job (claimed with SELECT ... FOR UPDATE SKIP LOCKED)."""

    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_claim", "status", "run_after"),
        Index("ix_jobs_user_status", "user_id", "status"),
        Index("ix_jobs_status_finished", "status", "finished_at"),  # retention of finished jobs
    )

    kind: Mapped[str] = mapped_column(String(60))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    status: Mapped[JobStatus] = enum_col(JobStatus, "status", default=JobStatus.QUEUED)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, default=5, server_default="5")
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    dedupe_key: Mapped[str | None] = mapped_column(String(120), unique=True)
    progress: Mapped[float | None] = mapped_column(Float)
    # the person who asked for this work (NULL = system). exit_policy says what happens when they leave:
    # keep (default: obligations such as verification/payroll), cancel, or pause (resumes when they return)
    user_id: Mapped[int | None] = fk("users.id", "SET NULL")
    exit_policy: Mapped[str] = mapped_column(String(10), default="keep", server_default="keep")
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(String(80))
