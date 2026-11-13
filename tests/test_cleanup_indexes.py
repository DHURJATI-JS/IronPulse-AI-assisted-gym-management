"""Indexes of the load-safety leftovers (users, staff, chat, e-mail outbox, audit trail, bans, tokens, sessions).

Each index is declared on its model (create_all / the additive sync_schema / create_indexes_concurrently build it) and
EXPLAIN-verified against the statement the real page actually runs: the page is requested on a seeded database, its SQL
is captured as sent, and the plan of THAT statement (same parameters) must use the index - the review queue and its nav
badge, the outbox search, the conversation lists, the owner's audit filters, the retention DELETEs. The composite
chat / audit indexes replaced single-column foreign-key indexes, so today's super-admin ?actor= filter, the owner's
default audit view and the ON DELETE lookups are checked against them too.

Three indexes only pay off because the page's query takes the shape they were built for: the super-admin user search
compares the citext e-mail / username as text, the owner / staff conversation lists read each gym's / employment's
newest conversations through LATERAL, the owner audit filters drop the redundant "my gyms OR my actions" scope. Each of
those is a strict check: a page that loses its shape (and with it the index) fails here."""

from __future__ import annotations

import itertools
import json
import random
import re
import string
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import event, insert, or_, select, text

from app import db as dbmod
from app.core.utils import utcnow
from app.models import AuditLog, AuthToken, ChatThread, EmailOutbox, Employee, Gym, IpBan, User, UserSession
from app.models.enums import (
    EmailStatus,
    EmployeeRole,
    EmploymentStatus,
    IdStatus,
    Role,
    ThreadStatus,
    TokenPurpose,
    VerificationStatus,
    WorkMode,
)
from app.services import chat as chat_service
from app.services import retention
from tests.conftest import login_as, make_client
from tests.factories import make_user

DAY = timedelta(days=1)

NEW_INDEXES: dict[str, tuple[str, str]] = {
    "ix_users_full_name_trgm": ("users", "USING gin (full_name gin_trgm_ops)"),
    "ix_users_email_trgm": ("users", "USING gin (((email)::text) gin_trgm_ops)"),
    "ix_users_username_trgm": ("users", "USING gin (((username)::text) gin_trgm_ops)"),
    "ix_users_phone_normalized_trgm": ("users", "USING gin (phone_normalized gin_trgm_ops)"),
    "ix_auth_tokens_used_at": ("auth_tokens", "(used_at) WHERE (used_at IS NOT NULL)"),
    "ix_auth_tokens_expires_unused": ("auth_tokens", "(expires_at) WHERE (used_at IS NULL)"),
    "ix_employees_needs_review": ("employees",
                                  "(updated_at, id) WHERE ((verification_status)::text = 'needs_review'::text)"),
    "ix_chat_threads_customer_activity": ("chat_threads",
                                          "(customer_id, COALESCE(last_message_at, created_at) DESC, id DESC)"),
    "ix_chat_threads_employee_activity": ("chat_threads",
                                          "(employee_id, COALESCE(last_message_at, created_at) DESC, id DESC)"),
    "ix_chat_threads_gym_activity": ("chat_threads", "(gym_id, COALESCE(last_message_at, created_at) DESC, id DESC)"),
    "ix_email_outbox_to_email_trgm": ("email_outbox", "USING gin (to_email gin_trgm_ops)"),
    "ix_email_outbox_subject_trgm": ("email_outbox", "USING gin (subject gin_trgm_ops)"),
    "ix_audit_logs_gym_created": ("audit_logs", "(gym_id, created_at, id)"),
    "ix_audit_logs_actor_created": ("audit_logs", "(actor_id, created_at, id)"),
    "ix_ip_bans_lifted_at": ("ip_bans", "(lifted_at) WHERE (lifted_at IS NOT NULL)"),
}


# ============================================================================================ helpers
async def index_defs(names: Any = None) -> dict[str, tuple[str, str]]:
    async with dbmod.get_engine().connect() as conn:
        rows = (await conn.execute(text("SELECT indexname, tablename, indexdef FROM pg_indexes WHERE schemaname = 'public'"
                                        " AND indexname = ANY(:names)"), {"names": list(names or NEW_INDEXES)})).all()
    return {name: (table, definition) for name, table, definition in rows}


