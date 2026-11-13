"""Nightly batched data retention (app/services/retention.py): only old rows that no page needs, in bounded
transactions, once per UTC day under the periodic advisory lock."""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import event, func, insert, select, text

from app import db as dbmod
from app.config import get_settings
from app.core.utils import utcnow
from app.models import AICallLog, AuditLog, AuthToken, EmailOutbox, IpBan, Job, Notification, SecurityEvent, UserSession
from app.models.enums import EmailStatus, JobStatus, NotificationCategory, Severity, ThreatCategory, TokenPurpose
from app.security.crypto import token_hash
from app.services import retention
from app.services.jobs import _lock_id, registered_periodic, run_periodic
from tests.factories import make_customer, make_employee, make_gym, make_owner

DAY = timedelta(days=1)


# ============================================================================================ seeding
async def add_events(db: Any, n: int, at: datetime, ip: str = "198.51.100.7") -> None:
    if n:
        await db.execute(insert(SecurityEvent), [
            {"ip": ip, "method": "GET", "path": f"/probe/{i}", "category": ThreatCategory.SCANNER, "severity": 2,
             "created_at": at} for i in range(n)])
        await db.commit()


async def add_ai_logs(db: Any, n: int, at: datetime) -> None:
    await db.execute(insert(AICallLog), [
        {"feature": "assistant", "provider": "gateway", "model": "gemma", "ok": True, "latency_ms": 50, "created_at": at}
        for _ in range(n)])
    await db.commit()


async def add_mail(db: Any, status: EmailStatus, at: datetime, n: int = 1) -> None:
    await db.execute(insert(EmailOutbox), [
        {"to_email": "x@example.com", "subject": "Hi", "text_body": "body", "status": status, "created_at": at}
        for _ in range(n)])
    await db.commit()


async def add_notes(db: Any, user_id: int, at: datetime, *, read: bool, n: int = 1) -> None:
    await db.execute(insert(Notification), [
        {"user_id": user_id, "title": "T", "body": "B", "category": NotificationCategory.SYSTEM, "severity": Severity.INFO,
         "read_at": at if read else None, "created_at": at} for _ in range(n)])
    await db.commit()


async def add_job(db: Any, status: JobStatus, *, created: datetime, finished: datetime | None) -> None:
    await db.execute(insert(Job).values(kind="noop", status=status, created_at=created, finished_at=finished))
    await db.commit()


async def add_audit(db: Any, n: int, at: datetime) -> None:
    await db.execute(insert(AuditLog), [
        {"action": "test.old", "entity_type": "shop_order", "entity_id": i, "detail": {}, "created_at": at}
        for i in range(n)])
    await db.commit()


async def count(db: Any, model: Any, *where: Any) -> int:
    return int(await db.scalar(select(func.count()).select_from(model).where(*where)) or 0)


