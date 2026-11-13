"""Create the IronPulse PostgreSQL database (if missing), its extensions and every table.

    python scripts/init_db.py                     # DATABASE_URL / DATABASE_ADMIN_URL from .env
    python scripts/init_db.py --with-test-db      # also prepare TEST_DATABASE_URL's database (no tables)
    python scripts/init_db.py --wait 120          # wait up to 120 s for the server (Docker just started)
    python scripts/init_db.py --check             # report only - change nothing
    python scripts/manage.py init-db ...          # the same thing

Works the same on Windows, macOS and Linux and is safe to run repeatedly (idempotent).

How it connects
* ``DATABASE_ADMIN_URL`` (a superuser such as ``postgres``), when set, is used to create the app role,
  the databases and the extensions ``vector``, ``citext`` and ``pg_trgm`` (pgvector needs superuser rights).
* Without it, the app's own ``DATABASE_URL`` is used against the ``postgres`` maintenance database. That works
  when the app role may ``CREATE DATABASE`` and the extensions already exist (or are trusted).
* User-space "runtime" installs (``./setup.sh --db runtime`` on macOS/Linux) keep the superuser password in
  ``~/.ironpulse/pg_superuser_password``; it is picked up automatically for a local server on that port.
Then ``app.db.create_all()`` creates the tables (plus the additive schema sync).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

EXTENSIONS = ("vector", "citext", "pg_trgm")
NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,62}$")
LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "::1"}
URL_PREFIXES = ("postgresql+asyncpg://", "postgresql+psycopg://", "postgresql+psycopg2://", "postgresql://", "postgres://")

OK, CREATED, UPDATED, WARN, FAIL = "ok", "created", "updated", "warn", "fail"


def runtime_home() -> Path:
    return Path(os.environ.get("IRONPULSE_HOME") or (Path.home() / ".ironpulse"))


# ============================================================================= URL helpers
def normalize_url(url: str | None) -> str:
    """Any PostgreSQL URL -> the ``postgresql+asyncpg://`` form used by the app."""
    url = (url or "").strip()
    for prefix in URL_PREFIXES:
        if url.startswith(prefix):
            return "postgresql+asyncpg://" + url[len(prefix):]
    return url


def parse_url(url: str) -> Any:
    from sqlalchemy.engine import make_url

    return make_url(normalize_url(url))


def render_url(u: Any) -> str:
    return u.render_as_string(hide_password=False)


def with_database(url: str, database: str) -> str:
    return render_url(parse_url(url).set(database=database))


def redact(url: str | None) -> str:
    if not url:
        return "(not set)"
    try:
        return parse_url(url).render_as_string(hide_password=True)
    except Exception:  # noqa: BLE001
        return "(invalid URL)"


def asyncpg_connect_args(url: str) -> tuple[str, dict[str, Any]]:
    """(dsn, kwargs) for ``asyncpg.connect``. SQLAlchemy's ``?ssl=...`` becomes the ``ssl`` keyword."""
    u = parse_url(url)
    query = dict(u.query)
    kwargs: dict[str, Any] = {}
    ssl = query.pop("ssl", None)
    if ssl is not None:
        kwargs["ssl"] = ssl[0] if isinstance(ssl, tuple) else ssl
    return render_url(u.set(drivername="postgresql", query=query)), kwargs


def quote_ident(name: str) -> str:
    """Quote a role/database name for DDL. Only plain names are accepted (no injection surface)."""
    if not NAME_RE.match(name or ""):
        raise ValueError(f"unsupported PostgreSQL name {name!r} - use letters, digits, '_' or '-' (max 63)")
    return '"' + name.replace('"', '""') + '"'


def quote_literal(value: str) -> str:
    if "\x00" in value:
        raise ValueError("NUL byte in value")
    return "'" + value.replace("'", "''") + "'"


