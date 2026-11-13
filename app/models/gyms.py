"""Gym brands, physical branches, opening hours, features, photos, documents and AI verification runs."""

from __future__ import annotations

from datetime import datetime, time
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    Time,
    UniqueConstraint,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.base import CreatedMixin, IdMixin, TimestampMixin, enum_col, fk
from app.models.enums import BranchDocType, PhotoKind, VerificationStatus

if TYPE_CHECKING:
    from app.models.billing import Plan
    from app.models.staff import Employee, JobPosting, TrainerShift
    from app.models.users import User


class Gym(IdMixin, TimestampMixin, Base):
    """A gym brand owned by one owner. Online mode and physical branches are toggled per brand."""

    __tablename__ = "gyms"
    __table_args__ = (
        # the shop's store typeahead (services/shop.search_stores): name ILIKE '%q%' through pg_trgm
        Index("ix_gyms_name_trgm", "name", postgresql_using="gin", postgresql_ops={"name": "gin_trgm_ops"}),
    )

    owner_id: Mapped[int] = fk("users.id", "CASCADE")
    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(140), unique=True, index=True)
    tagline: Mapped[str | None] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    logo_key: Mapped[str | None] = mapped_column(String(255))
    country_code: Mapped[str] = mapped_column(String(2))
    currency: Mapped[str] = mapped_column(String(3), default="INR", server_default="INR")
    contact_email: Mapped[str | None] = mapped_column(String(254))
    contact_phone: Mapped[str | None] = mapped_column(String(32))
    website: Mapped[str | None] = mapped_column(String(255))

    online_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    online_open: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())  # admin can close
    offline_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    # false = the gym does not operate: hidden from public pages, search, checkout, the shop and the jobs board. Only
    # the platform's governance (app/services/governance.py) switches it off: a suspension (owner ban, deletion
    # scheduled) or the final deletion (archived - every row is kept: financial records are never deleted)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    suspension_kind: Mapped[str | None] = mapped_column(String(20))  # owner_ban | deletion
    suspension_reason: Mapped[str | None] = mapped_column(String(300))
    deletion_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delete_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # end of the cooling-off period
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # the final deletion

    owner: Mapped[User] = relationship(back_populates="owned_gyms", lazy="raise")
    branches: Mapped[list[Branch]] = relationship(back_populates="gym", lazy="raise", passive_deletes=True)
    plans: Mapped[list[Plan]] = relationship(back_populates="gym", lazy="raise", passive_deletes=True)
    employees: Mapped[list[Employee]] = relationship(back_populates="gym", lazy="raise", passive_deletes=True)
    job_postings: Mapped[list[JobPosting]] = relationship(back_populates="gym", lazy="raise", passive_deletes=True)


class Branch(IdMixin, TimestampMixin, Base):
    """A physical location. Shown publicly only when verified, open and with >= 3 photos."""

    __tablename__ = "branches"
    # bounding-box pre-filter of the "near me" distance queries (latitude BETWEEN .. AND longitude BETWEEN ..)
    __table_args__ = (Index("ix_branches_lat_lon", "latitude", "longitude"),)

    gym_id: Mapped[int] = fk("gyms.id", "CASCADE")
    name: Mapped[str] = mapped_column(String(120))
    address_line1: Mapped[str] = mapped_column(String(200))
    address_line2: Mapped[str | None] = mapped_column(String(200))
    city: Mapped[str] = mapped_column(String(120), index=True)
    state: Mapped[str | None] = mapped_column(String(120))
    postal_code: Mapped[str | None] = mapped_column(String(20))
    country_code: Mapped[str] = mapped_column(String(2))
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", server_default="UTC")
    phone: Mapped[str | None] = mapped_column(String(32))
    email: Mapped[str | None] = mapped_column(String(254))

    # owner-pinned coordinates (Geoapify map) and what the geocoder says about the typed address
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    geocoded_latitude: Mapped[float | None] = mapped_column(Float)
    geocoded_longitude: Mapped[float | None] = mapped_column(Float)
    formatted_address: Mapped[str | None] = mapped_column(String(300))
    geo_place_id: Mapped[str | None] = mapped_column(String(300))
    geo_confidence: Mapped[float | None] = mapped_column(Float)

    registration_number: Mapped[str | None] = mapped_column(String(80))
    verification_status: Mapped[VerificationStatus] = enum_col(
        VerificationStatus, "verification_status", default=VerificationStatus.DRAFT, index=True
    )
    verification_summary: Mapped[str | None] = mapped_column(Text)
    verification_checks: Mapped[list | None] = mapped_column(JSONB)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # closing a branch never discards its verification; reopening needs no re-verification
    is_open: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_reason: Mapped[str | None] = mapped_column(String(255))

    gym: Mapped[Gym] = relationship(back_populates="branches", lazy="raise")
    hours: Mapped[list[BranchHours]] = relationship(
        back_populates="branch", lazy="raise", passive_deletes=True, order_by="BranchHours.weekday"
    )
    features: Mapped[list[BranchFeature]] = relationship(
        back_populates="branch", lazy="raise", passive_deletes=True, order_by="BranchFeature.name"
    )
    photos: Mapped[list[BranchPhoto]] = relationship(
        back_populates="branch", lazy="raise", passive_deletes=True, order_by="BranchPhoto.sort_order"
    )
    documents: Mapped[list[BranchDocument]] = relationship(back_populates="branch", lazy="raise", passive_deletes=True)
    verification_runs: Mapped[list[VerificationRun]] = relationship(
        back_populates="branch", lazy="raise", passive_deletes=True, foreign_keys="VerificationRun.branch_id"
    )
    shifts: Mapped[list[TrainerShift]] = relationship(back_populates="branch", lazy="raise", passive_deletes=True)

    @property
    def is_verified(self) -> bool:
        return self.verification_status == VerificationStatus.VERIFIED


