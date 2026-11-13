"""Bounded background work: every periodic task, fan-out job and security loop walks its rows in chunks - one short
transaction each, a LIMIT in SQL, statements of bounded size - and is resumable and idempotent
(app/services/batching.py and its users).

Each test seeds more rows than one chunk (batch sizes are monkeypatched down), then checks that every row is handled,
in several bounded transactions, with a constant number of statements per chunk.
"""

from __future__ import annotations

import itertools
import json
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import event, func, insert, select, update
from sqlalchemy.orm import Session

from app import db as dbmod
from app.core.utils import utcnow
from app.db import QueryCounter
from app.models import (
    AuditLog,
    CustomerProfile,
    EmailOutbox,
    IpBan,
    Job,
    Notification,
    Plan,
    SecurityEvent,
    ShopOrder,
    Subscription,
    Task,
    TrainerClient,
    Transaction,
    User,
)
from app.models.enums import (
    IdStatus,
    JobStatus,
    PlanMode,
    PurchaseChannel,
    Role,
    Severity,
    ShiftMode,
    SubscriptionStatus,
    TaskKind,
    ThreatCategory,
    TxCategory,
    TxDirection,
    TxStatus,
)
from app.models.shop import OrderStatus
from app.security import bans
from app.services import accounts, member_notices
from app.services import checkout as svc
from app.services import health as hs
from app.services import payroll as pr
from app.services import security_triage as st
from app.services import shop_payments as pay
from app.services import tasks as task_service
from app.services.batching import Chunks
from tests.conftest import login_as, post_form, run_jobs
from tests.factories import make_branch, make_customer, make_employee, make_gym, make_owner, make_plans, make_subscription
from tests.test_superadmin import flash_text

_seq = itertools.count(1)
T0 = datetime(2026, 11, 10, 16, 35, tzinfo=UTC)  # 22:05 IST: inside the 22:00-22:30 reminder window of a 06:30 wake-up


