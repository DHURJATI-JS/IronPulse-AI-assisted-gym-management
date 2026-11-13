"""Nightly data retention: old rows of the fast-growing operational tables are deleted in small batches.

``@periodic("data_retention", every_seconds=3600)`` wakes every hour in every process. Under ``run_periodic``'s
advisory lock it acts once per UTC day, at the first tick from RETENTION_HOUR_UTC on, and records each finished run as
a ``system.retention`` audit row (rows deleted per table) - that row is how later ticks, and the other processes, know
today's run is done. A run that uses up its time budget (RETENTION_MAX_SECONDS) records nothing, so the next hourly
tick carries on where it stopped.

What goes (each ``RETENTION_*_DAYS`` setting; 0 keeps that table for ever), and why no page misses it:

=================  =============================================  ==============================================
table              deleted                                        kept / readers checked
=================  =============================================  ==============================================
security_events    created more than 90 days ago                  super-admin windows reach back 30 days at most
ai_call_logs       created more than 30 days ago                  only read over the last 24 hours
email_outbox       sent / logged / failed, created > 30 days ago  queued and sending mail is never touched
notifications      read, created more than 90 days ago            unread notifications are never deleted
jobs               done / failed / cancelled, finished > 14 days  queued, running and paused jobs are never
                   ago                                            touched; enqueue recycles a finished dedupe key
                                                                  anyway and sweeps/status pages treat a missing
                                                                  job like a finished one
ip_bans            lifted, or expired (never lifted), more than   an active ban is never touched (it is neither
                   90 days ago                                    lifted nor expired); the ban sync reads active
                                                                  bans and the repeat-offence count looks back 30
                                                                  days, so fewer than 30 days is never applied
auth_tokens        used / superseded more than 30 days ago;       an unused invite stays: the owner's member and
                   unused e-mail-verification / password-reset    staff pages show "invite sent ..." from it (one
                   links expired more than 30 days ago            per member / employee: re-sending supersedes it).
                                                                  A link deleted later reads "isn't valid"
user_sessions      expired more than 30 days ago                  sign-in and revocation only read live sessions
audit_logs         created more than 365 days ago                 order and staff timelines fall back to the
                                                                  record's own timestamps for older events
=================  =============================================  ==============================================

Each batch is one short transaction under ``long_statement``::

    DELETE FROM t WHERE id IN (SELECT id FROM t WHERE <time> < :cutoff AND <time> >= :floor ...
                               ORDER BY <time> LIMIT :batch FOR UPDATE SKIP LOCKED) RETURNING <time>

No id list is ever loaded into Python or bound as parameters, rows another transaction holds are skipped (they go the
next night), and the keyset floor keeps every batch an index range scan however large the backlog is.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import BigInteger, delete, func, literal, select, true
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core import caps
from app.core.utils import utcnow
from app.db import long_statement, session_scope
from app.models import AICallLog, AuditLog, AuthToken, EmailOutbox, IpBan, Job, Notification, SecurityEvent, UserSession
from app.models.enums import EmailStatus, JobStatus, TokenPurpose
from app.services.audit import audit
from app.services.jobs import periodic

log = logging.getLogger("ironpulse.retention")

PERIODIC_NAME = "data_retention"
RETENTION_ACTION = "system.retention"  # audit row of a finished run (also the once-per-day marker)
MAIL_DONE = (EmailStatus.SENT, EmailStatus.LOGGED, EmailStatus.FAILED)
JOBS_DONE = (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED)
# app/security/bans.py and app/services/security_triage.py count an address's bans of the last 30 days (a repeat
# offender without a subscription is banned for good): ip_bans retention never goes below this window
BAN_OFFENCE_WINDOW_DAYS = 30
# one-time links whose expired, unused tokens go; an unused invite (INVITE / STAFF_INVITE) stays - see the table above
ONE_TIME_LINKS = (TokenPurpose.EMAIL_VERIFY, TokenPurpose.PASSWORD_RESET)


@dataclass(frozen=True, slots=True)
class Rule:
    """Rows of ``model`` whose ``time_col`` is older than ``days_setting`` days and that match ``where``."""

    table: str  # report key
    model: Any
    time_col: Any  # indexed together with the equality columns of ``where``
    days_setting: str
    where: tuple[Any, ...] = ()
    min_days: int = 0  # a shorter configured window is raised to this (what a reader still needs)


def rules() -> list[Rule]:
    """In run order: e-mail before notifications (deleting a notification nulls its outbox rows' reference)."""
    return [
        Rule("security_events", SecurityEvent, SecurityEvent.created_at, "retention_security_days"),
        Rule("ai_call_logs", AICallLog, AICallLog.created_at, "retention_ai_log_days"),
        *(Rule("email_outbox", EmailOutbox, EmailOutbox.created_at, "retention_email_days", (EmailOutbox.status == s,))
          for s in MAIL_DONE),  # ix_email_outbox_status_created
        Rule("notifications", Notification, Notification.created_at, "retention_notification_days",
             (Notification.read_at.is_not(None),)),
        *(Rule("jobs", Job, Job.finished_at, "retention_jobs_days", (Job.status == s,))
          for s in JOBS_DONE),  # ix_jobs_status_finished
        # finished rows without a finished_at stamp (written by older versions): their age is created_at
        Rule("jobs", Job, Job.created_at, "retention_jobs_days", (Job.status.in_(JOBS_DONE), Job.finished_at.is_(None))),
        # never an active ban: lifted ones by lifted_at (ix_ip_bans_lifted_at), expired never-lifted ones by
        # banned_until (ix_ip_bans_unlifted_until); a permanent ban (banned_until NULL, not lifted) is neither
        Rule("ip_bans", IpBan, IpBan.lifted_at, "retention_ip_ban_days", min_days=BAN_OFFENCE_WINDOW_DAYS),
        Rule("ip_bans", IpBan, IpBan.banned_until, "retention_ip_ban_days", (IpBan.lifted_at.is_(None),),
             min_days=BAN_OFFENCE_WINDOW_DAYS),
        Rule("auth_tokens", AuthToken, AuthToken.used_at, "retention_auth_token_days"),  # ix_auth_tokens_used_at
        Rule("auth_tokens", AuthToken, AuthToken.expires_at, "retention_auth_token_days",  # ix_auth_tokens_expires_unused
             (AuthToken.used_at.is_(None), AuthToken.purpose.in_(ONE_TIME_LINKS))),
        Rule("user_sessions", UserSession, UserSession.expires_at, "retention_session_days"),  # ix_user_sessions_expires_at
        Rule("audit_logs", AuditLog, AuditLog.created_at, "retention_audit_days"),
    ]


@dataclass
class RetentionReport:
    deleted: dict[str, int] = field(default_factory=dict)  # rows per table
    batches: dict[str, int] = field(default_factory=dict)  # DELETE transactions per table
    complete: bool = True  # False: the time budget ran out (the next tick continues)
    seconds: float = 0.0

    @property
    def total(self) -> int:
        return sum(self.deleted.values())

    def add(self, table: str, rows: int) -> None:
        self.deleted[table] = self.deleted.get(table, 0) + rows
        self.batches[table] = self.batches.get(table, 0) + 1

    def detail(self) -> dict[str, Any]:
        return {"deleted": dict(self.deleted), "batches": dict(self.batches), "complete": self.complete,
                "seconds": round(self.seconds, 1), "summary": f"{self.total} old row{'s' if self.total != 1 else ''} deleted"}


def _clock() -> float:
    return asyncio.get_running_loop().time()


async def _purge(rule: Rule, cutoff: datetime, *, batch: int, deadline: float | None, report: RetentionReport) -> bool:
    """Delete the rule's rows older than ``cutoff``, ``batch`` rows per transaction, oldest first. False when the
    time budget ran out first."""
    model, col = rule.model, rule.time_col
    floor: datetime | None = None
    while True:
        if deadline is not None and _clock() >= deadline:
            return False
        conds = [col < cutoff, *rule.where]
        if floor is not None:
            conds.append(col >= floor)  # keyset: never walk back over what this run already deleted
        doomed = select(model.id).where(*conds).order_by(col).limit(batch).with_for_update(skip_locked=True)
        async with session_scope() as db:
            await long_statement(db)
            stamps = list((await db.execute(delete(model).where(model.id.in_(doomed)).returning(col))).scalars().all())
        report.add(rule.table, len(stamps))
        if stamps:
            floor = max(stamps)
        if len(stamps) < batch:
            return True


async def run_retention(*, now: datetime | None = None) -> RetentionReport:
    """One pass over every enabled rule (each batch in its own transaction). Safe to run next to the app and next to
    another pass: rows locked elsewhere are skipped."""
    s = get_settings()
    now = now or utcnow()
    batch = max(1, int(s.retention_batch))
    started = _clock()
    deadline = started + s.retention_max_seconds if s.retention_max_seconds > 0 else None
    report = RetentionReport()
    for rule in rules():
        days = int(getattr(s, rule.days_setting))
        if days <= 0:
            continue
        days = max(days, rule.min_days)
        report.deleted.setdefault(rule.table, 0)
        if not await _purge(rule, now - timedelta(days=days), batch=batch, deadline=deadline, report=report):
            report.complete = False
            break
    else:
        report.complete = await trim_notifications(caps.limit("notifications_kept"), deadline=deadline, report=report)
    report.seconds = _clock() - started
    return report


TRIM_USERS = 100  # people whose surplus notifications one transaction deletes
TRIM_ROWS = 5000  # ... at most this many rows of one person per statement (a bigger surplus goes the next night)


async def trim_notifications(keep: int, *, deadline: float | None = None,
                             report: RetentionReport | None = None) -> bool:
    """Cap ``notifications_kept``: everyone keeps their newest ``keep`` notifications - read or not - and older ones
    go, so even an account that never reads its bell stays bounded. People are walked in user-id order, TRIM_USERS per
    transaction (one GROUP BY over ix_notifications_user_created finds them); False when the time budget ran out."""
    report = report if report is not None else RetentionReport()
    after = 0
    while True:
        if deadline is not None and _clock() >= deadline:
            return False
        async with session_scope() as db:
            await long_statement(db)
            people = list((await db.execute(
                select(Notification.user_id).where(Notification.user_id > after).group_by(Notification.user_id)
                .having(func.count() > keep).order_by(Notification.user_id).limit(TRIM_USERS)
            )).scalars().all())
            removed = 0
            if people:  # ONE statement for the chunk: each person's surplus is an index range (LATERAL per person)
                person = select(func.unnest(literal(people, ARRAY(BigInteger))).label("uid")).subquery("p")
                surplus = (select(Notification.id).where(Notification.user_id == person.c.uid)
                           .order_by(Notification.created_at.desc(), Notification.id.desc())
                           .offset(keep).limit(TRIM_ROWS).lateral("s"))
                doomed = select(surplus.c.id).select_from(person.join(surplus, true()))
                removed = (await db.execute(delete(Notification).where(Notification.id.in_(doomed)))).rowcount or 0
        if removed:  # reported only when something went (a quiet night adds nothing to the run's audit row)
            report.add("notifications_over_cap", removed)
        if len(people) < TRIM_USERS:
            return True
        after = people[-1]


async def _done_today(db: AsyncSession, now: datetime) -> bool:
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return await db.scalar(
        select(AuditLog.id).where(AuditLog.action == RETENTION_ACTION, AuditLog.created_at >= day_start).limit(1)
    ) is not None


@periodic(PERIODIC_NAME, every_seconds=3600, initial_delay=300)
async def nightly_retention(db: AsyncSession) -> RetentionReport | None:
    """The hourly tick: run once per UTC day from RETENTION_HOUR_UTC on (see the module docstring)."""
    now = utcnow()
    if now.hour < get_settings().retention_hour_utc or await _done_today(db, now):
        return None
    # db's transaction (the day check above) stays idle (no snapshot, nothing written) while the batches commit in
    # their own transactions - run_periodic holds the task's lock on a connection of its own: lift its idle limit only.
    await long_statement(db, idle_ms=0)
    report = await run_retention(now=now)
    log.info("data retention: %s rows deleted in %.1f s (%s)%s", report.total, report.seconds,
             ", ".join(f"{k} {v}" for k, v in report.deleted.items() if v) or "nothing old enough",
             "" if report.complete else " - time budget used up, the next tick continues")
    if report.complete:
        marker = await audit(db, None, RETENTION_ACTION, "system", None, detail=report.detail())
        marker.created_at = now  # the day this run belongs to (the same clock as the check above)
    return report
