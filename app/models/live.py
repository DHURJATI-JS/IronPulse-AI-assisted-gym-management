"""Live AI workout sessions (voice companion with an animated face) and their event log."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, DateTime, Integer, SmallInteger, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.base import CreatedMixin, IdMixin, TimestampMixin, fk

if TYPE_CHECKING:
    from app.models.users import User


class WorkoutSession(IdMixin, TimestampMixin, Base):
    """One live, voice-guided workout. ``items`` snapshots the exercises (so later plan edits don't change history)."""

    __tablename__ = "workout_sessions"
    __table_args__ = (
        CheckConstraint("intensity BETWEEN -3 AND 3", name="intensity_range"),
        CheckConstraint(
            "status IN ('active', 'paused', 'completed', 'stopped', 'stopped_for_safety')", name="status_values"
        ),
    )

    customer_id: Mapped[int] = fk("users.id", "CASCADE")
    plan_id: Mapped[int | None] = fk("workout_plans.id", "SET NULL")
    title: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(20), default="active", server_default="active", index=True)
    intensity: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")  # -3 easier .. +3 harder
    current_index: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
    items: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_reason: Mapped[str | None] = mapped_column(String(255))
    voice_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    mic_used: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    ai_messages: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    customer: Mapped[User] = relationship(lazy="raise")
    events: Mapped[list[WorkoutSessionEvent]] = relationship(
        back_populates="session", lazy="raise", passive_deletes=True, order_by="WorkoutSessionEvent.id"
    )


class WorkoutSessionEvent(IdMixin, CreatedMixin, Base):
    """kind: user_said | ai_said | ai_refused | command | discomfort | red_flag | exercise_change | intensity_change |
    set_done | exercise_done | paused | resumed | completed | stopped

    ai_refused: the AI guard / queue refused a chat reply (message = the spoken refusal line, detail = {code, detail,
    retry_after}); it is not part of the AI history fed back to the model."""

    __tablename__ = "workout_session_events"

    session_id: Mapped[int] = fk("workout_sessions.id", "CASCADE")
    kind: Mapped[str] = mapped_column(String(30), index=True)
    message: Mapped[str | None] = mapped_column(Text)  # what was said (truncated)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))

    session: Mapped[WorkoutSession] = relationship(back_populates="events", lazy="raise")
