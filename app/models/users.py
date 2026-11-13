"""Accounts: users, sessions, one-time tokens, customer profiles and identity documents.

Deletion policy (relational, enforced by FK ON DELETE rules):
* Deleting an account is a *soft* delete: ``deleted_at`` + ``purge_after = now + 30 days``.
  The row (and therefore its unique email / phone) stays, so nobody can register with the same
  e-mail or phone during the retention window, and chats show "Deleted user".
* The daily purge job hard-deletes the user row after ``purge_after``; ON DELETE CASCADE removes the
  person's data (profile, metrics, chats, tasks, notifications ...); ledger rows use SET NULL so the
  money trail survives with an anonymised party label.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import TYPE_CHECKING

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Time,
    func,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import CITEXT, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.base import EMBEDDING_DIM, CreatedMixin, IdMixin, TimestampMixin, enum_col, fk
from app.models.enums import FitnessLevel, Goal, IdDocType, IdStatus, Role, TokenPurpose

if TYPE_CHECKING:
    from app.models.gyms import Gym
    from app.models.health import HealthCondition
    from app.models.staff import Employee


class User(IdMixin, TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("(deleted_at IS NULL) = (purge_after IS NULL)", name="deletion_window"),
        CheckConstraint("username IS NULL OR char_length(username) BETWEEN 3 AND 30", name="username_length"),
        # ban decisions and security triage look accounts up by the IP they last signed in from
        Index("ix_users_last_login_ip", "last_login_ip", postgresql_where=text("last_login_ip IS NOT NULL")),
        # the super-admin user search: e-mail / name / username ILIKE '%q%' OR phone LIKE '%digits%' - a BitmapOr needs
        # every arm indexed. e-mail and username are citext, whose own ILIKE operator no trigram index serves: the
        # search compares them as text (CAST(email AS TEXT) ILIKE ...), which these expression indexes match.
        Index("ix_users_full_name_trgm", "full_name", postgresql_using="gin", postgresql_ops={"full_name": "gin_trgm_ops"}),
        Index("ix_users_email_trgm", text("(email::text) gin_trgm_ops"), postgresql_using="gin"),
        Index("ix_users_username_trgm", text("(username::text) gin_trgm_ops"), postgresql_using="gin"),
        Index("ix_users_phone_normalized_trgm", "phone_normalized", postgresql_using="gin",
              postgresql_ops={"phone_normalized": "gin_trgm_ops"}),
    )

    email: Mapped[str | None] = mapped_column(CITEXT(), unique=True, index=True)
    # public handle used to sign in (with e-mail, phone or member ID) and shown in the account chip;
    # reserved like e-mail/phone while a deleted account is in its 30-day retention window
    username: Mapped[str | None] = mapped_column(CITEXT(), unique=True, index=True)
    phone: Mapped[str | None] = mapped_column(String(32))
    phone_normalized: Mapped[str | None] = mapped_column(String(20), unique=True, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    full_name: Mapped[str] = mapped_column(String(120))
    role: Mapped[Role] = enum_col(Role, "role", default=Role.CUSTOMER, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    date_of_birth: Mapped[date | None] = mapped_column(Date)
    gender: Mapped[str | None] = mapped_column(String(32))
    photo_key: Mapped[str | None] = mapped_column(String(255))  # public media key (compulsory for owner/staff)

    # location (browser geolocation or geocoded city) - used for 30 km offline checks & bedtime
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", server_default="UTC")
    country_code: Mapped[str | None] = mapped_column(String(2))
    city: Mapped[str | None] = mapped_column(String(120))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    location_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    id_status: Mapped[IdStatus] = enum_col(IdStatus, "id_status", default=IdStatus.MISSING)

    failed_login_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_ip: Mapped[str | None] = mapped_column(String(45))

    # soft delete / 30-day retention
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    purge_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    deletion_reason: Mapped[str | None] = mapped_column(String(255))

    customer_profile: Mapped[CustomerProfile | None] = relationship(
        back_populates="user", uselist=False, lazy="raise", passive_deletes=True, foreign_keys="CustomerProfile.user_id"
    )
    owned_gyms: Mapped[list[Gym]] = relationship(back_populates="owner", lazy="raise", passive_deletes=True)
    employments: Mapped[list[Employee]] = relationship(
        back_populates="user", lazy="raise", passive_deletes=True, foreign_keys="Employee.user_id"
    )
    identity_documents: Mapped[list[IdentityDocument]] = relationship(
        back_populates="user", lazy="raise", passive_deletes=True, foreign_keys="IdentityDocument.user_id"
    )

    # ---------------------------------------------------------------- helpers
    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    @property
    def display_name(self) -> str:
        return "Deleted user" if self.deleted_at is not None else self.full_name

    def age(self, today: date | None = None) -> int | None:
        if not self.date_of_birth:
            return None
        today = today or date.today()
        dob = self.date_of_birth
        return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))

    @property
    def is_minor(self) -> bool:
        age = self.age()
        return age is not None and age < 18

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User {self.id} {self.role} {self.email}>"


class UserSession(Base):
    """Server-side session. The cookie holds a random token; only its SHA-256 is stored."""

    __tablename__ = "user_sessions"
    __table_args__ = (
        # trust & safety (app/services/trust_safety.py _other_accounts_on): which other accounts used one address
        # recently - an index range instead of a scan of every session
        Index("ix_user_sessions_ip_seen", "ip", "last_seen_at", postgresql_where=text("ip IS NOT NULL")),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = fk("users.id", "CASCADE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ip: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(300))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(lazy="raise")


class AuthToken(IdMixin, CreatedMixin, Base):
    """One-time tokens: e-mail verification, password reset, customer invite, staff invite."""

    __tablename__ = "auth_tokens"
    __table_args__ = (
        # nightly retention (app/services/retention.py): tokens used / superseded long ago, and unused links that
        # expired long ago - each an index range, oldest first
        Index("ix_auth_tokens_used_at", "used_at", postgresql_where=text("used_at IS NOT NULL")),
        Index("ix_auth_tokens_expires_unused", "expires_at", postgresql_where=text("used_at IS NULL")),
    )

    purpose: Mapped[TokenPurpose] = enum_col(TokenPurpose, "purpose")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int | None] = fk("users.id", "CASCADE", nullable=True)
    email: Mapped[str | None] = mapped_column(CITEXT())
    employee_id: Mapped[int | None] = fk("employees.id", "CASCADE", nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User | None] = relationship(lazy="raise")


class CustomerProfile(TimestampMixin, Base):
    """Fitness profile of a customer (1:1 with users). Health conditions live in ``health_conditions``."""

    __tablename__ = "customer_profiles"

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    goal: Mapped[Goal] = enum_col(Goal, "goal", default=Goal.GENERAL_FITNESS)
    fitness_level: Mapped[FitnessLevel] = enum_col(FitnessLevel, "fitness_level", default=FitnessLevel.BEGINNER)
    height_cm: Mapped[float | None] = mapped_column(Float)
    weight_kg: Mapped[float | None] = mapped_column(Float)
    health_notes: Mapped[str | None] = mapped_column(Text)
    # privacy choice (account settings): share health & safety details with the member's trainers. The gym's owner /
    # admin team always sees them; staff then see "chose not to share - ask before training" (app/services/trainer_safety.py)
    share_health_with_trainers: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())

    # guardian (mandatory for minors) and emergency contact
    guardian_name: Mapped[str | None] = mapped_column(String(120))
    guardian_phone: Mapped[str | None] = mapped_column(String(32))
    guardian_email: Mapped[str | None] = mapped_column(CITEXT())
    guardian_relation: Mapped[str | None] = mapped_column(String(40))
    emergency_contact_name: Mapped[str | None] = mapped_column(String(120))
    emergency_contact_phone: Mapped[str | None] = mapped_column(String(32))

    # safety suspension (AI / rule engine) - only an admin can reactivate
    activity_suspended: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    suspension_reason: Mapped[str | None] = mapped_column(Text)
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    suspended_by: Mapped[str | None] = mapped_column(String(40))  # "rule" | "ai" | "owner:<id>"

    # recovery / environment
    wake_time: Mapped[time] = mapped_column(Time, default=time(6, 30), server_default=text("'06:30'"))
    sleep_reminders_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    last_bedtime_reminder_on: Mapped[date | None] = mapped_column(Date)
    ac_recommendation: Mapped[dict | None] = mapped_column(JSONB)

    intake_source: Mapped[str] = mapped_column(String(20), default="self", server_default="self")
    registered_by_id: Mapped[int | None] = fk("users.id", "SET NULL")
    health_embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))

    user: Mapped[User] = relationship(back_populates="customer_profile", lazy="raise", foreign_keys=[user_id])
    conditions: Mapped[list[HealthCondition]] = relationship(
        lazy="raise",
        primaryjoin="CustomerProfile.user_id == foreign(HealthCondition.customer_id)",
        viewonly=True,
    )


# Hard ceiling of every fitness-profile list. The product caps (20 / 20 / 10 / 30, app/services/fitness_profile.py)
# are enforced under the row's lock; these CHECKs are the database backstop whatever a future code path does.
FITNESS_LIST_CEILING = 50


class FitnessProfile(TimestampMixin, Base):
    """How a member likes to train (1:1 with users): exercise types they like / avoid, goals, equipment, sessions per
    week, session length, preferred time of day and notes - typed on /me/health/profile/fitness or confirmed from the
    assistant / live coach ("remember this?"). Every AI feature reads it through ``app.ai.member_context``.

    Health facts (conditions, allergies, medications, injuries) stay in ``health_conditions``; the main plan goal and
    the experience level stay on ``customer_profiles`` (``goal``, ``fitness_level``). List items are vocabulary slugs
    (``app.services.fitness_profile``) or the member's own short labels ("other"). ``forgotten`` keeps what the member
    asked the AI to forget (newest first, capped), so the assistant also hides it from its view of past chats."""

    __tablename__ = "fitness_profiles"
    __table_args__ = (
        CheckConstraint(f"jsonb_array_length(likes) <= {FITNESS_LIST_CEILING}", name="fitness_likes_cap"),
        CheckConstraint(f"jsonb_array_length(avoids) <= {FITNESS_LIST_CEILING}", name="fitness_avoids_cap"),
        CheckConstraint(f"jsonb_array_length(goals) <= {FITNESS_LIST_CEILING}", name="fitness_goals_cap"),
        CheckConstraint(f"jsonb_array_length(equipment) <= {FITNESS_LIST_CEILING}", name="fitness_equipment_cap"),
        CheckConstraint(f"jsonb_array_length(forgotten) <= {FITNESS_LIST_CEILING}", name="fitness_forgotten_cap"),
        CheckConstraint("sessions_per_week IS NULL OR sessions_per_week BETWEEN 1 AND 14", name="fitness_sessions_range"),
        CheckConstraint("session_minutes IS NULL OR session_minutes BETWEEN 10 AND 240", name="fitness_minutes_range"),
    )

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    likes: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    avoids: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    goals: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    equipment: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    sessions_per_week: Mapped[int | None] = mapped_column(Integer)
    session_minutes: Mapped[int | None] = mapped_column(Integer)
    preferred_time: Mapped[str | None] = mapped_column(String(20))
    notes: Mapped[str | None] = mapped_column(String(500))
    forgotten: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))


class IdentityDocument(IdMixin, TimestampMixin, Base):
    """Government ID card scan (mandatory for customers, staff and gym admins).

    Images are encrypted at rest. ID numbers are stored only masked + as a peppered hash
    (used to detect the same ID being used by several accounts).
    """

    __tablename__ = "identity_documents"
    __table_args__ = (
        CheckConstraint("(user_id IS NOT NULL) OR (employee_id IS NOT NULL)", name="has_subject"),
        Index("ix_identity_documents_number_hash", "id_number_hash"),
    )

    user_id: Mapped[int | None] = fk("users.id", "CASCADE", nullable=True)
    employee_id: Mapped[int | None] = fk("employees.id", "CASCADE", nullable=True)
    doc_type: Mapped[IdDocType] = enum_col(IdDocType, "doc_type")
    country_code: Mapped[str] = mapped_column(String(2))
    front_key: Mapped[str] = mapped_column(String(255))  # encrypted private file
    back_key: Mapped[str | None] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, server_default="0")  # front + back: the member's quota

    status: Mapped[IdStatus] = enum_col(IdStatus, "status", default=IdStatus.PENDING, index=True)
    id_number_masked: Mapped[str | None] = mapped_column(String(64))
    id_number_hash: Mapped[str | None] = mapped_column(String(64))
    name_on_id: Mapped[str | None] = mapped_column(String(160))
    dob_on_id: Mapped[date | None] = mapped_column(Date)
    expires_on: Mapped[date | None] = mapped_column(Date)

    engines: Mapped[list | None] = mapped_column(JSONB)  # engine names that ran
    engine_results: Mapped[list | None] = mapped_column(JSONB)  # per-engine extracted (masked) fields
    checks: Mapped[list | None] = mapped_column(JSONB)  # [{"check": str, "passed": bool, "detail": str}]
    consensus_score: Mapped[float | None] = mapped_column(Float)
    ai_summary: Mapped[str | None] = mapped_column(Text)
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    uploaded_by_id: Mapped[int | None] = fk("users.id", "SET NULL")
    reviewed_by_id: Mapped[int | None] = fk("users.id", "SET NULL")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User | None] = relationship(back_populates="identity_documents", lazy="raise", foreign_keys=[user_id])
    employee: Mapped[Employee | None] = relationship(lazy="raise", foreign_keys=[employee_id])
