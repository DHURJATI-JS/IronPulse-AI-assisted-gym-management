"""Finance and payroll at scale (load-safety round, owner-finance).

* The three transaction indexes exist, are built by ``sync_schema`` / ``create_indexes_concurrently``, and serve the
  queries the pages really send: each EXPLAIN runs the statement captured from the route or job, with its own bind
  values, on a seeded ledger (several gyms, three years, other owners' rows around it).
* The transactions page reads one bounded period (last 12 months by default) and its totals stop at TOTALS_ROW_CAP.
* Payroll generation works for a gym with 2,000+ staff in bounded statements; the monthly run commits each gym on
  its own; "run payroll" for all of an owner's gyms is a background job with progress.
* No AI or OCR call holds a database transaction (or a row lock) open: the stand-ins check pg_stat_activity.
"""

from __future__ import annotations

import csv
import io
import json
import math
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import event, func, insert, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app import db as dbmod
from app.core.utils import utcnow
from app.db import QueryCounter
from app.models import AuditLog, Employee, Job, Notification, SalaryStatement, Transaction, User
from app.models.enums import (
    EmployeeRole,
    EmploymentStatus,
    IdStatus,
    JobStatus,
    Role,
    StatementStatus,
    TxCategory,
    TxDirection,
    TxStatus,
    VerificationStatus,
)
from app.routers import owner_finance
from app.services import payroll as pr
from app.services.batching import slices
from tests.conftest import login_as, post_form, run_jobs
from tests.factories import make_gym, make_owner, make_user, png_bytes

PREV = date.today().replace(day=1) - timedelta(days=1)  # last month: what "run payroll" offers by default
INDEXES = {  # name -> fragments of its definition (pg_indexes.indexdef)
    "ix_transactions_gym_booked": ("(gym_id, COALESCE(completed_at, created_at) DESC, id DESC)",),
    "ix_transactions_direction_status_booked": ("(direction, status, COALESCE(completed_at, created_at))",),
    "ix_transactions_proof_sha256": ("((meta -> 'proof'::text) ->> 'sha256'::text)",
                                     "WHERE (((meta -> 'proof'::text) ->> 'sha256'::text) IS NOT NULL)"),
}


# ============================================================================================ helpers
def flash_text(response: Any) -> str:
    """The flash message(s) of a redirect, decoded from the signed flash cookie (as tests/test_superadmin.py does)."""
    from itsdangerous import URLSafeSerializer

    from app.config import get_settings
    from app.core.templating import FLASH_COOKIE

    raw = response.cookies.get(FLASH_COOKIE)
    if not raw:
        return ""
    data = URLSafeSerializer(get_settings().secret_key, salt="ironpulse-flash").loads(raw)
    return " | ".join(f"{d['c']}: {d['m']}" for d in data)