# ============================================================================= hints
def pgvector_install_hint(platform: str | None = None, pg_major: int | None = None) -> str:
    """OS-specific instructions for installing the pgvector extension into an existing server."""
    platform = platform or sys.platform
    major = str(pg_major or 17)
    if platform.startswith("win"):
        return (
            "pgvector is not installed in this PostgreSQL server. Windows: the easiest way is Docker "
            "(setup.ps1 -DbMode docker uses the pgvector/pgvector image). For a native install build it with "
            "Visual Studio 2022 Build Tools - in an Administrator PowerShell run: "
            f"scripts\\windows\\install_pgvector.ps1 -PgRoot \"C:\\Program Files\\PostgreSQL\\{major}\""
        )
    if platform == "darwin":
        return (
            "pgvector is not installed in this PostgreSQL server. macOS: `brew install pgvector` (for Homebrew "
            "postgresql@17), Postgres.app already bundles pgvector, or use the user-space runtime: ./setup.sh --db runtime"
        )
    return (
        "pgvector is not installed in this PostgreSQL server. Linux: Debian/Ubuntu `sudo apt install "
        f"postgresql-{major}-pgvector` (PGDG repository), Fedora `sudo dnf install pgvector`, Arch "
        "`sudo pacman -S pgvector` - or use ./setup.sh --db docker / --db runtime"
    )


def contrib_hint(platform: str | None = None) -> str:
    platform = platform or sys.platform
    if platform.startswith("linux"):
        return "Install the PostgreSQL contrib package (e.g. `sudo apt install postgresql-contrib` / `sudo dnf install postgresql-contrib`)."
    return "Your PostgreSQL build lacks the standard contrib extensions - reinstall PostgreSQL with contrib modules."


def superuser_hint() -> str:
    return (
        "Creating extensions needs a PostgreSQL superuser. Set DATABASE_ADMIN_URL in .env to a superuser URL, e.g. "
        "postgresql+asyncpg://postgres:<password>@localhost:5432/postgres - then run: python scripts/manage.py init-db"
    )


def start_server_hint() -> str:
    if sys.platform.startswith("win"):
        return (
            "Start PostgreSQL: Docker -> `docker compose --env-file .env -f docker/compose.yml up -d db`; native -> "
            "`Start-Service postgresql-x64-17` (Administrator PowerShell)."
        )
    return (
        "Start PostgreSQL: runtime -> `scripts/runtime_postgres.sh start`; Docker -> `docker compose --env-file .env "
        "-f docker/compose.yml up -d db`; Homebrew -> `brew services start postgresql@17`; Linux -> "
        "`sudo systemctl start postgresql`."
    )


def runtime_admin_url(app_url: str, home: Path | None = None) -> str | None:
    """Superuser URL of the user-space runtime cluster (``~/.ironpulse``), when it applies to ``app_url``."""
    home = home or runtime_home()
    pw_file = home / "pg_superuser_password"
    try:
        u = parse_url(app_url)
    except Exception:  # noqa: BLE001
        return None
    runtime_port = int(os.environ.get("IRONPULSE_PGPORT") or 5432)
    if (u.host or "") not in LOCAL_HOSTS or (u.port or 5432) != runtime_port or not pw_file.is_file():
        return None
    password = pw_file.read_text(encoding="utf-8").strip()
    if not password:
        return None
    return render_url(u.set(username="postgres", password=password, database="postgres"))


# ============================================================================= report
@dataclass
class Step:
    name: str
    status: str
    detail: str = ""
    hint: str = ""


@dataclass
class InitReport:
    steps: list[Step] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "", hint: str = "") -> Step:
        step = Step(name, status, detail, hint)
        self.steps.append(step)
        return step

    @property
    def ok(self) -> bool:
        return not any(s.status == FAIL for s in self.steps)

    def get(self, name: str) -> Step | None:
        return next((s for s in self.steps if s.name == name), None)


# ============================================================================= errors
def sqlstate(exc: BaseException) -> str:
    return str(getattr(exc, "sqlstate", "") or "")


