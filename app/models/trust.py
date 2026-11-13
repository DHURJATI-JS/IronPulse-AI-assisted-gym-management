"""Trust & safety: staff conduct violations, platform account bans (+ the network addresses each ban acted on) and the
blocked-identity registry (app/services/trust_safety.py has the rules; /superadmin/trust shows all of it).

* ``staff_violations`` - one IMMUTABLE row per employment a gym ended for a VIOLATION or SEVERE reason (the ORM refuses
  updates; only the database's ON DELETE SET NULL may blank a link). It keeps snapshots (gym name, person name) and the
  person's keyed identity hashes, so it outlives the employee record, the gym and the account it describes: the
  auto-ban threshold counts a person's violations across every gym, and an owner's gyms refuse to re-add them.
* ``account_bans`` - a platform-wide ban of an account (auto-ban after repeated violations, or a severe one). At most
  one active ban per account (partial unique index); the super-admin lifts it with a note.
* ``trust_ip_actions`` - what a severe ban did with each recent network address of the account: banned (through the
  ordinary ``ip_bans`` table) or skipped and flagged (shared by many accounts, the acting admin's own address, a
  platform super-admin's address, not a public address, whitelisted).
* ``blocked_identities`` - "one person per ID and e-mail, forever": keyed hashes (HMAC-SHA256 with ID_HASH_PEPPER, the
  scheme of ``identity_documents.id_number_hash``) of the e-mail, phone and ID numbers of every deleted or banned
  account. Only hashes are stored - never the plain values - so the registry can answer "was this identifier used by
  a deleted or banned account?" without keeping the personal data the deletion promised to erase; without the pepper
  a leaked table cannot be reversed by guessing likely e-mail addresses or phone numbers. Rows are kept indefinitely
  and survive the account purge (no foreign key to ``users``).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import CreatedMixin, IdMixin, fk

VIOLATION_CATEGORIES = ("violation", "severe")
# violations / severe: automatic (app/services/trust_safety.py); platform: a super-admin's governance decision
# (app/services/governance.py). An existing database's CHECK is widened by app.db.sync_schema at start-up.
BAN_SOURCES = ("violations", "severe", "platform")
IP_ACTIONS = ("banned", "shared", "actor", "platform_admin", "not_public", "whitelisted")
IDENTITY_KINDS = ("email", "phone", "id_number")
IDENTITY_REASONS = ("self_deleted", "admin_deleted", "banned", "severe_violation")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


class StaffViolation(IdMixin, CreatedMixin, Base):
    """An employment ended for a conduct violation (immutable)."""

    __tablename__ = "staff_violations"
    __table_args__ = (
        CheckConstraint(_in("category", VIOLATION_CATEGORIES), name="category_kind"),
        # the person's violations: by account, or by any of their identity hashes (GIN: ``subject_hashes && :hashes``)
        Index("ix_staff_violations_subject_user", "subject_user_id", "created_at"),
        Index("ix_staff_violations_subject_hashes", "subject_hashes", postgresql_using="gin"),
        Index("ix_staff_violations_created", "created_at", "id"),  # the super-admin list, newest first
    )

    employee_id: Mapped[int | None] = fk("employees.id", "SET NULL")
    gym_id: Mapped[int | None] = fk("gyms.id", "SET NULL")
    owner_id: Mapped[int | None] = fk("users.id", "SET NULL")  # the gym's owner when it happened (rehire is per owner)
    actor_id: Mapped[int | None] = fk("users.id", "SET NULL")
    # no foreign key: the record outlives the account (the threshold and the registry still need the person)
    subject_user_id: Mapped[int | None] = mapped_column(BigInteger)
    subject_hashes: Mapped[list[str]] = mapped_column(ARRAY(String(64)), default=list, server_default=text("'{}'"))
    gym_name: Mapped[str] = mapped_column(String(160))
    person_name: Mapped[str] = mapped_column(String(120))
    reason: Mapped[str] = mapped_column(String(40))
    category: Mapped[str] = mapped_column(String(10))
    note: Mapped[str] = mapped_column(String(500))


@event.listens_for(StaffViolation, "before_update")
def _violations_are_immutable(mapper, connection, target) -> None:  # noqa: ANN001
    raise RuntimeError("staff_violations rows are immutable")


class AccountBan(IdMixin, CreatedMixin, Base):
    """A platform-wide ban of an account: sign-in refused, sessions revoked, AI work stopped."""

    __tablename__ = "account_bans"
    __table_args__ = (
        CheckConstraint(_in("source", BAN_SOURCES), name="source_kind"),
        # at most one ACTIVE ban per account - a backstop for the per-person lock of app/services/trust_safety.py
        Index("uq_account_bans_one_active", "user_id", unique=True, postgresql_where=text("lifted_at IS NULL")),
        Index("ix_account_bans_user_created", "user_id", "created_at"),
        Index("ix_account_bans_created", "created_at", "id"),
    )

    user_id: Mapped[int] = mapped_column(BigInteger)  # no foreign key: the history outlives the account
    person_name: Mapped[str] = mapped_column(String(120))
    source: Mapped[str] = mapped_column(String(20))  # violations (threshold reached) | severe
    reason: Mapped[str] = mapped_column(String(300))
    violation_id: Mapped[int | None] = fk("staff_violations.id", "SET NULL")
    created_by_id: Mapped[int | None] = fk("users.id", "SET NULL")  # the admin whose removal triggered it
    lifted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lifted_by_id: Mapped[int | None] = fk("users.id", "SET NULL")
    lift_note: Mapped[str | None] = mapped_column(String(500))


class TrustIpAction(IdMixin, CreatedMixin, Base):
    """One recent network address of a banned account: banned through ``ip_bans``, or skipped and flagged."""

    __tablename__ = "trust_ip_actions"
    __table_args__ = (
        CheckConstraint(_in("action", IP_ACTIONS), name="action_kind"),
        Index("ix_trust_ip_actions_created", "created_at", "id"),
    )

    account_ban_id: Mapped[int] = fk("account_bans.id", "CASCADE")
    user_id: Mapped[int] = mapped_column(BigInteger)
    ip: Mapped[str] = mapped_column(String(45))
    action: Mapped[str] = mapped_column(String(20))
    other_accounts: Mapped[int | None] = mapped_column(Integer)  # other active accounts seen on it (shared check)
    ip_ban_id: Mapped[int | None] = fk("ip_bans.id", "SET NULL")


class BlockedIdentity(IdMixin, CreatedMixin, Base):
    """A keyed hash of an identifier (e-mail / phone / ID number) of a deleted or banned account - kept forever."""

    __tablename__ = "blocked_identities"
    __table_args__ = (
        CheckConstraint(_in("kind", IDENTITY_KINDS), name="kind_kind"),
        CheckConstraint(_in("reason", IDENTITY_REASONS), name="reason_kind"),
        # one row per (identifier, reason): a re-registration is an INSERT ... ON CONFLICT DO NOTHING; the lookup
        # (kind, value_hash) is this index's prefix
        UniqueConstraint("kind", "value_hash", "reason"),
        Index("ix_blocked_identities_created", "created_at", "id"),
        Index("ix_blocked_identities_user", "user_id", postgresql_where=text("user_id IS NOT NULL")),
    )

    kind: Mapped[str] = mapped_column(String(10))
    value_hash: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(String(20))
    user_id: Mapped[int | None] = mapped_column(BigInteger)  # the account it came from (no foreign key: survives purge)
    actor_id: Mapped[int | None] = fk("users.id", "SET NULL")
    note: Mapped[str | None] = mapped_column(String(255))
