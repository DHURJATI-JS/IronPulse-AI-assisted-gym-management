"""Server-side limits on every pooled connection (app/db.py): statement_timeout, idle_in_transaction_session_timeout and
the pool wait; ``long_statement`` for batch work; the background idle limit; the friendly 503 (app/core/errors.py);
and the indexes the paginated lists and the retention job rely on (fresh database + additive sync_schema)."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import pytest
from asyncpg.exceptions import QueryCanceledError
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app import db as dbmod
from app.config import TEST_DB_IDLE_TX_TIMEOUT_MS, Settings, get_settings
from app.core.deps import DB
from app.db import BackgroundSession, db_timeout_kind, engine_options, long_statement, server_settings, session_scope
from tests.conftest import make_client


def db_url() -> str:
    return dbmod.get_engine().url.render_as_string(hide_password=False)


async def setting(conn: Any, name: str) -> str:
    return str(await conn.scalar(text("SELECT setting FROM pg_settings WHERE name = :n"), {"n": name}))


class Statements:
    def __init__(self, engine: AsyncEngine | None = None) -> None:
        self.engine = engine or dbmod.get_engine()
        self.sql: list[str] = []

    def _on(self, conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        self.sql.append(statement)

    def __enter__(self) -> "Statements":
        event.listen(self.engine.sync_engine, "before_cursor_execute", self._on)
        return self

    def __exit__(self, *exc: object) -> None:
        event.remove(self.engine.sync_engine, "before_cursor_execute", self._on)


# ============================================================================================ engine options
def test_defaults_and_engine_options(monkeypatch: pytest.MonkeyPatch) -> None:
    app_defaults = Settings(environment="development")
    assert (app_defaults.db_statement_timeout_ms, app_defaults.db_idle_tx_timeout_ms, app_defaults.db_pool_timeout_s,
            app_defaults.db_long_statement_timeout_ms, app_defaults.db_background_idle_tx_timeout_ms) == \
        (15_000, 60_000, 10.0, 300_000, 600_000)
    monkeypatch.setattr(get_settings(), "db_idle_tx_timeout_ms", 60_000)
    s = get_settings()
    assert server_settings() == {"statement_timeout": "15000", "idle_in_transaction_session_timeout": "60000"}
    pooled = engine_options()
    assert pooled["pool_timeout"] == 10.0 and pooled["pool_size"] == s.db_pool_size
    assert pooled["connect_args"] == {"server_settings": server_settings()}
    nullpool = engine_options(poolclass=NullPool)
    assert "pool_timeout" not in nullpool and "pool_size" not in nullpool  # NullPool takes no sizing arguments
    assert nullpool["connect_args"] == {"server_settings": server_settings()}
    merged = engine_options(connect_args={"server_settings": {"statement_timeout": "0", "application_name": "x"},
                                          "timeout": 5})
    assert merged["connect_args"] == {"timeout": 5, "server_settings": {
        "statement_timeout": "0", "idle_in_transaction_session_timeout": "60000", "application_name": "x"}}


def test_zero_leaves_the_server_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "db_statement_timeout_ms", 0)
    monkeypatch.setattr(get_settings(), "db_idle_tx_timeout_ms", 0)
    assert server_settings() == {}
    assert "connect_args" not in engine_options()


def test_the_test_environment_gets_a_longer_idle_limit_unless_configured() -> None:
    assert Settings(environment="test").db_idle_tx_timeout_ms == TEST_DB_IDLE_TX_TIMEOUT_MS  # fixtures span a test
    assert Settings(environment="test", db_idle_tx_timeout_ms=5_000).db_idle_tx_timeout_ms == 5_000
    # outside tests the shipped default - checked without the developer's .env (its production rules, e.g. a weak
    # SUPERADMIN_PASSWORD or CORS_ORIGINS=*, would fail this test for reasons that have nothing to do with it)
    s = get_settings()
    outside = Settings(_env_file=None, environment="development", secret_key=s.secret_key,  # type: ignore[call-arg]
                       data_encryption_key=s.data_encryption_key, id_hash_pepper=s.id_hash_pepper)
    assert outside.db_idle_tx_timeout_ms == Settings.model_fields["db_idle_tx_timeout_ms"].default == 60_000


async def test_every_app_connection_carries_the_limits() -> None:
    async with dbmod.get_sessionmaker()() as s:  # request sessions (get_db)
        assert await setting(s, "statement_timeout") == "15000"
        assert await setting(s, "idle_in_transaction_session_timeout") == str(get_settings().db_idle_tx_timeout_ms)


# ============================================================================================ the limits work
async def test_statement_timeout_cancels_a_slow_query_and_long_statement_lifts_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "db_statement_timeout_ms", 100)
    engine = create_async_engine(db_url(), **engine_options(poolclass=NullPool))
    try:
        async with engine.connect() as conn:
            with pytest.raises(DBAPIError) as caught:
                await conn.execute(text("SELECT pg_sleep(0.6)"))
            assert db_timeout_kind(caught.value) == "statement"
            await conn.rollback()
            await long_statement(conn, 5_000)  # this transaction only
            assert await conn.scalar(text("SHOW statement_timeout")) == "5s"
            await conn.execute(text("SELECT pg_sleep(0.3)"))
            await conn.commit()
            assert await conn.scalar(text("SHOW statement_timeout")) == "100ms"  # back after COMMIT
            with pytest.raises(DBAPIError):
                await conn.execute(text("SELECT pg_sleep(0.6)"))
            await conn.rollback()
            await long_statement(conn, 0, idle_ms=0)
            assert await conn.scalar(text("SHOW statement_timeout")) == "0"
            assert await conn.scalar(text("SHOW idle_in_transaction_session_timeout")) == "0"
            await conn.rollback()
            await long_statement(conn)  # default: DB_LONG_STATEMENT_TIMEOUT_MS
            assert await setting(conn, "statement_timeout") == str(get_settings().db_long_statement_timeout_ms)
            await conn.rollback()
    finally:
        await engine.dispose()


async def test_idle_in_transaction_connections_are_closed_by_the_server(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "db_idle_tx_timeout_ms", 200)
    engine = create_async_engine(db_url(), **engine_options(poolclass=NullPool))
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
            await conn.commit()
            await asyncio.sleep(0.5)  # idle OUTSIDE a transaction: fine
            assert await conn.scalar(text("SELECT 2")) == 2
            await asyncio.sleep(0.5)  # idle INSIDE the transaction the SELECT opened: terminated
            with pytest.raises(DBAPIError):
                await conn.execute(text("SELECT 3"))
    finally:
        await engine.dispose()


async def test_pool_wait_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "db_pool_timeout_s", 0.3)
    engine = create_async_engine(db_url(), **engine_options(pool_size=1, max_overflow=0))
    try:
        held = await engine.connect()
        await held.execute(text("SELECT 1"))
        started = time.monotonic()
        with pytest.raises(PoolTimeoutError) as caught:
            async with engine.connect() as second:
                await second.execute(text("SELECT 1"))
        assert time.monotonic() - started < 3
        assert db_timeout_kind(caught.value) == "pool"
        await held.close()
    finally:
        await engine.dispose()


# ============================================================================================ background sessions
async def test_background_sessions_get_the_background_idle_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "db_idle_tx_timeout_ms", 60_000)  # the production defaults
    monkeypatch.setattr(get_settings(), "db_background_idle_tx_timeout_ms", 600_000)
    engine = create_async_engine(db_url(), **engine_options(poolclass=NullPool))
    background = async_sessionmaker(engine, sync_session_class=BackgroundSession)
    try:
        async with background() as s:
            assert await setting(s, "idle_in_transaction_session_timeout") == "600000"
            assert await setting(s, "statement_timeout") == "15000"  # statements keep the request limit
            await s.commit()
            assert await setting(s, "idle_in_transaction_session_timeout") == "600000"  # every transaction
        async with async_sessionmaker(engine)() as s:
            assert await setting(s, "idle_in_transaction_session_timeout") == "60000"
        with Statements(engine) as st:
            async with background() as s:
                await s.execute(text("SELECT 1"))
                async with s.begin_nested():
                    await s.execute(text("SELECT 2"))
        assert sum("idle_in_transaction_session_timeout" in sql for sql in st.sql) == 1  # once per root transaction
    finally:
        await engine.dispose()
    assert dbmod.get_background_sessionmaker().kw["sync_session_class"] is BackgroundSession  # what session_scope uses


async def test_no_extra_statement_unless_the_background_limit_is_longer(monkeypatch: pytest.MonkeyPatch) -> None:
    base = get_settings().db_idle_tx_timeout_ms
    for value in (0, base, base // 2):
        monkeypatch.setattr(get_settings(), "db_background_idle_tx_timeout_ms", value)
        with Statements() as st:
            async with session_scope() as s:
                await s.execute(text("SELECT 1"))
        assert st.sql == ["SELECT 1"]


async def test_a_background_transaction_may_idle_past_the_request_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "db_idle_tx_timeout_ms", 200)
    monkeypatch.setattr(get_settings(), "db_background_idle_tx_timeout_ms", 5_000)
    engine = create_async_engine(db_url(), **engine_options(poolclass=NullPool))
    background = async_sessionmaker(engine, sync_session_class=BackgroundSession)
    request = async_sessionmaker(engine)
    try:
        async with background() as s:  # e.g. a job handler that read its rows, then waits for the AI
            await s.execute(text("SELECT 1"))
            await asyncio.sleep(0.5)
            assert await s.scalar(text("SELECT 2")) == 2
        async with request() as s:
            await s.execute(text("SELECT 1"))
            await asyncio.sleep(0.5)
            with pytest.raises(DBAPIError):
                await s.execute(text("SELECT 2"))
    finally:
        await engine.dispose()


async def test_schema_ddl_runs_without_the_statement_limit() -> None:
    with Statements() as st:
        await dbmod.create_all()  # idempotent
    lifted = [i for i, sql in enumerate(st.sql) if "set_config('statement_timeout'" in sql]
    assert lifted, st.sql[:5]
    assert any("pg_catalog" in sql or "information_schema" in sql or "pg_class" in sql for sql in st.sql[lifted[0]:])


# ============================================================================================ classification + 503
class _Orig(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(f"sqlstate {sqlstate}")
        self.sqlstate = sqlstate


def test_db_timeout_kind() -> None:
    assert db_timeout_kind(PoolTimeoutError("QueuePool limit of size 1 overflow 0 reached")) == "pool"
    assert db_timeout_kind(DBAPIError("SELECT 1", {}, _Orig("57014"))) == "statement"
    assert db_timeout_kind(QueryCanceledError("canceling statement due to statement timeout")) == "statement"
    assert db_timeout_kind(DBAPIError("SELECT 1/0", {}, _Orig("22012"))) is None
    assert db_timeout_kind(ValueError("x")) is None
    wrapped = RuntimeError("outer")
    wrapped.__cause__ = QueryCanceledError("canceling statement due to statement timeout")
    assert db_timeout_kind(wrapped) == "statement"


@pytest.fixture
def timeout_routes(app: Any):  # noqa: ANN201
    """Routes that hit the limits, on the real app (middleware, error pages) - removed afterwards."""

    async def slow(db: DB) -> dict[str, bool]:
        await db.execute(text("SET LOCAL statement_timeout = 50"))
        await db.execute(text("SELECT pg_sleep(2)"))
        return {"ok": True}

    async def pool_exhausted(db: DB) -> dict[str, bool]:
        raise PoolTimeoutError("QueuePool limit of size 10 overflow 20 reached, connection timed out, timeout 10.00")

    async def broken(db: DB) -> dict[str, bool]:
        await db.execute(text("SELECT 1 / 0"))
        return {"ok": True}

    before = list(app.router.routes)
    for path, fn in (("/api/test-db/slow", slow), ("/test-db/slow", slow), ("/api/test-db/pool", pool_exhausted),
                     ("/test-db/pool", pool_exhausted), ("/test-db/broken", broken)):
        app.add_api_route(path, fn, methods=["GET"])
    yield
    app.router.routes[:] = before


async def test_statement_timeout_is_a_friendly_503(timeout_routes: None, client: Any, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="ironpulse.errors")
    r = await client.get("/api/test-db/slow")
    assert r.status_code == 503 and r.headers["retry-after"] == "5"
    assert r.json() == {"error": "Temporarily unavailable", "detail": "This page is taking too long - please try again.",
                        "code": "timeout"}
    assert any("statement timeout on GET /api/test-db/slow (route /api/test-db/slow)" in m for m in caplog.messages)
    page = await client.get("/test-db/slow", headers={"Accept": "text/html"})
    assert page.status_code == 503 and page.headers["content-type"].startswith("text/html")
    assert "Taking too long" in page.text and "This page is taking too long - please try again." in page.text
    assert (await client.get("/healthz")).status_code == 200  # the request's session was rolled back cleanly


async def test_pool_timeout_is_a_friendly_503(timeout_routes: None, client: Any, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="ironpulse.errors")
    r = await client.get("/api/test-db/pool")
    assert r.status_code == 503 and r.json()["code"] == "timeout"
    assert any("pool timeout on GET /api/test-db/pool" in m for m in caplog.messages)
    page = await client.get("/test-db/pool")
    assert page.status_code == 503 and "taking too long" in page.text


async def test_other_database_errors_are_unchanged(timeout_routes: None) -> None:
    async with make_client() as c:
        with pytest.raises(DBAPIError):  # still the generic 500 path, which re-raises (as before this change)
            await c.get("/test-db/broken")


# ============================================================================================ indexes
NEW_INDEXES: dict[str, tuple[str, str]] = {
    "ix_security_events_created_at": ("security_events", "(created_at)"),
    "ix_security_events_category_created": ("security_events", "(category, created_at)"),
    "ix_ip_bans_unlifted_until": ("ip_bans", "(banned_until) WHERE (lifted_at IS NULL)"),
    "ix_audit_logs_entity": ("audit_logs", "(entity_type, entity_id, created_at)"),
    "ix_audit_logs_created_at": ("audit_logs", "(created_at)"),
    "ix_ai_call_logs_created_at": ("ai_call_logs", "(created_at)"),
    "ix_jobs_status_finished": ("jobs", "(status, finished_at)"),
    "ix_notifications_user_created": ("notifications", "(user_id, created_at, id)"),
    "ix_notifications_created_at": ("notifications", "(created_at)"),
    "ix_email_outbox_status_id": ("email_outbox", "(status, id)"),
    "ix_email_outbox_status_created": ("email_outbox", "(status, created_at)"),
    "ix_chat_messages_thread_id_id": ("chat_messages", "(thread_id, id)"),
    "ix_tasks_reminder_due": ("tasks", "(remind_at) WHERE ((reminded_at IS NULL) AND (completed_at IS NULL))"),
    "ix_users_last_login_ip": ("users", "(last_login_ip) WHERE (last_login_ip IS NOT NULL)"),
    "ix_products_name_trgm": ("products", "USING gin (name gin_trgm_ops)"),
    "ix_products_brand_trgm": ("products", "USING gin (brand gin_trgm_ops)"),
    "ix_products_embedding_hnsw": ("products", "USING hnsw (embedding vector_cosine_ops)"),
}


async def index_defs() -> dict[str, tuple[str, str]]:
    async with dbmod.get_engine().connect() as conn:
        rows = (await conn.execute(text("SELECT indexname, tablename, indexdef FROM pg_indexes WHERE schemaname = 'public'"
                                        " AND indexname = ANY(:names)"), {"names": list(NEW_INDEXES)})).all()
    return {name: (table, definition) for name, table, definition in rows}


async def test_new_indexes_exist_on_a_fresh_database() -> None:
    defs = await index_defs()
    for name, (table, fragment) in NEW_INDEXES.items():
        assert name in defs, name
        assert defs[name][0] == table and fragment in defs[name][1], (name, defs[name])


async def test_sync_schema_adds_the_indexes_to_an_existing_database() -> None:
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


VECTOR = "[" + ",".join(["0.1"] * 384) + "]"
PLANS: list[tuple[str, tuple[str, ...]]] = [
    ("SELECT * FROM audit_logs WHERE entity_type = 'shop_order' AND entity_id = 7 ORDER BY created_at, id",
     ("ix_audit_logs_entity",)),
    ("SELECT * FROM notifications WHERE user_id = 7 ORDER BY created_at DESC, id DESC LIMIT 20",
     ("ix_notifications_user_created",)),
    ("SELECT id FROM products WHERE name ILIKE '%whey%'", ("ix_products_name_trgm",)),
    ("SELECT id FROM products WHERE brand ILIKE '%optimum%'", ("ix_products_brand_trgm",)),
    (f"SELECT id FROM products ORDER BY embedding <=> '{VECTOR}' LIMIT 5", ("ix_products_embedding_hnsw",)),
    ("SELECT count(*) FROM security_events WHERE created_at >= now() - interval '1 day'",
     ("ix_security_events_created_at", "ix_security_events_category_created")),
    ("SELECT count(*) FROM ai_call_logs WHERE created_at >= now() - interval '1 day'", ("ix_ai_call_logs_created_at",)),
    ("SELECT id FROM email_outbox WHERE status = 'queued' ORDER BY id LIMIT 25", ("ix_email_outbox_status_id",)),
    ("SELECT id FROM email_outbox WHERE status = 'sent' AND created_at < now() ORDER BY created_at LIMIT 10",
     ("ix_email_outbox_status_created",)),
    ("SELECT id FROM jobs WHERE status = 'done' AND finished_at < now() ORDER BY finished_at LIMIT 10",
     ("ix_jobs_status_finished",)),
    ("SELECT id FROM notifications WHERE read_at IS NOT NULL AND created_at < now() ORDER BY created_at LIMIT 10",
     ("ix_notifications_created_at",)),
    ("SELECT id FROM chat_messages WHERE thread_id = 3 AND id < 99 ORDER BY id DESC LIMIT 51",
     ("ix_chat_messages_thread_id_id",)),
    ("SELECT id FROM tasks WHERE remind_at <= now() AND reminded_at IS NULL AND completed_at IS NULL "
     "ORDER BY remind_at LIMIT 300", ("ix_tasks_reminder_due",)),
    ("SELECT id FROM users WHERE last_login_ip = '203.0.113.9'", ("ix_users_last_login_ip",)),
    ("SELECT ip FROM ip_bans WHERE lifted_at IS NULL AND (banned_until IS NULL OR banned_until > now())",
     ("ix_ip_bans_unlifted_until",)),
]


async def index_plan(conn: Any, sql: str) -> str:
    """EXPLAIN with sequential scans off and - for an ordered window (``LIMIT``), which its index serves in order -
    explicit sorts off too: the check is that the index CAN serve the reader. On a near-empty test table the cost model
    otherwise follows whatever statistics an earlier test left behind (TRUNCATE keeps them): with "read notifications
    are rare" it preferred ix_notifications_user_unread + a Sort for the retention reader (a full-suite flake)."""
    await conn.execute(text("SET LOCAL enable_seqscan = off"))
    if " LIMIT " in sql:
        await conn.execute(text("SET LOCAL enable_sort = off"))
    return "\n".join((await conn.execute(text("EXPLAIN " + sql))).scalars())


