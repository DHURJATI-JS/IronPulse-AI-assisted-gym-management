"""SMTP delivery of the e-mail outbox (aiosmtplib). Without SMTP_HOST, mails are spooled to var/mail/."""

from __future__ import annotations

import logging
from datetime import timedelta
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.utils import utcnow
from app.models import EmailOutbox
from app.models.enums import EmailStatus

log = logging.getLogger("ironpulse.mail")
MAX_ATTEMPTS = 6
CLAIM_LEASE = timedelta(minutes=15)  # a claimed batch (25 x SMTP_TIMEOUT 20 s at worst) is re-claimed after this


def build_message(row: EmailOutbox) -> EmailMessage:
    settings = get_settings()
    msg = EmailMessage()
    msg["From"] = settings.smtp_from
    msg["To"] = row.to_email
    if row.cc:
        msg["Cc"] = ", ".join(row.cc)
    msg["Subject"] = row.subject
    msg["Date"] = formatdate(localtime=False)
    domain = parseaddr(settings.smtp_from)[1].split("@")[-1] or "ironpulse.local"
    msg["Message-ID"] = make_msgid(domain=domain)
    msg.set_content(row.text_body)
    if row.html_body:
        msg.add_alternative(row.html_body, subtype="html")
    return msg


async def _deliver(msg: EmailMessage) -> None:
    import aiosmtplib

    s = get_settings()
    await aiosmtplib.send(
        msg,
        hostname=s.smtp_host,
        port=s.smtp_port,
        username=s.smtp_username or None,
        password=s.smtp_password or None,
        start_tls=s.smtp_starttls and not s.smtp_ssl,
        use_tls=s.smtp_ssl,
        timeout=s.smtp_timeout,
    )


def _spool(row: EmailOutbox, msg: EmailMessage) -> None:
    folder = get_settings().mail_dir
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{utcnow():%Y%m%d-%H%M%S}-{row.id}.eml").write_bytes(bytes(msg))


async def send_pending_emails(db: AsyncSession, limit: int = 25) -> int:
    """Deliver up to ``limit`` queued e-mails. No transaction (nor row lock) is held during the SMTP conversation:

    1. claim - ONE ``UPDATE ... RETURNING`` marks the batch SENDING, counts the attempt and sets a lease
       (``next_attempt_at`` = now + CLAIM_LEASE; rows other workers hold are skipped) - then COMMIT;
    2. deliver each one outside any transaction (SMTP, or the var/mail spool without SMTP_HOST);
    3. write every outcome in one bulk UPDATE (the caller commits).

    A row still SENDING after its lease belongs to a worker that died mid-batch: it is claimed again (at-least-once
    delivery, like any outbox). Returns how many rows were claimed."""
    now = utcnow()
    due = or_(
        and_(EmailOutbox.status == EmailStatus.QUEUED,
             or_(EmailOutbox.next_attempt_at.is_(None), EmailOutbox.next_attempt_at <= now)),
        and_(EmailOutbox.status == EmailStatus.SENDING, EmailOutbox.next_attempt_at <= now),  # a dead worker's batch
    )
    batch = (select(EmailOutbox.id).where(due).order_by(EmailOutbox.id).limit(limit)
             .with_for_update(skip_locked=True).scalar_subquery())
    rows = list((await db.execute(
        update(EmailOutbox).where(EmailOutbox.id.in_(batch))
        .values(status=EmailStatus.SENDING, attempts=EmailOutbox.attempts + 1, next_attempt_at=now + CLAIM_LEASE)
        .returning(EmailOutbox)
        .execution_options(synchronize_session=False)
    )).scalars().all())
    await db.commit()  # the claim is durable; nothing is held while mail servers answer
    if not rows:
        return 0
    settings = get_settings()
    outcomes: list[dict] = []
    for row in sorted(rows, key=lambda r: r.id):
        msg = build_message(row)
        done = {"id": row.id, "status": row.status, "sent_at": None, "last_error": None, "next_attempt_at": None}
        try:
            if settings.smtp_enabled:
                await _deliver(msg)
                done["status"] = EmailStatus.SENT
            else:
                _spool(row, msg)
                done["status"] = EmailStatus.LOGGED
            done["sent_at"] = utcnow()
        except Exception as exc:  # noqa: BLE001 - one bad address never stops the batch
            log.warning("e-mail %s to %s failed: %s", row.id, row.to_email, exc)
            done["last_error"] = f"{exc.__class__.__name__}: {exc}"[:1000]
            if row.attempts >= MAX_ATTEMPTS:
                done["status"] = EmailStatus.FAILED
            else:
                done["status"] = EmailStatus.QUEUED
                done["next_attempt_at"] = utcnow() + timedelta(minutes=2 ** row.attempts)
        outcomes.append(done)
    await db.execute(update(EmailOutbox), outcomes)  # one bulk UPDATE by primary key
    return len(rows)
