"""Database connection budget (app/db.py, ARCHITECTURE.md "Database sizing").

Every worker process has its own pool (DB_POOL_SIZE + DB_MAX_OVERFLOW, + 1 LISTEN connection with REALTIME_PG_BRIDGE).
These tests pin the defaults (4 workers fit a default PostgreSQL with room to spare), the budget maths, where the worker
count comes from, the once-per-process startup check and its WARNING, and what a visitor gets under overload: a request
beyond the pool waits up to DB_POOL_TIMEOUT_S and then gets the friendly 503 - and a connection PostgreSQL itself
refuses ("too many clients" / CONNECTION LIMIT) is that same 503, never a 500."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from app import db as dbmod
from app.config import Settings, get_settings
from app.core.deps import DB
from app.db import (
    BUDGET_SHARE,
    WORKERS_ENV,
    ConnectionBudget,
    check_connection_budget,
    configured_workers,
    db_timeout_kind,
    report_connection_budget,
)
from tests.conftest import make_client

DEFAULT_PG = {"max_connections": 100, "superuser_reserved": 3, "reserved": 0, "database_limit": -1, "role_limit": -1,
              "superuser": False, "database": "ironpulse"}


def db_url() -> str:
    return dbmod.get_engine().url.render_as_string(hide_password=False)


def budget_lines(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "ironpulse.db" and "connection budget" in r.getMessage()]


# ============================================================================================ defaults + maths
def test_the_defaults_fit_four_workers_on_a_default_postgresql() -> None:
    fields = Settings.model_fields
    assert (fields["db_pool_size"].default, fields["db_max_overflow"].default) == (10, 5)
    assert (fields["db_pool_timeout_s"].default, BUDGET_SHARE) == (10.0, 0.8)
    four = ConnectionBudget.from_server(DEFAULT_PG, workers=4, pool_size=10, max_overflow=5)
    assert (four.per_worker, four.total, four.server_slots, four.server_share) == (15, 60, 97, 77)
    assert four.problems() == []
    with_bridges = ConnectionBudget(workers=4, pool_size=10, max_overflow=5, bridge=1)  # REALTIME_PG_BRIDGE
    assert with_bridges.total == 64 and with_bridges.problems() == []  # 33 connections stay free for everybody else
    assert ConnectionBudget(workers=5, pool_size=10, max_overflow=5, bridge=1).problems()  # 80 > 77: a fifth one warns
    old = ConnectionBudget(workers=4, pool_size=10, max_overflow=20, bridge=1)  # the previous defaults: 124 > 97
    assert old.total == 124 and old.problems()


def test_env_example_no_longer_pins_the_old_pool() -> None:
    """setup / scripts/make_env.py copy .env.example into a new .env: the DB_MAX_OVERFLOW=20 it used to pin gave every
    new install 10 + 20 per worker (4 workers: 124 connections) whatever the defaults say. Documented, commented out,
    with the real defaults."""
    from dotenv import dotenv_values

    from app.config import BASE_DIR

    path = BASE_DIR / ".env.example"
    pinned = dotenv_values(path)
    for key in ("DB_POOL_SIZE", "DB_MAX_OVERFLOW"):
        assert key not in pinned, f"{key} pinned in .env.example overrides the default in every new .env"
    text = path.read_text(encoding="utf-8")
    for name in ("db_pool_size", "db_max_overflow", "db_pool_timeout_s", "db_statement_timeout_ms",
                 "db_idle_tx_timeout_ms", "db_long_statement_timeout_ms", "db_background_idle_tx_timeout_ms"):
        default = Settings.model_fields[name].default
        shown = int(default) if float(default).is_integer() else default
        assert f"# {name.upper()}={shown}" in text, name


def test_budget_maths() -> None:
    b = ConnectionBudget(workers=3, pool_size=8, max_overflow=4, bridge=1, max_connections=200, reserved=5)
    assert (b.per_worker, b.total, b.server_slots, b.server_share) == (13, 39, 195, 156)
    assert b.formula() == "3 workers x (pool 8 + overflow 4 + 1 LISTEN) = 39 connections"
    assert ConnectionBudget(workers=1, pool_size=2, max_overflow=0).formula() == "1 worker x (pool 2 + overflow 0) = 2 connections"
    # a database / role CONNECTION LIMIT is a hard cap of its own, whatever the server allows
    assert ConnectionBudget(workers=4, pool_size=10, max_overflow=4, bridge=1, database_limit=64).problems() == []
    assert ConnectionBudget(workers=4, pool_size=10, max_overflow=6, bridge=1, database_limit=64).problems() == [
        "the database's CONNECTION LIMIT is 64"]
    assert "the role's CONNECTION LIMIT is 20" in ConnectionBudget(
        workers=2, pool_size=10, max_overflow=5, role_limit=20).problems()
    # SQLAlchemy reads pool_size 0 / max_overflow -1 as "no limit"
    assert "no upper bound" in ConnectionBudget(workers=1, pool_size=5, max_overflow=-1).problems()[0]
    assert "no upper bound" in ConnectionBudget(workers=1, pool_size=0, max_overflow=5).problems()[0]
    # from the server row: PostgreSQL 16+ reserved_connections count too; -1 limits and superusers have no cap
    row = {**DEFAULT_PG, "reserved": 2, "database_limit": 50, "role_limit": -1}
    assert ConnectionBudget.from_server(row, workers=1, pool_size=1, max_overflow=0).reserved == 5
    assert ConnectionBudget.from_server(row, workers=1, pool_size=1, max_overflow=0).database_limit == 50
    assert ConnectionBudget.from_server({**row, "superuser": True}, workers=1).database_limit is None
    assert ConnectionBudget.from_server(DEFAULT_PG, workers=1).role_limit is None


def test_the_worker_count_comes_from_the_launcher_the_command_line_or_web_concurrency() -> None:
    assert configured_workers({}, []) == 1
    assert configured_workers({"WEB_CONCURRENCY": "3"}, []) == 3
    # `uvicorn app.main:app --workers 4`: every worker process inherits the master's argv (multiprocessing spawn)
    uvicorn_argv = [".venv/lib/python3.15/site-packages/uvicorn/__main__.py", "app.main:app", "--workers", "4"]
    assert configured_workers({}, uvicorn_argv) == 4
    assert configured_workers({"WEB_CONCURRENCY": "2"}, uvicorn_argv) == 4  # the explicit flag wins, as in uvicorn
    assert configured_workers({}, ["uvicorn", "app.main:app", "--workers=6"]) == 6
    assert configured_workers({"WEB_CONCURRENCY": "4"}, ["uvicorn", "app.main:app", "--reload"]) == 1
    assert configured_workers({WORKERS_ENV: "2", "WEB_CONCURRENCY": "8"}, uvicorn_argv) == 2  # python -m app's hand-off
    assert configured_workers({WORKERS_ENV: "x", "WEB_CONCURRENCY": "0"}, ["--workers", "-3"]) == 1  # junk is ignored
    # uvicorn's own environment form of its options (click auto_envvar_prefix UVICORN): below the flag, above the default
    assert configured_workers({"UVICORN_WORKERS": "3", "WEB_CONCURRENCY": "8"}, ["uvicorn", "app.main:app"]) == 3
    assert configured_workers({"UVICORN_WORKERS": "3"}, uvicorn_argv) == 4
    assert configured_workers({"UVICORN_WORKERS": "3", "UVICORN_RELOAD": "true"}, ["uvicorn", "app.main:app"]) == 1


def test_python_m_app_hands_its_worker_count_to_the_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util

    from app.config import BASE_DIR

    spec = importlib.util.spec_from_file_location("ironpulse_launcher", BASE_DIR / "app" / "__main__.py")
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    assert launcher.WORKERS_ENV == WORKERS_ENV
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    assert (launcher._worker_count(False), launcher._worker_count(True)) == (1, 1)
    monkeypatch.setenv("WEB_CONCURRENCY", "4")  # uvicorn.run(workers=None) starts WEB_CONCURRENCY processes ...
    assert (launcher._worker_count(False), launcher._worker_count(True)) == (4, 1)  # ... but one with reload


# ============================================================================================ the startup check
def test_the_warning_names_the_budget_and_the_fix(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="ironpulse.db")
    assert report_connection_budget(ConnectionBudget.from_server(DEFAULT_PG, workers=4, pool_size=10, max_overflow=5))
    assert [r.levelno for r in budget_lines(caplog)] == [logging.INFO]
    caplog.clear()
    assert not report_connection_budget(ConnectionBudget(workers=4, pool_size=10, max_overflow=20, bridge=1))
    [warning] = budget_lines(caplog)
    message = warning.getMessage()
    assert warning.levelno == logging.WARNING
    assert "4 workers x (pool 10 + overflow 20 + 1 LISTEN) = 124 connections" in message
    assert "max_connections 100 - 3 reserved" in message and "(77)" in message
    assert "DB_POOL_SIZE" in message and "DB_MAX_OVERFLOW" in message and "PgBouncer" in message
    # the load-test analysis counts any log line with "timeout" / "timed out" as a timeout - this is not one
    assert "timeout" not in message.lower() and "timed out" not in message.lower()


async def test_the_check_reads_the_servers_limits(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="ironpulse.db")
    async with dbmod.get_engine().connect() as conn:
        expected = int(await conn.scalar(text("SHOW max_connections")))
        reserved = int(await conn.scalar(text("SHOW superuser_reserved_connections")))
        fits = await check_connection_budget(conn, workers=1, pool_size=2, max_overflow=1)
        too_many = await check_connection_budget(conn, workers=expected, pool_size=10, max_overflow=5)
    assert (fits.max_connections, fits.workers, fits.total) == (expected, 1, 3)
    assert fits.reserved >= reserved and fits.problems() == []
    assert too_many.problems()
    assert [r.levelno for r in budget_lines(caplog)] == [logging.INFO, logging.WARNING]


async def test_the_first_connection_of_a_pooled_engine_runs_the_check_once(monkeypatch: pytest.MonkeyPatch,
                                                                           caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="ironpulse.db")
    url = db_url()
    monkeypatch.setenv(WORKERS_ENV, "500")  # 500 x (2 + 1) connections: more than any test server allows
    await dbmod.dispose_engine()
    try:
        engine = dbmod.configure_engine(url, pool_size=2, max_overflow=1)
        for _ in range(3):
            async with engine.connect() as conn:
                assert await conn.scalar(text("SELECT 1")) == 1
        await engine.dispose()  # a recreated pool (create_all does this at startup) does not check again
        async with engine.connect() as conn:
            await conn.scalar(text("SELECT 1"))
        [warning] = budget_lines(caplog)
        assert warning.levelno == logging.WARNING
        assert "500 workers x (pool 2 + overflow 1) = 1500 connections" in warning.getMessage()
        async with engine.connect() as conn:  # the check left no transaction or state behind on the connection
            assert await conn.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE pid = pg_backend_pid() "
                                          "AND state = 'active'")) == 1
        # NullPool engines (tests, scripts, create_indexes_concurrently) have no pool to budget: no check
        await dbmod.dispose_engine()
        caplog.clear()
        engine = dbmod.configure_engine(url, poolclass=NullPool)
        async with engine.connect() as conn:
            await conn.scalar(text("SELECT 1"))
        assert budget_lines(caplog) == []
    finally:
        await dbmod.dispose_engine()
        dbmod.configure_engine(url, poolclass=NullPool)


async def test_a_failing_check_never_breaks_the_connection(monkeypatch: pytest.MonkeyPatch,
                                                           caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="ironpulse.db")
    url = db_url()
    monkeypatch.setattr(dbmod, "BUDGET_SQL", "SELECT no_such_column FROM pg_database")
    await dbmod.dispose_engine()
    try:
        engine = dbmod.configure_engine(url, pool_size=1, max_overflow=0)
        async with engine.connect() as conn:
            assert await conn.scalar(text("SELECT 7")) == 7
        assert any("connection budget not checked" in r.getMessage() for r in caplog.records)
    finally:
        await dbmod.dispose_engine()
        dbmod.configure_engine(url, poolclass=NullPool)


# ============================================================================================ overload -> 503
@pytest.fixture
def ping_routes(app: Any):  # noqa: ANN201
    """A route that needs one pooled connection, on the real app (middleware, error pages) - removed afterwards."""

    async def ping(db: DB) -> dict[str, int]:
        return {"one": int(await db.scalar(text("SELECT 1")))}

    before = list(app.router.routes)
    for path in ("/api/test-pool/ping", "/test-pool/ping"):
        app.add_api_route(path, ping, methods=["GET"])
    yield
    app.router.routes[:] = before


@pytest.fixture
async def tiny_pool(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """The app engine with ONE pooled connection (pool_size 1, max_overflow 0) and a 0.5 s pool wait."""
    url = db_url()
    monkeypatch.setattr(get_settings(), "db_pool_timeout_s", 0.5)
    await dbmod.dispose_engine()
    engine = dbmod.configure_engine(url, pool_size=1, max_overflow=0)
    try:
        yield engine
    finally:
        await dbmod.dispose_engine()
        dbmod.configure_engine(url, poolclass=NullPool)


async def test_a_request_beyond_the_pool_waits_then_gets_the_friendly_503(ping_routes: None, tiny_pool: Any) -> None:
    async with make_client() as client:
        assert (await client.get("/api/test-pool/ping")).json() == {"one": 1}
        async with tiny_pool.connect() as held:  # the pool's only connection is busy
            await held.execute(text("SELECT 1"))
            started = time.monotonic()
            r = await client.get("/api/test-pool/ping")
            waited = time.monotonic() - started
            page = await client.get("/test-pool/ping", headers={"Accept": "text/html"})
        assert r.status_code == 503 and r.headers["retry-after"] == "5"
        assert r.json() == {"error": "Temporarily unavailable", "detail": "This page is taking too long - please try again.",
                            "code": "timeout"}
        assert 0.45 <= waited < 5  # it waited for the pool (DB_POOL_TIMEOUT_S), then answered - no hang, no 500
        assert page.status_code == 503 and "taking too long" in page.text
        assert (await client.get("/api/test-pool/ping")).json() == {"one": 1}  # connection back: served again


async def test_a_request_waits_for_a_connection_that_frees_up_in_time(ping_routes: None, tiny_pool: Any,
                                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "db_pool_timeout_s", 5.0)
    await dbmod.dispose_engine()  # the tiny pool again, now with a 5 s wait (pool_timeout is read at creation)
    engine = dbmod.configure_engine(db_url(), pool_size=1, max_overflow=0)

    async def busy_for(seconds: float) -> None:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
            await asyncio.sleep(seconds)

    async with make_client() as client:
        holder = asyncio.create_task(busy_for(0.4))
        await asyncio.sleep(0.05)
        started = time.monotonic()
        r = await client.get("/api/test-pool/ping")
        waited = time.monotonic() - started
        await holder
    assert r.status_code == 200 and r.json() == {"one": 1}
    assert 0.2 <= waited < 3


class _Orig(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(f"sqlstate {sqlstate}")
        self.sqlstate = sqlstate


def test_a_refused_connection_is_classified_as_retryable() -> None:
    assert db_timeout_kind(DBAPIError("connect", {}, _Orig("53300"))) == "connections"  # too many clients
    assert db_timeout_kind(DBAPIError("connect", {}, _Orig("57P03"))) == "connections"  # starting up / shutting down
    assert db_timeout_kind(asyncpg.exceptions.TooManyConnectionsError("sorry, too many clients already")) == "connections"
    assert db_timeout_kind(DBAPIError("SELECT 1", {}, _Orig("53200"))) is None  # out of memory: not a capacity refusal
    assert db_timeout_kind(DBAPIError("SELECT 1", {}, _Orig("57014"))) == "statement"


async def test_too_many_clients_reaches_the_visitor_as_the_friendly_503(ping_routes: None) -> None:
    """PostgreSQL refusing a new connection (here: the test database's CONNECTION LIMIT, the same SQLSTATE 53300 as
    max_connections' "too many clients") is answered like a full pool, never with a 500."""
    async with dbmod.get_engine().connect() as probe:
        allowed = await probe.scalar(text(
            "SELECT pg_get_userbyid(d.datdba) = current_user AND NOT r.rolsuper FROM pg_database d, pg_roles r "
            "WHERE d.datname = current_database() AND r.rolname = current_user"))
        database = await probe.scalar(text("SELECT current_database()"))
    if not allowed:
        pytest.skip("needs a non-superuser role that owns the test database (ALTER DATABASE ... CONNECTION LIMIT)")
    raw_url = db_url().replace("postgresql+asyncpg://", "postgresql://")
    admin = await asyncpg.connect(raw_url)
    try:
        open_now = await admin.fetchval("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()")
        await admin.execute(f'ALTER DATABASE "{database}" CONNECTION LIMIT {int(open_now)}')
        async with make_client() as client:
            r = await client.get("/api/test-pool/ping")
            page = await client.get("/test-pool/ping", headers={"Accept": "text/html"})
    finally:
        await admin.execute(f'ALTER DATABASE "{database}" CONNECTION LIMIT -1')
        await admin.close()
    assert r.status_code == 503 and r.headers["retry-after"] == "5" and r.json()["code"] == "timeout"
    assert page.status_code == 503 and "taking too long" in page.text
    async with make_client() as client:
        assert (await client.get("/api/test-pool/ping")).json() == {"one": 1}