class Sent:
    """Every SQL statement (with its bind values) sent while the block runs."""

    def __init__(self) -> None:
        self.items: list[tuple[str, Any]] = []

    def _on(self, conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        self.items.append((statement, parameters))

    def __enter__(self) -> Sent:
        event.listen(dbmod.get_engine().sync_engine, "before_cursor_execute", self._on)
        return self

    def __exit__(self, *exc: object) -> None:
        event.remove(dbmod.get_engine().sync_engine, "before_cursor_execute", self._on)

    def find(self, *needles: str) -> list[tuple[str, Any]]:
        return [(s, p) for s, p in self.items if all(n in s for n in needles)]


class Commits:
    """Real COMMITs of any session inside the block (SAVEPOINT releases don't count)."""

    def __init__(self) -> None:
        self.n = 0

    def _on(self, session: Session) -> None:
        self.n += 1

    def __enter__(self) -> Commits:
        event.listen(Session, "after_commit", self._on)
        return self

    def __exit__(self, *exc: object) -> None:
        event.remove(Session, "after_commit", self._on)


async def plan_of(sql: str, params: Any) -> str:
    """EXPLAIN of a statement exactly as the app sent it - same SQL text, same bind values - with sequential scans
    off (a test table is small; the question is which index the planner picks for this query)."""
    async with dbmod.get_engine().connect() as conn:
        await conn.exec_driver_sql("SET enable_seqscan = off")
        rows = (await conn.exec_driver_sql("EXPLAIN " + sql, tuple(params or ()))).all()
    return "\n".join(r[0] for r in rows)


async def analyze() -> None:
    async with dbmod.get_engine().connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.exec_driver_sql("ANALYZE transactions")


async def seed_ledger(db, gym_ids: list[int], per_gym: int, *, years: int = 3, proof_every: int = 25) -> None:
    """``per_gym`` transactions per gym spread over ``years`` (every 10th one failed and never completed, every
    ``proof_every``th carrying a payment proof hash), inserted oldest first with the gyms interleaved - the physical
    layout of a real ledger, which grows over time from every gym at once."""
    now = utcnow()
    span = timedelta(days=365 * years)
    rows = []
    for g in gym_ids:
        for i in range(per_gym):
            booked = now - span * (i / per_gym) - timedelta(minutes=1)
            rows.append({
                "gym_id": g, "direction": TxDirection.OUTGOING if i % 3 == 0 else TxDirection.INCOMING,
                "category": TxCategory.SALARY if i % 3 == 0 else TxCategory.SUBSCRIPTION,
                "status": TxStatus.FAILED if i % 10 == 0 else TxStatus.SUCCESS, "amount": Decimal(100 + i % 50),
                "currency": "INR", "description": f"Ledger {g}-{i}", "provider": "test",
                "completed_at": None if i % 10 == 0 else booked, "created_at": booked,
                "meta": {"proof": {"sha256": f"{g:08d}{i:056d}"}} if i % proof_every == 0 else {},
            })
    rows.sort(key=lambda r: r["created_at"])
    for part in slices(rows, 2000):
        await db.execute(insert(Transaction), list(part))
    await db.commit()


async def idle_transactions() -> list[str]:
    """Connections to this test database sitting inside an open transaction right now (other than the probe)."""
    async with dbmod.get_engine().connect() as conn:
        rows = (await conn.execute(text(
            "SELECT state, left(query, 160) FROM pg_stat_activity WHERE datname = current_database() "
            "AND pid <> pg_backend_pid() AND state LIKE 'idle in transaction%'"))).all()
    return [f"{state}: {query}" for state, query in rows]


async def bulk_staff(db, gym, n: int, *, start: int = 0) -> None:
    """``n`` payable (ACTIVE, so with a linked staff account) employees of ``gym``, bulk-inserted."""
    people = [{"email": f"bulk{i}-{gym.id}@staff.example.com", "full_name": f"Bulk Staff {i:05d}", "role": Role.STAFF,
               "id_status": IdStatus.VERIFIED, "timezone": "Asia/Kolkata"} for i in range(start, start + n)]
    user_ids: list[int] = []
    for part in slices(people, 1000):
        user_ids += list((await db.execute(insert(User).returning(User.id), list(part))).scalars().all())
    rows = [{"gym_id": gym.id, "user_id": uid, "invite_email": p["email"], "full_name": p["full_name"],
             "role": EmployeeRole.RECEPTIONIST, "status": EmploymentStatus.ACTIVE, "country_code": "IN",
             "currency": "INR", "monthly_salary": Decimal("20000"), "hire_date": date(2024, 1, 1),
             "photo_key": f"public/staff/{uid:032x}.jpg", "id_status": IdStatus.VERIFIED,
             "verification_status": VerificationStatus.VERIFIED} for uid, p in zip(user_ids, people)]
    for part in slices(rows, 1000):
        await db.execute(insert(Employee), list(part))
    await db.commit()


def task_updates(monkeypatch) -> list[dict[str, Any]]:
    """Capture the WebSocket events the hub would publish (task.update progress and outcome)."""
    from app.services.realtime import hub

    seen: list[dict[str, Any]] = []
    real = hub.publish_nowait

    def record(user_ids, evt):  # noqa: ANN001
        seen.append({"to": list(user_ids), **evt})
        return real(user_ids, evt)

    monkeypatch.setattr(hub, "publish_nowait", record)
    return seen


# ============================================================================================ 1. indexes
async def index_defs() -> dict[str, str]:
    async with dbmod.get_engine().connect() as conn:
        rows = (await conn.execute(text("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' "
                                        "AND tablename = 'transactions' AND indexname = ANY(:n)"),
                                   {"n": list(INDEXES)})).all()
    return dict(rows)


async def test_the_transaction_indexes_are_declared_built_and_prebuilt_concurrently():
    defs = await index_defs()
    for name, fragments in INDEXES.items():
        assert name in defs and all(f in defs[name] for f in fragments), (name, defs.get(name))
    # an existing database gets them from sync_schema (additive, idempotent) ...
    async with dbmod.get_engine().begin() as conn:
        for name in INDEXES:
            await conn.execute(text(f'DROP INDEX IF EXISTS "{name}"'))
        applied = await dbmod.sync_schema(conn)
    assert {f"index {name}" for name in INDEXES} <= set(applied)
    assert set(await index_defs()) == set(INDEXES)
    async with dbmod.get_engine().begin() as conn:
        assert not [a for a in await dbmod.sync_schema(conn) if "transactions" in a]
    # ... and a large production table gets them without a write lock, before the deploy
    async with dbmod.get_engine().begin() as conn:
        for name in INDEXES:
            await conn.execute(text(f'DROP INDEX IF EXISTS "{name}"'))
    assert sorted(await dbmod.create_indexes_concurrently(dbmod.get_engine())) == sorted(INDEXES)
    assert (await index_defs()).keys() == INDEXES.keys()


@pytest.fixture
async def ledger(db):
    """Owner with two gyms (3,000 transactions each over three years) among three other owners' gyms."""
    owner = await make_owner(db)
    gyms = [await make_gym(db, owner, name="Ledger North"), await make_gym(db, owner, name="Ledger South")]
    others = [await make_gym(db, await make_owner(db)) for _ in range(3)]
    await seed_ledger(db, [g.id for g in gyms], 3000)
    await seed_ledger(db, [g.id for g in others], 2000)
    await analyze()
    return SimpleNamespace(owner=owner, gyms=gyms, others=others)


async def test_the_finance_pages_read_the_ledger_through_the_gym_booked_index(client, ledger, monkeypatch):
    """The recent list, the 12-month chart, the transactions page (rows + totals of the period) and every CSV batch -
    for an owner with TWO gyms, where ``gym_id IN (a, b) ORDER BY booked`` could not use the index order."""
    monkeypatch.setattr(owner_finance, "EXPORT_BATCH", 500)
    await login_as(client, ledger.owner)
    with Sent() as sent:
        assert (await client.get("/owner/finance")).status_code == 200
        assert (await client.get("/owner/finance/transactions?page=3")).status_code == 200
        r = await client.get("/owner/finance/transactions.csv?period=all")
        assert r.status_code == 200 and len(list(csv.reader(io.StringIO(r.text.lstrip("﻿"))))) == 6001
    recent = sent.find("JOIN LATERAL", "FROM transactions", "LIMIT")
    chart = sent.find("date_trunc", "FROM transactions")
    totals = sent.find("count(*)", "FROM transactions", "LIMIT")
    batches = [(s, p) for s, p in sent.find("JOIN LATERAL", "FROM transactions", "booked_at")
               if "transactions.id) <" in s]
    assert recent and chart and totals and len(batches) == 12  # 6,000 rows / 500: batches 2..13 carry the cursor
    plans = {label: await plan_of(sql, params) for label, (sql, params) in (
        ("recent list", recent[0]), ("page rows", recent[1]), ("12-month chart", chart[0]), ("period totals", totals[0]),
        ("csv batch 2", batches[0]), ("csv batch 13", batches[-1]))}
    assert all("ix_transactions_gym_booked" in plan for plan in plans.values()), plans
    assert "COALESCE(completed_at, created_at) >=" in plans["page rows"]  # the period is an index condition too
    # the CSV cursor is part of the index condition: every batch is a short range scan, never a re-read
    deep = plans["csv batch 13"]
    assert any("Index Cond" in line and "ROW(COALESCE" in line for line in deep.splitlines()), deep


async def test_the_owner_dashboard_revenue_uses_the_gym_booked_index(client, ledger):
    await login_as(client, ledger.owner)
    with Sent() as sent:
        assert (await client.get("/owner")).status_code == 200
    (sql, params), = sent.find("sum(transactions.amount)", "FROM transactions")
    assert "ix_transactions_gym_booked" in await plan_of(sql, params)


async def test_the_superadmin_30_day_revenue_uses_the_direction_status_booked_index(client, db, ledger):
    await login_as(client, await make_user(db, role=Role.SUPERADMIN))
    with Sent() as sent:
        assert (await client.get("/superadmin")).status_code == 200
    (sql, params), = sent.find("sum(transactions.amount)", "FROM transactions", "transactions.category")
    plan = await plan_of(sql, params)
    assert "ix_transactions_direction_status_booked" in plan, plan


async def test_the_payment_proof_duplicate_check_uses_the_sha256_index(db, ledger, monkeypatch):
    from tests.test_payroll import add_statement, fake_ocr, setup_staff

    owner, gym, emp = await setup_staff(db)
    st = await add_statement(db, emp, start=date(2026, 10, 1), end=date(2026, 10, 31))
    await pr.record_proof(db, st, emp, owner, upload=png_bytes(color="teal"), reference="UTR55554444",
                          amount_paid=st.net_pay, paid_on=date.today())
    await db.commit()
    await analyze()
    fake_ocr(monkeypatch, f"UTR55554444 paid {st.net_pay:,.2f} on {date.today():%Y-%m-%d}")
    with Sent() as sent:
        assert (await pr.verify_payment_proof(db, st.id)).passed
    (sql, params), = sent.find("'sha256'", "FROM transactions")
    assert "((transactions.meta -> 'proof') ->> 'sha256')" in sql  # literal keys: the indexed expression
    plan = await plan_of(sql, params)
    assert "ix_transactions_proof_sha256" in plan, plan


# ============================================================================================ 2. bounded period
async def test_the_transactions_page_shows_one_period_and_the_csv_follows_it(client, db):
    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    now = utcnow()
    for days, amount in ((5, "100"), (200, "200"), (500, "400"), (1000, "800")):
        db.add(Transaction(gym_id=gym.id, direction=TxDirection.INCOMING, category=TxCategory.SUBSCRIPTION,
                           status=TxStatus.SUCCESS, amount=Decimal(amount), currency="INR",
                           description=f"Paid {days} days ago", provider="test",
                           completed_at=now - timedelta(days=days)))
    await db.commit()
    await login_as(client, owner)
    r = await client.get("/owner/finance/transactions")
    assert "Showing 1-2 of 2" in r.text and "Paid 500 days ago" not in r.text
    assert "₹300.00" in r.text and "INR · completed · Last 12 months" in r.text  # totals of the period only
    assert 'href="/owner/finance/transactions?period=all"' in r.text
    assert 'aria-current="true">Last 12 months</a>' in r.text
    r = await client.get("/owner/finance/transactions?period=all")
    assert "Showing 1-4 of 4" in r.text and "₹1,500.00" in r.text and 'aria-current="true">All time</a>' in r.text
    assert 'name="period" value="all"' in r.text  # the filter form keeps the period
    assert "Showing 1-1 of 1" in (await client.get("/owner/finance/transactions?period=30d")).text
    since, until = (date.today() - timedelta(days=1100)).isoformat(), (date.today() - timedelta(days=400)).isoformat()
    r = await client.get(f"/owner/finance/transactions?from={since}&to={until}&period=30d")  # dates win
    assert "Showing 1-2 of 2" in r.text and "Paid 1000 days ago" in r.text and "Paid 5 days ago" not in r.text
    assert "₹1,200.00" in r.text
    default_csv = list(csv.reader(io.StringIO((await client.get("/owner/finance/transactions.csv")).text.lstrip("﻿"))))
    all_csv = list(csv.reader(io.StringIO((await client.get(
        "/owner/finance/transactions.csv?period=all")).text.lstrip("﻿"))))
    assert len(default_csv) == 1 + 2 and len(all_csv) == 1 + 4


async def test_an_owner_without_recent_transactions_is_offered_all_time(client, db):
    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    db.add(Transaction(gym_id=gym.id, direction=TxDirection.INCOMING, category=TxCategory.SUBSCRIPTION,
                       status=TxStatus.SUCCESS, amount=Decimal("50"), currency="INR", description="Old payment",
                       provider="test", completed_at=utcnow() - timedelta(days=700)))
    await db.commit()
    await login_as(client, owner)
    r = await client.get("/owner/finance/transactions")
    assert "No transactions in the last 12 months" in r.text and "Show all time" in r.text
    assert "Old payment" in (await client.get("/owner/finance/transactions?period=all")).text


async def test_the_totals_read_at_most_the_cap_and_never_show_partial_sums(client, db, monkeypatch):
    """Before: count + sums by direction/currency over the owner's whole matching history on every page view."""
    monkeypatch.setattr(owner_finance, "TOTALS_ROW_CAP", 5)
    monkeypatch.setattr(owner_finance, "PAGE_SIZE", 2)
    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    await seed_ledger(db, [gym.id], 8, years=0)  # all eight inside the default period
    await login_as(client, owner)
    with Sent() as sent:
        r = await client.get("/owner/finance/transactions")
    (sql, params), = sent.find("count(*)", "FROM transactions")
    assert "LIMIT" in sql and 6 in list(params)  # TOTALS_ROW_CAP + 1 rows at most
    assert "coalesce(transactions.completed_at, transactions.created_at) >=" in sql  # the default period bounds it
    assert "Showing 1-2 of 5+" in r.text and "More than 5 transactions match" in r.text
    assert "· completed ·" not in r.text  # no sums of an arbitrary part of the matches
    assert "Page 1 of 3+" in r.text
    last = await client.get("/owner/finance/transactions?page=99")
    assert "Page 3 of 3+" in last.text and "The list stops after 5 transactions" in last.text
    exact = await client.get("/owner/finance/transactions?direction=outgoing")  # 3 outgoing rows: under the cap
    assert "· completed ·" in exact.text and "More than 5" not in exact.text


async def test_the_transactions_page_reads_one_page_per_gym_and_constant_statements(client, db):
    owner = await make_owner(db)
    gyms = [await make_gym(db, owner) for _ in range(2)]
    await login_as(client, owner)
    counts = []
    for n in (30, 300):
        await seed_ledger(db, [g.id for g in gyms], n, years=0)
        await client.get("/owner/finance/transactions")
        with QueryCounter() as qc, Sent() as sent:
            assert (await client.get("/owner/finance/transactions?page=2")).status_code == 200
        counts.append(qc.count)
        (sql, params), = sent.find("JOIN LATERAL", "FROM transactions")
        assert owner_finance.PAGE_SIZE * 2 in list(params)  # each gym gives at most offset + one page
    assert counts[0] == counts[1]


# ============================================================================================ 3. payroll at scale
async def test_a_gym_with_2050_staff_gets_its_statements_in_bounded_statements(db):
    """Before the chunking, one multi-row VALUES of ~22 bind parameters per row failed above ~1,638 people (asyncpg
    caps a statement at 32,767 parameters) and the coverage lookups bound every id of the gym in one IN list."""
    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    await bulk_staff(db, gym, 2050)
    with Sent() as sent:
        res = await pr.generate_statements(db, gym, PREV.year, PREV.month)
    await db.commit()
    assert len(res.created_ids) == 2050 and not res.skipped
    sizes = [len(p or ()) for _s, p in sent.items]
    assert max(sizes) < 32_767
    inserts = sent.find("INSERT INTO salary_statements")
    assert len(inserts) == math.ceil(2050 / pr.STATEMENT_INSERT_ROWS)
    lookups = sent.find("FROM salary_statements", "salary_statements.employee_id IN")
    assert len(lookups) == 2 * math.ceil(2050 / pr.ID_LIST_CHUNK)  # coverage + settlements, 1,000 ids per list
    assert all(len(p) <= pr.ID_LIST_CHUNK + 10 for _s, p in lookups)
    assert await db.scalar(select(func.count(SalaryStatement.id)).where(SalaryStatement.gym_id == gym.id)) == 2050
    explained = await db.scalar(select(func.count(Job.id)).where(Job.kind == pr.EXPLAIN_JOB))
    assert explained == pr.EXPLAIN_PER_RUN  # not 2,050 AI jobs for one gym
    again = await pr.generate_statements(db, gym, PREV.year, PREV.month)
    assert again.created_ids == [] and len(again.skipped) == 2050


async def test_the_monthly_run_commits_every_gym_in_its_own_short_transaction(db, monkeypatch):
    """Before: a chunk of GEN_GYM_BATCH gyms (up to 25 x their whole staff) was one transaction."""
    monkeypatch.setattr(pr, "GEN_GYM_BATCH", 3)
    monkeypatch.setattr(pr, "EXPLAIN_PER_RUN", 2)
    gyms = []
    for k in range(3):
        gym = await make_gym(db, await make_owner(db), name=f"Monthly Gym {k}")
        await bulk_staff(db, gym, 3, start=10 * k)
        gyms.append(gym)
    await db.commit()
    with Commits() as commits, Sent() as sent:
        assert await pr.run_monthly_generation(db, today=date(2026, 11, 1)) == 9
    assert commits.n == 3  # one per gym (the picks run on the periodic's own lock-holding session)
    assert len(sent.find("FROM gyms", "LIMIT")) == 2  # one full chunk of 3 ids, then an empty pick ends the run
    assert await db.scalar(select(func.count(Job.id)).where(Job.kind == pr.EXPLAIN_JOB)) == 3 * 2
    notes = (await db.execute(select(Notification.user_id).where(
        Notification.title == "Salaries for October 2026 are ready"))).scalars().all()
    assert sorted(notes) == sorted(g.owner_id for g in gyms)


async def test_a_gym_that_fails_in_the_monthly_run_rolls_back_alone(db, monkeypatch):
    gyms = []
    for k in range(3):
        gym = await make_gym(db, await make_owner(db), name=f"Fragile Gym {k}")
        await bulk_staff(db, gym, 2, start=10 * k)
        gyms.append(gym)
    real = pr.generate_statements

    async def failing_second(session, gym, year, month, **kw):  # noqa: ANN001
        res = await real(session, gym, year, month, **kw)  # its rows are written ... then the gym fails
        if gym.id == gyms[1].id:
            raise DBAPIError("INSERT ...", {}, Exception("connection reset"))
        return res

    monkeypatch.setattr(pr, "generate_statements", failing_second)
    assert await pr.run_monthly_generation(db, today=date(2026, 11, 1)) == 4
    made = dict((await db.execute(select(SalaryStatement.gym_id, func.count(SalaryStatement.id))
                                  .group_by(SalaryStatement.gym_id))).all())
    assert made == {gyms[0].id: 2, gyms[2].id: 2}
    monkeypatch.setattr(pr, "generate_statements", real)
    assert await pr.run_monthly_generation(db, today=date(2026, 11, 1)) == 2  # the next hourly run picks it up


async def test_run_payroll_for_all_gyms_is_a_background_job_with_progress(client, db, monkeypatch):
    """Before: the request looped over every gym of the owner (each with its whole staff) before answering."""
    monkeypatch.setattr(pr, "EXPLAIN_PER_RUN", 3)
    events = task_updates(monkeypatch)
    owner = await make_owner(db)
    gyms = [await make_gym(db, owner, name=f"Chain Gym {k}") for k in range(3)]
    for k, gym in enumerate(gyms):
        await bulk_staff(db, gym, 2, start=10 * k)
    await login_as(client, owner)
    r = await post_form(client, "/owner/payroll/generate", {"month": f"{PREV:%Y-%m}", "gym": ""})
    assert r.status_code == 303 and r.headers["location"] == "/owner/payroll"
    assert "Payroll is being generated for 3 gyms" in flash_text(r)
    assert await db.scalar(select(func.count(SalaryStatement.id))) == 0  # nothing generated inside the request
    job = (await db.execute(select(Job).where(Job.kind == pr.GENERATE_JOB))).scalar_one()
    assert job.user_id == owner.id and job.status == JobStatus.QUEUED and job.payload["gyms"] == 3
    again = await post_form(client, "/owner/payroll/generate", {"month": f"{PREV:%Y-%m}", "gym": ""})
    assert "already being generated" in flash_text(again)
    assert await db.scalar(select(func.count(Job.id)).where(Job.kind == pr.GENERATE_JOB)) == 1
    page = await client.get("/owner/payroll")
    assert 'data-component="payroll-run"' in page.text and f'data-task="{job.id}"' in page.text
    assert "is being generated" in page.text and "<progress" in page.text
    status = (await client.get(f"/owner/payroll/runs/{job.id}")).json()
    assert status["status"] == "queued" and status["progress"] == 0
    await db.close()

    assert await run_jobs() >= 1
    made = dict((await db.execute(select(SalaryStatement.gym_id, func.count(SalaryStatement.id))
                                  .group_by(SalaryStatement.gym_id))).all())
    assert made == {g.id: 2 for g in gyms}
    assert await db.scalar(select(func.count(Job.id)).where(Job.kind == pr.EXPLAIN_JOB)) == 3  # one budget per run
    assert await db.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == "payroll.generate")) == 3
    progress = [e["progress"] for e in events if e.get("task_id") == job.id and e.get("status") == "running"]
    assert progress == [33, 67, 100]
    done = await db.get(Job, job.id, populate_existing=True)
    assert done.status == JobStatus.DONE and done.progress == 100 and len(done.payload["done"]) == 3
    note = (await db.execute(select(Notification).where(Notification.user_id == owner.id))).scalar_one()
    assert note.title == f"Payroll for {PREV:%B %Y} is ready" and "6 salary statements created across 3 gyms" in note.body
    assert (await client.get(f"/owner/payroll/runs/{job.id}")).json()["status"] == "done"
    assert 'data-component="payroll-run"' not in (await client.get("/owner/payroll")).text
    stranger = await make_owner(db)
    await login_as(client, stranger)
    assert (await client.get(f"/owner/payroll/runs/{job.id}")).status_code == 404