class Capture:
    """Every statement (and its parameters, exactly as sent) the app engine executes inside the block."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, Any]] = []

    def _on(self, conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        self.sent.append((statement, parameters))

    def __enter__(self) -> "Capture":
        event.listen(dbmod.get_engine().sync_engine, "before_cursor_execute", self._on)
        return self

    def __exit__(self, *exc: object) -> None:
        event.remove(dbmod.get_engine().sync_engine, "before_cursor_execute", self._on)

    def matching(self, *patterns: str) -> list[tuple[str, Any]]:
        return [(s, p) for s, p in self.sent if all(re.search(pat, s, re.S) for pat in patterns)]

    def one(self, *patterns: str) -> tuple[str, Any]:
        found = self.matching(*patterns)
        assert found, f"no statement matches {patterns}: {[s[:120] for s, _ in self.sent]}"
        return found[-1]


async def plan_of(statement: str, parameters: Any) -> str:
    """EXPLAIN of a captured statement with its own parameters (a custom plan, as the first executions get)."""
    async with dbmod.get_engine().connect() as conn:
        rows = (await conn.exec_driver_sql("EXPLAIN " + statement, parameters)).scalars().all()
        await conn.rollback()
    return "\n".join(rows)


async def sorted_rows(statement: str, parameters: Any) -> list[int]:
    """How many rows each Sort node of the statement's plan really sorts (EXPLAIN ANALYZE of the read-only page
    query): a list served by its index sorts at most one window, never the viewer's whole history."""
    async with dbmod.get_engine().connect() as conn:
        raw = (await conn.exec_driver_sql("EXPLAIN (ANALYZE, FORMAT JSON) " + statement, parameters)).scalar()
        await conn.rollback()
    doc = json.loads(raw) if isinstance(raw, str) else raw
    out: list[int] = []

    def walk(node: dict[str, Any]) -> None:
        if node.get("Node Type") in ("Sort", "Incremental Sort"):
            child = node["Plans"][0]
            out.append(int(child.get("Actual Rows", 0) * child.get("Actual Loops", 1)))
        for sub in node.get("Plans", []):
            walk(sub)

    walk(doc[0]["Plan"])
    return out


async def page_sql(user: User, url: str) -> Capture:
    async with make_client() as client:
        await login_as(client, user)
        with Capture() as cap:
            r = await client.get(url, headers={"Accept": "text/html"})
    assert r.status_code == 200, (url, r.status_code, r.text[:300])
    return cap


async def analyze(*tables: str) -> None:
    """VACUUM ANALYZE: the steady state autovacuum keeps a live database in - fresh statistics, and the GIN indexes'
    fast-update pending lists merged (right after a bulk load every trigram search would scan that list first)."""
    async with dbmod.get_engine().connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        for table in tables:
            await conn.exec_driver_sql(f"VACUUM ANALYZE {table}")


async def bulk(model: Any, rows: list[dict[str, Any]], *, ids: bool = False, chunk: int = 2000) -> list[Any]:
    out: list[Any] = []
    async with dbmod.get_sessionmaker()() as s:
        for i in range(0, len(rows), chunk):
            part = rows[i:i + chunk]
            if ids:
                out += list((await s.execute(insert(model).returning(model.id, sort_by_parameter_order=True), part))
                            .scalars())
            else:
                await s.execute(insert(model), part)
        await s.commit()
    return out


_seq = itertools.count(1)
_rng = random.Random(20261110)


def word(n: int) -> str:
    return "".join(_rng.choice(string.ascii_lowercase) for _ in range(n))


def person(role: Role) -> dict[str, Any]:
    """A user row with realistic, varied text (searches are judged on real-looking data, not on shared prefixes)."""
    n = next(_seq)
    first, last = word(6), word(8)
    return {"email": f"{first}.{last}{n}@{word(5)}.example.com", "username": f"{first}{n}",
            "full_name": f"{first.title()} {last.title()}", "role": role, "password_hash": "x",
            "id_status": IdStatus.VERIFIED, "phone_normalized": f"+91{7_000_000_000 + n}",
            "photo_key": "public/avatars/x.jpg" if role in (Role.OWNER, Role.STAFF) else None}


def gym(i: int, owner_id: int) -> dict[str, Any]:
    return {"owner_id": owner_id, "name": f"Index Gym {i}", "slug": f"index-gym-{i}", "country_code": "IN",
            "currency": "INR"}


def employee(i: int, gym_id: int, *, user_id: int | None = None, verification: VerificationStatus = VerificationStatus.VERIFIED,
             status: EmploymentStatus = EmploymentStatus.INVITED, **extra: Any) -> dict[str, Any]:
    return {"gym_id": gym_id, "user_id": user_id, "invite_email": f"coach{i}@example.com", "full_name": f"Coach {i:05d}",
            "role": EmployeeRole.TRAINER, "is_trainer": True, "work_mode": WorkMode.BOTH, "country_code": "IN",
            "currency": "INR", "monthly_salary": Decimal(30000), "hire_date": date.today() - timedelta(days=100),
            "status": status, "photo_key": "public/staff/x.jpg", "verification_status": verification,
            "id_status": IdStatus.VERIFIED, **extra}


