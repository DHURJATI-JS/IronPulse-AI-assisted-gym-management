"""Platform governance records (app/services/governance.py: bans of owners / staff, gym deletion with failsafes).

* ``governance_waivers`` - a super-admin's written decision that an employee's outstanding pay at a gym under
  governance (suspended, or scheduled for deletion) was settled outside IronPulse or is forgone. It is the only way to
  clear an employee's pay blocker of the final gym deletion without a recorded payment, so it keeps the amount that was
  outstanding when it was written and who wrote it. Immutable (the ORM refuses updates) and kept forever like the
  ledger: party foreign keys use SET NULL and the person's name is a snapshot.

Everything else a governance action does is recorded where it always is: the ledger (``transactions``: refunds,
payouts), salary statements (final settlements), ``account_bans`` (source ``platform``) and the audit log
(``governance.*`` actions with the impact preview the super-admin confirmed).
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import Index, Numeric, String, event, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import CreatedMixin, IdMixin, fk


class GovernanceWaiver(IdMixin, CreatedMixin, Base):
    """An employee's outstanding pay at a gym under governance, waived by a super-admin with a written note."""

    __tablename__ = "governance_waivers"
    __table_args__ = (
        # one waiver per employment record (a double submit, two tabs: the second finds it) - a backstop for the
        # employment row lock the service holds
        Index("uq_governance_waivers_employee", "employee_id", unique=True,
              postgresql_where=text("employee_id IS NOT NULL")),
        Index("ix_governance_waivers_gym_created", "gym_id", "created_at"),
    )

    gym_id: Mapped[int | None] = fk("gyms.id", "SET NULL")
    employee_id: Mapped[int | None] = fk("employees.id", "SET NULL")
    employee_name: Mapped[str] = mapped_column(String(120))
    actor_id: Mapped[int | None] = fk("users.id", "SET NULL")
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))  # outstanding (statements + accrued) when it was written
    currency: Mapped[str] = mapped_column(String(3))
    note: Mapped[str] = mapped_column(String(500))


@event.listens_for(GovernanceWaiver, "before_update")
def _waivers_are_immutable(mapper, connection, target) -> None:  # noqa: ANN001
    raise RuntimeError("governance_waivers rows are immutable")