class BranchHours(Base):
    """Opening hours per weekday (0 = Monday). The DB enforces the 14-hour daily maximum."""

    __tablename__ = "branch_hours"
    __table_args__ = (
        CheckConstraint("weekday BETWEEN 0 AND 6", name="weekday_range"),
        CheckConstraint("closes_at > opens_at", name="closes_after_opens"),
        CheckConstraint("(closes_at - opens_at) <= interval '14 hours'", name="max_14_hours"),
    )

    branch_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("branches.id", ondelete="CASCADE"), primary_key=True)
    weekday: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    opens_at: Mapped[time] = mapped_column(Time)
    closes_at: Mapped[time] = mapped_column(Time)

    branch: Mapped[Branch] = relationship(back_populates="hours", lazy="raise")


class BranchFeature(IdMixin, Base):
    __tablename__ = "branch_features"
    __table_args__ = (UniqueConstraint("branch_id", "name"),)

    branch_id: Mapped[int] = fk("branches.id", "CASCADE")
    name: Mapped[str] = mapped_column(String(60))

    branch: Mapped[Branch] = relationship(back_populates="features", lazy="raise")


class BranchPhoto(IdMixin, CreatedMixin, Base):
    __tablename__ = "branch_photos"

    branch_id: Mapped[int] = fk("branches.id", "CASCADE")
    image_key: Mapped[str] = mapped_column(String(255))
    caption: Mapped[str | None] = mapped_column(String(160))
    kind: Mapped[PhotoKind] = enum_col(PhotoKind, "kind", default=PhotoKind.OVERALL)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, server_default="0")  # the gym's storage quota

    branch: Mapped[Branch] = relationship(back_populates="photos", lazy="raise")


class BranchDocument(IdMixin, CreatedMixin, Base):
    """Proof of registration, security / fire certificates, insurance ... (encrypted at rest)."""

    __tablename__ = "branch_documents"

    branch_id: Mapped[int] = fk("branches.id", "CASCADE")
    doc_type: Mapped[BranchDocType] = enum_col(BranchDocType, "doc_type")
    file_key: Mapped[str] = mapped_column(String(255))
    original_filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(100))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    size_bytes: Mapped[int] = mapped_column(Integer)
    document_number: Mapped[str | None] = mapped_column(String(80))
    issued_on: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_on: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ocr_text: Mapped[str | None] = mapped_column(Text)
    ocr_engines: Mapped[list | None] = mapped_column(JSONB)
    assessment: Mapped[dict | None] = mapped_column(JSONB)
    accepted: Mapped[bool | None] = mapped_column(Boolean)

    branch: Mapped[Branch] = relationship(back_populates="documents", lazy="raise")


class VerificationRun(IdMixin, CreatedMixin, Base):
    """History of every automated (AI + rules) or manual verification attempt of a branch or employee."""

    __tablename__ = "verification_runs"
    __table_args__ = (
        CheckConstraint("(branch_id IS NOT NULL) <> (employee_id IS NOT NULL)", name="one_subject"),
    )

    branch_id: Mapped[int | None] = fk("branches.id", "CASCADE", nullable=True)
    employee_id: Mapped[int | None] = fk("employees.id", "CASCADE", nullable=True)
    outcome: Mapped[VerificationStatus] = enum_col(VerificationStatus, "outcome")
    score: Mapped[float | None] = mapped_column(Float)
    checks: Mapped[list | None] = mapped_column(JSONB)
    summary: Mapped[str | None] = mapped_column(Text)
    ai_provider: Mapped[str | None] = mapped_column(String(40))
    manual: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    reviewer_id: Mapped[int | None] = fk("users.id", "SET NULL")

    branch: Mapped[Branch | None] = relationship(back_populates="verification_runs", lazy="raise", foreign_keys=[branch_id])
    employee: Mapped[Employee | None] = relationship(lazy="raise", foreign_keys=[employee_id])