def uses(plan: str, index: str) -> bool:
    return re.search(rf"\b{re.escape(index)}\b", plan) is not None


# ============================================================================================ declared + built
async def test_the_new_indexes_exist_on_a_fresh_database() -> None:
    defs = await index_defs()
    for name, (table, fragment) in NEW_INDEXES.items():
        assert name in defs, name
        assert defs[name][0] == table and fragment in defs[name][1], (name, defs[name])


async def test_sync_schema_adds_them_to_an_existing_database() -> None:
    async with dbmod.get_engine().begin() as conn:
        for name in NEW_INDEXES:
            await conn.execute(text(f'DROP INDEX IF EXISTS "{name}"'))
    assert not await index_defs()
    async with dbmod.get_engine().begin() as conn:
        applied = await dbmod.sync_schema(conn)
    assert {f"index {name}" for name in NEW_INDEXES} <= set(applied)
    assert set(await index_defs()) == set(NEW_INDEXES)
    async with dbmod.get_engine().begin() as conn:
        assert await dbmod.sync_schema(conn) == []  # idempotent


async def test_they_can_be_prebuilt_concurrently_before_a_deploy() -> None:
    names = ("ix_users_email_trgm", "ix_chat_threads_gym_activity", "ix_employees_needs_review", "ix_ip_bans_lifted_at")
    async with dbmod.get_engine().begin() as conn:
        for name in names:
            await conn.execute(text(f'DROP INDEX IF EXISTS "{name}"'))
    with Capture() as cap:
        built = await dbmod.create_indexes_concurrently(dbmod.get_engine())
    assert sorted(built) == sorted(names)
    ddl = [s for s, _ in cap.sent if s.startswith("CREATE")]
    assert len(ddl) == 4 and all(s.startswith("CREATE INDEX CONCURRENTLY IF NOT EXISTS") for s in ddl)
    assert any("((email::text) gin_trgm_ops)" in s for s in ddl)
    assert any("(coalesce(last_message_at, created_at)) DESC, id DESC" in s for s in ddl)
    assert set(await index_defs(names)) == set(names)


SUPERSEDED = {  # the single-column foreign-key indexes the composite ones replace: (table, column, replacement)
    "ix_chat_threads_customer_id": ("chat_threads", "customer_id", "ix_chat_threads_customer_activity"),
    "ix_chat_threads_employee_id": ("chat_threads", "employee_id", "ix_chat_threads_employee_activity"),
    "ix_chat_threads_gym_id": ("chat_threads", "gym_id", "ix_chat_threads_gym_activity"),
    "ix_audit_logs_actor_id": ("audit_logs", "actor_id", "ix_audit_logs_actor_created"),
    "ix_audit_logs_gym_id": ("audit_logs", "gym_id", "ix_audit_logs_gym_created"),
}


async def test_the_composite_indexes_replace_the_single_column_foreign_key_indexes() -> None:
    """Same leading column: every foreign-key lookup (and ON DELETE) is served by the composite index, and a narrower
    single-column twin would only tempt the planner into "fetch all, then sort". A fresh database never gets the twins;
    an existing one loses them in create_indexes_concurrently(), once the replacement is valid."""
    declared = {idx.name for table in dbmod.Base.metadata.sorted_tables for idx in table.indexes}
    assert not set(SUPERSEDED) & declared
    assert not await index_defs(SUPERSEDED)
    for old, (table, column, new) in SUPERSEDED.items():
        info = next(t for t in dbmod.Base.metadata.sorted_tables if t.name == table).info
        assert info[dbmod.SUPERSEDED_INDEXES][old] == new
    async with dbmod.get_engine().begin() as conn:  # a database created by the previous version
        for old, (table, column, _) in SUPERSEDED.items():
            await conn.execute(text(f'CREATE INDEX "{old}" ON {table} ({column})'))
        assert await dbmod.sync_schema(conn) == []  # startup never drops anything
    assert set(await index_defs(SUPERSEDED)) == set(SUPERSEDED)
    with Capture() as cap:
        result = await dbmod.create_indexes_concurrently(dbmod.get_engine())
    assert sorted(result) == sorted(f"drop {old}" for old in SUPERSEDED)
    assert sorted(s for s, _ in cap.sent if s.startswith("DROP")) == sorted(
        f'DROP INDEX CONCURRENTLY IF EXISTS "{old}"' for old in SUPERSEDED)
    assert not await index_defs(SUPERSEDED)
    assert await dbmod.create_indexes_concurrently(dbmod.get_engine()) == []