@pytest.mark.parametrize("sql, indexes", PLANS)
async def test_the_readers_can_use_the_new_indexes(sql: str, indexes: tuple[str, ...]) -> None:
    async with dbmod.get_engine().begin() as conn:
        plan = await index_plan(conn, sql)
    assert any(name in plan for name in indexes), plan


async def test_the_index_check_does_not_depend_on_statistics_left_by_other_tests() -> None:
    """Regression: an emptied table (TRUNCATE) still carrying the statistics of 'mostly unread' notifications made the
    planner sort the retention reader's rows instead of walking ix_notifications_created_at. Built and rolled back in
    one transaction (INSERT, ANALYZE and TRUNCATE are all transactional): nothing leaks into other tests."""
    sql = next(sql for sql, _ in PLANS if "read_at IS NOT NULL" in sql)
    async with dbmod.get_engine().connect() as conn:
        user_id = await conn.scalar(text(
            "INSERT INTO users (full_name, role, id_status) VALUES ('Stats', 'customer', 'missing') RETURNING id"))
        await conn.execute(text(
            "INSERT INTO notifications (user_id, title, body, category, severity, created_at, read_at) "
            "SELECT :u, 'T', 'B', 'system', 'info', now() - g * interval '1 minute', "
            "CASE WHEN g % 100 = 0 THEN now() END FROM generate_series(1, 20000) g"), {"u": user_id})
        await conn.execute(text("ANALYZE notifications"))
        await conn.execute(text("TRUNCATE notifications CASCADE"))
        plan = await index_plan(conn, sql)
        await conn.rollback()
    assert "ix_notifications_created_at" in plan and "Sort" not in plan, plan