async def test_queuing_payroll_for_all_gyms_costs_the_same_for_any_number_of_gyms(client, db):
    counts = []
    for n in (2, 6):
        owner = await make_owner(db)
        for k in range(n):
            await bulk_staff(db, await make_gym(db, owner, name=f"Size {n} Gym {k}"), 2, start=100 * n + 10 * k)
        await login_as(client, owner)
        await client.get("/owner/payroll")
        with QueryCounter() as qc:
            r = await post_form(client, "/owner/payroll/generate", {"month": f"{PREV:%Y-%m}", "gym": ""})
        assert r.status_code == 303 and f"for {n} gyms" in flash_text(r)
        counts.append(qc.count)
    assert counts[0] == counts[1]


async def test_a_gym_that_fails_in_the_background_run_is_reported_and_the_others_are_kept(client, db, monkeypatch):
    owner = await make_owner(db)
    gyms = [await make_gym(db, owner, name=f"Batch Gym {k}") for k in range(3)]
    for k, gym in enumerate(gyms):
        await bulk_staff(db, gym, 2, start=10 * k)
    real = pr.generate_statements

    async def flaky(session, gym, year, month, **kw):  # noqa: ANN001
        if gym.id == gyms[1].id:
            raise DBAPIError("INSERT ...", {}, Exception("the number of query arguments cannot exceed 32767"))
        return await real(session, gym, year, month, **kw)

    monkeypatch.setattr(pr, "generate_statements", flaky)
    await login_as(client, owner)
    await post_form(client, "/owner/payroll/generate", {"month": f"{PREV:%Y-%m}", "gym": ""})
    await run_jobs()
    made = dict((await db.execute(select(SalaryStatement.gym_id, func.count(SalaryStatement.id))
                                  .group_by(SalaryStatement.gym_id))).all())
    assert made == {gyms[0].id: 2, gyms[2].id: 2}
    note = (await db.execute(select(Notification).where(Notification.user_id == owner.id))).scalar_one()
    assert "need another try" in note.title and "Batch Gym 1" in note.body and "please run it again" in note.body



