"""Account lifecycle: soft deletion with a 30-day retention window, identifier reservation, purge.

* ``soft_delete_user`` marks the account deleted (chats/records keep showing "Deleted user"), revokes
  sessions, cancels subscriptions, detaches staff employments, and e-mails the person (+ guardian). It also
  registers keyed hashes of the account's e-mail, phone and ID numbers in ``blocked_identities``
  (app/services/trust_safety.py): one person per ID and e-mail, forever - those identifiers can never open a new
  account, even after the purge.
* ``identifier_conflict`` returns a friendly reason for an e-mail / phone that an account uses, that a deleted
  account still holds, or that the blocked-identity registry holds; signups/edits must refuse them.
* ``purge_due_accounts`` (hourly) hard-deletes expired accounts with Core DELETE statements, in small
  batches (one transaction each, gym owners alone); the schema's ON DELETE CASCADE / SET NULL rules remove
  or anonymise everything else, then - after the COMMIT - stored files are wiped from disk. The registry rows have
  no foreign key to the account, so the purge never removes them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.utils import local_today, normalize_phone, utcnow
from app.db import long_statement, session_scope
from app.models import (
    Branch,
    BranchDocument,
    BranchPhoto,
    Employee,
    EmployeeDocument,
    Gym,
    IdentityDocument,
    JobEnquiry,
    ProductReview,
    SalaryStatement,
    Subscription,
    Transaction,
    User,
)
from app.models.enums import (
    EmploymentStatus,
    NotificationCategory,
    Role,
    Severity,
    StatementStatus,
    SubscriptionStatus,
)
from app.security.sessions import revoke_all_sessions
from app.services import storage
from app.services.audit import audit
from app.services.batching import Chunks
from app.services.jobs import periodic
from app.services.notify import Note, notify, notify_bulk

log = logging.getLogger("ironpulse.accounts")


class AccountDeletionBlocked(Exception):
    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


@dataclass(slots=True)
class IdentifierConflict:
    field: str  # "email" | "phone"
    message: str


async def identifier_conflict(
    db: AsyncSession, *, email: str | None = None, phone: str | None = None, country_code: str | None = None,
    exclude_user_id: int | None = None,
) -> IdentifierConflict | None:
    """Is this e-mail / phone already used - by an account, by a deleted one, or by any deleted or banned account
    ever (the blocked-identity registry: those identifiers can never open a new account)?"""
    from app.services.trust_safety import registration_blocked

    checks: list[tuple[str, object]] = []
    if email:
        checks.append(("email", User.email == email.strip().lower()))
    if phone:
        try:
            norm = normalize_phone(phone, country_code)
        except ValueError as exc:
            return IdentifierConflict("phone", str(exc))
        if norm:
            checks.append(("phone", User.phone_normalized == norm))
    for field, cond in checks:
        stmt = select(User.id, User.deleted_at, User.purge_after).where(cond)
        if exclude_user_id is not None:
            stmt = stmt.where(User.id != exclude_user_id)
        row = (await db.execute(stmt)).first()
        if row is None:
            continue
        if row.deleted_at is not None:
            return IdentifierConflict(
                field,
                f"This {field} belongs to an account that was deleted recently, so it can't be used for a new "
                "IronPulse account. If you think this is a mistake, please contact support.",
            )
        return IdentifierConflict(field, f"An account with this {field} already exists. Try signing in instead.")
    if email and (message := await registration_blocked(db, email=email, exclude_user_id=exclude_user_id)):
        return IdentifierConflict("email", message)
    if phone and (message := await registration_blocked(db, phone=phone, country_code=country_code,
                                                        exclude_user_id=exclude_user_id)):
        return IdentifierConflict("phone", message)
    return None


async def soft_delete_user(db: AsyncSession, user: User, *, reason: str, actor: User | None = None, request=None) -> None:
    """Soft-delete an account (caller commits). Raises AccountDeletionBlocked for owners with live gyms.

    The account row is locked first (held to COMMIT) and ``deleted_at`` re-read: two deletions at the same moment (a
    double submit, two tabs, the member and an admin) run one after the other and the second finds it done."""
    if user.deleted_at is not None:
        return
    current = await db.execute(select(User.deleted_at).where(User.id == user.id).with_for_update())
    if (row := current.first()) is None or row.deleted_at is not None:
        return
    if user.role == Role.SUPERADMIN:
        raise AccountDeletionBlocked(["The platform super-admin account cannot be deleted."])
    if user.role == Role.OWNER:
        reasons: list[str] = []
        live_subs = await db.scalar(
            select(func.count(Subscription.id)).join(Gym, Gym.id == Subscription.gym_id)
            .where(Gym.owner_id == user.id, Subscription.status == SubscriptionStatus.ACTIVE)
        )
        if live_subs:
            reasons.append(f"{live_subs} active member subscription(s) - close your gyms first.")
        unpaid = await db.scalar(
            select(func.count(SalaryStatement.id)).join(Gym, Gym.id == SalaryStatement.gym_id)
            .where(Gym.owner_id == user.id, SalaryStatement.status.not_in([StatementStatus.PAID, StatementStatus.VOID]))
        )
        if unpaid:
            reasons.append(f"{unpaid} unpaid salary statement(s) - pay your staff first.")
        active_staff = await db.scalar(
            select(func.count(Employee.id)).join(Gym, Gym.id == Employee.gym_id)
            .where(Gym.owner_id == user.id, Employee.status == EmploymentStatus.ACTIVE)
        )
        if active_staff:
            reasons.append(f"{active_staff} active employee(s) - settle and remove them first.")
        governed = await db.scalar(  # app/services/governance.py: suspended or archived by the platform
            select(func.count(Gym.id)).where(Gym.owner_id == user.id, Gym.is_active.is_(False),
                                             Gym.suspended_at.is_not(None))
        )
        if governed:
            reasons.append(f"{governed} gym(s) suspended or deleted by the platform keep financial records that must "
                           "never be deleted with the account.")
        if reasons:
            raise AccountDeletionBlocked(reasons)

    now = utcnow()
    retention = timedelta(days=get_settings().account_retention_days)
    user.deleted_at = now
    user.purge_after = now + retention
    user.deletion_reason = reason[:255]
    user.is_active = False
    await revoke_all_sessions(db, user.id)
    await _stop_background_work(db, user.id)

    # cancel subscriptions of a deleted customer
    await db.execute(
        update(Subscription)
        .where(Subscription.customer_id == user.id,
               Subscription.status.in_([SubscriptionStatus.ACTIVE, SubscriptionStatus.PENDING_OTP,
                                        SubscriptionStatus.PENDING_PAYMENT]))
        .values(status=SubscriptionStatus.CANCELLED, cancelled_at=now)
    )
    _forget_ai_entitlement_after_commit(db, user.id)
    # staff employments: keep the record (salary dues stay visible to the owner) but mark it deleted
    # (each employment with its gym's owner in ONE query; the owners are told in one bulk send - never per row)
    employments = (await db.execute(
        select(Employee, Gym.owner_id).join(Gym, Gym.id == Employee.gym_id)
        .where(Employee.user_id == user.id, Employee.deleted_at.is_(None))
    )).all()
    notes: list[Note] = []
    for emp, owner_id in employments:
        emp.deleted_at = now
        emp.purge_after = now + retention
        if emp.status != EmploymentStatus.TERMINATED:
            emp.status = EmploymentStatus.TERMINATED
            emp.termination_date = local_today(user.timezone)
            emp.termination_reason = "Staff account deleted"
        if owner_id:
            notes.append(Note(
                owner_id, "A staff member deleted their account",
                f"{emp.full_name} deleted their IronPulse account. Their records are kept for "
                f"{retention.days} days. Any unpaid salary is still listed under Payroll.",
                link=f"/owner/staff/{emp.id}", category=NotificationCategory.STAFF, severity=Severity.WARNING,
            ))
    if notes:
        await notify_bulk(db, notes)

    # one person per ID and e-mail, forever: keyed hashes only (app/models/trust.py), kept after the purge
    from app.services.trust_safety import register_identity

    by_holder = actor is None or actor.id == user.id
    await register_identity(db, user, reason="self_deleted" if by_holder else "admin_deleted", actor=actor or user,
                            note=reason[:255])
    await notify(
        db, user, "Your account has been deleted",
        f"Your IronPulse account was deleted ({reason}). Your data is kept for {retention.days} days and then "
        f"permanently erased on {user.purge_after:%d %b %Y}. This e-mail address, phone number and ID can never be "
        "used to create a new IronPulse account.",
        category=NotificationCategory.ACCOUNT, severity=Severity.WARNING, allow_deleted=True,
    )
    await audit(db, actor or user, "account.soft_delete", "user", user.id, detail={"reason": reason}, request=request)


def _forget_ai_entitlement_after_commit(db: AsyncSession, user_id: int) -> None:
    """The cancelled subscriptions end the AI tier the guard cached ("plan includes AI", 5 min): once the deletion
    COMMITs, forget it, so an account restored within that window is judged with its real tier (the same hook as
    the cancel/expiry paths in app/services/checkout.py). Dropped on rollback."""
    from functools import partial

    from app.ai.guard import guard
    from app.services.realtime import call_after_commit

    call_after_commit(db, partial(guard.forget_entitlement, user_id))


async def stop_background_work(db: AsyncSession, user_id: int, *, reason: str = "account_deleted") -> None:
    """A deleted (or banned) account gets no more compute: every unfinished job of the person is cancelled whatever
    its exit policy (in this transaction), and once it COMMITs their queued/running AI requests are cancelled too."""
    from app.services.jobs import cancel_user_jobs
    from app.services.realtime import call_after_commit

    await cancel_user_jobs(user_id, reason=reason, all_policies=True, db=db)

    def cancel_ai() -> None:
        try:
            from app.ai.policy import EXIT_CANCEL, EXIT_KEEP, EXIT_PAUSE
            from app.services.realtime import hub

            # 'keep' named explicitly: even obligations stop for an account that no longer exists - in every process
            hub.cancel_ai(user_id=user_id, reason=reason, policies={EXIT_KEEP, EXIT_PAUSE, EXIT_CANCEL})
        except Exception:  # noqa: BLE001 - the deletion itself already committed
            log.exception("could not cancel the AI requests of user %s (%s)", user_id, reason)

    call_after_commit(db, cancel_ai)


_stop_background_work = stop_background_work  # the deletion path's original name


async def _user_file_keys(db: AsyncSession, user_ids: list[int]) -> list[str]:
    keys: list[str | None] = []
    keys += (await db.execute(select(User.photo_key).where(User.id.in_(user_ids)))).scalars().all()
    for front, back in (await db.execute(
        select(IdentityDocument.front_key, IdentityDocument.back_key).where(IdentityDocument.user_id.in_(user_ids))
    )).all():
        keys += [front, back]
    # gyms owned by purged owners cascade away -> their branch photos/documents too
    gym_ids = select(Gym.id).where(Gym.owner_id.in_(user_ids))
    branch_ids = select(Branch.id).where(Branch.gym_id.in_(gym_ids))
    keys += (await db.execute(select(BranchPhoto.image_key).where(BranchPhoto.branch_id.in_(branch_ids)))).scalars().all()
    keys += (await db.execute(select(BranchDocument.file_key).where(BranchDocument.branch_id.in_(branch_ids)))).scalars().all()
    keys += (await db.execute(select(Gym.logo_key).where(Gym.id.in_(gym_ids)))).scalars().all()
    from app.models import JobPosting

    keys += (await db.execute(
        select(JobEnquiry.resume_key).join(JobPosting, JobPosting.id == JobEnquiry.job_id).where(JobPosting.gym_id.in_(gym_ids))
    )).scalars().all()
    emp_ids = select(Employee.id).where(Employee.gym_id.in_(gym_ids))
    keys += await _employee_file_keys(db, emp_ids)
    return [k for k in keys if k]


async def _employee_file_keys(db: AsyncSession, emp_ids) -> list[str]:  # noqa: ANN001
    keys: list[str | None] = []
    keys += (await db.execute(select(Employee.photo_key).where(Employee.id.in_(emp_ids)))).scalars().all()
    keys += (await db.execute(select(EmployeeDocument.file_key).where(EmployeeDocument.employee_id.in_(emp_ids)))).scalars().all()
    for front, back in (await db.execute(
        select(IdentityDocument.front_key, IdentityDocument.back_key).where(IdentityDocument.employee_id.in_(emp_ids))
    )).all():
        keys += [front, back]
    return [k for k in keys if k]


PURGE_BATCH = 25  # due accounts per chunk; a gym owner (whole gyms cascade with them) is always purged alone
PURGE_BUDGET_S = 300.0  # per hourly run and kind (users, employee records); the next run continues


async def purge_due_accounts(db: AsyncSession) -> dict[str, int]:
    """Hard-delete users and employee records whose 30-day retention has expired - in batches.

    Due ids are walked in keyset chunks of PURGE_BATCH (never one unbounded id list). Every purge is its own
    transaction under ``long_statement`` (a cascade may legitimately outlast the request statement limit): ordinary
    accounts of a chunk together, each gym owner on their own. Stored files are deleted only AFTER that transaction
    commits, so a rolled-back purge never leaves rows whose files are gone. A run that spends PURGE_BUDGET_S leaves the
    rest to the next hour; a purged row is gone, so nothing is purged twice. ``db`` only holds the periodic's lock."""
    now = utcnow()
    report = {"users": 0, "employees": 0, "files": 0}
    run = Chunks(holder=db, budget_s=PURGE_BUDGET_S)
    last_id = 0
    while await run.next():
        async with session_scope() as s:
            rows = (await s.execute(
                select(User.id, exists(select(Gym.id).where(Gym.owner_id == User.id)).label("owner"))
                .where(User.purge_after.is_not(None), User.purge_after <= now, User.id > last_id,
                       # never cascade away a gym the platform archived or suspended: its salary statements and
                       # memberships are financial records (app/services/governance.py) - the account row stays
                       ~exists(select(Gym.id).where(Gym.owner_id == User.id, Gym.suspended_at.is_not(None))))
                .order_by(User.id).limit(PURGE_BATCH)
            )).all()
        if rows:
            last_id = rows[-1].id
        plain = [r.id for r in rows if not r.owner]
        for ids in ([plain] if plain else []) + [[r.id] for r in rows if r.owner]:
            purged, files = await _purge_users(ids, now)
            report["users"] += purged
            report["files"] += files
        run.done(len(rows), PURGE_BATCH)

    # employee records: purge after retention unless salary is still owed
    owed = exists(select(SalaryStatement.id).where(
        SalaryStatement.employee_id == Employee.id,
        SalaryStatement.status.not_in([StatementStatus.PAID, StatementStatus.VOID]),
    ))
    run = Chunks(holder=db, budget_s=PURGE_BUDGET_S)
    last_id = 0
    while await run.next():
        files: list[str] = []
        async with session_scope() as s:
            await long_statement(s)
            emp_ids = list((await s.execute(
                select(Employee.id)
                .where(Employee.purge_after.is_not(None), Employee.purge_after <= now, Employee.id > last_id, ~owed)
                .order_by(Employee.id).limit(PURGE_BATCH).with_for_update(skip_locked=True)
            )).scalars().all())
            if emp_ids:
                files = await _employee_file_keys(s, emp_ids)
                await s.execute(update(Transaction).where(Transaction.employee_id.in_(emp_ids)).values(party_label="Former employee"))
                await s.execute(delete(Employee).where(Employee.id.in_(emp_ids)))
        if emp_ids:
            last_id = emp_ids[-1]
        report["employees"] += len(emp_ids)
        report["files"] += await _delete_files(files)
        run.done(len(emp_ids), PURGE_BATCH)

    if report["users"] or report["employees"]:
        log.info("purged %s users and %s employee records", report["users"], report["employees"])
    return report


