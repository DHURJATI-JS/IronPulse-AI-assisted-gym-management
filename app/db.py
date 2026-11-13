"""Async SQLAlchemy engine/session management for PostgreSQL (+ pgvector).

Rules (see ARCHITECTURE.md):
* every relationship is declared ``lazy="raise"`` - related rows must be loaded explicitly with
  ``selectinload`` / ``joinedload``. Accidental lazy loads (the N+1 pattern) raise immediately.
* hard deletes use Core ``delete()`` statements; ON DELETE rules in the schema do the cascading.
* every pooled connection carries server-side limits (``statement_timeout``, ``idle_in_transaction_session_timeout``
  from DB_STATEMENT_TIMEOUT_MS / DB_IDLE_TX_TIMEOUT_MS) and waiting for a free connection is capped by
  DB_POOL_TIMEOUT_S, so one slow query cannot hold the pool for ever; a request that hits a limit gets a 503
  (``db_timeout_kind`` + app/core/errors.py). Background work that legitimately needs longer raises the limit for its
  own transaction with ``await long_statement(db, ms)``.
* every worker process has its own pool (DB_POOL_SIZE + DB_MAX_OVERFLOW, shared by its requests and background work).
  The first connection a process opens checks the budget once - workers x per-worker connections against what
  PostgreSQL allows (``ConnectionBudget``) - and logs a WARNING when it does not fit (ARCHITECTURE.md "Database
  sizing"). A connection the server refuses anyway ("too many clients") is a 503 too, never a 500.
* a request's session gives its connection back to the pool as soon as the response starts (``release_request_session``,
  app/main.py): sending the body to a slow phone never keeps a pooled connection idle in a transaction. A client that
  goes away meanwhile cannot cut that ROLLBACK short (it is shielded from the cancellation): the connection goes back
  healthy instead of being torn down with a logged traceback.
* outside requests an overloaded or unreachable database (``db_unavailable``: pool timeout, refused or lost connection)
  is not a bug: presence, the job runner and the worker loops log one warning a minute per call site and retry later,
  /ws closes with 1013 (app/core/overload.py).
* start-up work (``create_all`` + the additive ``sync_schema``, the bootstrap) runs under ``startup_lock``: several
  worker processes starting together apply it once, one after the other, instead of racing into UniqueViolations.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import re
import sys
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import anyio
from sqlalchemy import MetaData, event, text
from sqlalchemy.ext.asyncio import AsyncAttrs, AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Session
from starlette.requests import HTTPConnection

from app.config import get_settings

log = logging.getLogger("ironpulse.db")

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

EXTENSIONS = ("vector", "citext", "pg_trgm")


class Base(AsyncAttrs, DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# --------------------------------------------------------------------------- query counter
# Incremented for every SQL statement executed while a counter is active (tests, X-Query-Count header).
_query_counter: contextvars.ContextVar[list[int] | None] = contextvars.ContextVar("ip_query_counter", default=None)


class QueryCounter:
    """``with QueryCounter() as qc: ...; qc.count`` - counts SQL statements in the current context."""

    def __init__(self) -> None:
        self._box: list[int] = [0]
        self._token: contextvars.Token | None = None

    def __enter__(self) -> "QueryCounter":
        self._token = _query_counter.set(self._box)
        return self

    def __exit__(self, *exc: object) -> None:
        if self._token is not None:
            _query_counter.reset(self._token)

    @property
    def count(self) -> int:
        return self._box[0]


def start_query_count() -> list[int]:
    box = [0]
    _query_counter.set(box)
    return box


def _install_listeners(engine: AsyncEngine) -> None:
    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _count(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        box = _query_counter.get()
        if box is not None:
            box[0] += 1

    # NOTE: no asyncpg pgvector codec is registered on purpose. pgvector.sqlalchemy.Vector binds vectors
    # as their text form ("[0.1,0.2,...]") and parses results itself; a binary codec on the connection
    # would make asyncpg reject that text ("expected list or ndarray"). With no codec asyncpg uses the
    # type's text I/O, so ORM reads/writes and .cosine_distance(...) work with plain Python lists.


# --------------------------------------------------------------------------- server-side limits
QUERY_CANCELED = "57014"  # SQLSTATE of a statement cancelled by statement_timeout (or pg_cancel_backend)


def server_settings() -> dict[str, str]:
    """``asyncpg`` startup parameters every pooled connection gets (0 = leave the server/role default)."""
    s = get_settings()
    out: dict[str, str] = {}
    if s.db_statement_timeout_ms > 0:
        out["statement_timeout"] = str(int(s.db_statement_timeout_ms))
    if s.db_idle_tx_timeout_ms > 0:
        out["idle_in_transaction_session_timeout"] = str(int(s.db_idle_tx_timeout_ms))
    return out


async def long_statement(db: Any, ms: int | None = None, *, idle_ms: int | None = None) -> None:
    """Raise ``statement_timeout`` for the CURRENT transaction only (``SET LOCAL``; reset at COMMIT / ROLLBACK).

    For background work that legitimately runs longer than the per-connection limit - periodic batch jobs, data
    retention, the CSV export. ``ms`` defaults to DB_LONG_STATEMENT_TIMEOUT_MS; 0 = no limit. ``idle_ms`` also sets
    ``idle_in_transaction_session_timeout`` (e.g. a stream waiting on a slow client; 0 = no limit). ``db`` is the
    AsyncSession or AsyncConnection whose transaction does the work - call it first thing in that transaction, and
    again after every commit."""
    value = get_settings().db_long_statement_timeout_ms if ms is None else ms
    params = {"st": str(max(0, int(value)))}
    sql = "SELECT set_config('statement_timeout', :st, true)"
    if idle_ms is not None:
        sql += ", set_config('idle_in_transaction_session_timeout', :it, true)"
        params["it"] = str(max(0, int(idle_ms)))
    await db.execute(text(sql), params)


# SQLSTATEs of a NEW connection the server refused: too_many_connections ("too many clients", or a database's / role's
# CONNECTION LIMIT) and cannot_connect_now (starting up / shutting down) - capacity, not a bug: a retryable 503
CONNECTION_REFUSED = ("53300", "57P03")


def db_timeout_kind(exc: BaseException) -> str | None:
    """``"statement"`` when ``exc`` is (or wraps) a statement cancelled by ``statement_timeout`` (asyncpg
    ``QueryCanceledError``, SQLSTATE 57014), ``"pool"`` when no pooled connection became free within
    ``pool_timeout`` (``sqlalchemy.exc.TimeoutError``), ``"connections"`` when PostgreSQL refused a new connection
    (``CONNECTION_REFUSED``), else ``None``. app/core/errors.py answers every non-None kind with the friendly 503."""
    from sqlalchemy.exc import TimeoutError as PoolTimeoutError

    if isinstance(exc, PoolTimeoutError):
        return "pool"
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        codes = (getattr(current, "sqlstate", None), getattr(current, "pgcode", None))
        if QUERY_CANCELED in codes:
            return "statement"
        if any(code in CONNECTION_REFUSED for code in codes):
            return "connections"
        current = getattr(current, "orig", None) or current.__cause__
    return None


# --------------------------------------------------------------------------- unavailable (overload / outage)
# SQLSTATEs of a server going away under a connection: admin_shutdown, crash_shutdown (besides class 08, "connection
# exception": could not connect, connection failure, connection does not exist ...)
CONNECTION_LOST = ("57P01", "57P02")
_DRIVER_DIRS = (f"{os.sep}sqlalchemy{os.sep}", f"{os.sep}asyncpg{os.sep}")


def _raised_by_driver(exc: BaseException) -> bool:
    """Was ``exc`` raised inside SQLAlchemy or asyncpg - a socket error while connecting to or talking with PostgreSQL
    (asyncpg's connect raises a plain ConnectionRefusedError / TimeoutError) - and not by another client of the same
    code path (a job's HTTP call, Redis)?"""
    tb = exc.__traceback__
    while tb is not None:
        if any(part in tb.tb_frame.f_code.co_filename for part in _DRIVER_DIRS):
            return True
        tb = tb.tb_next
    return False


def db_unavailable(exc: BaseException, *, statement_timeouts: bool = False) -> str | None:
    """Did the database fail to serve this because it is overloaded or out of reach right now - never because of the
    statement itself? ``"pool"`` (no pooled connection free within DB_POOL_TIMEOUT_S), ``"connections"`` (PostgreSQL
    refused a new connection: too many clients, starting up or shutting down) or ``"connection"`` (one could not be
    opened or was lost: SQLSTATE class 08 / 57P01 / 57P02, a SQLAlchemy error that invalidated its connection, a socket
    error raised by the driver); with ``statement_timeouts`` also ``"statement"`` (for steps whose statements are tiny,
    where a cancelled one can only mean overload or a lock - a job handler's slow query may be slow every time). None
    for anything else.

    Work outside a request (presence, the job runner, the worker loops) logs these once a minute per call site and
    retries later; /ws closes with 1013 (app/core/overload.py). A request answers every ``db_timeout_kind`` with the
    friendly 503 (app/core/errors.py)."""
    kind = db_timeout_kind(exc)
    if kind == "statement":
        return kind if statement_timeouts else None
    if kind is not None:
        return kind
    from sqlalchemy.exc import DBAPIError, DisconnectionError

    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, DisconnectionError) or (isinstance(current, DBAPIError)
                                                       and current.connection_invalidated):
            return "connection"
        for code in (getattr(current, "sqlstate", None), getattr(current, "pgcode", None)):
            if isinstance(code, str) and (code.startswith("08") or code in CONNECTION_LOST):
                return "connection"
        if isinstance(current, OSError) and _raised_by_driver(current):
            return "connection"
        current = getattr(current, "orig", None) or current.__cause__
    return None


class _CancelledCleanupFilter(logging.Filter):
    """SQLAlchemy logs "Exception terminating connection" (or "... closing ...", "... during reset ...") with a
    traceback when the task giving a connection back is cancelled half-way: a client that went away while a query or a
    ROLLBACK of its request was in flight, a timeout around a query. SQLAlchemy drops that connection (PostgreSQL rolls
    its transaction back) and lets the cancellation go on, as it should - an expected event, not an error: one DEBUG
    line on ironpulse.db instead. Every other pool record passes unchanged."""

    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        if isinstance(exc, asyncio.CancelledError):
            log.debug("%s - its task was cancelled (a client that went away): the connection is dropped",
                      record.getMessage())
            return False
        return True


CANCELLED_CLEANUP_FILTER = _CancelledCleanupFilter()


def _quiet_cancelled_cleanup() -> None:
    """Install ``CANCELLED_CLEANUP_FILTER`` on the loggers of SQLAlchemy's pool classes (each logs on
    ``sqlalchemy.pool.impl.<Class>``; a filter on a parent logger would not see those records). Idempotent."""
    from sqlalchemy import pool as sa_pool

    for name in ("Pool", "QueuePool", "AsyncAdaptedQueuePool", "NullPool", "SingletonThreadPool", "StaticPool",
                 "AssertionPool"):
        cls = getattr(sa_pool, name, None)
        if cls is None:
            continue
        logger = logging.getLogger(f"{cls.__module__}.{cls.__name__}")
        if CANCELLED_CLEANUP_FILTER not in logger.filters:
            logger.addFilter(CANCELLED_CLEANUP_FILTER)


_quiet_cancelled_cleanup()


# --------------------------------------------------------------------------- connection budget
# Each worker process has its own pool: up to DB_POOL_SIZE + DB_MAX_OVERFLOW connections, shared by its requests and
# its background work (jobs, periodic tasks, worker loops), plus one LISTEN connection with REALTIME_PG_BRIDGE. Inside
# that budget a burst queues in the pool (DB_POOL_TIMEOUT_S, then the friendly 503). PostgreSQL itself refuses a new
# connection ("too many clients") beyond max_connections minus its reserved slots, and beyond a database's / role's
# CONNECTION LIMIT - the check below warns at startup when the workers' pools could get there (ARCHITECTURE.md
# "Database sizing": formula, defaults, PgBouncer).
BUDGET_SHARE = 0.8  # all workers' pools together may claim at most this share of the server's connection slots
WORKERS_ENV = "IRONPULSE_WORKERS"  # the launcher's hand-off (app/__main__.py): how many worker processes it started
BUDGET_SQL = (  # one round trip; current_setting() = SHOW
    "SELECT current_setting('max_connections')::int AS max_connections, "
    "current_setting('superuser_reserved_connections')::int AS superuser_reserved, "
    "coalesce(current_setting('reserved_connections', true), '0')::int AS reserved, "  # PostgreSQL 16+
    "(SELECT datconnlimit FROM pg_database WHERE datname = current_database()) AS database_limit, "
    "(SELECT rolconnlimit FROM pg_roles WHERE rolname = current_user) AS role_limit, "
    "(SELECT rolsuper FROM pg_roles WHERE rolname = current_user) AS superuser, "
    "current_database() AS database"
)


def _positive_int(value: object) -> int | None:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def configured_workers(environ: Mapping[str, str] | None = None, argv: Sequence[str] | None = None) -> int:
    """How many worker processes serve the app, each with its own pool: the launcher's hand-off (IRONPULSE_WORKERS, set
    by ``python -m app`` and loadtest/run_server.sh), else the ``--workers N`` of a uvicorn command line (its worker
    processes inherit the master's argv; ``--reload`` runs one process), else UVICORN_WORKERS (the same option as an
    environment variable; UVICORN_RELOAD runs one process), else WEB_CONCURRENCY (the uvicorn / gunicorn / PaaS
    default), else 1."""
    env = os.environ if environ is None else environ
    if n := _positive_int(env.get(WORKERS_ENV)):
        return n
    args = list(sys.argv if argv is None else argv)
    if "--reload" in args or str(env.get("UVICORN_RELOAD", "")).strip().lower() in ("1", "true", "t", "yes", "y", "on"):
        return 1
    for i, arg in enumerate(args):
        if arg == "--workers" and i + 1 < len(args) and (n := _positive_int(args[i + 1])):
            return n
        if arg.startswith("--workers=") and (n := _positive_int(arg.split("=", 1)[1])):
            return n
    return _positive_int(env.get("UVICORN_WORKERS")) or _positive_int(env.get("WEB_CONCURRENCY")) or 1


@dataclass(frozen=True, slots=True)
class ConnectionBudget:
    """The connections the app's workers may open (``total`` = workers x per-worker) against what PostgreSQL lets this
    role open. It fits when ``problems()`` is empty: at most BUDGET_SHARE of the server's slots (the rest stays free for
    psql, backups, migrations, monitoring and anything else on the server) and within the database's / role's
    CONNECTION LIMIT."""

    workers: int
    pool_size: int
    max_overflow: int
    bridge: int = 0  # per worker: the LISTEN connection of REALTIME_PG_BRIDGE (outside the pool)
    max_connections: int = 100
    reserved: int = 3  # superuser_reserved_connections + reserved_connections
    database_limit: int | None = None  # the database's CONNECTION LIMIT (None: none, or a superuser role)
    role_limit: int | None = None  # the role's CONNECTION LIMIT
    database: str = ""

    @property
    def unbounded(self) -> bool:
        """SQLAlchemy reads pool_size 0 / max_overflow -1 as "no limit"."""
        return self.pool_size <= 0 or self.max_overflow < 0

    @property
    def per_worker(self) -> int:
        return self.pool_size + max(0, self.max_overflow) + self.bridge

    @property
    def total(self) -> int:
        return self.workers * self.per_worker

    @property
    def server_slots(self) -> int:
        """Connections a normal role can open: max_connections minus the reserved slots."""
        return max(0, self.max_connections - self.reserved)

    @property
    def server_share(self) -> int:
        return int(self.server_slots * BUDGET_SHARE)

    def formula(self) -> str:
        bridge = f" + {self.bridge} LISTEN" if self.bridge else ""
        return (f"{self.workers} worker{'s' if self.workers != 1 else ''} x (pool {self.pool_size} + overflow "
                f"{self.max_overflow}{bridge}) = {self.total} connections")

    def problems(self) -> list[str]:
        out: list[str] = []
        if self.unbounded:
            out.append("the pool has no upper bound (DB_POOL_SIZE 0 or DB_MAX_OVERFLOW -1)")
        elif self.total > self.server_share:
            out.append(f"PostgreSQL allows {self.server_slots} (max_connections {self.max_connections} - "
                       f"{self.reserved} reserved) and the app should claim at most {round(BUDGET_SHARE * 100)} % of "
                       f"them ({self.server_share})")
        for label, limit in (("database", self.database_limit), ("role", self.role_limit)):
            if limit is not None and (self.unbounded or self.total > limit):
                out.append(f"the {label}'s CONNECTION LIMIT is {limit}")
        return out

    @classmethod
    def from_server(cls, row: Mapping[str, Any], *, workers: int | None = None, pool_size: int | None = None,
                    max_overflow: int | None = None) -> ConnectionBudget:
        """From a ``BUDGET_SQL`` row and the settings (or the engine's actual pool sizing)."""
        s = get_settings()
        superuser = bool(row.get("superuser"))

        def limit(value: Any) -> int | None:  # -1 = no limit; superusers are exempt from both limits
            n = None if value is None else int(value)
            return None if superuser or n is None or n < 0 else n

        return cls(
            workers=workers or configured_workers(),
            pool_size=int(s.db_pool_size if pool_size is None else pool_size),
            max_overflow=int(s.db_max_overflow if max_overflow is None else max_overflow),
            bridge=1 if getattr(s, "realtime_pg_bridge", False) else 0,
            max_connections=int(row["max_connections"]),
            reserved=int(row.get("superuser_reserved") or 0) + int(row.get("reserved") or 0),
            database_limit=limit(row.get("database_limit")), role_limit=limit(row.get("role_limit")),
            database=str(row.get("database") or ""),
        )


def report_connection_budget(budget: ConnectionBudget) -> bool:
    """Log the budget: INFO when it fits, a WARNING with the computed budget and the limit it breaks otherwise."""
    problems = budget.problems()
    if not problems:
        log.info("database connection budget: %s of the %s PostgreSQL allows (max_connections %s - %s reserved)",
                 budget.formula(), budget.server_slots, budget.max_connections, budget.reserved)
        return True
    log.warning("database connection budget exceeded: %s, but %s - under load PostgreSQL refuses new connections "
                "('too many clients'). Lower DB_POOL_SIZE / DB_MAX_OVERFLOW (a full pool makes requests wait, then "
                "answers 503), run fewer workers, raise max_connections or put PgBouncer in front (ARCHITECTURE.md "
                "'Database sizing')", budget.formula(), "; ".join(problems))
    return False


async def check_connection_budget(conn: Any = None, *, workers: int | None = None, pool_size: int | None = None,
                                  max_overflow: int | None = None) -> ConnectionBudget:
    """Read the server's limits (``BUDGET_SQL``) on ``conn`` (AsyncConnection / AsyncSession; default: a connection of
    the app engine), log the budget (``report_connection_budget``) and return it."""
    if conn is None:
        async with get_engine().connect() as own:
            return await check_connection_budget(own, workers=workers, pool_size=pool_size, max_overflow=max_overflow)
    row = (await conn.execute(text(BUDGET_SQL))).mappings().one()
    budget = ConnectionBudget.from_server(row, workers=workers, pool_size=pool_size, max_overflow=max_overflow)
    report_connection_budget(budget)
    return budget


def _budget_check_listener(pool_size: int, max_overflow: int) -> Callable[[Any, Any], None]:
    """Pool ``connect`` listener (registered ``once=True``) of a pooled app engine: the once-per-process startup check
    on the first connection it opens - one cheap query on the raw driver connection (no transaction left behind).
    A failure is logged and ignored: a diagnostic never breaks a connection."""

    def on_first_connect(dbapi_connection: Any, connection_record: Any) -> None:
        try:
            row = dbapi_connection.run_async(lambda raw: raw.fetchrow(BUDGET_SQL))
            report_connection_budget(ConnectionBudget.from_server(dict(row), pool_size=pool_size,
                                                                  max_overflow=max_overflow))
        except Exception as exc:  # noqa: BLE001
            log.info("database connection budget not checked: %s", (str(exc).splitlines() or [""])[0][:200])

    return on_first_connect


class BackgroundSession(Session):
    """Session class behind ``session_scope()``: jobs, periodic tasks, worker loops, startup and scripts."""


@event.listens_for(BackgroundSession, "after_begin")
def _background_transaction_limits(session: Session, transaction: Any, connection: Any) -> None:
    """Background transactions may sit idle while an AI provider or OCR engine answers (a job handler that read its
    rows first): they get DB_BACKGROUND_IDLE_TX_TIMEOUT_MS instead of the request limit. Statements keep the
    per-connection ``statement_timeout`` - batch work that needs longer calls ``long_statement``."""
    if getattr(transaction, "nested", False):
        return
    s = get_settings()
    base, background = int(s.db_idle_tx_timeout_ms), int(s.db_background_idle_tx_timeout_ms)
    if 0 < base < background:  # only ever a longer limit (0 = no limit at all: nothing to lift)
        connection.exec_driver_sql(f"SET LOCAL idle_in_transaction_session_timeout = {background}")


# --------------------------------------------------------------------------- engine / sessions
_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None
_background_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def engine_options(**overrides: object) -> dict[str, object]:
    """``create_async_engine`` keyword arguments: pool sizing, DB_POOL_TIMEOUT_S and every connection's
    ``server_settings()`` (an explicit ``connect_args={"server_settings": {...}}`` override wins per key). With a
    ``poolclass`` override (e.g. NullPool in tests) the pool sizing / timeout arguments are left out."""
    settings = get_settings()
    kwargs: dict[str, object] = {
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        "pool_pre_ping": True,
        "pool_recycle": 1800,
        "pool_timeout": settings.db_pool_timeout_s,
        "echo": settings.db_echo,
    }
    overrides = dict(overrides)
    connect_args: dict[str, Any] = dict(overrides.pop("connect_args", None) or {})  # type: ignore[call-overload]
    limits = {**server_settings(), **dict(connect_args.get("server_settings") or {})}
    if limits:
        connect_args["server_settings"] = limits
    if connect_args:
        kwargs["connect_args"] = connect_args
    kwargs.update(overrides)
    if "poolclass" in overrides:  # e.g. NullPool in tests: no pool sizing arguments
        for key in ("pool_size", "max_overflow", "pool_recycle", "pool_timeout"):
            if key not in overrides:
                kwargs.pop(key, None)
    return kwargs


def configure_engine(url: str | None = None, **overrides: object) -> AsyncEngine:
    """(Re)create the global engine (``engine_options``). Tests and scripts may point it at another database."""
    global _engine, _sessionmaker, _background_sessionmaker
    settings = get_settings()
    options = engine_options(**overrides)
    _engine = create_async_engine(url or settings.database_url, **options)
    _install_listeners(_engine)
    if "poolclass" not in overrides:  # a pooled app engine (not NullPool tests / scripts): the startup budget check
        event.listen(_engine.sync_engine, "connect",
                     _budget_check_listener(int(options["pool_size"]), int(options["max_overflow"])), once=True)
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False, autoflush=False)
    _background_sessionmaker = async_sessionmaker(_engine, expire_on_commit=False, autoflush=False,
                                                  sync_session_class=BackgroundSession)
    return _engine