async def test_a_rolled_back_gym_does_not_use_up_the_runs_explanation_budget(client, db, monkeypatch):
    """The first gym fails after its statements and explanation jobs were written: its transaction rolls back alone and
    the run's budget of EXPLAIN_PER_RUN still goes to the statements that exist."""
    monkeypatch.setattr(pr, "EXPLAIN_PER_RUN", 3)
    owner = await make_owner(db)
    gyms = [await make_gym(db, owner, name=f"Budget Gym {k}") for k in range(3)]
    for k, gym in enumerate(gyms):
        await bulk_staff(db, gym, 2, start=10 * k)
    first = min(gyms, key=lambda g: g.name)  # the run goes by name
    real_audit = pr.audit

    async def audit_failing_for_the_first_gym(session, actor, action, entity_type, entity_id, **kw):  # noqa: ANN001
        if entity_id == first.id:
            raise DBAPIError("INSERT INTO audit_logs ...", {}, Exception("connection reset"))
        return await real_audit(session, actor, action, entity_type, entity_id, **kw)

    monkeypatch.setattr(pr, "audit", audit_failing_for_the_first_gym)
    await login_as(client, owner)
    await post_form(client, "/owner/payroll/generate", {"month": f"{PREV:%Y-%m}", "gym": ""})
    await run_jobs()
    made = dict((await db.execute(select(SalaryStatement.gym_id, func.count(SalaryStatement.id))
                                  .group_by(SalaryStatement.gym_id))).all())
    assert made == {g.id: 2 for g in gyms if g.id != first.id}
    jobs = (await db.execute(select(Job.payload).where(Job.kind == pr.EXPLAIN_JOB))).scalars().all()
    assert len(jobs) == 3 and {j["statement_id"] for j in jobs} <= set((await db.execute(
        select(SalaryStatement.id))).scalars())

