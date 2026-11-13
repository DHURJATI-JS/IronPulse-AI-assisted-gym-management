"""Notifications: in-app (Dynamic Island + bell, pushed live over /ws) AND e-mail for every one.

For minors the guardian's e-mail is CC'd on every message. Account changes must call ``notify``
(e.g. "Your phone number was changed by the gym admin").

Fan-out (many recipients - periodic reminders, gym-wide announcements) uses ``notify_bulk`` / ``notify_many``: the
same rules as ``notify`` in a constant number of statements per ``NOTIFY_CHUNK`` recipients (the users when ids are
given, the guardians' e-mails when there are minors, one multi-row notifications INSERT, one multi-row e-mail
INSERT) instead of a flush, a guardian lookup and an outbox row per person. Large audiences are also split into one
short transaction per chunk by the caller (a job or a chunked periodic task - app/services/batching.py).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import CustomerProfile, EmailOutbox, Notification, User
from app.models.enums import NotificationCategory, Severity
from app.services.realtime import publish_after_commit

NOTIFY_CHUNK = 500  # recipients per multi-row INSERT (bounded statements and bind parameters)
# bidi embeddings / overrides / isolates: a name or text carrying one would reorder the rest of a title or body in the
# bell, the Dynamic Island and the e-mail ("pay <RLO>gnp.exe" reads "pay exe.png") - removed from every notification
_BIDI_CONTROLS = dict.fromkeys(map(ord, "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"))


def _plain(text: str | None) -> str:
    return str(text or "").translate(_BIDI_CONTROLS)


async def _guardian_email(db: AsyncSession, user: User) -> str | None:
    if not user.is_minor:
        return None
    return await db.scalar(select(CustomerProfile.guardian_email).where(CustomerProfile.user_id == user.id))


def _cc(to: str, cc: Iterable[str | None] | None) -> list[str]:
    return [c for c in (cc or []) if c and c.lower() != to.lower()]


def _full_link(link: str | None) -> str | None:
    return f"{get_settings().public_base_url}{link}" if link and link.startswith("/") else link


def _live_event(notification_id: int, title: str, body: str, category: NotificationCategory, severity: Severity,
                link: str | None) -> dict[str, Any]:
    return {
        "type": "notification",
        "id": notification_id,
        "title": title,
        "body": body[:400],
        "category": category.value,
        "severity": severity.value,
        "link": link,
    }


async def queue_email(
    db: AsyncSession,
    *,
    to: str,
    subject: str,
    text: str,
    html: str | None = None,
    cc: list[str] | None = None,
    user_id: int | None = None,
    notification_id: int | None = None,
) -> EmailOutbox:
    row = EmailOutbox(
        to_email=to,
        cc=_cc(to, cc),
        subject=subject[:200],
        text_body=text,
        html_body=html,
        user_id=user_id,
        notification_id=notification_id,
    )
    db.add(row)
    return row


async def notify(
    db: AsyncSession,
    user: User | int,
    title: str,
    body: str,
    *,
    category: NotificationCategory = NotificationCategory.SYSTEM,
    severity: Severity = Severity.INFO,
    link: str | None = None,
    email: bool = True,
    allow_deleted: bool = False,
) -> Notification | None:
    """Create an in-app notification, queue its e-mail (+ guardian CC) and push it live after commit.

    The caller owns the transaction (``await db.commit()``). Many recipients: ``notify_bulk`` / ``notify_many``."""
    if isinstance(user, int):
        user = await db.get(User, user)
        if user is None:
            return None
    if user.deleted_at is not None and not allow_deleted:
        return None
    title, body = _plain(title), _plain(body)
    send_mail = bool(email and user.email)
    # ``emailed`` is known up front - set before the INSERT, so no extra UPDATE follows at the next flush
    n = Notification(user_id=user.id, title=title[:160], body=body, category=category, severity=severity, link=link,
                     emailed=send_mail)
    db.add(n)
    await db.flush()
    if send_mail:
        from app.core.templating import render_email

        settings = get_settings()
        guardian = await _guardian_email(db, user)
        text_body, html_body = render_email(
            title=title, body=body, link=_full_link(link), recipient=user.full_name, guardian_copy=bool(guardian)
        )
        await queue_email(
            db, to=user.email, cc=[guardian] if guardian else None, subject=f"{settings.app_name}: {title}",
            text=text_body, html=html_body, user_id=user.id, notification_id=n.id,
        )
    publish_after_commit(db, [user.id], _live_event(n.id, n.title, body, category, severity, link))
    return n


@dataclass(slots=True)
class Note:
    """One notification of a bulk send (``notify_bulk``) - the arguments of one ``notify`` call."""

    user: User | int
    title: str
    body: str
    link: str | None = None
    category: NotificationCategory = NotificationCategory.SYSTEM
    severity: Severity = Severity.INFO
    email: bool = True


async def notify_bulk(db: AsyncSession, notes: Iterable[Note], *, allow_deleted: bool = False) -> list[int]:
    """``notify`` for many recipients at once, with the same rules (missing / deleted accounts skipped, e-mail with
    the guardian in CC for minors, live push after COMMIT), in a constant number of statements per ``NOTIFY_CHUNK``
    notes: the users (only for ids), the guardians' e-mails (only when there are minors), one multi-row
    notifications INSERT and one multi-row e-mail INSERT. Returns the new notification ids in order (skipped
    recipients have none). The caller owns the transaction - and splits very large audiences into chunks."""
    pending = [n if n.title == _plain(n.title) and n.body == _plain(n.body) else
               Note(n.user, _plain(n.title), _plain(n.body), link=n.link, category=n.category, severity=n.severity,
                    email=n.email) for n in notes]
    created: list[int] = []
    for start in range(0, len(pending), NOTIFY_CHUNK):
        created += await _notify_chunk(db, pending[start:start + NOTIFY_CHUNK], allow_deleted=allow_deleted)
    return created


async def _notify_chunk(db: AsyncSession, notes: Sequence[Note], *, allow_deleted: bool) -> list[int]:
    wanted = {int(n.user) for n in notes if not isinstance(n.user, User)}
    loaded: dict[int, User] = {}
    if wanted:
        loaded = {u.id: u for u in (await db.execute(select(User).where(User.id.in_(wanted)))).scalars()}
    todo: list[tuple[Note, User]] = []
    for n in notes:
        user = n.user if isinstance(n.user, User) else loaded.get(int(n.user))
        if user is None or (user.deleted_at is not None and not allow_deleted):
            continue
        todo.append((n, user))
    if not todo:
        return []
    mailed = [bool(n.email and u.email) for n, u in todo]
    minors = {u.id for (_, u), m in zip(todo, mailed, strict=True) if m and u.is_minor}
    guardians: dict[int, str | None] = {}
    if minors:
        guardians = dict((await db.execute(
            select(CustomerProfile.user_id, CustomerProfile.guardian_email).where(CustomerProfile.user_id.in_(minors))
        )).tuples().all())
    # render_nulls: rows with and without a link share ONE multi-row INSERT (else the ORM groups rows by non-NULL keys)
    ids = list((await db.execute(
        insert(Notification).returning(Notification.id, sort_by_parameter_order=True)
        .execution_options(render_nulls=True),
        [{"user_id": u.id, "title": n.title[:160], "body": n.body, "category": n.category, "severity": n.severity,
          "link": n.link, "emailed": m} for (n, u), m in zip(todo, mailed, strict=True)],
    )).scalars().all())
    if any(mailed):
        from app.core.templating import render_email

        app_name = get_settings().app_name
        mails: list[dict[str, Any]] = []
        for (n, u), m, nid in zip(todo, mailed, ids, strict=True):
            if not m:
                continue
            guardian = guardians.get(u.id)
            text_body, html_body = render_email(title=n.title, body=n.body, link=_full_link(n.link),
                                                recipient=u.full_name, guardian_copy=bool(guardian))
            mails.append({"to_email": u.email, "cc": _cc(u.email, [guardian]), "subject": f"{app_name}: {n.title}"[:200],
                          "text_body": text_body, "html_body": html_body, "user_id": u.id, "notification_id": nid})
        await db.execute(insert(EmailOutbox).execution_options(render_nulls=True), mails)
    for (n, u), nid in zip(todo, ids, strict=True):
        publish_after_commit(db, [u.id], _live_event(nid, n.title[:160], n.body, n.category, n.severity, n.link))
    return ids


async def notify_many(
    db: AsyncSession,
    users: Iterable[User | int],
    title: str,
    body: str,
    *,
    category: NotificationCategory = NotificationCategory.SYSTEM,
    severity: Severity = Severity.INFO,
    link: str | None = None,
    email: bool = True,
    allow_deleted: bool = False,
) -> int:
    """The same notification for many users, in bulk (``notify_bulk``). Returns how many were created."""
    notes = [Note(u, title, body, link=link, category=category, severity=severity, email=email) for u in users]
    return len(await notify_bulk(db, notes, allow_deleted=allow_deleted))


async def email_address(
    db: AsyncSession, to: str, title: str, body: str, *, link: str | None = None, recipient: str | None = None
) -> EmailOutbox:
    """E-mail someone who has no account yet (staff invites, job enquiries, guardians)."""
    from app.core.templating import render_email

    settings = get_settings()
    text_body, html_body = render_email(title=title, body=body, link=_full_link(link), recipient=recipient,
                                        guardian_copy=False)
    return await queue_email(db, to=to, subject=f"{settings.app_name}: {title}", text=text_body, html=html_body)