# ============================================================================================ helpers
class SQL:
    """The statements executed inside the block: (single-line SQL, number of bind parameters)."""

    def __init__(self) -> None:
        self.items: list[tuple[str, int]] = []
        self._engine = dbmod.get_engine().sync_engine

    def __enter__(self) -> SQL:
        event.listen(self._engine, "before_cursor_execute", self._record)
        return self

    def __exit__(self, *exc: object) -> None:
        event.remove(self._engine, "before_cursor_execute", self._record)

    def _record(self, conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
        nested = executemany and parameters and isinstance(parameters[0], (list, tuple, dict))
        params = parameters[0] if nested else parameters  # an "insertmanyvalues" batch is one flat parameter list
        self.items.append((" ".join(statement.split()), len(params or ())))

    def where(self, *needles: str) -> list[str]:
        return [s for s, _ in self.items if all(n in s for n in needles)]

    def params(self, prefix: str) -> list[int]:
        return [n for s, n in self.items if s.startswith(prefix)]


class Commits:
    """Real COMMITs of any session inside the block (one per chunk transaction; SAVEPOINT releases don't count)."""

    def __init__(self) -> None:
        self.n = 0

    def __enter__(self) -> Commits:
        event.listen(Session, "after_commit", self._on)
        return self

    def __exit__(self, *exc: object) -> None:
        event.remove(Session, "after_commit", self._on)

    def _on(self, session: Session) -> None:
        if session.get_nested_transaction() is None:
            self.n += 1


async def seed_users(db: Any, n: int, *, role: Role = Role.CUSTOMER, profile: bool = True, **extra: Any) -> list[int]:
    """``n`` accounts in two statements (no password hashing), each with a customer profile."""
    k = next(_seq)
    ids = list((await db.execute(insert(User).returning(User.id, sort_by_parameter_order=True), [
        {"email": f"bulk{k}-{i}@example.com", "full_name": f"Member {k}-{i}", "role": role, "timezone": "Asia/Kolkata",
         "date_of_birth": date(1995, 5, 17), "id_status": IdStatus.VERIFIED, **extra} for i in range(n)
    ])).scalars().all())
    if profile:
        await db.execute(insert(CustomerProfile), [{"user_id": uid} for uid in ids])
    await db.commit()
    return ids


async def gym_with_members(db: Any, n: int, *, ends_on: date, plan_code: str = "online", branch: Any = None,
                           status: SubscriptionStatus = SubscriptionStatus.ACTIVE, created_at: datetime | None = None,
                           gym: Any = None) -> tuple[Any, dict[str, Any], list[int], list[int]]:
    if gym is None:
        gym = await make_gym(db, await make_owner(db))
    plans = {p.code: p for p in (await db.execute(select(Plan).where(Plan.gym_id == gym.id))).scalars()}
    if not plans:
        plans = {p.code: p for p in await make_plans(db, gym)}
    plan = plans[plan_code]
    members = await seed_users(db, n)
    rows = [{"customer_id": cid, "gym_id": gym.id, "plan_id": plan.id, "branch_id": branch.id if branch else None,
             "status": status, "channel": PurchaseChannel.ONLINE_CHECKOUT, "price": plan.price, "currency": plan.currency,
             "starts_on": ends_on - timedelta(days=30), "ends_on": ends_on,
             **({"created_at": created_at} if created_at else {})} for cid in members]
    subs = list((await db.execute(insert(Subscription).returning(Subscription.id, sort_by_parameter_order=True), rows))
                .scalars().all())
    await db.commit()
    return gym, plans, members, subs


# ============================================================================================ shared batching
async def test_chunks_stop_on_a_short_chunk_the_budget_or_the_cap():
    run = Chunks(budget_s=None, max_chunks=3)
    seen = 0
    while await run.next():
        seen += 1
        run.done(10, 10)  # full chunks: more may follow
    assert seen == 3 and run.stopped_early
    run = Chunks(budget_s=0)  # the first chunk always runs
    assert await run.next() and not await run.next()
    run = Chunks(budget_s=None)
    assert await run.next()
    run.done(3, 10)
    assert run.finished and not await run.next() and not run.stopped_early


# ============================================================================================ notify_bulk
async def test_notify_many_uses_a_constant_number_of_statements(db):
    from app.services.notify import notify_many

    few = await seed_users(db, 3)
    many = await seed_users(db, 150)
    with QueryCounter() as small:
        assert await notify_many(db, few, "Hello", "Body", link="/me") == 3
    await db.commit()
    with QueryCounter() as big, SQL() as sql:
        assert await notify_many(db, many, "Hello", "Body", link="/me") == 150
    await db.commit()
    assert small.count == big.count == 3  # the users, one notifications INSERT, one e-mail INSERT - for any audience
    assert len(sql.where("INSERT INTO notifications")) == 1 and len(sql.where("INSERT INTO email_outbox")) == 1
    assert not sql.where("UPDATE notifications")  # "emailed" is set in the INSERT itself
    assert await db.scalar(select(func.count(Notification.id)).where(Notification.emailed.is_(True))) == 153
    assert await db.scalar(select(func.count(EmailOutbox.id)).where(EmailOutbox.notification_id.is_not(None))) == 153


async def test_notify_bulk_keeps_the_rules_of_notify(db):
    from app.services.notify import Note, notify_bulk

    adult = await make_customer(db)
    minor = await make_customer(db, date_of_birth=date.today() - timedelta(days=365 * 15))
    (await db.get(CustomerProfile, minor.id)).guardian_email = "parent@example.com"
    gone = await make_customer(db)
    gone.deleted_at, gone.purge_after = utcnow(), utcnow() + timedelta(days=30)
    quiet = await make_customer(db)
    await db.commit()
    with QueryCounter() as qc:
        ids = await notify_bulk(db, [Note(adult.id, "A", "a"), Note(minor, "B", "b", link="/x"), Note(gone.id, "C", "c"),
                                     Note(quiet.id, "D", "d", email=False), Note(999_999, "E", "e")])
    await db.commit()
    assert len(ids) == 3 and qc.count == 4  # users, guardians (a minor is mailed), notifications, e-mails
    mails = {m.user_id: m for m in (await db.execute(select(EmailOutbox))).scalars()}
    assert set(mails) == {adult.id, minor.id}
    assert mails[minor.id].cc == ["parent@example.com"] and mails[adult.id].cc == []
    assert "http://testserver/x" in mails[minor.id].text_body and "guardian" in mails[minor.id].text_body
    notes = {n.user_id: n for n in (await db.execute(select(Notification))).scalars()}
    assert set(notes) == {adult.id, minor.id, quiet.id} and notes[quiet.id].emailed is False


# ============================================================================================ bedtime reminders
async def test_bedtime_reminders_walk_customers_in_bounded_chunks(db, monkeypatch):
    monkeypatch.setattr(hs, "BEDTIME_BATCH", 40)
    await seed_users(db, 150)
    quiet = await seed_users(db, 5)
    await db.execute(update(CustomerProfile).where(CustomerProfile.user_id.in_(quiet)).values(sleep_reminders_enabled=False))
    await db.commit()
    with SQL() as sql, Commits() as tx:
        assert await hs.bedtime_reminders(db, now=T0) == 150
    picks = sql.where("FROM users JOIN customer_profiles")
    assert len(picks) == 4 and all("LIMIT" in s for s in picks)  # 150 opted-in customers / 40 per chunk
    assert not any("health_embedding" in s or "ac_recommendation" in s for s in picks)  # only the columns it needs
    assert tx.n == 4 and len(sql.where("INSERT INTO notifications")) == 4  # one transaction + one bulk insert per chunk
    assert await db.scalar(select(func.count(Notification.id))) == 150
    assert await db.scalar(select(func.count()).select_from(CustomerProfile)
                           .where(CustomerProfile.last_bedtime_reminder_on == date(2026, 11, 10))) == 150
    assert await hs.bedtime_reminders(db, now=T0 + timedelta(minutes=5)) == 0  # once per evening


async def test_bedtime_run_out_of_budget_resumes_where_it_stopped(db, monkeypatch):
    monkeypatch.setattr(hs, "BEDTIME_BATCH", 2)
    monkeypatch.setattr(hs, "BEDTIME_BUDGET_S", 0)  # every run stops after its first chunk
    await seed_users(db, 5)
    assert [await hs.bedtime_reminders(db, now=T0) for _ in range(4)] == [2, 2, 1, 0]
    assert await db.scalar(select(func.count(Notification.id))) == 5


# ============================================================================================ task reminders
async def test_task_reminders_claim_due_rows_in_chunks_and_retire_dead_accounts(db, monkeypatch):
    monkeypatch.setattr(task_service, "REMINDER_BATCH", 40)
    users = await seed_users(db, 10)
    gone = await seed_users(db, 1, deleted_at=utcnow(), purge_after=utcnow() + timedelta(days=30), is_active=False)
    now = utcnow()
    await db.execute(insert(Task), [{"user_id": users[i % 10], "title": f"Ping {i}", "kind": TaskKind.REMINDER,
                                      "remind_at": now - timedelta(minutes=1 + i)} for i in range(150)])
    await db.execute(insert(Task), [{"user_id": gone[0], "title": "Ghost", "remind_at": now - timedelta(minutes=5)}])
    await db.commit()
    with SQL() as sql, QueryCounter() as qc:
        assert await task_service.send_task_reminders(db, now=now) == 150
    claims = sql.where("FROM tasks JOIN users", "FOR UPDATE OF tasks SKIP LOCKED")
    assert len(claims) == 4 and all("LIMIT" in s for s in claims)  # 151 due reminders / 40
    assert qc.count <= 4 * 4 + 1  # per chunk: claim, stamp, notifications, e-mails (+ keeping the lock alive once)
    assert await db.scalar(select(func.count(Notification.id))) == 150
    # the deleted account's reminder was stamped without a notification: it left the due set for good
    assert await db.scalar(select(func.count(Task.id)).where(Task.reminded_at.is_(None))) == 0
    assert await task_service.send_task_reminders(db, now=now) == 0


async def test_task_reminder_periodic_keeps_its_lock_across_chunks(db, monkeypatch):
    from app.services.jobs import registered_periodic, run_periodic

    monkeypatch.setattr(task_service, "REMINDER_BATCH", 2)
    users = await seed_users(db, 1)
    await db.execute(insert(Task), [{"user_id": users[0], "title": f"Ping {i}", "remind_at": utcnow() - timedelta(minutes=1)}
                                    for i in range(5)])
    await db.commit()
    with SQL() as sql:
        assert await run_periodic(registered_periodic()["task_reminders"]) is True
    # the advisory-lock transaction waits idle while 3 chunks commit: its idle limit was lifted once, before chunk 2
    assert len(sql.where("set_config('idle_in_transaction_session_timeout'")) == 1
    assert await db.scalar(select(func.count(Notification.id))) == 5


async def test_calendar_loads_at_most_one_capped_range_of_tasks(db, monkeypatch):
    monkeypatch.setattr(task_service, "RANGE_TASKS_MAX", 50)
    user = await make_customer(db, timezone="UTC")
    now = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    await db.execute(insert(Task), [{"user_id": user.id, "title": f"T{i}", "due_at": now + timedelta(hours=i % 72)}
                                    for i in range(150)])
    await db.commit()
    with SQL() as sql:
        view = await task_service.build_month(db, user, "2026-11", now=now)
    assert sum(len(d.items) for week in view.weeks for d in week) == 50 and view.truncated
    assert all("LIMIT" in s for s in sql.where("FROM tasks"))


# ============================================================================================ subscriptions (daily)
async def test_daily_expiry_runs_in_bounded_chunks(db, monkeypatch):
    monkeypatch.setattr(svc, "SUBS_BATCH", 40)
    today = date.today()
    gym, _, members, subs = await gym_with_members(db, 150, ends_on=today - timedelta(days=1))
    trainer = await make_employee(db, gym)
    await db.execute(insert(TrainerClient), [{"employee_id": trainer.id, "customer_id": c, "subscription_id": s,
                                              "mode": ShiftMode.ONLINE} for c, s in zip(members, subs, strict=True)])
    await db.commit()
    with SQL() as sql, Commits() as tx:
        assert await svc.expire_due_subscriptions(db, today) == 150
    updates = sql.where("UPDATE subscriptions SET status")
    assert len(updates) == 4 and all("LIMIT" in s and "SKIP LOCKED" in s for s in updates) and tx.n == 4
    assert max(sql.params("UPDATE trainer_clients")) <= 41  # one chunk's ids - never the whole day's cohort
    assert len(sql.where("INSERT INTO notifications")) == 4
    assert await db.scalar(select(func.count(Subscription.id)).where(Subscription.status == SubscriptionStatus.EXPIRED)) == 150
    assert await db.scalar(select(func.count(TrainerClient.id)).where(TrainerClient.active.is_(True))) == 0
    assert await db.scalar(select(func.count(Notification.id))
                           .where(Notification.title == "Your membership has expired")) == 150
    assert await svc.expire_due_subscriptions(db, today) == 0


async def test_expiry_reminders_are_chunked_and_sent_once(db, monkeypatch):
    monkeypatch.setattr(svc, "SUBS_BATCH", 40)
    today = date.today()
    await gym_with_members(db, 150, ends_on=today + timedelta(days=3))
    with SQL() as sql:
        assert await svc.send_expiry_reminders(db, today) == 150
    assert len(sql.where("INSERT INTO notifications")) == 4
    probe = sql.where("FROM notifications")  # "already reminded": an index range of recent notifications
    assert probe and all("notifications.created_at >=" in s for s in probe)
    assert await svc.send_expiry_reminders(db, today) == 0
    assert await db.scalar(select(func.count(Notification.id)).where(Notification.title == svc.REMINDER_TITLE)) == 150


async def test_stale_pending_purchases_are_cancelled_in_chunks(db, monkeypatch):
    monkeypatch.setattr(svc, "SUBS_BATCH", 40)
    gym, _, _, subs = await gym_with_members(db, 150, ends_on=date.today() + timedelta(days=30),
                                             status=SubscriptionStatus.PENDING_PAYMENT,
                                             created_at=utcnow() - timedelta(days=8))
    await db.execute(insert(Transaction), [
        {"gym_id": gym.id, "direction": TxDirection.INCOMING, "category": TxCategory.SUBSCRIPTION, "status": TxStatus.PENDING,
         "amount": Decimal("799"), "currency": "INR", "description": "x", "provider": "test", "subscription_id": s}
        for s in subs])
    await db.commit()
    with SQL() as sql:
        assert await svc.cancel_stale_pending(db) == 150
    assert len(sql.where("UPDATE subscriptions SET status")) == 4 and max(sql.params("UPDATE transactions")) <= 44
    assert await db.scalar(select(func.count(Transaction.id)).where(Transaction.status == TxStatus.CANCELLED)) == 150


async def test_daily_lifecycle_steps_do_not_block_each_other(db, monkeypatch):
    async def broken(*args: Any, **kwargs: Any) -> int:
        raise RuntimeError("expiry is broken")

    monkeypatch.setattr(svc, "expire_due_subscriptions", broken)
    await gym_with_members(db, 3, ends_on=date.today() + timedelta(days=30), status=SubscriptionStatus.PENDING_PAYMENT,
                           created_at=utcnow() - timedelta(days=8))
    await svc._subscriptions_daily(db)
    assert await db.scalar(select(func.count(Subscription.id)).where(Subscription.status == SubscriptionStatus.CANCELLED)) == 3


async def test_assign_trainer_counts_only_this_gyms_trainers(db):
    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    plans = {p.code: p for p in await make_plans(db, gym)}
    busy = await make_employee(db, gym)
    free = await make_employee(db, gym)
    elsewhere = await make_employee(db, await make_gym(db, owner))
    members = await seed_users(db, 30)
    await db.execute(insert(TrainerClient), [{"employee_id": elsewhere.id, "customer_id": c, "mode": ShiftMode.ONLINE}
                                             for c in members[:25]]
                     + [{"employee_id": busy.id, "customer_id": c, "mode": ShiftMode.ONLINE} for c in members[25:]])
    await db.commit()
    sub = await make_subscription(db, await make_customer(db), plans["online_trainer"])
    with SQL() as sql:
        trainer = await svc.assign_trainer(db, sub, plans["online_trainer"])
    assert trainer is not None and trainer.id == free.id
    pick = sql.where("FROM employees")[0]
    assert "GROUP BY" not in pick and "trainer_clients.employee_id = employees.id" in pick  # a per-candidate count


# ============================================================================================ gym-wide member notices
async def test_branch_close_queues_one_job_that_notifies_members_in_batches(client, db, monkeypatch):
    monkeypatch.setattr(member_notices, "MEMBERS_BATCH", 40)
    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    small, big = await make_branch(db, gym), await make_branch(db, gym)
    await gym_with_members(db, 2, ends_on=date.today() + timedelta(days=20), plan_code="offline", branch=small, gym=gym)
    await gym_with_members(db, 150, ends_on=date.today() + timedelta(days=20), plan_code="offline", branch=big, gym=gym)
    await login_as(client, owner)
    await client.get("/owner")
    counts = []
    for branch, n in ((small, 2), (big, 150)):
        with QueryCounter() as qc:
            r = await post_form(client, f"/owner/branches/{branch.id}/close", {"reason": "Deep cleaning"})
        assert r.status_code == 303 and f"{n} members will be notified." in flash_text(r)
        counts.append(qc.count)
    assert counts[0] == counts[1]  # the request never grows with the member count
    assert await db.scalar(select(func.count(Notification.id))) == 0  # nothing is sent inside the request
    jobs = (await db.execute(select(Job).where(Job.kind == "notify_members").order_by(Job.id))).scalars().all()
    assert [j.payload["branch_id"] for j in jobs] == [small.id, big.id]
    with SQL() as sql:
        assert await run_jobs() == 2
    assert len(sql.where("INSERT INTO notifications")) == 1 + 4  # 2 members: one chunk; 150 members: 4 chunks of 40
    assert await db.scalar(select(func.count(Notification.id)).where(Notification.title.like("%is closed"))) == 152


async def test_member_notice_retry_skips_members_already_told(db):
    gym, _, members, _ = await gym_with_members(db, 5, ends_on=date.today() + timedelta(days=20))
    kw: dict[str, Any] = dict(gym_id=gym.id, title="Online classes paused", body="Back soon.", link=f"/gyms/{gym.slug}",
                              severity=Severity.WARNING, since=utcnow() - timedelta(minutes=1), as_of=date.today(),
                              modes=[PlanMode.ONLINE, PlanMode.COMBO])
    assert await member_notices.send_member_notice(**kw) == 5
    assert await member_notices.send_member_notice(**kw) == 0  # a retried job carries on, it never repeats
    assert await db.scalar(select(func.count(Notification.id))) == 5


# ============================================================================================ payroll
async def test_monthly_generation_walks_gyms_in_chunks_and_skips_generated_ones(db, monkeypatch):
    monkeypatch.setattr(pr, "GEN_GYM_BATCH", 2)
    for _ in range(5):
        await make_employee(db, await make_gym(db, await make_owner(db)), hire_date=date(2020, 1, 1))
    with SQL() as sql, Commits() as tx:
        assert await pr.run_monthly_generation(db, today=date(2026, 11, 1)) == 5
    picks = sql.where("FROM gyms", "LIMIT")
    assert len(picks) == 3 and tx.n == 5  # 5 gyms picked 2 per chunk; one short transaction per gym
    assert await db.scalar(select(func.count(Notification.id))
                           .where(Notification.title == "Salaries for October 2026 are ready")) == 5
    with QueryCounter() as qc:
        assert await pr.run_monthly_generation(db, today=date(2026, 11, 1)) == 0
    assert qc.count == 1  # the other 23 runs of day 1: one query finds nothing left to generate


async def test_generate_statements_inserts_and_queues_in_bounded_statements(db, monkeypatch):
    from app.services.jobs import enqueue

    monkeypatch.setattr(pr, "STATEMENT_INSERT_ROWS", 2)
    monkeypatch.setattr(pr, "EXPLAIN_INSERT_ROWS", 2)
    gym = await make_gym(db, await make_owner(db))
    staff = await seed_users(db, 5, role=Role.STAFF, profile=False)
    for uid in staff:
        await make_employee(db, gym, user=await db.get(User, uid), hire_date=date(2020, 1, 1))
    with SQL() as sql:
        res = await pr.generate_statements(db, gym, 2026, 10)
    await db.commit()
    assert len(res.created_ids) == 5
    assert len(sql.where("INSERT INTO salary_statements")) == 3  # 2 + 2 + 1 rows
    assert len(sql.where("INSERT INTO jobs")) == 3 and not sql.where("FROM jobs")  # no lookups per statement
    jobs = (await db.execute(select(Job).where(Job.kind == "payroll_explain"))).scalars().all()
    assert sorted(j.dedupe_key for j in jobs) == sorted(f"payroll_explain:{i}" for i in res.created_ids)
    assert all(j.status == JobStatus.QUEUED and j.max_attempts == 2 and j.exit_policy == "keep" and j.user_id is None
               for j in jobs)
    again = await enqueue(db, "payroll_explain", {"statement_id": res.created_ids[0]},
                          dedupe_key=f"payroll_explain:{res.created_ids[0]}")
    assert again is not None and again.id in {j.id for j in jobs}  # the bulk rows are the jobs enqueue() knows


async def test_salary_reminders_are_sent_in_chunks(db, monkeypatch):
    from tests.test_payroll import add_statement, setup_staff

    monkeypatch.setattr(pr, "SALARY_REMINDER_BATCH", 2)
    owner, gym, emp = await setup_staff(db)
    for month in range(1, 6):  # five overdue statements
        await add_statement(db, emp, start=date(2026, month, 1), end=date(2026, month, 28),
                            due=date.today() - timedelta(days=1))
    with SQL() as sql:
        assert await pr.send_salary_reminders(db) == 5
    claims = sql.where("FROM salary_statements", "FOR UPDATE SKIP LOCKED")
    assert len(claims) == 3 and all("LIMIT" in s for s in claims)  # 5 statements / 2 per chunk
    assert len(sql.where("INSERT INTO notifications")) == 3  # one bulk insert per chunk
    assert await db.scalar(select(func.count(Notification.id)).where(Notification.user_id == owner.id)) == 5
    assert await pr.send_salary_reminders(db) == 0  # stamped: at most once a day


async def test_reused_reference_check_is_one_exists_probe(db, monkeypatch):
    from tests.test_payroll import add_statement, fake_ocr, setup_staff
    from tests.factories import png_bytes

    owner, gym, emp = await setup_staff(db)
    paid = await add_statement(db, emp, start=date(2026, 9, 1), end=date(2026, 9, 30))
    await db.execute(insert(Transaction), [  # the gym's earlier payouts, each with its own UTR
        {"gym_id": gym.id, "direction": TxDirection.OUTGOING, "category": TxCategory.SALARY, "status": TxStatus.SUCCESS,
         "amount": Decimal("100"), "currency": "INR", "description": "old salary", "provider": "manual",
         "provider_reference": f"utr-{i:08d}", "salary_statement_id": paid.id} for i in range(150)])
    await db.commit()
    st_ = await add_statement(db, emp, start=date(2026, 10, 1), end=date(2026, 10, 31))
    await pr.record_proof(db, st_, emp, owner, upload=png_bytes(), reference="UTR 0000 0149", amount_paid=st_.net_pay,
                          paid_on=date.today())
    await db.commit()
    fake_ocr(monkeypatch, f"UTR00000149 paid {st_.net_pay:,.2f} on {date.today():%Y-%m-%d}")
    with SQL() as sql:
        check = await pr.verify_payment_proof(db, st_.id)
    assert any(c["check"] == "duplicate_reference" for c in check.checks)  # utr-00000149 == UTR 0000 0149
    probes = sql.where("FROM transactions", "regexp_replace")
    assert len(probes) == 1 and "EXISTS" in probes[0]
    assert not sql.where("SELECT transactions.provider_reference")  # the gym's references are never loaded


# ============================================================================================ shop: stale unpaid orders
async def test_stale_unpaid_orders_keyset_past_flagged_orders(db, monkeypatch):
    from tests.test_shop import make_order, make_product, shop_setup

    _, gym, _ = await shop_setup(db)
    product = await make_product(db, gym)
    cust = await make_customer(db)
    flagged = [await make_order(db, cust, gym, [(product, 1)], status=OrderStatus.PAYMENT_FAILED, provider="cashfree")
               for _ in range(3)]
    stale = [await make_order(db, cust, gym, [(product, 1)], status=OrderStatus.PENDING_PAYMENT, provider="cashfree")
             for _ in range(5)]
    await db.execute(update(ShopOrder).where(ShopOrder.id.in_([o.id for o in flagged]))
                     .values(notes=f"{pay.MISMATCH_MARK} gateway reported another amount"))
    await db.execute(update(ShopOrder).values(created_at=utcnow() - timedelta(days=2)))
    await db.commit()
    polled: list[str] = []

    async def get_order(number: str) -> Any:
        polled.append(number)
        return SimpleNamespace(is_paid=False, is_dead=False, order_status="ACTIVE")

    monkeypatch.setattr(pay, "_cashfree", lambda: SimpleNamespace(get_order=get_order, create_order=None))
    sync = pay.sync_with_gateway

    async def sync_outside_a_transaction(s: Any, order: Any) -> str:
        assert not s.in_transaction(), "the gateway must be asked before the order's transaction begins"
        return await sync(s, order)

    monkeypatch.setattr(pay, "sync_with_gateway", sync_outside_a_transaction)
    assert await pay.expire_unpaid_orders(db, limit=2) == 5  # three chunks of two, past the flagged orders
    assert sorted(polled) == sorted(o.order_number for o in stale)  # flagged orders are left to the owner, not polled
    statuses = dict((await db.execute(select(ShopOrder.id, ShopOrder.status))).tuples().all())
    assert all(statuses[o.id] == OrderStatus.CANCELLED for o in stale)
    assert all(statuses[o.id] == OrderStatus.PAYMENT_FAILED for o in flagged)


# ============================================================================================ account purge
async def test_purge_runs_in_batches_owners_alone_and_deletes_files_after_commit(db, monkeypatch):
    from app.services import storage

    monkeypatch.setattr(accounts, "PURGE_BATCH", 3)
    past = utcnow() - timedelta(minutes=1)
    k = next(_seq)
    await db.execute(insert(User), [
        {"email": f"gone{k}-{i}@example.com", "full_name": f"Gone {i}", "role": Role.CUSTOMER, "deleted_at": past,
         "purge_after": past, "is_active": False, "photo_key": f"public/avatars/{k:016x}{i:016x}.jpg"} for i in range(7)])
    owner = await make_owner(db)
    await make_gym(db, owner)
    owner.deleted_at, owner.purge_after = past, past
    await db.commit()
    seen: list[tuple[str, int]] = []

    async def delete_key(key: str | None) -> None:
        async with dbmod.get_sessionmaker()() as s:  # another connection: is the row really gone (committed)?
            seen.append((key, await s.scalar(select(func.count(User.id)).where(User.photo_key == key))))

    monkeypatch.setattr(storage, "delete_key", delete_key)
    with SQL() as sql:
        result = await accounts.purge_due_accounts(db)
    assert result["users"] == 8
    assert sql.params("DELETE FROM users") == [3, 3, 1, 1]  # chunks of 3, the gym owner on their own
    assert len(seen) == 8 and all(still == 0 for _, still in seen)  # files go only after the rows are committed away
    assert await db.scalar(select(func.count(User.id))) == 0


# ============================================================================================ security: bans
async def test_security_flush_finalises_a_capped_batch_with_set_based_queries(db, monkeypatch):
    monkeypatch.setattr(bans, "MAX_BANS_PER_FLUSH", 100)
    for i in range(150):
        bans.security_events.request_ban(bans.PendingBan(f"198.18.{i // 200}.{i % 200 + 1}", 3, "probe", None))
    bans.security_events.request_ban(bans.PendingBan("198.18.0.1", 5, "again", None))  # same address once more
    with QueryCounter() as qc:
        assert await bans.flush_security_queue(db) == 100
    await db.commit()
    # the flush lock (serialises every worker's ban flushes), then active bans, 30-day offences, subscriber
    # addresses - for the whole batch
    assert qc.count == 4
    assert len(bans.security_events.bans) == 51  # the rest waits for the next flush
    assert await bans.flush_security_queue(db) == 50  # the duplicate address is banned already
    await db.commit()
    assert await db.scalar(select(func.count(IpBan.id))) == 150


async def test_security_flush_one_decision_per_address_and_subscriber_lookup(db):
    owner = await make_owner(db)
    plans = {p.code: p for p in await make_plans(db, await make_gym(db, owner))}
    member = await make_customer(db)
    member.last_login_ip = "203.0.113.9"
    await make_subscription(db, member, plans["online"])
    old = utcnow() - timedelta(days=3)
    for ip in ("203.0.113.9", "203.0.113.10"):  # one earlier, expired offence each
        db.add(IpBan(ip=ip, reason="old", severity=2, banned_until=old + timedelta(hours=2), created_at=old))
    await db.commit()
    for severity in (2, 4, 3):
        bans.security_events.request_ban(bans.PendingBan("203.0.113.9", severity, f"sev {severity}", None))
    bans.security_events.request_ban(bans.PendingBan("203.0.113.10", 3, "probe", None))
    assert await bans.flush_security_queue(db) == 2
    await db.commit()
    rows = {b.ip: b for b in (await db.execute(select(IpBan).where(IpBan.created_at > old))).scalars()}
    assert rows["203.0.113.9"].severity == 4 and rows["203.0.113.9"].had_subscription  # highest severity, one row
    assert rows["203.0.113.9"].banned_until is not None  # a subscriber: temporary even as a repeat offender
    assert rows["203.0.113.10"].banned_until is None and rows["203.0.113.10"].offense_count == 2  # permanent


async def test_ban_sync_is_incremental_after_the_first_full_load(db, monkeypatch):
    monkeypatch.setattr(bans, "SYNC_ROWS", 40)
    now = utcnow()
    await db.execute(insert(IpBan), [{"ip": f"198.19.0.{i}", "reason": "x" * 300, "severity": 3, "created_by": "auto",
                                      "banned_until": now + timedelta(hours=2), "created_at": now - timedelta(days=1)}
                                     for i in range(1, 51)])
    await db.commit()
    with SQL() as sql:
        assert await bans.sync_bans_from_db(db) == 50  # start-up: the whole active set, streamed
    assert not any("ip_bans.reason" in s for s in sql.where("FROM ip_bans"))  # three columns, never the reason
    assert bans.ban_registry.get("198.19.0.7") is not None
    await db.execute(insert(IpBan), [{"ip": f"198.20.{i // 200}.{i % 200 + 1}", "reason": "x", "severity": 3,
                                      "created_by": "auto", "banned_until": now + timedelta(hours=2)} for i in range(150)])
    await db.commit()
    with SQL() as sql:
        assert await bans.sync_bans_from_db(db) == 150  # only the new rows ...
    reads = sql.where("FROM ip_bans", "ip_bans.id >")
    assert len(reads) == 4 and all("LIMIT" in s for s in reads)  # ... 40 per query
    assert not sql.where("ip_bans.lifted_at IS NULL")  # and no reload of the active set
    assert bans.ban_registry.get("198.20.0.150") is not None
    with SQL() as sql:
        assert await bans.sync_bans_from_db(db) == 150  # quiet: the recent rows are re-read (late commits), nothing more
    assert len(sql.items) == 4 + 1  # the 150 recent rows, 40 per query, + one look at the audit trail


async def test_ban_sync_applies_lifts_extensions_and_late_commits(db):
    now = utcnow()
    old = now - timedelta(days=1)
    db.add_all([IpBan(id=500, ip="198.21.0.1", reason="x", severity=3, banned_until=now + timedelta(hours=2), created_at=old),
                IpBan(id=501, ip="198.21.0.2", reason="x", severity=3, banned_until=now + timedelta(hours=2), created_at=old)])
    await db.commit()
    await bans.sync_bans_from_db(db)
    # lifted in another process (the console audits it): this process still holds the ban
    async with dbmod.get_sessionmaker()() as s:
        await st.lift_ip_bans(s, "198.21.0.1", actor=None, note="false positive")
        await s.commit()
    bans.ban_registry.put("198.21.0.1", bans.BanInfo(until=now + timedelta(hours=2), reason="", severity=3))
    # extended in another process (triage): banned_until moves, the audit row names the address
    longer = now + timedelta(hours=20)
    await db.execute(update(IpBan).where(IpBan.id == 501).values(banned_until=longer))
    db.add(AuditLog(action="security.triage_extended", entity_type="ip_ban", entity_id=501, detail={"ip": "198.21.0.2"}))
    await db.commit()
    db.add(IpBan(id=1000, ip="198.21.0.3", reason="x", severity=3, banned_until=now + timedelta(hours=2)))
    await db.commit()
    await bans.sync_bans_from_db(db)
    assert bans.ban_registry.get("198.21.0.1") is None
    assert bans.ban_registry.get("198.21.0.2").until == longer
    assert bans.ban_registry.get("198.21.0.3") is not None
    # a ban whose (earlier) id commits after a later one: still inside the re-read window
    db.add(IpBan(id=700, ip="198.21.0.4", reason="x", severity=3, banned_until=now + timedelta(hours=2)))
    await db.commit()
    await bans.sync_bans_from_db(db)
    assert bans.ban_registry.get("198.21.0.4") is not None


# ============================================================================================ security: triage
async def test_stale_unreviewed_events_expire_in_chunks(db, monkeypatch):
    monkeypatch.setattr(st, "EXPIRE_BATCH", 40)
    old, fresh = utcnow() - timedelta(hours=49), utcnow() - timedelta(hours=1)
    await db.execute(insert(SecurityEvent), [
        {"ip": f"198.51.100.{i % 200}", "method": "GET", "path": "/x", "category": ThreatCategory.PROBE,
         "severity": 1 + i % 4, "created_at": old if i < 150 else fresh} for i in range(155)])
    await db.commit()
    with SQL() as sql:
        assert await st.expire_stale_events() == 150
    assert len(sql.where("UPDATE security_events")) == 4
    verdicts = (await db.execute(select(SecurityEvent.ai_verdict).where(SecurityEvent.ai_reviewed.is_(True)))).scalars().all()
    assert len(verdicts) == 150 and {json.loads(v)["verdict"] for v in verdicts} == {"expired"}
    assert await db.scalar(select(func.count(SecurityEvent.id)).where(SecurityEvent.ai_reviewed.is_(False))) == 5


async def test_pending_count_is_capped(db, monkeypatch):
    monkeypatch.setattr(st, "PENDING_COUNT_CAP", 10)
    await db.execute(insert(SecurityEvent), [{"ip": "198.51.100.1", "method": "GET", "path": "/x", "severity": 3,
                                              "category": ThreatCategory.PROBE} for _ in range(25)])
    await db.commit()
    with SQL() as sql:
        assert await st._pending_count(db) == 10
    assert "LIMIT" in sql.items[0][0]
    report = st.TriageReport(mode="ai", provider="gateway", events_reviewed=1, ips_reviewed=1, events_pending=10)
    assert "10+ event(s) left for the next run" in report.summary()


async def test_periodic_triage_reviews_the_backlog_in_several_batches(db, fake_ai):
    await db.execute(insert(SecurityEvent), [{"ip": f"198.51.{i // 200}.{i % 200 + 1}", "method": "GET", "path": "/x",
                                              "severity": 2, "category": ThreatCategory.PROBE} for i in range(120)])
    await db.commit()
    calls: list[int] = []

    def handler(**kw: Any) -> str:
        ips = re.findall(r'"ip":"([^"]+)"', kw["prompt"])
        calls.append(len(ips))
        return json.dumps({"ips": [{"ip": ip, "verdict": "benign", "recommended_action": "none", "severity": 1,
                                    "rationale": "Normal client."} for ip in ips]})

    fake_ai(handler=handler)
    with Commits() as tx:
        assert await st._periodic_triage(db) == 120  # one run: several batches (the AI sees <= 20 IPs per call)
    assert len(calls) >= 6 and tx.n >= len(calls)  # each batch committed on its own
    assert await db.scalar(select(func.count(SecurityEvent.id)).where(SecurityEvent.ai_reviewed.is_(False))) == 0


@pytest.mark.parametrize("name", ["bedtime_reminders", "task_reminders", "subscriptions_daily", "payroll_monthly_generation",
                                  "shop_expire_unpaid", "purge_deleted_accounts", "security_triage"])
def test_batched_periodics_are_registered(name):
    from app.services.jobs import registered_periodic

    assert name in registered_periodic()