async def test_a_retried_background_run_skips_the_gyms_it_already_finished(db, monkeypatch):
    owner = await make_owner(db)
    gyms = [await make_gym(db, owner, name=f"Resume Gym {k}") for k in range(3)]
    for k, gym in enumerate(gyms):
        await bulk_staff(db, gym, 2, start=10 * k)
    seen: list[int] = []
    real = pr.generate_statements

    async def spy(session, gym, year, month, **kw):  # noqa: ANN001
        seen.append(gym.id)
        return await real(session, gym, year, month, **kw)

    monkeypatch.setattr(pr, "generate_statements", spy)
    state = {"owner_id": owner.id, "year": PREV.year, "month": PREV.month, "gyms": 3, "done": [gyms[0].id],
             "created": 2, "skipped": 0, "explained": 2}
    out = await pr.run_owner_generation(owner.id, PREV.year, PREV.month, state=state)
    assert sorted(seen) == sorted(g.id for g in gyms[1:])  # the first attempt's gym is not generated again
    assert out["created"] == 6 and sorted(out["done"]) == sorted(g.id for g in gyms)


async def test_one_gym_is_still_generated_right_away(client, db):
    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    await bulk_staff(db, gym, 3)
    await login_as(client, owner)
    r = await post_form(client, "/owner/payroll/generate", {"month": f"{PREV:%Y-%m}", "gym": str(gym.id)})
    assert r.status_code == 303 and r.headers["location"] == "/owner/payroll?status=unpaid"
    assert "3 salary statements created" in flash_text(r)
    assert await db.scalar(select(func.count(Job.id)).where(Job.kind == pr.GENERATE_JOB)) == 0