def get_engine() -> AsyncEngine:
    return _engine if _engine is not None else configure_engine()


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        configure_engine()
    assert _sessionmaker is not None
    return _sessionmaker


def get_background_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _background_sessionmaker is None:
        configure_engine()
    assert _background_sessionmaker is not None
    return _background_sessionmaker


async def dispose_engine() -> None:
    global _engine, _sessionmaker, _background_sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
    _background_sessionmaker = None


REQUEST_SESSION_STATE = "ip_db_session"  # scope["state"] key of the request's session (release_request_session)
RELEASE_TIMEOUT_S = 10.0  # a ROLLBACK at the start of a response may take this long, cancelled or not (then: dropped)


async def get_db(request: HTTPConnection) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request. Handlers call ``await db.commit()`` explicitly. The session is
    remembered in the request's state so its connection goes back to the pool when the response starts
    (``release_request_session``) - FastAPI ends this dependency only after the whole body has been sent."""
    async with get_sessionmaker()() as session:
        request.scope.setdefault("state", {})[REQUEST_SESSION_STATE] = session
        try:
            yield session
        except Exception:
            # Detach loaded objects first: rollback() would expire them, and the error page still needs
            # request.state.user (otherwise every 403/404 for a signed-in user falls back to raw JSON). A flush or
            # commit that FAILED has expired them already (SQLAlchemy rolls the transaction back at once): the error
            # page reloads that user in a session of its own (app/core/errors.py _page_safe_user).
            session.expunge_all()
            await session.rollback()
            raise


async def release_request_session(scope: Mapping[str, Any]) -> bool:
    """End the request session's transaction and give its connection back to the pool - called when the response
    starts (app/main.py ``ReleaseDBOnResponseMiddleware``). The handler has returned by then: whatever it did not
    commit would be rolled back at the end of the request anyway, only after the whole body went out - to a slow phone
    that can take seconds, all of it 'idle in transaction' on a pooled connection. ``close()`` keeps loaded objects
    usable (detached, attributes loaded) and the session usable: a streaming body that queries again gets a fresh
    transaction, ended by ``get_db`` as before. True when a transaction was ended.

    A FileResponse / StreamingResponse sends its first message from a task of its own, which Starlette cancels when the
    client goes away (under overload: clients that gave up waiting). Cut short mid-ROLLBACK, SQLAlchemy tore the
    connection down and logged "Exception terminating connection" with a CancelledError traceback (the stress test's
    1,600-user step: 30 of them). The ROLLBACK is shielded from that cancellation (bounded by RELEASE_TIMEOUT_S): the
    connection goes back to the pool healthy, then the cancellation goes on."""
    session = (scope.get("state") or {}).get(REQUEST_SESSION_STATE)
    if session is None or not session.in_transaction():
        return False
    try:
        with anyio.move_on_after(RELEASE_TIMEOUT_S, shield=True) as scope_guard:
            await session.close()
    except Exception as exc:  # noqa: BLE001 - a broken connection is discarded by the pool; the response goes on
        log.warning("could not release the request's database session: %s", (str(exc).splitlines() or [""])[0][:200])
        return False
    if scope_guard.cancelled_caught:  # an unresponsive server: the pool drops that connection
        log.warning("could not release the request's database session within %.0f s", RELEASE_TIMEOUT_S)
        return False
    return True


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Standalone unit of work for background jobs/scripts: commit on success, rollback on error.

    A ``BackgroundSession``: its transactions get the background idle-in-transaction limit."""
    async with get_background_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def ensure_extensions(conn) -> None:  # noqa: ANN001
    for ext in EXTENSIONS:
        exists = await conn.scalar(text("SELECT 1 FROM pg_extension WHERE extname = :n"), {"n": ext})
        if exists:
            continue
        try:
            await conn.execute(text(f'CREATE EXTENSION IF NOT EXISTS "{ext}"'))
        except Exception as exc:  # needs superuser - scripts/init_db.py uses DATABASE_ADMIN_URL
            raise RuntimeError(
                f"PostgreSQL extension '{ext}' is missing and could not be created ({exc}). "
                "Run: python scripts/manage.py init-db  (with DATABASE_ADMIN_URL set to a superuser URL)"
            ) from exc


