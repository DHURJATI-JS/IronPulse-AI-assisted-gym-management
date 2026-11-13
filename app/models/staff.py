"""Employees (trainers and any other role, any country), their shifts, documents and hiring."""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    Time,
    UniqueConstraint,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import CITEXT, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.base import CreatedMixin, IdMixin, TimestampMixin, enum_col, fk
from app.models.enums import (
    EmploymentStatus,
    EmploymentType,
    EmployeeRole,
    EnquiryStatus,
    IdStatus,
    ShiftMode,
    VerificationStatus,
    WorkMode,
)

if TYPE_CHECKING:
    from app.models.gyms import Branch, Gym
    from app.models.users import User


class Employee(IdMixin, TimestampMixin, Base):
    """Staff member of a gym brand.

    A staff addition is approved (``ACTIVE``) only after the person has their own IronPulse account
    linked (``user_id``), uploaded a photo and an ID card, and passed verification.
    """

    __tablename__ = "employees"
    __table_args__ = (
        CheckConstraint("monthly_salary >= 0", name="salary_non_negative"),
        CheckConstraint("status <> 'active' OR user_id IS NOT NULL", name="active_requires_account"),
        CheckConstraint("(deleted_at IS NULL) = (purge_after IS NULL)", name="deletion_window"),
        CheckConstraint("removal_category IS NULL OR removal_category IN ('benign', 'violation', 'severe')",
                        name="removal_category_kind"),
        # the super-admin's staff review queue (ORDER BY updated_at, id) and its nav badge count: only the few rows
        # waiting for a decision are in this index (verification_status = 'needs_review', routers/superadmin.py)
        Index("ix_employees_needs_review", "updated_at", "id", postgresql_where=text("verification_status = 'needs_review'")),
    )

    gym_id: Mapped[int] = fk("gyms.id", "CASCADE")
    user_id: Mapped[int | None] = fk("users.id", "SET NULL")  # linked staff account (mandatory to activate)
    invite_email: Mapped[str] = mapped_column(CITEXT())
    full_name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str | None] = mapped_column(String(32))

    role: Mapped[EmployeeRole] = enum_col(EmployeeRole, "role")
    role_title: Mapped[str | None] = mapped_column(String(80))
    is_trainer: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), index=True)
    work_mode: Mapped[WorkMode] = enum_col(WorkMode, "work_mode", default=WorkMode.OFFLINE)
    employment_type: Mapped[EmploymentType] = enum_col(EmploymentType, "employment_type", default=EmploymentType.FULL_TIME)

    # payroll (employee may live in any country -> country-specific payout rules)
    country_code: Mapped[str] = mapped_column(String(2))
    currency: Mapped[str] = mapped_column(String(3))
    monthly_salary: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    hire_date: Mapped[date] = mapped_column(Date)
    termination_date: Mapped[date | None] = mapped_column(Date)
    termination_reason: Mapped[str | None] = mapped_column(String(255))
    payout_method: Mapped[str] = mapped_column(String(30), default="bank_transfer", server_default="bank_transfer")
    payout_details_enc: Mapped[str | None] = mapped_column(Text)  # Fernet-encrypted JSON

    status: Mapped[EmploymentStatus] = enum_col(EmploymentStatus, "status", default=EmploymentStatus.INVITED, index=True)
    photo_key: Mapped[str] = mapped_column(String(255))  # compulsory
    bio: Mapped[str | None] = mapped_column(Text)
    specialties: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    certifications: Mapped[str | None] = mapped_column(Text)
    years_experience: Mapped[int | None] = mapped_column(SmallInteger)
    is_public: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())

    verification_status: Mapped[VerificationStatus] = enum_col(
        VerificationStatus, "verification_status", default=VerificationStatus.DRAFT
    )
    verification_summary: Mapped[str | None] = mapped_column(Text)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    id_status: Mapped[IdStatus] = enum_col(IdStatus, "id_status", default=IdStatus.MISSING)

    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    purge_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    # how the gym ended this employment (app/services/trust_safety.py REMOVAL_REASONS): the reason code, its category
    # (benign = may be re-added later; violation / severe = never again at this owner's gyms), the admin's note, who
    # and when. NULL = still employed, or ended by an older path (treated as benign).
    removal_reason: Mapped[str | None] = mapped_column(String(40))
    removal_category: Mapped[str | None] = mapped_column(String(10))
    removal_note: Mapped[str | None] = mapped_column(String(500))
    removed_by_id: Mapped[int | None] = fk("users.id", "SET NULL")
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # a position change ends this record and re-adds the person: the new record points back at the one it replaced
    previous_employment_id: Mapped[int | None] = fk("employees.id", "SET NULL")

    gym: Mapped[Gym] = relationship(back_populates="employees", lazy="raise")
    user: Mapped[User | None] = relationship(back_populates="employments", lazy="raise", foreign_keys=[user_id])
    shifts: Mapped[list[TrainerShift]] = relationship(
        back_populates="employee", lazy="raise", passive_deletes=True, order_by="[TrainerShift.weekday, TrainerShift.start_time]"
    )
    documents: Mapped[list[EmployeeDocument]] = relationship(back_populates="employee", lazy="raise", passive_deletes=True)

    @property
    def display_name(self) -> str:
        return "Deleted staff member" if self.deleted_at is not None else self.full_name

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