def describe_connect_error(exc: BaseException, url: str) -> tuple[str, str]:
    """(detail, hint) for a failed connection attempt."""
    state = sqlstate(exc)
    try:
        u = parse_url(url)
        where, user, db = f"{u.host or 'localhost'}:{u.port or 5432}", u.username or "?", u.database or "?"
    except Exception:  # noqa: BLE001
        where, user, db = "?", "?", "?"
    text = str(exc).strip().splitlines()[0][:200] if str(exc).strip() else exc.__class__.__name__
    if state == "28P01":
        return (f"password authentication failed for role '{user}'",
                "Check the password in DATABASE_URL / DATABASE_ADMIN_URL (.env).")
    if state == "28000":
        return (f"login refused for role '{user}': {text}",
                "The role may not exist yet: set DATABASE_ADMIN_URL so init-db can create it, or check pg_hba.conf.")
    if state == "3D000":
        return (f"database '{db}' does not exist", "Run: python scripts/manage.py init-db (creates it).")
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return f"timed out connecting to {where}", start_server_hint()
    if isinstance(exc, OSError) and not state:
        return f"cannot reach PostgreSQL at {where} ({exc.__class__.__name__})", start_server_hint()
    return f"{exc.__class__.__name__}: {text}", ""


async def _connect(url: str, timeout: float = 8.0) -> Any:
    import asyncpg

    dsn, kwargs = asyncpg_connect_args(url)
    return await asyncpg.connect(dsn, timeout=timeout, **kwargs)


async def wait_for_server(url: str, seconds: float, log: Callable[[str], None] = print) -> BaseException | None:
    """Retry until the server answers (auth/database errors count as "up"). Returns the last error or None."""
    deadline = time.monotonic() + max(0.0, seconds)
    announced = False
    while True:
        try:
            conn = await _connect(url, timeout=5.0)
            await conn.close()
            return None
        except Exception as exc:  # noqa: BLE001
            if sqlstate(exc):  # the server answered (wrong password, missing database ...) - it is up
                return None
            if time.monotonic() >= deadline:
                return exc
            if not announced:
                log("waiting for PostgreSQL to accept connections ...")
                announced = True
            await asyncio.sleep(2.0)


# ============================================================================= steps
async def _ensure_role(admin: Any, admin_user: str, role: str, password: str | None, report: InitReport,
                       check_only: bool) -> None:
    exists = await admin.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", role)
    if exists or role == admin_user:
        report.add(f"Role {role}", OK, "exists")
        return
    if check_only:
        report.add(f"Role {role}", WARN, "missing", "Run init-db without --check to create it.")
        return
    stmt = f"CREATE ROLE {quote_ident(role)} LOGIN"
    if password:
        stmt += f" PASSWORD {quote_literal(password)}"
    await admin.execute(stmt)
    report.add(f"Role {role}", CREATED, "login role created")


async def _ensure_database(dbname: str, owner: str, url: str, admin: Any, admin_user: str | None,
                           report: InitReport, check_only: bool) -> bool:
    label = f"Database {dbname}"
    conn = admin
    own_conn = False
    if conn is None:
        # without a superuser: is the database already there (connect to it directly)?
        try:
            probe = await _connect(url)
            await probe.close()
            report.add(label, OK, "exists")
            return True
        except Exception as exc:  # noqa: BLE001
            if sqlstate(exc) != "3D000":
                detail, hint = describe_connect_error(exc, url)
                report.add(label, FAIL, detail, hint)
                return False
        if check_only:
            report.add(label, WARN, "missing", "Run init-db without --check to create it.")
            return False
        last: BaseException = RuntimeError("no maintenance database reachable")
        for maintenance in ("postgres", "template1"):
            try:
                conn = await _connect(with_database(url, maintenance))
                own_conn = True
                break
            except Exception as exc:  # noqa: BLE001
                last = exc
        if conn is None:
            detail, hint = describe_connect_error(last, url)
            report.add(label, FAIL, f"missing and cannot connect to a maintenance database: {detail}",
                       hint or "Set DATABASE_ADMIN_URL to a superuser URL.")
            return False
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", dbname)
        if exists:
            report.add(label, OK, "exists")
            return True
        if check_only:
            report.add(label, WARN, "missing", "Run init-db without --check to create it.")
            return False
        owner_clause = f" OWNER {quote_ident(owner)}" if admin is not None and owner != admin_user else ""
        try:
            await conn.execute(f"CREATE DATABASE {quote_ident(dbname)}{owner_clause}")
        except Exception as exc:  # noqa: BLE001
            if sqlstate(exc) == "42501":
                report.add(label, FAIL, f"the role is not allowed to create database '{dbname}'",
                           "Set DATABASE_ADMIN_URL to a superuser URL, or ask your DBA to run: "
                           f"CREATE DATABASE {quote_ident(dbname)} OWNER {quote_ident(owner)};")
                return False
            raise
        report.add(label, CREATED, f"owner {owner}")
        return True
    finally:
        if own_conn and conn is not None:
            await conn.close()