# ============================================================================================ super-admin review queue
async def test_the_staff_review_queue_and_its_badge_read_only_the_waiting_rows(db: Any) -> None:
    admin = await make_user(db, role=Role.SUPERADMIN, full_name="Platform Admin")
    owner_id = (await bulk(User, [person(Role.OWNER)], ids=True))[0]
    gyms = await bulk(Gym, [gym(i, owner_id) for i in range(10)], ids=True)
    waiting = {i for i in range(0, 6000, 150)}  # 40 of 6,000 staff records wait for the super-admin
    await bulk(Employee, [employee(i, gyms[i % 10], verification=VerificationStatus.NEEDS_REVIEW if i in waiting
                                   else VerificationStatus.VERIFIED) for i in range(6000)])
    await analyze("employees")
    cap = await page_sql(admin, "/superadmin/reviews?tab=staff")
    badge = cap.one(r"FROM employees\s+WHERE employees\.verification_status = ", r"count\(\*\)")
    queue = cap.one(r"FROM employees", r"ORDER BY employees\.updated_at, employees\.id")
    for statement, params in (badge, queue):  # only the 40 waiting rows are read, never the 6,000 staff records
        plan = await plan_of(statement, params)
        assert uses(plan, "ix_employees_needs_review"), plan
        assert "Seq Scan on employees" not in plan, plan


# ============================================================================================ super-admin searches
async def test_the_outbox_search_uses_the_trigram_indexes(db: Any) -> None:
    admin = await make_user(db, role=Role.SUPERADMIN, full_name="Platform Admin")
    now = utcnow()
    subjects = ["Welcome to IronPulse", "Your plan renews soon", "Payment received", "New message from your coach",
                "Your workout plan is ready", "Sign-in from a new device"]
    body = ("Hello! Your coach updated your plan for this week: three strength sessions, two easy runs and a rest day. "
            "Open IronPulse to see the details, log your workouts and message your coach if anything feels off. ") * 3
    mail = [{"to_email": person(Role.CUSTOMER)["email"], "subject": f"{subjects[i % 6]} - {word(5)}", "text_body": body,
             "status": EmailStatus.SENT, "created_at": now - (i % 30) * DAY} for i in range(20_000)]  # real mail: ~600 B
    await bulk(EmailOutbox, mail)
    await analyze("email_outbox")
    wanted = mail[12_345]["to_email"].split("@")[0].split(".")[1][:8]  # one recipient's surname
    cap = await page_sql(admin, f"/superadmin/outbox?q={wanted}")
    found = cap.matching(r"FROM email_outbox", r"email_outbox\.to_email ILIKE", r"email_outbox\.subject ILIKE")
    assert len(found) >= 2  # the capped match count and the page itself
    for statement, params in found:
        plan = await plan_of(statement, params)
        assert uses(plan, "ix_email_outbox_to_email_trgm") and uses(plan, "ix_email_outbox_subject_trgm"), plan
        assert "BitmapOr" in plan, plan


async def test_the_user_search_uses_the_trigram_indexes(db: Any) -> None:
    admin = await make_user(db, role=Role.SUPERADMIN, full_name="Platform Admin")
    people = [person(Role.CUSTOMER) for _ in range(20_000)]
    await bulk(User, people)
    await analyze("users")
    digits = people[12_345]["phone_normalized"][-8:]  # 8 digits: every arm runs, the phone one included
    cap = await page_sql(admin, f"/superadmin/users?q={digits}")
    found = cap.matching(r"FROM users", r"users\.full_name ILIKE")
    assert len(found) >= 2  # the role counts and the page
    for statement, params in found:
        # citext's own ILIKE is served by no trigram index: _user_search compares the e-mail / username as text, so
        # every arm of the OR has an index and the search is one BitmapOr, never a scan of every user
        plan = await plan_of(statement, params)
        for name in ("ix_users_email_trgm", "ix_users_full_name_trgm", "ix_users_username_trgm",
                     "ix_users_phone_normalized_trgm"):
            assert uses(plan, name), plan
        assert "Seq Scan on users" not in plan, plan