_IN_LIST_CHECK = re.compile(r"^\s*([a-z_][a-z0-9_]*)\s+IN\s*\(\s*('(?:[^']|'')*'(?:\s*,\s*'(?:[^']|'')*')*)\s*\)\s*$",
                            re.IGNORECASE)


def _enum_values(cons: object) -> set[str] | None:
    """The allowed values of a one-column value-list CHECK: an enum column's, or a plain ``<column> IN ('a', 'b')``
    CHECK (app/models/trust.py ``_in``) - both are widened in place when the model's list changes."""
    from sqlalchemy import Enum as SAEnum

    cols = list(getattr(cons, "columns", []) or [])
    if len(cols) == 1 and isinstance(cols[0].type, SAEnum):
        return {str(v) for v in cols[0].type.enums}
    sqltext = getattr(cons, "sqltext", None)
    match = _IN_LIST_CHECK.match(str(sqltext)) if sqltext is not None else None
    if match is not None:
        return {v.replace("''", "'") for v in re.findall(r"'((?:[^']|'')*)'", match.group(2))}
    return None


async def sync_schema(conn) -> list[str]:  # noqa: ANN001
    """ADDITIVE schema sync for existing databases (create_all never alters existing tables).

    Adds missing columns, indexes, unique and CHECK constraints, and widens enum CHECK constraints
    whose allowed values changed. Never drops tables, columns or data. Each statement runs in a
    savepoint so a single failure (e.g. existing rows violating a new CHECK) is logged, not fatal."""
    import re

    from sqlalchemy import CheckConstraint, UniqueConstraint
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy.schema import AddConstraint, CreateColumn, CreateIndex

    def _snapshot(sync_conn):  # noqa: ANN001
        insp = sa_inspect(sync_conn)
        return {
            t: {
                "columns": {c["name"] for c in insp.get_columns(t)},
                "indexes": {i["name"] for i in insp.get_indexes(t)},
                "uniques": {u["name"] for u in insp.get_unique_constraints(t)},
            }
            for t in insp.get_table_names()
        }

    preparer = conn.dialect.identifier_preparer

    def rendered(obj) -> str:  # noqa: ANN001
        """Name exactly as PostgreSQL stores it (SQLAlchemy truncates names > 63 chars with a hash)."""
        return preparer.format_constraint(obj).strip('"')

    snap = await conn.run_sync(_snapshot)
    rows = await conn.execute(text(
        "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE contype = 'c' AND connamespace = 'public'::regnamespace"
    ))
    checks = {name: definition for name, definition in rows.all()}
    applied: list[str] = []

    async def run(label: str, *stmts) -> None:  # noqa: ANN002
        try:
            async with conn.begin_nested():  # all or nothing: e.g. a data fix and the unique index it makes possible
                for stmt in stmts:
                    await conn.execute(stmt)
            applied.append(label)
        except Exception as exc:  # noqa: BLE001
            log.warning("schema sync could not apply %s: %s", label, str(exc).splitlines()[0][:200])

    for table in Base.metadata.sorted_tables:
        info = snap.get(table.name)
        if info is None:
            continue  # brand-new table: create_all already made it
        for col in table.columns:
            if col.name in info["columns"]:
                continue
            ddl = str(CreateColumn(col).compile(dialect=conn.dialect))
            if not col.nullable and col.server_default is None:
                ddl = ddl.replace(" NOT NULL", "")  # existing rows have no value yet
            await run(f"column {table.name}.{col.name}", text(f'ALTER TABLE "{table.name}" ADD COLUMN {ddl}'))
        for idx in table.indexes:
            if idx.name and rendered(idx) not in info["indexes"]:
                fix = (table.info.get(BEFORE_UNIQUE_INDEX) or {}).get(idx.name)  # existing rows that would break it
                await run(f"index {idx.name}", *([text(fix)] if fix else []), CreateIndex(idx, if_not_exists=True))
        for cons in table.constraints:
            name = rendered(cons) if cons.name else None
            if not name:
                continue
            if isinstance(cons, UniqueConstraint):
                if name not in info["uniques"] and name not in info["indexes"]:
                    await run(f"unique {name}", AddConstraint(cons))
            elif isinstance(cons, CheckConstraint):
                current = checks.get(name)
                if current is None:
                    await run(f"check {name}", AddConstraint(cons))
                    continue
                wanted = _enum_values(cons)
                if wanted is not None and set(re.findall(r"'((?:[^']|'')*)'", current)) != wanted:
                    try:
                        async with conn.begin_nested():
                            await conn.execute(text(f'ALTER TABLE "{table.name}" DROP CONSTRAINT "{name}"'))
                            await conn.execute(AddConstraint(cons))
                        applied.append(f"enum check {name}")
                    except Exception as exc:  # noqa: BLE001
                        log.warning("schema sync could not update %s: %s", name, str(exc).splitlines()[0][:200])
    if applied:
        log.info("schema sync applied: %s", ", ".join(applied))
    return applied