async def _purge_users(user_ids: list[int], now: datetime) -> tuple[int, int]:
    """Purge these accounts in ONE transaction (still due - re-checked under a row lock), then delete their files.
    Returns (accounts purged, files deleted)."""
    files: list[str] = []
    async with session_scope() as s:
        await long_statement(s)
        ids = list((await s.execute(
            select(User.id).where(User.id.in_(user_ids), User.purge_after.is_not(None), User.purge_after <= now)
            .order_by(User.id).with_for_update(skip_locked=True)
        )).scalars().all())
        if ids:
            from app.services.shop import refresh_product_ratings

            files = await _user_file_keys(s, ids)
            # their product reviews go with them (ON DELETE CASCADE - past the ORM flush hook that keeps
            # products.rating_avg / review_count in line): note the products first, rewrite their summary after
            reviewed = list((await s.execute(
                select(ProductReview.product_id).where(ProductReview.user_id.in_(ids)).distinct()
            )).scalars())
            # anonymise ledger rows (they survive via ON DELETE SET NULL)
            await s.execute(update(Transaction).where(Transaction.customer_id.in_(ids)).values(party_label="Deleted customer"))
            await s.execute(delete(User).where(User.id.in_(ids)))
            await refresh_product_ratings(s, reviewed)  # same transaction: cards / "Top rated" never count them
    return len(ids), await _delete_files(files)


async def _delete_files(keys: list[str]) -> int:
    """Delete stored files of rows that are already gone (after their COMMIT). Returns how many keys were handled."""
    unique = set(keys)
    for key in unique:
        try:
            await storage.delete_key(key)
        except Exception:  # noqa: BLE001 - the rows are purged; an orphaned file is logged, never fatal
            log.exception("could not delete stored file %s of a purged account", key)
    return len(unique)


@periodic("purge_deleted_accounts", every_seconds=3600, initial_delay=60)
async def _purge_job(db: AsyncSession) -> None:
    await purge_due_accounts(db)