class Statements:
    def __init__(self) -> None:
        self.sent: list[tuple[str, Any]] = []
        self.commits = 0

    def _on(self, conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        self.sent.append((statement, parameters))

    def _commit(self, conn) -> None:  # noqa: ANN001
        self.commits += 1

    def __enter__(self) -> "Statements":
        engine = dbmod.get_engine().sync_engine
        event.listen(engine, "before_cursor_execute", self._on)
        event.listen(engine, "commit", self._commit)
        return self

    def __exit__(self, *exc: object) -> None:
        engine = dbmod.get_engine().sync_engine
        event.remove(engine, "before_cursor_execute", self._on)
        event.remove(engine, "commit", self._commit)

    def deletes(self, table: str) -> list[tuple[str, Any]]:
        return [(s, p) for s, p in self.sent if s.startswith(f"DELETE FROM {table} ")]


# ============================================================================================ policy
def test_registered_as_an_hourly_periodic() -> None:
    task = registered_periodic()[retention.PERIODIC_NAME]
    assert task.every_seconds == 3600 and task.fn is retention.nightly_retention
    s = get_settings()
    assert (s.retention_batch, s.retention_security_days, s.retention_audit_days, s.retention_ai_log_days,
            s.retention_email_days, s.retention_jobs_days, s.retention_notification_days) == (1000, 90, 365, 30, 30, 14, 90)
    assert (s.retention_ip_ban_days, s.retention_auth_token_days, s.retention_session_days) == (90, 30, 30)
    assert retention.BAN_OFFENCE_WINDOW_DAYS == 30


def test_env_example_documents_every_retention_setting() -> None:
    from app.config import BASE_DIR, Settings

    text = (BASE_DIR / ".env.example").read_text(encoding="utf-8")
    names = [name for name in Settings.model_fields if name.startswith("retention_")]
    assert len(names) == 12
    for name in names:
        assert f"# {name.upper()}={Settings.model_fields[name].default}" in text, name  # commented out, real default


async def test_deletes_only_old_rows_no_page_needs(db: Any) -> None:
    now = utcnow()
    user = await make_customer(db)
    await add_events(db, 3, now - 100 * DAY)
    await add_events(db, 2, now - 10 * DAY)
    await add_ai_logs(db, 4, now - 40 * DAY)
    await add_ai_logs(db, 1, now - 5 * DAY)
    for status in (EmailStatus.SENT, EmailStatus.SENT, EmailStatus.LOGGED, EmailStatus.FAILED,
                   EmailStatus.QUEUED, EmailStatus.SENDING):
        await add_mail(db, status, now - 40 * DAY)
    await add_mail(db, EmailStatus.SENT, now - 5 * DAY)
    await add_notes(db, user.id, now - 100 * DAY, read=True, n=3)
    await add_notes(db, user.id, now - 100 * DAY, read=False, n=2)  # unread: kept however old
    await add_notes(db, user.id, now - 10 * DAY, read=True)
    for status in (JobStatus.DONE, JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED):
        await add_job(db, status, created=now - 21 * DAY, finished=now - 20 * DAY)
    for status in (JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.PAUSED):  # in flight: never touched
        await add_job(db, status, created=now - 60 * DAY, finished=None)
    await add_job(db, JobStatus.DONE, created=now - 3 * DAY, finished=now - 2 * DAY)
    await add_job(db, JobStatus.DONE, created=now - 30 * DAY, finished=None)  # finished before finished_at existed
    await add_audit(db, 2, now - 400 * DAY)
    await add_audit(db, 1, now - 100 * DAY)

    report = await retention.run_retention(now=now)

    assert report.complete
    assert report.deleted == {"security_events": 3, "ai_call_logs": 4, "email_outbox": 4, "notifications": 3,
                              "jobs": 5, "ip_bans": 0, "auth_tokens": 0, "user_sessions": 0, "audit_logs": 2}
    assert await count(db, SecurityEvent) == 2 and await count(db, AICallLog) == 1
    left_mail = (await db.execute(select(EmailOutbox.status).order_by(EmailOutbox.id))).scalars().all()
    assert left_mail == [EmailStatus.QUEUED, EmailStatus.SENDING, EmailStatus.SENT]
    assert await count(db, Notification, Notification.read_at.is_(None)) == 2
    assert await count(db, Notification, Notification.read_at.is_not(None)) == 1
    left_jobs = sorted(s.value for s in (await db.execute(select(Job.status))).scalars())
    assert left_jobs == ["done", "paused", "queued", "running"]
    assert await count(db, AuditLog) == 1
    assert report.detail()["summary"] == "21 old rows deleted"


async def test_each_batch_is_its_own_bounded_transaction(db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "retention_batch", 2)
    now = utcnow()
    await add_events(db, 7, now - 100 * DAY)
    await add_events(db, 2, now - DAY)
    with Statements() as st:
        report = await retention.run_retention(now=now)
    assert report.deleted["security_events"] == 7 and report.batches["security_events"] == 4  # 2 + 2 + 2 + 1
    deletes = st.deletes("security_events")
    assert len(deletes) == 4
    for sql, params in deletes:
        assert "LIMIT" in sql and "FOR UPDATE SKIP LOCKED" in sql and "RETURNING" in sql
        assert 2 in list(params) and len(list(params)) <= 4  # cutoff, floor, batch size - never a list of ids
    assert "created_at >=" not in deletes[0][0] and "created_at >=" in deletes[1][0]  # keyset floor after batch 1
    lifted = [i for i, (s, _) in enumerate(st.sent) if "set_config('statement_timeout'" in s]
    first_delete = st.sent.index(deletes[0])
    assert any(i < first_delete for i in lifted)  # long_statement inside the batch's transaction
    assert st.commits >= 4
    assert await count(db, SecurityEvent) == 2


async def test_an_exact_multiple_of_the_batch_ends_with_an_empty_batch(db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "retention_batch", 3)
    now = utcnow()
    await add_ai_logs(db, 6, now - 31 * DAY)
    report = await retention.run_retention(now=now)
    assert report.deleted["ai_call_logs"] == 6 and report.batches["ai_call_logs"] == 3
    assert await count(db, AICallLog) == 0


async def test_zero_days_keeps_a_table(db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "retention_security_days", 0)
    monkeypatch.setattr(get_settings(), "retention_audit_days", 0)
    now = utcnow()
    await add_events(db, 3, now - 1000 * DAY)
    await add_audit(db, 2, now - 1000 * DAY)
    with Statements() as st:
        report = await retention.run_retention(now=now)
    assert "security_events" not in report.deleted and "audit_logs" not in report.deleted
    assert not st.deletes("security_events") and not st.deletes("audit_logs")
    assert await count(db, SecurityEvent) == 3 and await count(db, AuditLog) == 2


async def test_rows_held_by_another_transaction_are_skipped(db: Any) -> None:
    now = utcnow()
    await add_events(db, 4, now - 200 * DAY)
    held_id = await db.scalar(select(func.min(SecurityEvent.id)))
    async with dbmod.get_sessionmaker()() as other:
        await other.execute(select(SecurityEvent.id).where(SecurityEvent.id == held_id).with_for_update())
        report = await retention.run_retention(now=now)  # never waits for the lock
        assert report.deleted["security_events"] == 3 and report.complete
        await other.rollback()
    assert (await retention.run_retention(now=now)).deleted["security_events"] == 1
    assert await count(db, SecurityEvent) == 0


async def test_the_time_budget_stops_between_batches(db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "retention_batch", 2)
    monkeypatch.setattr(get_settings(), "retention_max_seconds", 150)
    ticks = itertools.count(0, 100)  # each look at the clock: 100 s later
    monkeypatch.setattr(retention, "_clock", lambda: float(next(ticks)))
    now = utcnow()
    await add_events(db, 5, now - 100 * DAY)
    report = await retention.run_retention(now=now)  # start 0 -> deadline 150: one batch at 100, stop at 200
    assert not report.complete and report.deleted == {"security_events": 2}
    assert await count(db, SecurityEvent) == 3


# ============================================================================================ bans, one-time tokens, sessions
async def add_ban(db: Any, ip: str, *, created: datetime, until: datetime | None, lifted: datetime | None = None) -> None:
    await db.execute(insert(IpBan).values(ip=ip, reason="probe", severity=3, created_at=created, banned_until=until,
                                          lifted_at=lifted))
    await db.commit()


async def test_ip_bans_lifted_or_expired_long_ago_go_and_active_bans_never(db: Any) -> None:
    from app.security.bans import ban_registry, sync_bans_from_db
    from app.services.security_triage import _offenses_30d

    now = utcnow()
    await add_ban(db, "203.0.113.1", created=now - 200 * DAY, until=now - 199 * DAY, lifted=now - 120 * DAY)  # goes
    await add_ban(db, "203.0.113.2", created=now - 200 * DAY, until=None, lifted=now - 100 * DAY)  # lifted permanent: goes
    await add_ban(db, "203.0.113.3", created=now - 200 * DAY, until=now - 95 * DAY)  # expired 95 days ago: goes
    await add_ban(db, "203.0.113.4", created=now - 200 * DAY, until=now - 199 * DAY, lifted=now - 10 * DAY)  # stays
    await add_ban(db, "203.0.113.5", created=now - 20 * DAY, until=now - 19 * DAY)  # in the offence window: stays
    await add_ban(db, "203.0.113.6", created=now - 400 * DAY, until=None)  # permanent: never
    await add_ban(db, "203.0.113.7", created=now - 400 * DAY, until=now + 30 * DAY)  # active: never
    report = await retention.run_retention(now=now)
    assert report.complete and report.deleted["ip_bans"] == 3
    left = sorted((await db.execute(select(IpBan.ip))).scalars())
    assert left == ["203.0.113.4", "203.0.113.5", "203.0.113.6", "203.0.113.7"]
    async with dbmod.get_sessionmaker()() as s:  # what the ban machinery reads is intact
        await sync_bans_from_db(s)
        assert await _offenses_30d(s, "203.0.113.5") == 1  # the repeat-offence count
    assert ban_registry.get("203.0.113.6") is not None and ban_registry.get("203.0.113.7") is not None
    assert ban_registry.get("203.0.113.1") is None and ban_registry.get("203.0.113.3") is None


async def test_ip_ban_retention_never_goes_below_the_repeat_offence_window(db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.security_triage import _offenses_30d

    monkeypatch.setattr(get_settings(), "retention_ip_ban_days", 7)  # shorter than the 30-day offence window
    now = utcnow()
    await add_ban(db, "198.51.100.20", created=now - 12 * DAY, until=now - 11 * DAY)  # still counts as an offence
    await add_ban(db, "198.51.100.21", created=now - 25 * DAY, until=now - 24 * DAY, lifted=now - 20 * DAY)
    await add_ban(db, "198.51.100.22", created=now - 45 * DAY, until=now - 44 * DAY)  # past the window: goes
    report = await retention.run_retention(now=now)
    assert report.deleted["ip_bans"] == 1
    assert await _offenses_30d(db, "198.51.100.20") == 1 and await _offenses_30d(db, "198.51.100.21") == 1
    monkeypatch.setattr(get_settings(), "retention_ip_ban_days", 0)  # 0 keeps the table for ever
    await add_ban(db, "198.51.100.23", created=now - 900 * DAY, until=now - 899 * DAY)
    assert "ip_bans" not in (await retention.run_retention(now=now)).deleted


async def add_token(db: Any, purpose: TokenPurpose, raw: str, *, created: datetime, expires: datetime,
                    used: datetime | None = None, user_id: int | None = None, employee_id: int | None = None) -> None:
    await db.execute(insert(AuthToken).values(purpose=purpose, token_hash=token_hash(raw), user_id=user_id,
                                              employee_id=employee_id, created_at=created, expires_at=expires,
                                              used_at=used))
    await db.commit()


async def test_auth_tokens_used_or_expired_long_ago_go_and_unused_invites_stay(db: Any, client: Any) -> None:
    from app.models.enums import EmploymentStatus
    from app.services import auth_flows, intake
    from tests.conftest import login_as

    now = utcnow()
    member = await make_customer(db, password=None)  # registered by a gym, never activated
    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    invited = await make_employee(db, gym, status=EmploymentStatus.INVITED)  # staff record waiting for its account
    raw = {name: f"{name}-{'a' * 40}" for name in ("reset_used", "verify_used", "reset_dead", "verify_recent",
                                                   "invite_old", "invite_superseded", "staff_old", "reset_live")}
    await add_token(db, TokenPurpose.PASSWORD_RESET, raw["reset_used"], user_id=member.id, created=now - 41 * DAY,
                    expires=now - 41 * DAY + timedelta(hours=1), used=now - 41 * DAY)  # used long ago: goes
    await add_token(db, TokenPurpose.EMAIL_VERIFY, raw["verify_used"], user_id=member.id, created=now - 12 * DAY,
                    expires=now - 10 * DAY, used=now - 11 * DAY)  # used lately: stays
    await add_token(db, TokenPurpose.PASSWORD_RESET, raw["reset_dead"], user_id=member.id, created=now - 45 * DAY,
                    expires=now - 45 * DAY + timedelta(hours=1))  # never used, expired long ago: goes
    await add_token(db, TokenPurpose.EMAIL_VERIFY, raw["verify_recent"], user_id=member.id, created=now - 5 * DAY,
                    expires=now - 3 * DAY)  # expired lately: stays
    await add_token(db, TokenPurpose.INVITE, raw["invite_superseded"], user_id=member.id, created=now - 90 * DAY,
                    expires=now - 83 * DAY, used=now - 60 * DAY)  # re-sent 60 days ago (superseded = used): goes
    await add_token(db, TokenPurpose.INVITE, raw["invite_old"], user_id=member.id, created=now - 60 * DAY,
                    expires=now - 53 * DAY)  # the member's latest invite, long expired: stays
    await add_token(db, TokenPurpose.STAFF_INVITE, raw["staff_old"], employee_id=invited.id, created=now - 70 * DAY,
                    expires=now - 63 * DAY)  # the staff record's latest link, long expired: stays
    await add_token(db, TokenPurpose.PASSWORD_RESET, raw["reset_live"], user_id=member.id, created=now,
                    expires=now + timedelta(hours=1))
    report = await retention.run_retention(now=now)
    assert report.complete and report.deleted["auth_tokens"] == 3
    gone = {name for name in raw if await db.scalar(select(func.count()).select_from(AuthToken).where(
        AuthToken.token_hash == token_hash(raw[name]))) == 0}
    assert gone == {"reset_used", "reset_dead", "invite_superseded"}
    # the pages that read old tokens still tell the truth
    assert await intake.last_invite_at(db, member.id) == now - 60 * DAY  # "Invite sent 2 months ago", not "never"
    await login_as(client, owner)
    page = await client.get(f"/owner/staff/{invited.id}")
    assert page.status_code == 200 and "Last link sent" in page.text and "expired" in page.text
    assert (await auth_flows.lookup_token(db, raw["reset_live"], TokenPurpose.PASSWORD_RESET)).state == "ok"
    assert (await auth_flows.lookup_token(db, raw["verify_recent"], TokenPurpose.EMAIL_VERIFY)).state == "expired"
    assert (await auth_flows.lookup_token(db, raw["reset_dead"], TokenPurpose.PASSWORD_RESET)).state == "missing"


async def test_sessions_expired_long_ago_go_and_live_ones_stay(db: Any) -> None:
    from app.security.sessions import load_session

    now = utcnow()
    member = await make_customer(db)
    sessions = {"dead": now - 45 * DAY, "revoked_dead": now - 31 * DAY, "recent": now - 10 * DAY, "live": now + DAY}
    for name, expires in sessions.items():
        await db.execute(insert(UserSession).values(
            id=token_hash(f"{name}-{'s' * 40}"), user_id=member.id, created_at=expires - 7 * DAY, expires_at=expires,
            last_seen_at=expires - 7 * DAY, revoked_at=expires - 6 * DAY if name == "revoked_dead" else None))
    await db.commit()
    report = await retention.run_retention(now=now)
    assert report.complete and report.deleted["user_sessions"] == 2
    left = set((await db.execute(select(UserSession.id))).scalars())
    assert left == {token_hash(f"recent-{'s' * 40}"), token_hash(f"live-{'s' * 40}")}
    assert (await load_session(db, f"live-{'s' * 40}")) is not None  # signing in still works


async def test_the_new_rules_delete_in_bounded_batches(db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "retention_batch", 2)
    now = utcnow()
    member = await make_customer(db)
    for i in range(5):
        await add_ban(db, f"192.0.2.{i}", created=now - 300 * DAY, until=now - 299 * DAY, lifted=now - (150 + i) * DAY)
    await db.execute(insert(UserSession), [{"id": token_hash(f"old-{i}-{'s' * 30}"), "user_id": member.id,
                                            "expires_at": now - (60 + i) * DAY} for i in range(3)])
    await db.commit()
    with Statements() as st:
        report = await retention.run_retention(now=now)
    assert (report.deleted["ip_bans"], report.batches["ip_bans"]) == (5, 4)  # lifted 2 + 2 + 1, then the expired rule
    assert (report.deleted["user_sessions"], report.batches["user_sessions"]) == (3, 2)
    for table in ("ip_bans", "user_sessions", "auth_tokens"):
        for sql, params in st.deletes(table):
            assert "LIMIT" in sql and "FOR UPDATE SKIP LOCKED" in sql and len(list(params)) <= 6, sql


# ============================================================================================ the nightly periodic
class Clock:
    def __init__(self) -> None:
        self.now = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    def at(self, hour: int, minute: int = 0, *, day: int = 0) -> datetime:
        midnight = utcnow().replace(hour=0, minute=0, second=0, microsecond=0) + day * DAY
        self.now = midnight.replace(hour=hour, minute=minute)
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(retention, "utcnow", lambda: c.now)
    return c


async def tick() -> bool:
    return await run_periodic(registered_periodic()[retention.PERIODIC_NAME])


async def markers(db: Any) -> list[AuditLog]:
    db.expire_all()
    return list((await db.execute(select(AuditLog).where(AuditLog.action == retention.RETENTION_ACTION)
                                  .order_by(AuditLog.id))).scalars())


async def test_acts_once_per_utc_day_from_the_configured_hour(db: Any, clock: Clock) -> None:
    hour = get_settings().retention_hour_utc
    clock.at(hour - 1, 30)
    await add_events(db, 3, clock.now - 100 * DAY)
    assert await tick()  # too early: nothing happens
    assert await count(db, SecurityEvent) == 3 and not await markers(db)

    clock.at(hour, 5)
    assert await tick()
    assert await count(db, SecurityEvent) == 0
    [marker] = await markers(db)
    assert marker.actor_id is None and marker.entity_type == "system"
    assert marker.detail["deleted"]["security_events"] == 3 and marker.detail["complete"] is True
    assert marker.created_at == clock.now

    await add_events(db, 2, clock.now - 100 * DAY)
    clock.at(min(hour + 2, 23), 0)
    assert await tick()  # later the same day: already done
    assert await count(db, SecurityEvent) == 2 and len(await markers(db)) == 1

    clock.at(hour, 10, day=1)
    assert await tick()  # the next night
    assert await count(db, SecurityEvent) == 0 and len(await markers(db)) == 2


async def test_another_process_holding_the_lock_skips_the_tick(db: Any, clock: Clock) -> None:
    clock.at(get_settings().retention_hour_utc, 1)
    await add_events(db, 2, clock.now - 100 * DAY)
    async with dbmod.get_sessionmaker()() as other:
        await other.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _lock_id(retention.PERIODIC_NAME)})
        assert await tick() is False
        await other.rollback()
    assert await count(db, SecurityEvent) == 2 and not await markers(db)
    assert await tick() is True
    assert await count(db, SecurityEvent) == 0


async def test_an_unfinished_run_is_continued_by_the_next_tick(db: Any, clock: Clock,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    clock.at(get_settings().retention_hour_utc, 0)
    monkeypatch.setattr(get_settings(), "retention_batch", 2)
    monkeypatch.setattr(get_settings(), "retention_max_seconds", 150)
    ticks = itertools.count(0, 100)
    monkeypatch.setattr(retention, "_clock", lambda: float(next(ticks)))
    await add_events(db, 5, clock.now - 100 * DAY)
    assert await tick()
    assert await count(db, SecurityEvent) == 3 and not await markers(db)  # out of time: not marked done
    monkeypatch.setattr(get_settings(), "retention_max_seconds", 0)  # no budget
    clock.at(get_settings().retention_hour_utc + 1 if get_settings().retention_hour_utc < 23 else 23, 0)
    assert await tick()
    assert await count(db, SecurityEvent) == 0
    [marker] = await markers(db)
    assert marker.detail["deleted"]["security_events"] == 3