# --------------------------------------------------------------------------- start-up serialisation
STARTUP_LOCK_KEY = 0x49504C53  # "IPLS": pg_advisory_lock key of create_all / sync_schema / bootstrap (per database)
STARTUP_LOCK_POLL_S = 0.2
STARTUP_LOCK_LOG_EVERY_S = 10.0
_startup_lock_held: contextvars.ContextVar[bool] = contextvars.ContextVar("ip_startup_lock_held", default=False)


@asynccontextmanager
async def startup_lock(key: int = STARTUP_LOCK_KEY, *, what: str = "start-up") -> AsyncIterator[None]:
    """Run the block holding a PostgreSQL advisory lock, so that several worker processes (or app hosts) starting
    together apply ``create_all``, the additive ``sync_schema`` and the bootstrap ONE AT A TIME. Racing, they stopped
    a fresh multi-worker start: three of four workers failed with UniqueViolation on the same CREATE TABLE / CREATE
    INDEX / super-admin INSERT, and uvicorn stops the whole server when a worker fails to start. The others wait here,
    then find everything in place (every step is idempotent).

    A session-level lock on a connection of its own (NullPool, autocommit, no statement limit): it is never 'idle in
    transaction', waits however long the first process's DDL takes (logging every STARTUP_LOCK_LOG_EVERY_S), and its
    close releases the lock even if the unlock failed (a crashed process's lock goes with its connection). Advisory
    locks are per database: test databases never wait for each other. Re-entrant within one task (``create_all``
    inside the lifespan's lock)."""
    if _startup_lock_held.get():
        yield
        return
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(get_engine().url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
            await conn.exec_driver_sql("SET statement_timeout = 0")
            started = logged = time.monotonic()
            while not await conn.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": key}):
                now = time.monotonic()
                if now - logged >= STARTUP_LOCK_LOG_EVERY_S:
                    logged = now
                    log.info("waiting %.0f s for another process's %s (schema sync, bootstrap)", now - started, what)
                await asyncio.sleep(STARTUP_LOCK_POLL_S)
            token = _startup_lock_held.set(True)
            try:
                yield
            finally:
                _startup_lock_held.reset(token)
                try:
                    await conn.scalar(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
                except Exception:  # noqa: BLE001 - closing the connection below releases it anyway
                    log.warning("could not release the %s lock explicitly; closing its connection", what)
    finally:
        await engine.dispose()


async def create_all() -> None:
    """Create extensions (if permitted) and all tables, then apply additive changes. Idempotent; one process at a
    time (``startup_lock``)."""
    import app.models  # noqa: F401  - registers every table on Base.metadata

    async with startup_lock(what="schema sync"):
        engine = get_engine()
        async with engine.begin() as conn:
            await ensure_extensions(conn)
        # re-create the pool so new connections pick up the pgvector codec
        await engine.dispose()
        async with engine.begin() as conn:
            # DDL is not request work: building a new index on an existing large table can take longer than
            # statement_timeout (large tables: pre-build new indexes CONCURRENTLY first - ARCHITECTURE.md §3)
            await long_statement(conn, 0)
            await conn.run_sync(Base.metadata.create_all)
            if get_settings().auto_migrate:
                await sync_schema(conn)


SUPERSEDED_INDEXES = "superseded_indexes"  # Table.info key (``__table_args__`` dict): {old index: the one replacing it}
# Table.info key: {unique index: SQL that makes the existing rows fit it} - run right before the index is built (by
# ``sync_schema`` in the same savepoint, by ``create_indexes_concurrently`` just before the build), e.g. closing
# duplicate rows an old race left behind. Must be idempotent: it runs again whenever the index is still missing.
BEFORE_UNIQUE_INDEX = "before_unique_index"


async def create_indexes_concurrently(engine: AsyncEngine | None = None) -> list[str]:
    """Build every index the models declare but the database lacks with ``CREATE INDEX CONCURRENTLY`` (no write lock,
    outside any transaction, no statement limit) and return their names. Run it against a large production database
    BEFORE deploying code that adds indexes - ``sync_schema`` (a plain, write-blocking CREATE INDEX at startup) then
    finds them and skips them. An index left INVALID by an interrupted concurrent build is dropped and rebuilt.
    Tables that do not exist yet are left to ``create_all``::

        python -c "import asyncio, app.db as d; print(asyncio.run(d.create_indexes_concurrently()))"

    Then it drops (``DROP INDEX CONCURRENTLY``) the indexes a model declares superseded (``Table.info``
    SUPERSEDED_INDEXES, e.g. a single-column foreign-key index replaced by a composite one with the same leading
    column), each only once its replacement exists and is valid; those come back as ``"drop <name>"``. ``sync_schema``
    never drops anything (a DROP at startup would queue every query of the table behind its lock)."""
    import re

    import app.models  # noqa: F401  - registers every table on Base.metadata
    from sqlalchemy.pool import NullPool
    from sqlalchemy.schema import CreateIndex

    own = engine is None
    if engine is None:
        engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    built: list[str] = []
    try:
        async with engine.connect() as conn:
            conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
            await conn.exec_driver_sql("SET statement_timeout = 0")
            try:
                tables = set((await conn.exec_driver_sql(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")).scalars())
                existing = dict((await conn.exec_driver_sql(
                    "SELECT c.relname, i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public'")).all())
                preparer = conn.dialect.identifier_preparer
                for table in Base.metadata.sorted_tables:
                    if table.name not in tables:
                        continue
                    for idx in table.indexes:
                        name = preparer.format_constraint(idx).strip('"') if idx.name else None
                        if not name or existing.get(name) is True:
                            continue
                        if name in existing:  # INVALID leftover of an interrupted concurrent build
                            await conn.exec_driver_sql(f'DROP INDEX CONCURRENTLY IF EXISTS "{name}"')
                        if fix := (table.info.get(BEFORE_UNIQUE_INDEX) or {}).get(idx.name):
                            log.info("making existing rows fit index %s", name)
                            await conn.exec_driver_sql(fix)
                        ddl = str(CreateIndex(idx, if_not_exists=True).compile(dialect=conn.dialect))
                        ddl = re.sub(r"^CREATE (UNIQUE )?INDEX ", lambda m: f"CREATE {m.group(1) or ''}INDEX CONCURRENTLY ",
                                     ddl.strip(), count=1)
                        log.info("building index %s concurrently", name)
                        await conn.exec_driver_sql(ddl)
                        built.append(name)
                valid = {name for name, ok in existing.items() if ok} | set(built)
                for table in Base.metadata.sorted_tables:
                    for old, new in sorted((table.info.get(SUPERSEDED_INDEXES) or {}).items()):
                        if table.name in tables and old in existing and new in valid:
                            log.info("dropping index %s concurrently (superseded by %s)", old, new)
                            await conn.exec_driver_sql(f'DROP INDEX CONCURRENTLY IF EXISTS "{old}"')
                            built.append(f"drop {old}")
            finally:
                await conn.exec_driver_sql("RESET statement_timeout")
    finally:
        if own:
            await engine.dispose()
    return built


async def drop_all() -> None:
    import app.models  # noqa: F401

    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