# ============================================================================================ 7. no transaction across AI / OCR
async def test_the_payroll_explanation_holds_no_transaction_while_the_ai_writes(db, fake_ai):
    """Before: the job read the statement and kept that transaction (a pooled connection) open for the whole AI call."""
    from tests.test_payroll import add_statement, setup_staff

    owner, gym, emp = await setup_staff(db)
    st = await add_statement(db, emp, start=date(2026, 10, 1), end=date(2026, 10, 31))
    open_during_call: list[list[str]] = []

    async def answer(**kwargs: Any) -> str:
        open_during_call.append(await idle_transactions())
        return json.dumps({"summary": "Net pay follows the days worked.", "steps": [], "country_notes": [],
                           "warnings": []})

    fake_ai(handler=answer)
    async with dbmod.get_sessionmaker()() as s:
        await pr.queue_explanations_for(s, [st.id])
        await s.execute(update(Job).where(Job.kind == pr.EXPLAIN_JOB).values(run_after=utcnow()))
        await s.commit()
    await db.close()
    assert await run_jobs() == 1
    assert open_during_call == [[]]  # the AI answered while no transaction was open
    stored = await db.get(SalaryStatement, st.id, populate_existing=True)
    assert stored.ai_explanation and stored.ai_provider == "gateway"