class EmployeeDocument(IdMixin, CreatedMixin, Base):
    """Certifications, résumé, contract ... (encrypted at rest)."""

    __tablename__ = "employee_documents"

    employee_id: Mapped[int] = fk("employees.id", "CASCADE")
    doc_type: Mapped[str] = mapped_column(String(40))  # certification | resume | contract | other
    file_key: Mapped[str] = mapped_column(String(255))
    original_filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(100))
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    ocr_text: Mapped[str | None] = mapped_column(Text)
    assessment: Mapped[dict | None] = mapped_column(JSONB)

    employee: Mapped[Employee] = relationship(back_populates="documents", lazy="raise")


class TrainerShift(IdMixin, CreatedMixin, Base):
    """Weekly recurring shift, one row per weekday (0 = Monday).

    Rules enforced in app.services (and partly by the DB):
    * offline shifts only at VERIFIED branches, inside opening hours;
    * shifts at *different* branches need >= 6 h between them;
    * a trainer cannot work at two branches of the same brand that are >= 50 km apart.
    """

    __tablename__ = "trainer_shifts"
    __table_args__ = (
        CheckConstraint("weekday BETWEEN 0 AND 6", name="weekday_range"),
        CheckConstraint("end_time > start_time", name="end_after_start"),
        CheckConstraint("(mode = 'offline') = (branch_id IS NOT NULL)", name="offline_needs_branch"),
    )

    employee_id: Mapped[int] = fk("employees.id", "CASCADE")
    branch_id: Mapped[int | None] = fk("branches.id", "CASCADE", nullable=True)
    mode: Mapped[ShiftMode] = enum_col(ShiftMode, "mode")
    weekday: Mapped[int] = mapped_column(SmallInteger)
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)

    employee: Mapped[Employee] = relationship(back_populates="shifts", lazy="raise")
    branch: Mapped[Branch | None] = relationship(back_populates="shifts", lazy="raise")


class TrainerClient(IdMixin, CreatedMixin, Base):
    """A customer assigned to a trainer (plans that include a trainer)."""

    __tablename__ = "trainer_clients"
    __table_args__ = (UniqueConstraint("employee_id", "customer_id", "subscription_id"),)

    employee_id: Mapped[int] = fk("employees.id", "CASCADE")
    customer_id: Mapped[int] = fk("users.id", "CASCADE")
    subscription_id: Mapped[int | None] = fk("subscriptions.id", "CASCADE", nullable=True)
    mode: Mapped[ShiftMode] = enum_col(ShiftMode, "mode")
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())

    employee: Mapped[Employee] = relationship(lazy="raise")
    customer: Mapped[User] = relationship(lazy="raise")


class JobPosting(IdMixin, TimestampMixin, Base):
    """Open positions -> "Enquire for jobs" appears in the navbar while any posting is open."""

    __tablename__ = "job_postings"
    __table_args__ = (
        CheckConstraint("salary_max IS NULL OR salary_min IS NULL OR salary_max >= salary_min", name="salary_range"),
        # the jobs board's "All jobs" order (newest first) over the OPEN postings only - the predicate is the board's
        # own (routers/public.py open_job_clauses) - read in index order, never sorted; gym_id rides along for the joins
        Index("ix_job_postings_open_recent", text("created_at DESC"), text("id DESC"), postgresql_where=text("is_open IS true"),
              postgresql_include=["gym_id"]),
    )

    gym_id: Mapped[int] = fk("gyms.id", "CASCADE")
    branch_id: Mapped[int | None] = fk("branches.id", "SET NULL")
    title: Mapped[str] = mapped_column(String(120))
    role: Mapped[EmployeeRole] = enum_col(EmployeeRole, "role")
    employment_type: Mapped[EmploymentType] = enum_col(EmploymentType, "employment_type")
    location: Mapped[str] = mapped_column(String(160))  # city or "Remote"
    salary_min: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    salary_max: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    currency: Mapped[str] = mapped_column(String(3))
    salary_period: Mapped[str] = mapped_column(String(10), default="month", server_default="month")
    requirements: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    is_open: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true(), index=True)

    gym: Mapped[Gym] = relationship(back_populates="job_postings", lazy="raise")
    branch: Mapped[Branch | None] = relationship(lazy="raise")
    enquiries: Mapped[list[JobEnquiry]] = relationship(back_populates="job", lazy="raise", passive_deletes=True)


class JobEnquiry(IdMixin, CreatedMixin, Base):
    __tablename__ = "job_enquiries"
    __table_args__ = (
        # a posting's enquiries newest first: the public enquiry form's cooldown / duplicate check reads only the last
        # 24 h of one posting (routers/public.py enquiry_rate_refusal), the owner's enquiry list one page - never every
        # enquiry the posting ever received. Also the foreign key's index (leading column), replacing the single-column
        # one (dropped by create_indexes_concurrently()): no extra index on a table anonymous visitors write to
        Index("ix_job_enquiries_job_recent", "job_id", text("created_at DESC"), text("id DESC")),
        {"info": {"superseded_indexes": {  # app.db.SUPERSEDED_INDEXES: old index -> the index that replaces it
            "ix_job_enquiries_job_id": "ix_job_enquiries_job_recent",
        }}},
    )

    # indexed by ix_job_enquiries_job_recent above
    job_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("job_postings.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(CITEXT())
    phone: Mapped[str | None] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text)
    # SHA-256 (hex) of the message as the owner reads it (routers/public.py enquiry_message_key): what the duplicate
    # check compares. NULL on enquiries stored before it existed (compared by their text instead)
    message_key: Mapped[str | None] = mapped_column(String(64))
    resume_key: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[EnquiryStatus] = enum_col(EnquiryStatus, "status", default=EnquiryStatus.NEW)
    ip: Mapped[str | None] = mapped_column(String(45))

    job: Mapped[JobPosting] = relationship(back_populates="enquiries", lazy="raise")