async def test_indexes_can_be_prebuilt_concurrently_before_a_deploy() -> None:
    """The roll-out path for large tables: CREATE INDEX CONCURRENTLY first, then sync_schema has nothing to do."""
    names = ("ix_audit_logs_entity", "ix_products_name_trgm", "ix_tasks_reminder_due")
    async with dbmod.get_engine().begin() as conn:
        for name in names:
            await conn.execute(text(f'DROP INDEX IF EXISTS "{name}"'))
    with Statements() as st:
        built = await dbmod.create_indexes_concurrently(dbmod.get_engine())
    assert sorted(built) == sorted(names)
    ddl = [sql for sql in st.sql if sql.startswith("CREATE")]
    assert len(ddl) == 3 and all(sql.startswith("CREATE INDEX CONCURRENTLY IF NOT EXISTS") for sql in ddl)
    assert any("USING gin (name gin_trgm_ops)" in sql for sql in ddl)
    assert any("WHERE reminded_at IS NULL AND completed_at IS NULL" in sql for sql in ddl)
    defs = await index_defs()
    assert all(name in defs for name in names)
    async with dbmod.get_engine().connect() as conn:
        invalid = await conn.scalar(text("SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                                         "WHERE c.relname = ANY(:n) AND NOT i.indisvalid"), {"n": list(names)})
        assert invalid == 0
        assert await conn.scalar(text("SHOW statement_timeout")) == "15s"  # the session setting did not leak
    assert await dbmod.create_indexes_concurrently(dbmod.get_engine()) == []
    async with dbmod.get_engine().begin() as conn:
        assert await dbmod.sync_schema(conn) == []