# ============================================================================================ conversation lists
async def _chat_world() -> dict[str, Any]:
    """A busy platform: the big owner's gym holds 3,000 conversations (a coach with 2,500 members + 500 with the admin
    team), the owner's second gym 400, 40 other gyms 100 each with a trainer who has 60 of them, and a 'whale' member
    who is a member of 250 gyms and talks to every one of their admin teams."""
    now = utcnow()
    owner_id, other_owner = await bulk(User, [person(Role.OWNER), person(Role.OWNER)], ids=True)
    coach_user, whale = await bulk(User, [person(Role.STAFF), person(Role.CUSTOMER)], ids=True)
    trainer_users = await bulk(User, [person(Role.STAFF) for _ in range(40)], ids=True)
    members = await bulk(User, [person(Role.CUSTOMER) for _ in range(3000)], ids=True)
    big, second = await bulk(Gym, [gym(0, owner_id), gym(1, owner_id)], ids=True)
    others = await bulk(Gym, [gym(i, other_owner) for i in range(2, 252)], ids=True)
    coach = (await bulk(Employee, [employee(0, big, user_id=coach_user, status=EmploymentStatus.ACTIVE)], ids=True))[0]
    trainers = await bulk(Employee, [employee(i + 1, others[i], user_id=u, status=EmploymentStatus.ACTIVE)
                                     for i, u in enumerate(trainer_users)], ids=True)

    def thread(gym_id: int, customer: int, emp: int | None, k: int) -> dict[str, Any]:
        at = now - timedelta(minutes=37 * k)
        return {"gym_id": gym_id, "customer_id": customer, "employee_id": emp, "subject": "Hello", "created_at": at,
                "updated_at": at, "last_message_at": None if k % 5 == 0 else at + timedelta(minutes=k % 11)}

    rows = [thread(big, m, coach, k) for k, m in enumerate(members[:2500])]
    rows += [thread(big, m, None, k) for k, m in enumerate(members[2500:])]
    rows += [thread(second, m, None, k) for k, m in enumerate(members[:400])]
    for i, g in enumerate(others[:40]):
        rows += [thread(g, m, trainers[i], k) for k, m in enumerate(members[i * 60:(i + 1) * 60])]
        rows += [thread(g, m, None, k) for k, m in enumerate(members[2400 + i * 15:2400 + (i + 1) * 15])]
    rows += [thread(g, whale, None, k) for k, g in enumerate(others)]
    await bulk(ChatThread, rows)
    await analyze("chat_threads", "employees", "gyms", "users")
    async with dbmod.get_sessionmaker()() as s:
        return {"whale": await s.get(User, whale), "coach": await s.get(User, coach_user), "owner": await s.get(User, owner_id)}


LIST_SQL = (r"FROM chat_threads", r"ORDER BY coalesce\(chat_threads\.last_message_at, chat_threads\.created_at\) DESC")


async def test_a_members_conversation_list_reads_its_activity_index_in_order(db: Any) -> None:
    world = await _chat_world()
    statement, params = (await page_sql(world["whale"], "/chat")).one(*LIST_SQL)
    plan = await plan_of(statement, params)
    assert uses(plan, "ix_chat_threads_customer_activity"), plan
    assert max(await sorted_rows(statement, params), default=0) <= 51  # never a sort of the 250 conversations


@pytest.mark.parametrize("who, index, scopes", [("coach", "ix_chat_threads_employee_activity", 1),
                                                ("owner", "ix_chat_threads_gym_activity", 2)])
async def test_staff_and_owner_conversation_lists_read_their_activity_index(db: Any, who: str, index: str,
                                                                            scopes: int) -> None:
    """Each employment's / gym's newest window is read in index order through LATERAL and merged (chat._window_ids):
    one IN (subquery) over all of them would sort every conversation of the coach / owner for every window."""
    world = await _chat_world()
    statement, params = (await page_sql(world[who], "/chat")).one(*LIST_SQL)
    plan = await plan_of(statement, params)
    assert uses(plan, index), plan
    assert max(await sorted_rows(statement, params), default=0) <= 51 * scopes, plan  # one window per gym / employment


