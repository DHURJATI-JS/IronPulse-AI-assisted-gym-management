"""Health conditions, daily metrics, alerts, safety suspensions, wellness checks, coaching content."""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.base import EMBEDDING_DIM, CreatedMixin, IdMixin, TimestampMixin, enum_col, fk
from app.models.enums import AnomalyLevel, ConditionKind, FitnessLevel, Goal, StressLevel

if TYPE_CHECKING:
    from app.models.users import User


class HealthCondition(IdMixin, CreatedMixin, Base):
    """One illness / allergy / medication / injury of a customer (typed in, or extracted by the AI intake).

    ``tag`` is the normalised safety tag (asthma, hypertension, knee_injury ...) used by the exercise
    safety guardrails; ``name`` keeps what the person/owner wrote.
    """

    __tablename__ = "health_conditions"
    __table_args__ = (UniqueConstraint("customer_id", "kind", "name"),)

    customer_id: Mapped[int] = fk("users.id", "CASCADE")
    kind: Mapped[ConditionKind] = enum_col(ConditionKind, "kind")
    name: Mapped[str] = mapped_column(String(120))
    tag: Mapped[str | None] = mapped_column(String(40), index=True)
    notes: Mapped[str | None] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String(20), default="self", server_default="self")  # self|owner|ai_intake

    customer: Mapped[User] = relationship(lazy="raise")


class MetricEntry(IdMixin, CreatedMixin, Base):
    """One height/weight reading per customer per day (the form is pre-filled with the last values)."""

    __tablename__ = "metric_entries"
    __table_args__ = (
        UniqueConstraint("customer_id", "recorded_on"),
        CheckConstraint("height_cm BETWEEN 40 AND 260", name="height_range"),
        CheckConstraint("weight_kg BETWEEN 10 AND 400", name="weight_range"),
    )

    customer_id: Mapped[int] = fk("users.id", "CASCADE")
    recorded_on: Mapped[date] = mapped_column(Date, index=True)
    height_cm: Mapped[float] = mapped_column(Float)
    weight_kg: Mapped[float] = mapped_column(Float)
    bmi: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(20), default="manual", server_default="manual")  # manual|ocr|owner
    anomaly_level: Mapped[AnomalyLevel] = enum_col(AnomalyLevel, "anomaly_level", default=AnomalyLevel.NONE)
    anomaly_reasons: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    ai_note: Mapped[str | None] = mapped_column(Text)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))


class HealthAlert(IdMixin, CreatedMixin, Base):
    __tablename__ = "health_alerts"

    customer_id: Mapped[int] = fk("users.id", "CASCADE")
    metric_entry_id: Mapped[int | None] = fk("metric_entries.id", "SET NULL")
    wellness_check_id: Mapped[int | None] = fk("wellness_checks.id", "SET NULL")
    level: Mapped[AnomalyLevel] = enum_col(AnomalyLevel, "level")
    title: Mapped[str] = mapped_column(String(160))
    message: Mapped[str] = mapped_column(Text)
    recommendation: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(10), default="rule", server_default="rule")  # rule|ai
    guardian_notified: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SuspensionEvent(IdMixin, CreatedMixin, Base):
    """Audit trail of safety suspensions (AI/rules) and admin reactivations."""

    __tablename__ = "suspension_events"

    customer_id: Mapped[int] = fk("users.id", "CASCADE")
    action: Mapped[str] = mapped_column(String(12))  # suspend | reactivate
    reason: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(20))  # rule | ai | owner | superadmin
    actor_user_id: Mapped[int | None] = fk("users.id", "SET NULL")
    health_alert_id: Mapped[int | None] = fk("health_alerts.id", "SET NULL")


class WellnessCheck(IdMixin, CreatedMixin, Base):
    """Stress check-in: PSS-4 questionnaire + symptoms + optional face photo analysed by the vision model.
    The photo itself is never stored."""

    __tablename__ = "wellness_checks"

    customer_id: Mapped[int] = fk("users.id", "CASCADE")
    pss_answers: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    pss_score: Mapped[int | None] = mapped_column(SmallInteger)
    symptoms: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    face_analysis: Mapped[dict | None] = mapped_column(JSONB)
    stress_level: Mapped[StressLevel] = enum_col(StressLevel, "stress_level", default=StressLevel.LOW)
    recommendations: Mapped[str | None] = mapped_column(Text)
    ai_provider: Mapped[str | None] = mapped_column(String(40))


class WorkoutPlan(IdMixin, TimestampMixin, Base):
    """A personalised plan (AI-made then safety-filtered, a trainer plan, or a standard template)."""

    __tablename__ = "workout_plans"

    customer_id: Mapped[int] = fk("users.id", "CASCADE")
    gym_id: Mapped[int | None] = fk("gyms.id", "SET NULL")
    created_by_id: Mapped[int | None] = fk("users.id", "SET NULL")
    goal: Mapped[Goal] = enum_col(Goal, "goal")
    source: Mapped[str] = mapped_column(String(12))  # ai | trainer | template
    title: Mapped[str] = mapped_column(String(160))
    plan: Mapped[dict] = mapped_column(JSONB)  # {"days": [{"name":..., "items": [{exercise, sets, reps, load, rest_s, notes}]}]}
    safety_notes: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    removed_items: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    ai_provider: Mapped[str | None] = mapped_column(String(40))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), index=True)


class Exercise(IdMixin, Base):
    """Exercise library with contraindication tags (the guardrail the AI's suggestions are checked against)."""

    __tablename__ = "exercises"

    slug: Mapped[str] = mapped_column(String(80), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    category: Mapped[str] = mapped_column(String(30))  # calisthenics|strength|cardio|mobility|breathing
    goals: Mapped[list] = mapped_column(JSONB, default=list)
    muscles: Mapped[list] = mapped_column(JSONB, default=list)
    equipment: Mapped[str] = mapped_column(String(60), default="none")
    difficulty: Mapped[FitnessLevel] = enum_col(FitnessLevel, "difficulty", default=FitnessLevel.BEGINNER)
    intensity: Mapped[str] = mapped_column(String(10), default="moderate")  # low|moderate|high
    impact: Mapped[str] = mapped_column(String(10), default="low")  # low|high
    description: Mapped[str] = mapped_column(Text)
    cues: Mapped[list] = mapped_column(JSONB, default=list)
    contraindications: Mapped[list] = mapped_column(JSONB, default=list)  # safety tags
    video_slug: Mapped[str | None] = mapped_column(String(80))
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))


class Video(IdMixin, Base):
    """Pre-recorded (programmatically generated) exercise video. Available with every subscription,
    and still available when every AI provider is down."""

    __tablename__ = "videos"

    slug: Mapped[str] = mapped_column(String(80), unique=True)
    title: Mapped[str] = mapped_column(String(160))
    goal: Mapped[str] = mapped_column(String(30), index=True)  # calisthenics|bulking|weight_loss|general_fitness|recovery
    level: Mapped[str] = mapped_column(String(20), default="beginner")
    description: Mapped[str] = mapped_column(Text)
    duration_seconds: Mapped[int] = mapped_column(Integer)
    file_name: Mapped[str] = mapped_column(String(160))
    poster_name: Mapped[str | None] = mapped_column(String(160))
    exercise_slugs: Mapped[list] = mapped_column(JSONB, default=list)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
