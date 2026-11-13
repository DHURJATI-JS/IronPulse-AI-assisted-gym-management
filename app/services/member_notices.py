"""Notices to every active member of a gym (or of one branch / some plan modes): queued, then sent in batches.

An owner's gym or branch status change (online classes switched off / closed / reopened, physical branches off / on,
a branch closed / reopened) concerns every active member, and a gym can have tens of thousands. The request only
records the change, counts the audience (one ``COUNT(DISTINCT customer_id)``) and queues ONE ``notify_members`` job
(``queue_member_notice``). The job walks the audience in keyset chunks of MEMBERS_BATCH customer ids - one short
transaction and one bulk insert (``notify_bulk``) each - so neither the owner's request nor any single transaction
grows with the member count.

Retries are idempotent: a member who already got this notice since it was queued (same title and link, created at or
after ``since`` - the request's database clock) is skipped, so a run interrupted half-way carries on where it stopped.
Each chunk checks and inserts under the notice's advisory lock (``_notice_lock_key``), so two runs carrying the same
notice at once (a retry next to the original, two workers) never both send it to a member. The owner routes lock the
gym / branch row before reading the state they change, so one change queues one notice.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

from sqlalchemy import distinct, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import session_scope
from app.models import Notification, Plan, Subscription
from app.models.enums import NotificationCategory, PlanMode, Severity, SubscriptionStatus
from app.services.batching import Chunks
from app.services.jobs import enqueue, job_handler
from app.services.notify import notify_many

log = logging.getLogger("ironpulse.member_notices")

JOB_KIND = "notify_members"
MEMBERS_BATCH = 500  # members per chunk - one short transaction, one bulk notification insert


def _audience(gym_id: int, *, as_of: date, branch_id: int | None = None,
              modes: Sequence[PlanMode] | None = None) -> list[Any]:
    """Active (not yet ended) subscriptions of the gym; ``Plan`` must be joined for ``modes``."""
    conds: list[Any] = [
        Subscription.gym_id == gym_id,
        Subscription.status == SubscriptionStatus.ACTIVE,
        or_(Subscription.ends_on.is_(None), Subscription.ends_on >= as_of),
    ]
    if branch_id is not None:
        conds.append(Subscription.branch_id == branch_id)
    if modes:
        conds.append(Plan.mode.in_(list(modes)))
    return conds


async def queue_member_notice(db: AsyncSession, *, gym_id: int, title: str, body: str, link: str | None,
                              severity: Severity, branch_id: int | None = None,
                              modes: Sequence[PlanMode] | None = None) -> int:
    """Queue the notice for the gym's active members (nothing is queued for an empty audience) and return how many
    members it goes to. The caller commits - the job runs only once the status change it announces is saved."""
    as_of = date.today()
    count, since = (await db.execute(
        select(func.count(distinct(Subscription.customer_id)), func.now())
        .select_from(Subscription).join(Plan, Plan.id == Subscription.plan_id)
        .where(*_audience(gym_id, as_of=as_of, branch_id=branch_id, modes=modes))
    )).one()
    if count:
        await enqueue(db, JOB_KIND, {
            "gym_id": gym_id, "branch_id": branch_id, "modes": [PlanMode(m).value for m in modes or ()],
            "title": title, "body": body, "link": link, "severity": Severity(severity).value,
            "as_of": as_of.isoformat(), "since": since.isoformat(),
        })
    return int(count or 0)


def _notice_lock_key(gym_id: int, branch_id: int | None, title: str, link: str | None) -> int:
    """64-bit advisory-lock key of one notice of one gym (or branch)."""
    digest = hashlib.blake2b(f"member_notice:{gym_id}:{branch_id}:{title}:{link}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


async def send_member_notice(*, gym_id: int, title: str, body: str, link: str | None, severity: Severity,
                             since: datetime, as_of: date, branch_id: int | None = None,
                             modes: Sequence[PlanMode] | None = None) -> int:
    """Notify the audience in keyset chunks (own transactions); returns the notifications created by this run."""
    stored_title = title[:160]  # as notify stores it
    already = exists(select(Notification.id).where(
        Notification.user_id == Subscription.customer_id, Notification.created_at >= since,
        Notification.title == stored_title, Notification.link.is_not_distinct_from(link),
    ))
    conds = _audience(gym_id, as_of=as_of, branch_id=branch_id, modes=modes)
    run = Chunks(budget_s=None)  # a job finishes its audience; every chunk is its own short transaction
    lock_key = _notice_lock_key(gym_id, branch_id, stored_title, link)
    sent = last_id = 0
    while await run.next():
        async with session_scope() as s:
            # check + insert under the notice's lock (until this chunk's COMMIT): a second run of the same notice
            # waits here and then skips the members this one reached
            await s.execute(select(func.pg_advisory_xact_lock(lock_key)))
            ids = list((await s.execute(
                select(Subscription.customer_id).join(Plan, Plan.id == Subscription.plan_id)
                .where(*conds, Subscription.customer_id > last_id, ~already)
                .group_by(Subscription.customer_id).order_by(Subscription.customer_id).limit(MEMBERS_BATCH)
            )).scalars().all())
            if ids:
                sent += await notify_many(s, ids, title, body, category=NotificationCategory.SYSTEM,
                                          severity=severity, link=link)
        if ids:
            last_id = ids[-1]
        run.done(len(ids), MEMBERS_BATCH)
    return sent


@job_handler(JOB_KIND)  # exit policy "keep": system work, no requester waits for it
async def notify_members_job(db: AsyncSession, payload: dict[str, Any]) -> None:
    sent = await send_member_notice(
        gym_id=int(payload["gym_id"]), branch_id=payload.get("branch_id"),
        modes=[PlanMode(m) for m in payload.get("modes") or ()], title=str(payload["title"]),
        body=str(payload["body"]), link=payload.get("link"), severity=Severity(payload.get("severity") or "info"),
        since=datetime.fromisoformat(payload["since"]), as_of=date.fromisoformat(payload["as_of"]),
    )
    log.info("gym %s notice %r sent to %s member(s)", payload.get("gym_id"), payload.get("title"), sent)
