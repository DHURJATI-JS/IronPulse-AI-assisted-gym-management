"""Plans, subscriptions, the transaction ledger, desk OTPs and salary statements."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ColumnElement,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    literal_column,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.base import CreatedMixin, IdMixin, TimestampMixin, enum_col, fk
from app.models.enums import (
    PlanMode,
    PurchaseChannel,
    StatementStatus,
    SubscriptionStatus,
    TxCategory,
    TxDirection,
    TxStatus,
)

if TYPE_CHECKING:
    from app.models.gyms import Branch, Gym
    from app.models.staff import Employee
    from app.models.users import User


class Plan(IdMixin, TimestampMixin, Base):
    """Subscription plan of a gym. Codes: offline, offline_trainer, offline_ai, offline_trainer_ai,
    online, online_trainer, online_ai, online_trainer_ai, combo, combo_trainer, combo_ai, combo_trainer_ai.
    Pre-recorded videos are included in every plan."""

    __tablename__ = "plans"
    __table_args__ = (
        UniqueConstraint("gym_id", "code"),
        CheckConstraint("price >= 0", name="price_non_negative"),
        CheckConstraint("duration_days BETWEEN 1 AND 3660", name="duration_range"),
    )

    gym_id: Mapped[int] = fk("gyms.id", "CASCADE")
    code: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(120))
    mode: Mapped[PlanMode] = enum_col(PlanMode, "mode")
    includes_trainer: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    includes_ai: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3))
    duration_days: Mapped[int] = mapped_column(Integer, default=30, server_default="30")
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    description: Mapped[str | None] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    gym: Mapped[Gym] = relationship(back_populates="plans", lazy="raise")

    @property
    def needs_branch(self) -> bool:
        return self.mode in (PlanMode.OFFLINE, PlanMode.COMBO)


class Subscription(IdMixin, TimestampMixin, Base):
    __tablename__ = "subscriptions"
    __table_args__ = (CheckConstraint("price >= 0", name="price_non_negative"),)

    customer_id: Mapped[int] = fk("users.id", "CASCADE")
    gym_id: Mapped[int] = fk("gyms.id", "CASCADE")
    plan_id: Mapped[int] = fk("plans.id", "NO ACTION", nullable=False)  # plans are disabled, never deleted
    branch_id: Mapped[int | None] = fk("branches.id", "SET NULL")
    trainer_id: Mapped[int | None] = fk("employees.id", "SET NULL")

    status: Mapped[SubscriptionStatus] = enum_col(SubscriptionStatus, "status", default=SubscriptionStatus.PENDING_PAYMENT, index=True)
    channel: Mapped[PurchaseChannel] = enum_col(PurchaseChannel, "channel")
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3))
    starts_on: Mapped[date | None] = mapped_column(Date)
    ends_on: Mapped[date | None] = mapped_column(Date, index=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # fraud protection evidence
    physically_present: Mapped[bool | None] = mapped_column(Boolean)
    otp_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    customer_latitude: Mapped[float | None] = mapped_column(Float)
    customer_longitude: Mapped[float | None] = mapped_column(Float)
    distance_km: Mapped[float | None] = mapped_column(Float)
    fraud_score: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    fraud_flags: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    created_ip: Mapped[str | None] = mapped_column(String(45))

    customer: Mapped[User] = relationship(lazy="raise", foreign_keys=[customer_id])
    gym: Mapped[Gym] = relationship(lazy="raise")
    plan: Mapped[Plan] = relationship(lazy="raise")
    branch: Mapped[Branch | None] = relationship(lazy="raise")
    trainer: Mapped[Employee | None] = relationship(lazy="raise")


# "Booked at" of a ledger row - completion time, else creation time - as the finance pages filter and sort on it
# (owner_finance.BOOKED_AT renders exactly this expression, which is what lets the indexes below serve those queries).
BOOKED_AT_SQL = "coalesce(completed_at, created_at)"
# The SHA-256 of an uploaded payment proof (Transaction.meta["proof"]["sha256"], written by payroll.record_proof).
PROOF_SHA256_SQL = "((meta -> 'proof') ->> 'sha256')"


class Transaction(IdMixin, TimestampMixin, Base):
    """Ledger of every incoming and outgoing money movement (shown in the admin panel).

    Party foreign keys use SET NULL so the ledger survives account purges; ``party_label`` keeps a
    human-readable (anonymised after purge) description.
    """

    __tablename__ = "transactions"
    __table_args__ = (
        CheckConstraint("amount >= 0", name="amount_non_negative"),
        # one gym's ledger, newest first: the finance overview's recent list and 12-month chart, the transactions page
        # (its rows and the totals of a period), every CSV export batch (keyset on (booked, id)) and the owner
        # dashboard's revenue this month - WHERE gym_id = ? [AND booked >= ?] ORDER BY booked DESC, id DESC
        Index("ix_transactions_gym_booked", "gym_id", text(f"{BOOKED_AT_SQL} DESC"), text("id DESC")),
        # every gym at once: the super-admin overview's 30-day revenue - WHERE direction = ? AND status = ? AND booked >= ?
        Index("ix_transactions_direction_status_booked", "direction", "status", text(BOOKED_AT_SQL)),
        # the payment-proof duplicate check (payroll.verify_payment_proof): WHERE meta->'proof'->>'sha256' = ?. Only
        # rows that carry a proof are indexed (an equality implies IS NOT NULL, so the partial index always applies)
        Index("ix_transactions_proof_sha256", text(PROOF_SHA256_SQL),
              postgresql_where=text(f"{PROOF_SHA256_SQL} IS NOT NULL")),
    )

    gym_id: Mapped[int | None] = fk("gyms.id", "SET NULL")
    direction: Mapped[TxDirection] = enum_col(TxDirection, "direction", index=True)
    category: Mapped[TxCategory] = enum_col(TxCategory, "category", index=True)
    status: Mapped[TxStatus] = enum_col(TxStatus, "status", default=TxStatus.CREATED, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    currency: Mapped[str] = mapped_column(String(3))
    description: Mapped[str] = mapped_column(String(255))
    party_label: Mapped[str | None] = mapped_column(String(160))

    provider: Mapped[str] = mapped_column(String(20))  # cashfree | test | manual | cash
    provider_order_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    provider_payment_id: Mapped[str | None] = mapped_column(String(64))
    provider_reference: Mapped[str | None] = mapped_column(String(80))  # UTR / bank reference
    payment_session_id: Mapped[str | None] = mapped_column(String(255))

    customer_id: Mapped[int | None] = fk("users.id", "SET NULL")
    employee_id: Mapped[int | None] = fk("employees.id", "SET NULL")
    subscription_id: Mapped[int | None] = fk("subscriptions.id", "SET NULL")
    salary_statement_id: Mapped[int | None] = fk("salary_statements.id", "SET NULL")
    created_by_id: Mapped[int | None] = fk("users.id", "SET NULL")

    proof_key: Mapped[str | None] = mapped_column(String(255))  # proof of transaction (encrypted)
    proof_check: Mapped[dict | None] = mapped_column(JSONB)  # multi-OCR + AI comparison result
    proof_verified: Mapped[bool | None] = mapped_column(Boolean)
    meta: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    gym: Mapped[Gym | None] = relationship(lazy="raise")
    customer: Mapped[User | None] = relationship(lazy="raise", foreign_keys=[customer_id])
    employee: Mapped[Employee | None] = relationship(lazy="raise")
    subscription: Mapped[Subscription | None] = relationship(lazy="raise")


def proof_sha256() -> ColumnElement[str]:
    """``meta -> 'proof' ->> 'sha256'`` with its keys as SQL literals - exactly what ``ix_transactions_proof_sha256``
    indexes (``Transaction.meta["proof"]["sha256"].astext`` binds the keys as parameters, which a generic plan of a
    prepared statement cannot match to the index)."""
    proof = Transaction.meta.op("->", return_type=JSONB)(literal_column("'proof'"))
    return proof.op("->>", return_type=String)(literal_column("'sha256'"))


class OtpChallenge(IdMixin, CreatedMixin, Base):
    """Desk OTP for offline purchases: sent to the gym owner, told to the customer in person."""

    __tablename__ = "otp_challenges"

    purpose: Mapped[str] = mapped_column(String(30), default="offline_purchase", server_default="offline_purchase")
    subscription_id: Mapped[int] = fk("subscriptions.id", "CASCADE")
    branch_id: Mapped[int] = fk("branches.id", "CASCADE")
    customer_id: Mapped[int] = fk("users.id", "CASCADE")
    recipient_user_id: Mapped[int] = fk("users.id", "CASCADE")
    code_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, default=5, server_default="5")
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_ip: Mapped[str | None] = mapped_column(String(45))

    subscription: Mapped[Subscription] = relationship(lazy="raise")


class SalaryStatement(IdMixin, TimestampMixin, Base):
    """Salary for one period, computed by days worked with country-specific gratuity/tax estimates."""

    __tablename__ = "salary_statements"
    __table_args__ = (
        UniqueConstraint("employee_id", "period_start", "period_end", "is_final_settlement"),
        CheckConstraint("period_end >= period_start", name="period_order"),
        CheckConstraint("days_worked >= 0", name="days_non_negative"),
    )

    employee_id: Mapped[int] = fk("employees.id", "CASCADE")
    gym_id: Mapped[int] = fk("gyms.id", "CASCADE")
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    days_in_period: Mapped[int] = mapped_column(Integer)
    days_worked: Mapped[int] = mapped_column(Integer)
    unpaid_leave_days: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    monthly_salary: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    gross_pay: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    gratuity: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"), server_default="0")
    tax_withholding: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"), server_default="0")
    employee_deductions: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    employer_contributions: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    net_pay: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    currency: Mapped[str] = mapped_column(String(3))
    country_code: Mapped[str] = mapped_column(String(2))
    rules: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    ai_explanation: Mapped[str | None] = mapped_column(Text)
    ai_provider: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[StatementStatus] = enum_col(StatementStatus, "status", default=StatementStatus.DUE, index=True)
    due_date: Mapped[date] = mapped_column(Date, index=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_reminder_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    transaction_id: Mapped[int | None] = fk("transactions.id", "SET NULL", use_alter=True)
    is_final_settlement: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))

    employee: Mapped[Employee] = relationship(lazy="raise")