async def test_a_statement_adjusted_while_the_ai_writes_is_explained_again(db, fake_ai):
    from tests.test_payroll import add_statement, setup_staff

    owner, gym, emp = await setup_staff(db)
    st = await add_statement(db, emp, start=date(2026, 11, 1), end=date(2026, 11, 30))
    prompts: list[str] = []

    async def answer(**kwargs: Any) -> str:
        prompts.append(kwargs["prompt"])
        if len(prompts) == 1:  # the owner records unpaid leave while the AI explains the old numbers
            async with dbmod.get_sessionmaker()() as s:
                row = await s.get(SalaryStatement, st.id)
                await pr.adjust_unpaid_leave(s, row, await s.get(Employee, emp.id), owner, 3, gym_country="IN")
                await s.commit()
        return json.dumps({"summary": "Explained.", "steps": [], "country_notes": [], "warnings": []})

    fake_ai(handler=answer)
    await db.close()
    async with dbmod.get_sessionmaker()() as s:
        assert await pr.explain_statement(s, st.id) is True
        await s.commit()
    assert len(prompts) == 2 and '"unpaid_leave_days":0' in prompts[0] and '"unpaid_leave_days":3' in prompts[1]
    stored = await db.get(SalaryStatement, st.id, populate_existing=True)
    assert stored.unpaid_leave_days == 3 and stored.ai_explanation