async def _check_login(url: str, admin: Any, role: str, password: str | None, report: InitReport,
                       check_only: bool, label: str) -> bool:
    try:
        conn = await _connect(url)
        await conn.close()
        report.add(label, OK, f"{role} can sign in")
        return True
    except Exception as exc:  # noqa: BLE001
        if sqlstate(exc) == "28P01" and admin is not None and password and not check_only:
            await admin.execute(f"ALTER ROLE {quote_ident(role)} PASSWORD {quote_literal(password)}")
            try:
                conn = await _connect(url)
                await conn.close()
            except Exception as exc2:  # noqa: BLE001
                detail, hint = describe_connect_error(exc2, url)
                report.add(label, FAIL, detail, hint)
                return False
            report.add(label, UPDATED, f"password of role {role} updated to match the URL in .env")
            return True
        detail, hint = describe_connect_error(exc, url)
        report.add(label, FAIL, detail, hint)
        return False


async def _ensure_extensions(dbname: str, conn_url: str, report: InitReport, check_only: bool,
                             via_admin: bool) -> None:
    try:
        conn = await _connect(conn_url)
    except Exception as exc:  # noqa: BLE001
        detail, hint = describe_connect_error(exc, conn_url)
        report.add(f"Extensions in {dbname}", FAIL, detail, hint)
        return
    try:
        installed = {r["extname"]: r["extversion"] for r in await conn.fetch("SELECT extname, extversion FROM pg_extension")}
        available = {
            r["name"]: r["default_version"]
            for r in await conn.fetch(
                "SELECT name, default_version FROM pg_available_extensions WHERE name = ANY($1::text[])", list(EXTENSIONS)
            )
        }
        server_major = int(str(await conn.fetchval("SHOW server_version_num"))[:-4] or 0)
        for ext in EXTENSIONS:
            label = f"Extension {ext} ({dbname})"
            if ext in installed:
                report.add(label, OK, f"version {installed[ext]}")
                continue
            if ext not in available:
                hint = pgvector_install_hint(pg_major=server_major) if ext == "vector" else contrib_hint()
                report.add(label, FAIL, "not available on this PostgreSQL server", hint)
                continue
            if check_only:
                report.add(label, WARN, "not created yet", "Run init-db without --check.")
                continue
            try:
                await conn.execute(f'CREATE EXTENSION IF NOT EXISTS "{ext}"')
            except Exception as exc:  # noqa: BLE001
                if sqlstate(exc) == "42501":
                    report.add(label, FAIL, "permission denied" + ("" if via_admin else " (no DATABASE_ADMIN_URL)"),
                               superuser_hint())
                    continue
                if sqlstate(exc) in ("58P01", "0A000"):
                    hint = pgvector_install_hint(pg_major=server_major) if ext == "vector" else contrib_hint()
                    report.add(label, FAIL, str(exc).splitlines()[0][:160], hint)
                    continue
                raise
            version = await conn.fetchval("SELECT extversion FROM pg_extension WHERE extname = $1", ext)
            report.add(label, CREATED, f"version {version}")
    finally:
        await conn.close()