async def test_the_lateral_lists_return_exactly_the_viewers_conversations_in_order(db: Any) -> None:
    """The reshaped list (chat._window_ids: each employment's / gym's newest window through LATERAL, merged) must give
    every viewer exactly what the plain definition gives - every conversation they may open (visible_clause), filtered,
    most recent activity first - window after window through the cursor: across several employments (an ended one,
    whose conversations are closed to the coach) and gyms, ties in activity broken by id, conversations nobody wrote in
    yet, and every filter."""
    now = utcnow()
    owner_id, other_owner, coach_user, whale = await bulk(
        User, [person(Role.OWNER), person(Role.OWNER), person(Role.STAFF), person(Role.CUSTOMER)], ids=True)
    members = await bulk(User, [person(Role.CUSTOMER) for _ in range(60)], ids=True)
    g1, g2, g3 = await bulk(Gym, [gym(300 + i, owner_id) for i in range(3)], ids=True)
    (g4,) = await bulk(Gym, [gym(310, other_owner)], ids=True)
    e1, e2, e3 = await bulk(Employee, [employee(301, g1, user_id=coach_user, status=EmploymentStatus.ACTIVE),
                                       employee(302, g2, user_id=coach_user, status=EmploymentStatus.ACTIVE),
                                       employee(303, g4, user_id=coach_user, status=EmploymentStatus.TERMINATED)], ids=True)
    seq = itertools.count()
    rows: list[dict[str, Any]] = []

    def add(gym_id: int, customer: int, emp: int | None, *, closed: bool = False) -> None:
        k = next(seq)
        at = now - timedelta(minutes=k // 3)  # three conversations in a row (different gyms) share one moment
        rows.append({"gym_id": gym_id, "customer_id": customer, "employee_id": emp, "subject": f"Topic {word(4)}",
                     "status": ThreadStatus.CLOSED if closed else ThreadStatus.OPEN, "created_at": at, "updated_at": at,
                     "last_message_at": None if k % 4 == 0 else at, "last_message_preview": word(6)})

    for i, m in enumerate(members):
        add(g1, m, e1, closed=i % 5 == 0)
        if i < 40:
            add(g2, m, e2)
        if i < 25:
            add(g3, m, None, closed=i % 3 == 0)
        if i < 30:
            add(g1, m, None)
        if i < 20:
            add(g4, m, e3)
    for g in (g1, g2, g3, g4):
        add(g, whale, None)
    await bulk(ChatThread, rows)
    await analyze("chat_threads")

    async def reference(viewer: User, *, status: str = "", gym_id: int | None = None, scope: str = "",
                        q: str = "") -> list[int]:
        stmt = select(ChatThread.id).where(chat_service.visible_clause(viewer))
        if status:
            stmt = stmt.where(ChatThread.status == ThreadStatus(status))
        if gym_id:
            stmt = stmt.where(ChatThread.gym_id == gym_id)
        if scope == "team":
            stmt = stmt.where(ChatThread.employee_id.is_(None))
        elif scope == "staff":
            stmt = stmt.where(ChatThread.employee_id.is_not(None))
        if q:
            stmt = stmt.where(chat_service._thread_search(q))
        stmt = stmt.order_by(chat_service.thread_activity().desc(), ChatThread.id.desc())
        async with dbmod.get_sessionmaker()() as s:
            return list((await s.execute(stmt)).scalars())

    async with dbmod.get_sessionmaker()() as s:
        viewers = {name: await s.get(User, uid) for name, uid in
                   (("owner", owner_id), ("coach", coach_user), ("whale", whale), ("other owner", other_owner))}
    lengths: dict[str, int] = {}
    for name, viewer in viewers.items():
        for filters in ({}, {"status": "open"}, {"status": "closed"}, {"scope": "team"}, {"scope": "staff"},
                        {"gym_id": g2}, {"q": "topic"}, {"q": "no-such-words"}):
            expected = await reference(viewer, **filters)
            got: list[int] = []
            older: str | None = None
            for _ in range(len(expected) + 2):
                async with dbmod.get_sessionmaker()() as s:
                    page = await chat_service.list_threads_page(s, viewer, older=older, limit=7, **filters)
                assert len(page.items) <= 7 and (page.next_cursor is not None) == page.has_more
                got += [t.id for t in page.items]
                if not page.has_more:
                    break
                older = page.next_cursor
            assert got == expected, (name, filters, len(got), len(expected))
            if not filters:
                lengths[name] = len(got)
    # every gym of the owner, every ACTIVE employment of the coach - the ended one's conversations are closed to them
    # (F27: suspended or former staff no longer open members' conversations) - all four of the whale's gyms
    assert lengths == {"owner": 60 + 40 + 25 + 30 + 3, "coach": 60 + 40, "whale": 4, "other owner": 20 + 1}


# ============================================================================================ owner audit page
async def _audit_world() -> dict[str, Any]:
    now = utcnow()
    owner_id, other = await bulk(User, [person(Role.OWNER), person(Role.OWNER)], ids=True)
    actors = await bulk(User, [person(Role.CUSTOMER) for _ in range(300)], ids=True)
    mine = await bulk(Gym, [gym(100, owner_id), gym(101, owner_id)], ids=True)
    theirs = await bulk(Gym, [gym(200 + i, other) for i in range(40)], ids=True)
    rows = []
    for i in range(40_000):
        gym_id = mine[i % 2] if i % 25 == 0 else theirs[i % 40]
        actor = owner_id if i % 50 == 0 else actors[i % 300]
        rows.append({"actor_id": actor, "gym_id": gym_id, "action": ("shop.order_status", "staff.update", "auth.login")[i % 3],
                     "entity_type": "user", "entity_id": i, "detail": {}, "created_at": now - timedelta(minutes=13 * i)})
    await bulk(AuditLog, rows, chunk=5000)
    await analyze("audit_logs", "users", "gyms")
    async with dbmod.get_sessionmaker()() as s:
        return {"owner": await s.get(User, owner_id), "gym": mine[0]}


@pytest.mark.parametrize("query, index", [("gym", "ix_audit_logs_gym_created"), ("who=me", "ix_audit_logs_actor_created")])
async def test_the_owner_audit_filters_read_their_index_newest_first(db: Any, query: str, index: str) -> None:
    world = await _audit_world()
    url = f"/owner/audit?gym={world['gym']}" if query == "gym" else "/owner/audit?who=me"
    statement, params = (await page_sql(world["owner"], url)).one(
        r"FROM audit_logs", r"ORDER BY audit_logs\.created_at DESC, audit_logs\.id DESC")
    # the filter alone implies the owner's scope (a gym of theirs / their own actions): no "my gyms OR my actions"
    # BitmapOr of all the owner's rows plus a sort - the index reads the 40 rows of the page newest first
    plan = await plan_of(statement, params)
    assert uses(plan, index), plan
    assert max(await sorted_rows(statement, params), default=0) <= 41, plan  # newest first straight from the index


async def test_the_owner_audit_filters_list_exactly_the_owners_rows(db: Any) -> None:
    """Dropping the "my gyms OR my actions" scope next to a gym / who=me filter must not change a single row: a gym id
    is kept only when it is one of the owner's (a foreign one falls back to the scoped view) and "my actions" are
    the owner's own rows, so each filter implies the scope. Every page equals the plain definition's page."""
    world = await _audit_world()
    owner = world["owner"]
    async with dbmod.get_sessionmaker()() as s:
        mine = list((await s.execute(select(Gym.id).where(Gym.owner_id == owner.id))).scalars())
        foreign = (await s.execute(select(Gym.id).where(Gym.owner_id != owner.id).limit(1))).scalar()
    scope = or_(AuditLog.gym_id.in_(mine), AuditLog.actor_id == owner.id)
    others = or_(AuditLog.actor_id.is_(None), AuditLog.actor_id != owner.id)
    first, second = sorted(mine)  # the first gym's rows are the owner's own actions, the second gym's are others'
    cases = {
        f"gym={first}": [scope, AuditLog.gym_id == first],
        f"gym={second}": [scope, AuditLog.gym_id == second],
        "who=me": [scope, AuditLog.actor_id == owner.id],
        f"gym={first}&who=me": [scope, AuditLog.gym_id == first, AuditLog.actor_id == owner.id],
        f"gym={second}&who=others": [scope, AuditLog.gym_id == second, others],
        f"gym={foreign}": [scope],  # not the owner's gym: ignored
        "": [scope],
    }
    async with make_client() as client:
        await login_as(client, owner)
        for query, where in cases.items():
            for page in (1, 2, 7):
                async with dbmod.get_sessionmaker()() as s:
                    expected = list((await s.execute(
                        select(AuditLog.entity_id).where(*where).order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                        .offset((page - 1) * 40).limit(40))).scalars())
                r = await client.get(f"/owner/audit?{query}&page={page}")
                assert r.status_code == 200
                shown = [int(x) for x in re.findall(r"user #(\d+)", r.text)]
                assert len(expected) == 40  # entity 0 is labelled "user" (no number) on the page
                assert shown == [e for e in expected if e], (query, page, shown[:5], expected[:5])


async def test_todays_audit_pages_and_foreign_keys_use_the_composite_indexes(db: Any) -> None:
    """What the two audit indexes serve as the pages are written today (they replaced the single-column foreign-key
    indexes, so they must): the super-admin's ?actor= filter (count and page), the owner's default view (a BitmapOr of
    "my gyms" and "my actions" - never the whole platform's audit log) and the lookups ON DELETE SET NULL makes when a
    user or a gym is deleted."""
    world = await _audit_world()
    admin = await make_user(db, role=Role.SUPERADMIN, full_name="Platform Admin")
    async with dbmod.get_sessionmaker()() as s:  # an everyday actor: 133 of the 40,000 rows
        actor = (await s.execute(text("SELECT actor_id FROM audit_logs WHERE actor_id <> :o LIMIT 1"),
                                 {"o": world["owner"].id})).scalar()
    cap = await page_sql(admin, f"/superadmin/audit?actor={actor}")
    found = cap.matching(r"FROM audit_logs", r"audit_logs\.actor_id = ")
    assert len(found) == 2  # the capped count and the page
    for statement, params in found:
        plan = await plan_of(statement, params)
        assert uses(plan, "ix_audit_logs_actor_created") and "Seq Scan on audit_logs" not in plan, plan
    statement, params = (await page_sql(world["owner"], "/owner/audit")).one(
        r"FROM audit_logs", r"audit_logs\.gym_id IN", r"LIMIT", r"count\(")
    plan = await plan_of(statement, params)
    assert "BitmapOr" in plan and uses(plan, "ix_audit_logs_gym_created") and uses(plan, "ix_audit_logs_actor_created"), plan
    for column, value, index in (("actor_id", actor, "ix_audit_logs_actor_created"),
                                 ("gym_id", world["gym"], "ix_audit_logs_gym_created")):
        plan = await plan_of(f"SELECT 1 FROM ONLY audit_logs x WHERE {column} = $1 FOR KEY SHARE OF x", (value,))
        assert uses(plan, index) and "Seq Scan" not in plan, plan  # the referential-integrity lookup


async def test_the_chat_activity_indexes_serve_the_foreign_key_lookups(db: Any) -> None:
    """Deleting a gym / member / staff record cascades to their conversations: each lookup reads one index range."""
    await _chat_world()
    async with dbmod.get_sessionmaker()() as s:
        row = (await s.execute(text("SELECT gym_id, customer_id, employee_id FROM chat_threads WHERE employee_id IS NOT "
                                    "NULL ORDER BY id DESC LIMIT 1"))).one()  # a small gym's trainer (the big gym's
        # 3,000 conversations are half the table: a sequential scan is right for that one)
    unique = "uq_chat_threads_gym_id_customer_id_employee_id"  # the unique constraint's index leads with gym_id too
    for column, value, indexes in (("gym_id", row.gym_id, ("ix_chat_threads_gym_activity", unique)),
                                   ("customer_id", row.customer_id, ("ix_chat_threads_customer_activity",)),
                                   ("employee_id", row.employee_id, ("ix_chat_threads_employee_activity",))):
        plan = await plan_of(f"SELECT 1 FROM ONLY chat_threads x WHERE {column} = $1 FOR KEY SHARE OF x", (value,))
        assert any(uses(plan, name) for name in indexes) and "Seq Scan" not in plan, (column, plan)


# ============================================================================================ retention DELETEs
async def test_the_new_retention_deletes_walk_their_indexes(db: Any) -> None:
    now = utcnow()
    user_id = (await bulk(User, [person(Role.CUSTOMER)], ids=True))[0]
    bans = [{"ip": f"198.51.{i // 250}.{i % 250}", "reason": "probe", "severity": 3, "created_at": now - DAY,
             "banned_until": now + DAY} for i in range(4000)]  # active
    bans += [{"ip": f"203.0.{i // 250}.{i % 250}", "reason": "probe", "severity": 3, "created_at": now - 200 * DAY,
              "banned_until": now - 199 * DAY, "lifted_at": now - (150 + i % 20) * DAY} for i in range(150)]
    bans += [{"ip": f"192.0.{i // 250}.{i % 250}", "reason": "probe", "severity": 3, "created_at": now - 200 * DAY,
              "banned_until": now - (120 + i % 20) * DAY} for i in range(150)]
    await bulk(IpBan, bans)
    tokens = [{"purpose": TokenPurpose.PASSWORD_RESET, "token_hash": f"live{i:060d}", "user_id": user_id,
               "expires_at": now + DAY} for i in range(4000)]
    tokens += [{"purpose": TokenPurpose.EMAIL_VERIFY, "token_hash": f"used{i:060d}", "user_id": user_id,
                "expires_at": now - 58 * DAY, "used_at": now - (59 + i % 5) * DAY} for i in range(150)]
    tokens += [{"purpose": TokenPurpose.PASSWORD_RESET, "token_hash": f"gone{i:060d}", "user_id": user_id,
                "expires_at": now - (60 + i % 5) * DAY} for i in range(150)]
    await bulk(AuthToken, tokens)
    sessions = [{"id": f"live{i:060d}", "user_id": user_id, "expires_at": now + 2 * DAY} for i in range(4000)]
    sessions += [{"id": f"dead{i:060d}", "user_id": user_id, "expires_at": now - (60 + i % 9) * DAY} for i in range(150)]
    await bulk(UserSession, sessions)
    await analyze("ip_bans", "auth_tokens", "user_sessions")
    with Capture() as cap:
        report = await retention.run_retention(now=now)
    assert (report.deleted["ip_bans"], report.deleted["auth_tokens"], report.deleted["user_sessions"]) == (300, 300, 150)
    expected = {
        r"DELETE FROM ip_bans .*ip_bans\.lifted_at < ": "ix_ip_bans_lifted_at",
        r"DELETE FROM ip_bans .*ip_bans\.banned_until < ": "ix_ip_bans_unlifted_until",
        r"DELETE FROM auth_tokens .*auth_tokens\.used_at < ": "ix_auth_tokens_used_at",
        r"DELETE FROM auth_tokens .*auth_tokens\.expires_at < ": "ix_auth_tokens_expires_unused",
        r"DELETE FROM user_sessions .*user_sessions\.expires_at < ": "ix_user_sessions_expires_at",
    }
    for pattern, index in expected.items():
        statement, params = cap.matching(pattern)[0]  # the first batch: from the oldest row on
        plan = await plan_of(statement, params)
        assert uses(plan, index), (index, plan)  # the batch's "oldest n" subquery walks the index ...
        assert not re.search(r"Seq Scan on \w+ \w+_1\b", plan), plan  # ... never the whole table