async def test_the_proof_check_holds_no_transaction_or_row_lock_while_ocr_reads(db, monkeypatch):
    """Before: the job locked the statement (FOR UPDATE) and kept the transaction open while the OCR engines read."""
    from app.services import ocr
    from app.services.ocr import MultiOcrResult, OcrEngineResult
    from tests.test_payroll import add_statement, setup_staff

    owner, gym, emp = await setup_staff(db)
    st = await add_statement(db, emp, start=date(2026, 10, 1), end=date(2026, 10, 31))
    await pr.record_proof(db, st, emp, owner, upload=png_bytes(color="navy"), reference="UTR77776666",
                          amount_paid=st.net_pay, paid_on=date.today())
    await db.commit()
    seen: list[tuple[list[str], bool]] = []

    async def read(data: bytes, content_type: str, **kwargs: Any) -> Any:
        locked = None
        async with dbmod.get_engine().connect() as conn:  # could the owner's page lock the statement right now?
            try:
                async with conn.begin():
                    locked = await conn.scalar(text("SELECT id FROM salary_statements WHERE id = :i FOR UPDATE NOWAIT"),
                                               {"i": st.id})
            except DBAPIError:  # LockNotAvailable: the job holds the row
                locked = None
        seen.append((await idle_transactions(), locked == st.id))
        return MultiOcrResult([OcrEngineResult("tesseract", True, f"UTR77776666 paid {st.net_pay:,.2f} on "
                                                                  f"{date.today():%Y-%m-%d}", 0.9)])

    monkeypatch.setattr(ocr, "ocr_document", read)
    await db.close()
    assert await run_jobs() == 1
    assert seen == [([], True)]
    assert (await db.get(SalaryStatement, st.id, populate_existing=True)).status == StatementStatus.PAID


async def test_a_proof_replaced_while_it_was_read_is_read_in_turn(db, monkeypatch):
    from app.services import ocr
    from app.services.ocr import MultiOcrResult, OcrEngineResult
    from tests.test_payroll import add_statement, setup_staff

    owner, gym, emp = await setup_staff(db)
    st = await add_statement(db, emp, start=date(2026, 10, 1), end=date(2026, 10, 31))
    await pr.record_proof(db, st, emp, owner, upload=png_bytes(color="red"), reference="UTR11112222",
                          amount_paid=st.net_pay, paid_on=date.today())
    await db.commit()
    reads: list[int] = []

    async def read(data: bytes, content_type: str, **kwargs: Any) -> Any:
        reads.append(len(data))
        if len(reads) == 1:  # the owner uploads a clearer proof while the first one is read
            async with dbmod.get_sessionmaker()() as s:
                row = await s.get(SalaryStatement, st.id)
                row.status = StatementStatus.AWAITING_PROOF  # (the upload route accepts a new proof in review too)
                await pr.record_proof(s, row, await s.get(Employee, emp.id), owner, upload=png_bytes(color="green"),
                                      reference="UTR11112222", amount_paid=st.net_pay, paid_on=date.today())
                await s.commit()
            return MultiOcrResult([OcrEngineResult("tesseract", True, "blurred", 0.2)])
        return MultiOcrResult([OcrEngineResult("tesseract", True, f"UTR11112222 paid {st.net_pay:,.2f} on "
                                                                  f"{date.today():%Y-%m-%d}", 0.9)])

    monkeypatch.setattr(ocr, "ocr_document", read)
    await db.close()
    async with dbmod.get_sessionmaker()() as s:
        check = await pr.verify_payment_proof(s, st.id)
        await s.commit()
    assert len(reads) == 2 and check is not None and check.passed  # the newer proof decided, not the blurred one
    assert (await db.get(SalaryStatement, st.id, populate_existing=True)).status == StatementStatus.PAID


async def test_explanations_are_not_queued_beyond_the_run_budget(db):
    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    await bulk_staff(db, gym, pr.EXPLAIN_PER_RUN + 7)
    res = await pr.generate_statements(db, gym, PREV.year, PREV.month)
    await db.commit()
    jobs = (await db.execute(select(Job.payload).where(Job.kind == pr.EXPLAIN_JOB))).scalars().all()
    assert len(res.created_ids) == pr.EXPLAIN_PER_RUN + 7 and len(jobs) == pr.EXPLAIN_PER_RUN
    assert {j["statement_id"] for j in jobs} == set(res.created_ids[:pr.EXPLAIN_PER_RUN])