async def _create_tables(app_url: str, report: InitReport) -> None:
    """``app.db.create_all()`` on a temporary engine; the previously configured engine is restored."""
    try:
        from sqlalchemy.pool import NullPool

        from app import db as dbmod
    except Exception as exc:  # noqa: BLE001
        report.add("Tables", FAIL, f"cannot import the app ({exc.__class__.__name__}: {exc})",
                   "Install the requirements: python -m pip install -r requirements.txt")
        return
    previous = (dbmod._engine, dbmod._sessionmaker)  # noqa: SLF001 - restored below (tests share the module)
    try:
        dbmod.configure_engine(normalize_url(app_url), poolclass=NullPool)
        await dbmod.create_all()
        conn = await _connect(app_url)
        try:
            count = await conn.fetchval(
                "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
            )
        finally:
            await conn.close()
        report.add("Tables", OK, f"{count} tables ready (additive schema sync applied)")
    except Exception as exc:  # noqa: BLE001
        report.add("Tables", FAIL, f"{exc.__class__.__name__}: {str(exc).splitlines()[0][:200] if str(exc) else ''}",
                   "Fix the error above and run init-db again.")
    finally:
        try:
            await dbmod.dispose_engine()
        finally:
            dbmod._engine, dbmod._sessionmaker = previous  # noqa: SLF001


# ============================================================================= main routine
async def init_database(
    app_url: str,
    *,
    admin_url: str | None = None,
    test_url: str | None = None,
    create_tables: bool = True,
    wait_seconds: float = 0.0,
    check_only: bool = False,
    log: Callable[[str], None] = print,
) -> InitReport:
    """Create role, database(s), extensions and tables. Never raises for expected problems - see the report."""
    report = InitReport()
    try:
        app = parse_url(app_url)
        if not app.database or not app.username:
            raise ValueError("the URL needs a user name and a database name")
        quote_ident(app.database)
        quote_ident(app.username)
    except Exception as exc:  # noqa: BLE001
        report.add("Database URL", FAIL, f"invalid DATABASE_URL: {exc}",
                   "Example: postgresql+asyncpg://ironpulse:<password>@localhost:5432/ironpulse")
        return report

    # (database, url, role, password) for the app database and optionally the test database
    targets: list[tuple[str, str, str, str | None]] = [(app.database, app_url, app.username, app.password)]
    if test_url:
        try:
            t = parse_url(test_url)
            quote_ident(t.database or "")
            quote_ident(t.username or "")
            if t.database != app.database:
                targets.append((t.database, test_url, t.username, t.password))
        except Exception as exc:  # noqa: BLE001
            report.add("Test database URL", WARN, f"ignored: {exc}", "Fix TEST_DATABASE_URL in .env.")

    probe = with_database(admin_url, parse_url(admin_url).database or "postgres") if admin_url else with_database(app_url, "postgres")
    err = await wait_for_server(probe, wait_seconds, log)
    if err is not None:
        detail, hint = describe_connect_error(err, probe)
        report.add("PostgreSQL server", FAIL, detail, hint)
        return report

    admin = None
    admin_user: str | None = None
    if admin_url:
        try:
            admin = await _connect(probe)
            admin_user = parse_url(admin_url).username
            is_super = await admin.fetchval("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
            version = await admin.fetchval("SHOW server_version")
            report.add("Admin connection", OK if is_super else WARN,
                       f"{admin_user} ({'superuser' if is_super else 'NOT a superuser'}) - PostgreSQL {version}",
                       "" if is_super else superuser_hint())
        except Exception as exc:  # noqa: BLE001
            detail, hint = describe_connect_error(exc, probe)
            report.add("Admin connection", WARN, f"{detail} - continuing with the app role", hint)
            admin = None
    try:
        if admin is not None:
            seen: set[str] = set()
            for _db, _url, role, password in targets:
                if role not in seen:
                    seen.add(role)
                    await _ensure_role(admin, admin_user or "", role, password, report, check_only)
        ready: list[tuple[str, str, str, str | None]] = []
        for dbname, url, role, password in targets:
            if await _ensure_database(dbname, role, url, admin, admin_user, report, check_only):
                ready.append((dbname, url, role, password))
        for dbname, url, role, password in ready:
            label = "App login" if url == app_url else f"Login to {dbname}"
            if not await _check_login(url, admin, role, password, report, check_only, label):
                continue
            ext_url = with_database(admin_url, dbname) if admin is not None and admin_url else url
            await _ensure_extensions(dbname, ext_url, report, check_only, via_admin=admin is not None)
    finally:
        if admin is not None:
            await admin.close()

    if create_tables and not check_only:
        if report.ok:
            await _create_tables(app_url, report)
        else:
            report.add("Tables", WARN, "skipped because of the errors above")
    return report


# ============================================================================= CLI
def add_arguments(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--database-url", metavar="URL", help="app database URL (default: DATABASE_URL from .env)")
    ap.add_argument("--admin-url", metavar="URL",
                    help="superuser URL used to create the role/database/extensions (default: DATABASE_ADMIN_URL)")
    ap.add_argument("--with-test-db", action="store_true",
                    help="also create TEST_DATABASE_URL's database + extensions (the tests create their tables)")
    ap.add_argument("--no-tables", action="store_true", help="skip app.db.create_all()")
    ap.add_argument("--wait", type=float, default=0.0, metavar="SECONDS",
                    help="wait up to SECONDS for PostgreSQL to accept connections (e.g. right after docker compose up)")
    ap.add_argument("--check", action="store_true", help="only report what is missing - change nothing")


def load_db_config() -> dict[str, str | None]:
    """DATABASE_URL & co. from the app settings, or straight from the environment/.env when they don't load."""
    try:
        from app.config import get_settings

        s = get_settings()
        return {"database_url": s.database_url, "admin_url": s.database_admin_url, "test_url": s.test_database_url}
    except Exception:  # noqa: BLE001 - incomplete .env: fall back to raw values
        raw: dict[str, str] = {}
        env_path = ROOT / ".env"
        if env_path.is_file():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                m = re.match(r"^\s*([A-Z][A-Z0-9_]*)=(.*)$", line)
                if m:
                    raw[m.group(1)] = re.split(r"\s+#", m.group(2), maxsplit=1)[0].strip().strip('"').strip("'")

        def get(key: str) -> str | None:
            return os.environ.get(key) or raw.get(key) or None

        return {"database_url": get("DATABASE_URL"), "admin_url": get("DATABASE_ADMIN_URL"), "test_url": get("TEST_DATABASE_URL")}


STATUS_LABEL = {OK: "ok", CREATED: "created", UPDATED: "updated", WARN: "warning", FAIL: "FAILED"}


def print_report(report: InitReport, out: Callable[[str], None] = print) -> None:
    width = max((len(s.name) for s in report.steps), default=10) + 2
    for s in report.steps:
        out(f"[init-db] {s.name.ljust(width, '.')} {STATUS_LABEL.get(s.status, s.status):8} {s.detail}")
    hints = [s for s in report.steps if s.status in (WARN, FAIL) and s.hint]
    if hints:
        out("")
        out("[init-db] How to fix:")
        for s in hints:
            out(f"  - {s.name}: {s.hint}")


def run(args: argparse.Namespace) -> int:
    cfg = load_db_config()
    app_url = normalize_url(args.database_url or cfg["database_url"] or "")
    if not app_url:
        print("[init-db] DATABASE_URL is not set. Run: python scripts/make_env.py (or the setup script) first.", file=sys.stderr)
        return 1
    admin_url = normalize_url(args.admin_url or cfg["admin_url"] or "") or None
    discovered = False
    if not admin_url:
        admin_url = runtime_admin_url(app_url)
        discovered = admin_url is not None
    test_url = normalize_url(cfg["test_url"] or "") if args.with_test_db else None
    print(f"[init-db] database: {redact(app_url)}")
    if admin_url:
        print(f"[init-db] admin:    {redact(admin_url)}" + ("  (user-space runtime superuser)" if discovered else ""))
    report = asyncio.run(
        init_database(app_url, admin_url=admin_url, test_url=test_url or None, create_tables=not args.no_tables,
                      wait_seconds=args.wait, check_only=args.check)
    )
    print_report(report)
    if report.ok:
        print("[init-db] " + ("check finished." if args.check else "done - the database is ready."))
        return 0
    print("[init-db] finished with errors (see 'How to fix' above).", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="init_db.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_arguments(ap)
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
