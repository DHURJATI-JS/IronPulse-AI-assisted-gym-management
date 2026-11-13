"""IronPulse environment diagnostics (Windows / macOS / Linux).

    python scripts/check_env.py                 # table + fix suggestions; exit code 1 on critical failures
    python scripts/check_env.py --json          # machine-readable report
    python scripts/check_env.py --no-network    # skip AI / voice / SMTP probes (offline)
    python scripts/check_env.py --strict        # warnings also make the exit code 1
    python scripts/manage.py check ...          # the same thing

Checks: Python + virtualenv, packages, .env secrets, PostgreSQL (+ pgvector & extensions), Redis sessions,
Tesseract + languages, OCR engines, Tailwind CLI + built assets, ffmpeg, generated videos, AI providers
(Gemma gateway + its status page, Gemma in Docker, Claude), Kokoro voice, SMTP, Geoapify, Cashfree, app port.
Every probe is isolated and uses short timeouts - this script never hangs on a dead service.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import inspect
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OK, INFO, WARN, FAIL, SKIP = "ok", "info", "warn", "fail", "skip"
STATUSES = (OK, INFO, WARN, FAIL, SKIP)
MIN_PYTHON = (3, 10)
PLACEHOLDERS = {"", "change-me-run-setup-to-generate-a-64-char-secret"}
GROUP_ORDER = ("System", "Configuration", "Database", "OCR", "Assets", "AI", "Voice", "Integrations", "Server")

# (import name, pip package, critical)
PACKAGES: tuple[tuple[str, str, bool], ...] = (
    ("fastapi", "fastapi", True),
    ("uvicorn", "uvicorn", True),
    ("starlette", "starlette", True),
    ("jinja2", "jinja2", True),
    ("multipart", "python-multipart", True),
    ("itsdangerous", "itsdangerous", True),
    ("sqlalchemy", "sqlalchemy[asyncio]", True),
    ("asyncpg", "asyncpg", True),
    ("pgvector", "pgvector", True),
    ("greenlet", "greenlet", True),
    ("pydantic", "pydantic", True),
    ("pydantic_settings", "pydantic-settings", True),
    ("email_validator", "email-validator", True),
    ("argon2", "argon2-cffi", True),
    ("cryptography", "cryptography", True),
    ("httpx", "httpx", True),
    ("openai", "openai", True),
    ("anthropic", "anthropic", True),
    ("PIL", "pillow", True),
    ("numpy", "numpy", True),
    ("pypdfium2", "pypdfium2", True),
    ("pytesseract", "pytesseract", False),
    ("websockets", "websockets", True),
    ("aiosmtplib", "aiosmtplib", True),
    ("redis", "redis", False),
    ("rjsmin", "rjsmin", False),
    ("imageio", "imageio", False),
    ("imageio_ffmpeg", "imageio-ffmpeg", False),
)


# ============================================================================= results
@dataclass
class Result:
    group: str
    name: str
    status: str
    detail: str = ""
    fix: str = ""
    critical: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Report:
    results: list[Result]
    strict: bool = False

    @property
    def failures(self) -> list[Result]:
        return [r for r in self.results if r.status == FAIL]

    @property
    def critical_failures(self) -> list[Result]:
        return [r for r in self.results if r.status == FAIL and r.critical]

    @property
    def warnings(self) -> list[Result]:
        return [r for r in self.results if r.status == WARN]

    @property
    def exit_code(self) -> int:
        if self.critical_failures:
            return 1
        if self.strict and (self.failures or self.warnings):
            return 1
        return 0

    def counts(self) -> dict[str, int]:
        return {s: sum(1 for r in self.results if r.status == s) for s in STATUSES}

    def by_name(self, name: str) -> Result | None:
        return next((r for r in self.results if r.name == name), None)

    def as_dict(self) -> dict[str, Any]:
        return {"results": [r.as_dict() for r in self.results], "counts": self.counts(), "exit_code": self.exit_code}


# ============================================================================= helpers
def load_script(name: str) -> Any:
    """Import a sibling script (scripts/<name>.py) without needing scripts/ on sys.path."""
    key = f"ironpulse_scripts_{name}"
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, SCRIPTS / f"{name}.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"scripts/{name}.py not found")
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(key, None)
        raise
    return module


def read_dotenv(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        return load_script("make_env").parse(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def run_async(factory: Callable[[], Any]) -> Any:
    """Run a coroutine factory from sync code - also when an event loop is already running (async tests)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())
    box: dict[str, Any] = {}

    def worker() -> None:
        try:
            box["value"] = asyncio.run(factory())
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


def http_get(url: str, headers: dict[str, str] | None = None, timeout: float = 3.0) -> tuple[int | None, Any, str | None]:
    """(status, parsed JSON or text, error). Never raises. Tests replace this function."""
    req = urllib.request.Request(url, headers={"User-Agent": "ironpulse-check/1.0", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - URLs come from .env
            status = resp.status
            body = resp.read(512 * 1024)
    except urllib.error.HTTPError as exc:
        return exc.code, None, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 - connection refused, timeout, DNS ...
        reason = getattr(exc, "reason", None)
        return None, None, f"{exc.__class__.__name__}: {reason or exc}"[:160]
    text = body.decode("utf-8", errors="replace")
    try:
        return status, json.loads(text), None
    except ValueError:
        return status, text, None


def short_error(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    return f"{exc.__class__.__name__}: {text}"[:180] if text else exc.__class__.__name__


def os_hint(mac: str, linux: str, windows: str) -> str:
    if sys.platform == "darwin":
        return mac
    if sys.platform.startswith("win"):
        return windows
    return linux


class Ctx:
    """Settings (if they load) + raw .env values + probe options."""

    def __init__(self, *, network: bool = True, db: bool = True, timeout: float = 3.0, env_path: Path | None = None,
                 settings: Any = None, load_app_settings: bool = True, ai_probe: bool = False,
                 probe_timeout: float = 60.0) -> None:
        self.network = network
        self.db = db
        self.timeout = timeout
        self.ai_probe = ai_probe  # also ask each Gemma server for a 1-token answer (its latency)
        self.probe_timeout = probe_timeout
        self.env_path = env_path or (ROOT / ".env")
        self.raw = read_dotenv(self.env_path)
        self.settings = settings
        self.settings_error: str | None = None
        self.config_problems: list[str] = []  # app.config.ConfigError: one line per problem, naming the variable
        if settings is None and load_app_settings:
            try:
                from app.config import ConfigError, get_settings
            except Exception as exc:  # noqa: BLE001 - the app itself does not import (packages missing ...)
                self.settings_error = short_error(exc)
            else:
                try:
                    self.settings = get_settings()
                except ConfigError as exc:  # a SystemExit for servers - a finding here
                    self.config_problems = list(exc.problems)
                    self.settings_error = "; ".join(exc.problems)[:900]
                except Exception as exc:  # noqa: BLE001
                    first = str(exc).strip().splitlines()
                    self.settings_error = f"{exc.__class__.__name__}: {' '.join(first[:3])}"[:300]

    def get(self, key: str, default: Any = None) -> Any:
        if self.settings is not None and hasattr(self.settings, key.lower()):
            value = getattr(self.settings, key.lower())
            return default if value in (None, "") else value
        value = os.environ.get(key.upper())
        if value in (None, ""):
            value = self.raw.get(key.upper())
        return default if value in (None, "") else value


# ============================================================================= System
def check_python(version_info: Any = None) -> Result:
    v = version_info or sys.version_info
    text = ".".join(str(p) for p in tuple(v)[:3])
    if tuple(v)[:2] < MIN_PYTHON:
        return Result("System", "Python version", FAIL, f"{text} - IronPulse needs Python 3.10 or newer",
                      os_hint("brew install python@3.12  (or ./setup.sh)", "sudo apt install python3.12 python3.12-venv  (or ./setup.sh)",
                              "winget install Python.Python.3.12  (or setup.ps1)"), critical=True)
    return Result("System", "Python version", OK, f"{text} ({platform.python_implementation()}, {platform.system()} {platform.machine()})")


def check_venv(prefix: str | None = None, base_prefix: str | None = None) -> Result:
    prefix = prefix if prefix is not None else sys.prefix
    base_prefix = base_prefix if base_prefix is not None else sys.base_prefix
    if prefix != base_prefix:
        return Result("System", "Virtual environment", OK, prefix)
    venv = ROOT / ".venv"
    py = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if py.exists():
        return Result("System", "Virtual environment", WARN, f"not active (this is {sys.executable})",
                      f"Use the project interpreter: {py.relative_to(ROOT)} scripts/check_env.py  (run.sh / run.ps1 do this for you)")
    return Result("System", "Virtual environment", WARN, "no .venv in the project yet",
                  os_hint("./setup.sh", "./setup.sh", "powershell -ExecutionPolicy Bypass -File setup.ps1"))


def check_packages() -> list[Result]:
    missing_critical: list[str] = []
    missing_optional: list[str] = []
    for module, pip_name, critical in PACKAGES:
        try:
            found = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            (missing_critical if critical else missing_optional).append(pip_name)
    results: list[Result] = []
    fix = "python -m pip install -r requirements.txt  (inside the .venv)"
    if missing_critical:
        results.append(Result("System", "Python packages", FAIL, "missing: " + ", ".join(missing_critical), fix, critical=True))
    elif missing_optional:
        results.append(Result("System", "Python packages", WARN, "optional packages missing: " + ", ".join(missing_optional), fix))
    else:
        results.append(Result("System", "Python packages", OK, f"{len(PACKAGES)} key packages importable"))
    return results


# ============================================================================= Configuration
def check_env_file(ctx: Ctx) -> Result:
    if not ctx.env_path.is_file():
        return Result("Configuration", ".env file", FAIL, f"{ctx.env_path.name} is missing",
                      "Create it with fresh secrets: python scripts/make_env.py", critical=True)
    mode = ""
    if os.name != "nt":
        perms = ctx.env_path.stat().st_mode & 0o777
        if perms & 0o077:
            return Result("Configuration", ".env file", WARN, f"present but readable by other users (mode {oct(perms)})",
                          f"chmod 600 {ctx.env_path}")
        mode = " (mode 600)"
    return Result("Configuration", ".env file", OK, f"{ctx.env_path}{mode}")


def _valid_fernet(key: str) -> bool:
    import base64
    import binascii

    try:
        return len(base64.urlsafe_b64decode(key.encode())) == 32
    except (binascii.Error, ValueError):
        return False


def check_secrets(ctx: Ctx) -> list[Result]:
    values = {k: (os.environ.get(k) or ctx.raw.get(k, "")) for k in ("SECRET_KEY", "DATA_ENCRYPTION_KEY", "ID_HASH_PEPPER", "SUPERADMIN_PASSWORD")}
    problems: list[str] = []
    if values["SECRET_KEY"] in PLACEHOLDERS or len(values["SECRET_KEY"]) < 32:
        problems.append("SECRET_KEY")
    if not _valid_fernet(values["DATA_ENCRYPTION_KEY"]):
        problems.append("DATA_ENCRYPTION_KEY")
    if len(values["ID_HASH_PEPPER"]) < 16:
        problems.append("ID_HASH_PEPPER")
    results: list[Result] = []
    if problems:
        results.append(Result("Configuration", "Required secrets", FAIL, "missing or placeholder: " + ", ".join(problems),
                              "Generate them: python scripts/make_env.py  (keeps existing values)", critical=True))
    else:
        results.append(Result("Configuration", "Required secrets", OK, "SECRET_KEY, DATA_ENCRYPTION_KEY (Fernet), ID_HASH_PEPPER"))
    if not values["SUPERADMIN_PASSWORD"]:
        results.append(Result("Configuration", "Super-admin password", WARN, "SUPERADMIN_PASSWORD is empty - no security-centre admin is created",
                              "python scripts/make_env.py  or  python scripts/manage.py create-superadmin"))
    return results


def check_settings(ctx: Ctx) -> Result:
    if ctx.settings is not None:
        env = ctx.get("ENVIRONMENT", "development")
        debug = " · DEBUG on" if ctx.get("DEBUG", False) is True else ""
        return Result("Configuration", "Settings load", OK, f"environment={env}{debug} · public URL {ctx.get('PUBLIC_BASE_URL', '?')}")
    fix = "Fix .env (compare with .env.example) - python scripts/make_env.py fills missing secrets"
    if ctx.config_problems:
        fix = "Set the variables named above in .env or the environment (README 'Deploying to production'); " + fix
    return Result("Configuration", "Settings load", FAIL, ctx.settings_error or "app settings could not be loaded", fix,
                  critical=True)


def check_production(ctx: Ctx) -> Result:
    """The ENVIRONMENT=production rules (app/config.py ``Settings._production_mode``): unsafe settings stop the app
    (they show up under 'Settings load'), explicit opt-outs of the secure defaults are warnings."""
    name = "Production settings"
    if ctx.settings is None:
        return Result("Configuration", name, SKIP, "settings did not load (see 'Settings load')")
    if not getattr(ctx.settings, "is_production", False):
        return Result("Configuration", name, INFO, f"ENVIRONMENT={ctx.get('ENVIRONMENT', 'development')} - the "
                      "production rules apply with ENVIRONMENT=production (README 'Deploying to production')")
    warnings = list(ctx.settings.production_warnings())
    if warnings:
        return Result("Configuration", name, WARN, "; ".join(warnings),
                      "Remove the explicit opt-outs or fix the settings named above (README 'Deploying to production')")
    return Result("Configuration", name, OK, "secure defaults in force: Secure cookies, HTTPS redirect + HSTS, Host "
                  "check, X-Forwarded-For/-Proto only from TRUSTED_PROXIES")


# ============================================================================= Database / Redis
async def _db_probe(url: str, timeout: float) -> dict[str, Any]:
    import asyncpg

    dsn, kwargs = load_script("init_db").asyncpg_connect_args(url)
    conn = await asyncpg.connect(dsn, timeout=timeout, **kwargs)
    try:
        exts = {r["extname"]: r["extversion"] for r in await conn.fetch("SELECT extname, extversion FROM pg_extension")}
        avail = {
            r["name"]: r["default_version"]
            for r in await conn.fetch("SELECT name, default_version FROM pg_available_extensions "
                                      "WHERE name = ANY($1::text[])", ["vector", "citext", "pg_trgm"])
        }
        return {
            "version": await conn.fetchval("SHOW server_version"),
            "version_num": int(await conn.fetchval("SHOW server_version_num")),
            "extensions": exts,
            "available": avail,
            "tables": int(await conn.fetchval("SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public' "
                                              "AND table_type = 'BASE TABLE'")),
            "users_table": bool(await conn.fetchval("SELECT to_regclass('public.users') IS NOT NULL")),
            "database": await conn.fetchval("SELECT current_database()"),
            "budget": dict(await conn.fetchrow(_budget_sql())),
        }
    finally:
        await conn.close()


def _budget_sql() -> str:
    from app.db import BUDGET_SQL

    return BUDGET_SQL


PLANNED_WORKERS = 4  # what `manage.py run --workers 4` / the README's multi-worker start uses


def connection_budget_result(row: dict[str, Any], settings: Any) -> Result:
    """The workers' pools against what PostgreSQL allows (app/db.py ConnectionBudget), for the workers configured now
    and for a PLANNED_WORKERS-worker start: a stale .env that pins DB_MAX_OVERFLOW=20 is flagged here, before the
    workers log it at start-up and requests get 503s under load."""
    from app.db import ConnectionBudget, configured_workers

    name = "Database connections"
    if settings is None:
        return Result("Database", name, SKIP, "settings did not load")
    workers = configured_workers()
    budgets = [ConnectionBudget.from_server(row, workers=n, pool_size=settings.db_pool_size,
                                            max_overflow=settings.db_max_overflow)
               for n in sorted({workers, max(workers, PLANNED_WORKERS)})]
    broken = [b for b in budgets if b.problems()]
    if not broken:
        b = budgets[-1]
        return Result("Database", name, OK, f"{b.formula()} - fits the {b.server_share} the app may claim "
                                            f"(max_connections {b.max_connections})")
    b = broken[0]
    return Result("Database", name, WARN, f"{b.formula()}, but {'; '.join(b.problems())}",
                  "lower DB_MAX_OVERFLOW / DB_POOL_SIZE in .env (an older .env.example pinned DB_MAX_OVERFLOW=20; the "
                  "default is 5), run fewer workers or raise max_connections (ARCHITECTURE.md 'Database sizing')")


def check_database(ctx: Ctx) -> list[Result]:
    if not ctx.db:
        return [Result("Database", "PostgreSQL connection", SKIP, "skipped (--no-db)")]
    url = ctx.get("DATABASE_URL")
    if not url:
        return [Result("Database", "PostgreSQL connection", FAIL, "DATABASE_URL is not set", "python scripts/make_env.py, then set DATABASE_URL",
                       critical=True)]
    init_db = load_script("init_db")
    try:
        info = run_async(lambda: _db_probe(url, max(ctx.timeout, 3.0)))
    except Exception as exc:  # noqa: BLE001
        detail, hint = init_db.describe_connect_error(exc, url)
        return [Result("Database", "PostgreSQL connection", FAIL, f"{init_db.redact(url)}: {detail}",
                       hint or "python scripts/manage.py init-db", critical=True)]
    results = [Result("Database", "PostgreSQL connection", OK, f"{info['database']} on PostgreSQL {info['version']}")]
    major = info["version_num"] // 10000
    if major < 15:
        results.append(Result("Database", "PostgreSQL version", WARN, f"PostgreSQL {major} - version 15 or newer is recommended"))
    missing = [e for e in ("vector", "citext", "pg_trgm") if e not in info["extensions"]]
    if missing:
        not_installed = [e for e in missing if e not in info["available"]]
        hint = (init_db.pgvector_install_hint(pg_major=major) if "vector" in not_installed
                else "python scripts/manage.py init-db  (with DATABASE_ADMIN_URL set to a superuser URL)")
        results.append(Result("Database", "Extensions", FAIL, "missing: " + ", ".join(missing)
                              + (f" (not installed on the server: {', '.join(not_installed)})" if not_installed else ""), hint, critical=True))
    else:
        results.append(Result("Database", "Extensions", OK, "vector, citext, pg_trgm installed"))
    if "vector" in info["extensions"]:
        results.append(Result("Database", "pgvector", OK, f"version {info['extensions']['vector']}"))
    elif "vector" in info["available"]:
        results.append(Result("Database", "pgvector", WARN, f"available ({info['available']['vector']}) but not created in this database",
                              "python scripts/manage.py init-db"))
    else:
        results.append(Result("Database", "pgvector", FAIL, "not installed on this PostgreSQL server",
                              init_db.pgvector_install_hint(pg_major=major), critical=True))
    if info["users_table"]:
        results.append(Result("Database", "Tables", OK, f"{info['tables']} tables"))
    else:
        results.append(Result("Database", "Tables", WARN, "the IronPulse tables do not exist yet",
                              "python scripts/manage.py init-db  (the app also creates them at startup)"))
    if info.get("budget"):
        results.append(connection_budget_result(info["budget"], ctx.settings))
    return results


async def _redis_probe(timeout: float) -> dict[str, Any]:
    from app.security import sessions

    try:
        return await asyncio.wait_for(sessions.redis_status(), timeout=timeout)
    finally:  # the shared client is bound to this short-lived loop: close it and let the app create a new one
        client = getattr(sessions, "_redis", None)
        if client is not None:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                pass
            sessions._redis = None  # noqa: SLF001


def check_redis(ctx: Ctx) -> Result:
    url = ctx.get("REDIS_URL")
    if not url:
        return Result("Database", "Redis sessions", INFO,
                      "REDIS_URL not set - sessions are stored in PostgreSQL only (fine for one worker; needed with "
                      "--workers N so presence is shared and rate limits, threat scores and the sign-in lockout are "
                      "one allowance for every worker - without it each worker keeps its own: N x the allowance)",
                      "Optional: docker compose --env-file .env -f docker/compose.yml up -d redis  (setup scripts configure REDIS_URL)")
    if not ctx.db:
        return Result("Database", "Redis sessions", SKIP, "skipped (--no-db)")
    if importlib.util.find_spec("redis") is None:
        return Result("Database", "Redis sessions", FAIL, "REDIS_URL is set but the 'redis' package is missing",
                      "python -m pip install -r requirements.txt")
    if ctx.settings is None:
        return Result("Database", "Redis sessions", SKIP, "settings did not load")
    safe = url.split("@", 1)[-1] if "@" in url else url
    try:
        status = run_async(lambda: _redis_probe(max(ctx.timeout, 2.0)))
    except Exception as exc:  # noqa: BLE001
        status = {"configured": True, "ok": False, "error": short_error(exc)}
    if status.get("ok"):
        return Result("Database", "Redis sessions", OK, f"connected ({safe}) - session lookups are cached in Redis")
    fix = os_hint("scripts/runtime_redis.sh start  or  docker compose --env-file .env -f docker/compose.yml up -d redis",
                  "scripts/runtime_redis.sh start  or  docker compose --env-file .env -f docker/compose.yml up -d redis",
                  "powershell -File scripts\\windows\\services.ps1 start  (Docker) - or use Memurai / WSL")
    return Result("Database", "Redis sessions", WARN,
                  f"cannot reach {safe} ({status.get('error', 'no answer')}) - the app falls back to PostgreSQL sessions "
                  "and to per-worker rate limits, threat scores and sign-in lockout", fix)


# ============================================================================= OCR
def _run(cmd: list[str], timeout: float = 10.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, short_error(exc)


def check_tesseract(ctx: Ctx) -> list[Result]:
    cmd = None
    if ctx.settings is not None and hasattr(ctx.settings, "resolved_tesseract_cmd"):
        cmd = ctx.settings.resolved_tesseract_cmd()
    else:
        configured = ctx.get("TESSERACT_CMD")
        cmd = configured if configured and Path(configured).exists() else shutil.which("tesseract")
    if not cmd:
        return [Result("OCR", "Tesseract", WARN, "the Tesseract binary was not found (OCR uses the other engines)",
                       os_hint("brew install tesseract  (or ./setup.sh --db runtime installs it under ~/.ironpulse)",
                               "sudo apt install tesseract-ocr tesseract-ocr-eng  (dnf: tesseract tesseract-langpack-eng)",
                               "winget install UB-Mannheim.TesseractOCR  - then set TESSERACT_CMD in .env"))]
    code, out = _run([cmd, "--version"])
    version = next((line.strip() for line in out.splitlines() if line.strip().lower().startswith("tesseract")), "")
    if code != 0 and not version:
        return [Result("OCR", "Tesseract", WARN, f"{cmd} does not run ({out.strip()[:120]})", "Reinstall Tesseract or fix TESSERACT_CMD")]
    results = [Result("OCR", "Tesseract", OK, f"{version or 'tesseract'} at {cmd}")]
    code, out = _run([cmd, "--list-langs"])
    langs = sorted({line.strip() for line in out.splitlines()[1:] if line.strip() and " " not in line.strip()})
    wanted = [lang for lang in str(ctx.get("TESSERACT_LANGS", "eng")).split("+") if lang]
    missing = [lang for lang in wanted if lang not in langs]
    if missing:
        results.append(Result("OCR", "Tesseract languages", WARN, f"missing: {', '.join(missing)} (installed: {', '.join(langs) or 'none'})",
                              os_hint("brew install tesseract-lang", "sudo apt install tesseract-ocr-eng", "re-run the UB-Mannheim installer and tick the language")))
    elif len(langs) > 8:
        results.append(Result("OCR", "Tesseract languages", OK, f"{len(langs)} installed, including {', '.join(wanted)}"))
    else:
        results.append(Result("OCR", "Tesseract languages", OK, ", ".join(langs)))
    return results


def check_ocr_engines(ctx: Ctx) -> Result:
    need = int(ctx.get("OCR_MIN_ENGINES", 2) or 2)
    if ctx.settings is None:
        return Result("OCR", "OCR engines", SKIP, "settings did not load")
    try:
        from app.services import ocr

        engines = ocr.available_engines()
        if inspect.isawaitable(engines):
            awaitable = engines
            engines = run_async(lambda: _await(awaitable))
        engines = list(engines or [])
    except NotImplementedError:
        return Result("OCR", "OCR engines", WARN, "the multi-engine OCR service is not installed yet (app/services/ocr.py)",
                      "It is provided by the identity/OCR work package; ID cards go to manual review until then.")
    except Exception as exc:  # noqa: BLE001
        return Result("OCR", "OCR engines", WARN, f"could not list OCR engines ({short_error(exc)})", "Check the OCR service")
    label = ", ".join(engines) if engines else "none"
    if len(engines) >= need:
        return Result("OCR", "OCR engines", OK, f"{len(engines)} available ({label}); {need} must agree on an ID card")
    return Result("OCR", "OCR engines", WARN, f"{len(engines)} available ({label}) but OCR_MIN_ENGINES={need} - ID cards fall back to manual review",
                  os_hint("Install Tesseract; Apple Vision needs pyobjc-framework-Vision", "Install Tesseract; RapidOCR needs Python <= 3.13",
                          "Install Tesseract; Windows OCR needs the winrt packages from requirements.txt"))


async def _await(awaitable: Any) -> Any:
    return await awaitable


# ============================================================================= Assets
def check_tailwind() -> Result:
    try:
        build_assets = load_script("build_assets")
        path = build_assets.find_tailwind()
    except Exception as exc:  # noqa: BLE001
        return Result("Assets", "Tailwind CLI", WARN, f"cannot inspect build_assets.py ({short_error(exc)})")
    if path:
        return Result("Assets", "Tailwind CLI", OK, str(path))
    return Result("Assets", "Tailwind CLI", WARN, "not downloaded yet - the prebuilt CSS is used until then",
                  "python scripts/manage.py build-assets  (downloads the standalone CLI for this OS)")


def check_dist_assets() -> Result:
    dist = ROOT / "app" / "static" / "dist"
    files = {name: dist / name for name in ("app.min.css", "app.min.js")}
    missing = [n for n, p in files.items() if not p.is_file() or p.stat().st_size == 0]
    if missing:
        return Result("Assets", "Built CSS/JS", FAIL, "missing: " + ", ".join(missing), "python scripts/manage.py build-assets", critical=True)
    sizes = " · ".join(f"{n} {p.stat().st_size / 1024:.0f} KiB" for n, p in files.items())
    js_sources = list((ROOT / "app" / "static" / "src" / "js").glob("*.js"))
    newest_js = max((p.stat().st_mtime for p in js_sources), default=0)
    if newest_js > files["app.min.js"].stat().st_mtime + 1:
        return Result("Assets", "Built CSS/JS", WARN, f"{sizes} - the JS bundle is older than its sources",
                      "python scripts/manage.py build-assets  (or keep python scripts/dev_watch.py running)")
    return Result("Assets", "Built CSS/JS", OK, sizes)


def check_ffmpeg() -> Result:
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        return Result("Assets", "ffmpeg (imageio-ffmpeg)", OK, exe)
    except Exception as exc:  # noqa: BLE001
        return Result("Assets", "ffmpeg (imageio-ffmpeg)", WARN, f"not available ({short_error(exc)}) - videos cannot be generated",
                      "python -m pip install imageio-ffmpeg")


def check_videos() -> Result:
    folder = ROOT / "app" / "static" / "videos"
    videos = sorted(folder.glob("*.mp4")) if folder.is_dir() else []
    if not videos:
        return Result("Assets", "Generated videos", WARN, "no workout videos generated yet (they are included in every plan)",
                      "python scripts/manage.py gen-videos")
    size = sum(p.stat().st_size for p in videos) / (1024 * 1024)
    return Result("Assets", "Generated videos", OK, f"{len(videos)} videos ({size:.1f} MiB) in app/static/videos")


# ============================================================================= AI providers
def probe_models(base_url: str, api_key: str | None, timeout: float) -> dict[str, Any]:
    """GET <base>/models of an OpenAI-compatible server (llama.cpp gateway, Ollama)."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    status, data, error = http_get(base_url.rstrip("/") + "/models", headers=headers, timeout=timeout)
    models: list[str] = []
    if isinstance(data, dict):
        for item in data.get("data") or []:
            if isinstance(item, dict) and item.get("id"):
                models.append(str(item["id"]))
        if not models:
            for item in data.get("models") or []:
                if isinstance(item, dict) and (item.get("model") or item.get("name")):
                    models.append(str(item.get("model") or item.get("name")))
    return {"ok": status == 200, "status": status, "models": models, "error": error}


def http_post_json(url: str, payload: dict[str, Any], headers: dict[str, str] | None = None,
                   timeout: float = 60.0) -> tuple[int | None, Any, str | None]:
    """(status, parsed JSON, error) of a JSON POST. Never raises. Tests replace this function."""
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"User-Agent": "ironpulse-check/1.0", "Content-Type": "application/json",
                                          **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - URLs come from .env
            status, body = resp.status, resp.read(256 * 1024)
    except urllib.error.HTTPError as exc:
        return exc.code, None, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 - refused, timed out ...
        reason = getattr(exc, "reason", None)
        return None, None, f"{exc.__class__.__name__}: {reason or exc}"[:160]
    try:
        return status, json.loads(body.decode("utf-8", errors="replace")), None
    except ValueError:
        return status, None, "the answer is not JSON"


def probe_completion(base_url: str, api_key: str | None, model: str, timeout: float) -> dict[str, Any]:
    """A real 1-token answer ('Say OK.') from an OpenAI-compatible server, timed - the latency a member waits for
    the first words (a cold Ollama loads the model first)."""
    started = time.perf_counter()
    status, data, error = http_post_json(
        base_url.rstrip("/") + "/chat/completions",
        {"model": model, "messages": [{"role": "user", "content": "Say OK."}], "max_tokens": 1, "temperature": 0},
        headers={"Authorization": f"Bearer {api_key}"} if api_key else None, timeout=timeout)
    ms = int((time.perf_counter() - started) * 1000)
    choices = data.get("choices") if isinstance(data, dict) else None
    if status == 200 and isinstance(choices, list) and choices:
        return {"ok": True, "ms": ms, "error": None}
    return {"ok": False, "ms": ms, "error": error or ("no choices in the answer" if status == 200 else f"HTTP {status}")}


def _probe_text(ctx: Ctx, base: str, api_key: Any, model: str) -> tuple[str, str | None]:
    """(' · 1-token answer in N ms', None) or ('', why it failed); nothing without ctx.ai_probe."""
    if not ctx.ai_probe:
        return "", None
    probe = probe_completion(base, api_key, model, ctx.probe_timeout)
    if probe["ok"]:
        return f" · 1-token answer in {probe['ms']} ms", None
    return "", f"{probe['error']} after {probe['ms']} ms"


def _runtime_note(ctx: Ctx) -> str:
    """What can run the local fallback here: Docker (running or not), a native Ollama - or nothing."""
    try:
        fallback = load_script("ai_fallback")
        probes = fallback.Probes()
        docker = probes.docker()
        if docker:
            return " · Docker installed, " + ("running" if probes.docker_running(docker) else "NOT running")
        if probes.ollama_cli():
            return " · native Ollama installed"
        return " · neither Docker nor Ollama is installed"
    except Exception:  # noqa: BLE001 - a hint only
        return ""


def _span_text(span: Any) -> str:
    """(400.0, 1500.0) -> '400..1500' (the AI_SIM_* ranges)."""
    try:
        low, high = (int(v) if float(v).is_integer() else v for v in span)
    except (TypeError, ValueError):
        return str(span)
    return f"{low}" if low == high else f"{low}..{high}"


def gateway_status_url(ctx: Ctx) -> str | None:
    explicit = ctx.get("AI_GATEWAY_STATUS_URL")
    if explicit:
        return str(explicit)
    base = ctx.get("AI_GATEWAY_BASE_URL")
    if not base:
        return None
    parts = urlsplit(str(base))
    return f"{parts.scheme}://{parts.netloc}/status" if parts.scheme and parts.netloc else None


def check_ai(ctx: Ctx) -> list[Result]:
    order = ctx.get("AI_PROVIDER_ORDER", ["gateway", "docker", "anthropic"])
    if isinstance(order, str):
        order = [p.strip() for p in order.split(",") if p.strip()]
    results: list[Result] = []
    reachable: list[str] = []
    timeout = ctx.timeout

    # 1) Gemma gateway (OpenAI SDK -> llama.cpp)
    base = ctx.get("AI_GATEWAY_BASE_URL")
    model = str(ctx.get("AI_GATEWAY_MODEL", "gemma"))
    if not base:
        results.append(Result("AI", "Gemma gateway", INFO, "AI_GATEWAY_BASE_URL not set"))
    elif not ctx.network:
        results.append(Result("AI", "Gemma gateway", SKIP, f"{base} (not probed: --no-network)"))
    else:
        probe = probe_models(str(base), ctx.get("AI_GATEWAY_API_KEY"), timeout)
        if probe["ok"]:
            has_model = not probe["models"] or model in probe["models"]
            latency, broken = _probe_text(ctx, str(base), ctx.get("AI_GATEWAY_API_KEY"), model) if has_model else ("", None)
            if not broken:
                reachable.append("gateway")
            detail = f"{base} · models: {', '.join(probe['models']) or '?'}{latency}"
            if broken:
                results.append(Result("AI", "Gemma gateway", WARN, f"{detail} · lists its models but did not answer "
                                      f"({broken})", "Restart the llama.cpp backend(s) on the AI PC; the app fails "
                                      "over to Gemma in Docker meanwhile"))
            else:
                results.append(Result("AI", "Gemma gateway", OK if has_model else WARN, detail,
                                      "" if has_model else f"AI_GATEWAY_MODEL={model} is not served - set it to one of the listed models"))
        else:
            why = probe["error"] or f"HTTP {probe['status']}"
            fix = ("Check AI_GATEWAY_API_KEY in .env" if probe["status"] in (401, 403)
                   else "Start the gateway on the AI PC (and allow its port in the firewall - see README 'AI providers'); "
                        "the app falls back to Gemma in Docker, then Claude")
            results.append(Result("AI", "Gemma gateway", WARN, f"{base} unreachable ({why})", fix))
        status_url = gateway_status_url(ctx)
        if status_url:
            status, data, error = http_get(status_url, timeout=timeout)
            if status == 200 and isinstance(data, dict) and isinstance(data.get("backends"), list):
                backends = data["backends"]
                online = [b for b in backends if isinstance(b, dict) and b.get("online")]
                parts = [f"{b.get('name', '?')} {'up' if b.get('online') else 'DOWN'} {b.get('busy_slots', '')}".strip()
                         for b in backends if isinstance(b, dict)]
                state = OK if online and len(online) == len(backends) else (WARN if online else FAIL)
                results.append(Result("AI", "Gateway status page", state if state != FAIL else WARN,
                                      f"{len(online)}/{len(backends)} backends online ({'; '.join(parts)})",
                                      "" if state == OK else "Restart the offline llama.cpp backend(s) on the AI PCs"))
            elif status == 200:
                results.append(Result("AI", "Gateway status page", INFO, f"{status_url} answered (unknown format)"))
            else:
                results.append(Result("AI", "Gateway status page", WARN if "gateway" in reachable else INFO,
                                      f"{status_url} unavailable ({error or status})"))

    # 2) Gemma in Docker (Ollama failsafe)
    docker_base = ctx.get("AI_DOCKER_BASE_URL")
    docker_model = str(ctx.get("AI_DOCKER_MODEL", "gemma3:4b"))
    start_fix = "docker compose --env-file .env -f docker/compose.yml up -d ollama ollama-pull"
    if not docker_base:
        results.append(Result("AI", "Gemma in Docker", INFO, "AI_DOCKER_BASE_URL not set"))
    elif not ctx.network:
        results.append(Result("AI", "Gemma in Docker", SKIP, f"{docker_base} (not probed: --no-network)"))
    else:
        probe = probe_models(str(docker_base), ctx.get("AI_DOCKER_API_KEY"), min(timeout, 2.0))
        if probe["ok"]:
            pulled = any(m == docker_model or m.split(":")[0] == docker_model for m in probe["models"])
            if pulled:
                latency, broken = _probe_text(ctx, str(docker_base), ctx.get("AI_DOCKER_API_KEY"), docker_model)
                if broken:
                    results.append(Result("AI", "Gemma in Docker", WARN, f"{docker_base} · {docker_model} pulled but it "
                                          f"did not answer ({broken})", "docker compose --env-file .env -f "
                                          "docker/compose.yml restart ollama (logs: ... logs ollama)"))
                else:
                    reachable.append("docker")
                    results.append(Result("AI", "Gemma in Docker", OK, f"{docker_base} · {docker_model} ready{latency}"))
            else:
                results.append(Result("AI", "Gemma in Docker", WARN, f"Ollama is up but {docker_model} is not pulled yet",
                                      "docker compose --env-file .env -f docker/compose.yml up ollama-pull"))
        else:
            why = probe["error"] or probe["status"]
            results.append(Result("AI", "Gemma in Docker", INFO, f"failsafe not running ({why}){_runtime_note(ctx)}",
                                  start_fix + " - ./run.sh starts it by itself when Docker or Ollama is installed"))

    # 3) Claude (Anthropic) - last resort; the key is checked, not spent
    if ctx.get("ANTHROPIC_API_KEY"):
        reachable.append("anthropic")
        results.append(Result("AI", "Claude (Anthropic)", OK, f"API key set · model {ctx.get('ANTHROPIC_MODEL', '?')}"))
    else:
        results.append(Result("AI", "Claude (Anthropic)", INFO, "ANTHROPIC_API_KEY not set (optional last resort)"))

    # 4) the simulated provider (load tests): only when AI_PROVIDER_ORDER names it - never by default
    if "simulated" in order:
        reachable.append("simulated")
        detail = "AI_PROVIDER_ORDER names 'simulated': answers are generated locally and no model runs (load tests only)"
        s = ctx.settings
        if s is not None and hasattr(s, "ai_sim_first_token_ms"):
            detail += (f" · first words after {_span_text(s.ai_sim_first_token_ms)} ms, "
                       f"{_span_text(s.ai_sim_tokens_per_s)} words/s, {s.ai_sim_failure_rate:.0%} failures"
                       + (" · coach voice simulated too" if s.ai_sim_tts else ""))
        results.append(Result("AI", "Simulated AI", WARN, detail,
                              "Remove 'simulated' from AI_PROVIDER_ORDER after the load test (ENVIRONMENT=production "
                              "also needs AI_SIMULATED_ALLOWED=true to run it at all)"))

    if ctx.network:
        active = [p for p in order if p in reachable]
        if active:
            results.append(Result("AI", "AI failover chain", OK, f"order {' -> '.join(order)}; answering now: {active[0]}"))
        else:
            results.append(Result("AI", "AI failover chain", WARN, "no AI provider reachable - pages show 'AI unavailable' (everything else works)",
                                  "Start the Gemma gateway, or " + start_fix + ", or set ANTHROPIC_API_KEY"))
    return results


def check_kokoro(ctx: Ctx) -> Result:
    base = ctx.get("TTS_BASE_URL")
    if not base:
        return Result("Voice", "Kokoro TTS", INFO, "TTS_BASE_URL not set - the live coach uses the browser's voice")
    if not ctx.network:
        return Result("Voice", "Kokoro TTS", SKIP, f"{base} (not probed: --no-network)")
    status, data, error = http_get(str(base).rstrip("/") + "/audio/voices", timeout=min(ctx.timeout, 2.0))
    if status == 200:
        voices = data.get("voices") if isinstance(data, dict) else None
        voice = str(ctx.get("TTS_VOICE", "af_heart"))
        names = [v if isinstance(v, str) else str(v.get("id") or v.get("name")) for v in voices] if isinstance(voices, list) else []
        if names and voice.split("+")[0] not in names:
            return Result("Voice", "Kokoro TTS", WARN, f"{base} up, but voice {voice} is not available",
                          "Set TTS_VOICE to one of the Kokoro voices (e.g. af_heart)")
        return Result("Voice", "Kokoro TTS", OK, f"{base} · {len(names) or '?'} voices")
    return Result("Voice", "Kokoro TTS", INFO, f"not running ({error or status}) - the live coach falls back to the browser voice",
                  "docker compose --env-file .env -f docker/compose.yml --profile voice up -d kokoro")


# ============================================================================= Integrations
def check_smtp(ctx: Ctx) -> Result:
    host = ctx.get("SMTP_HOST")
    if not host:
        return Result("Integrations", "E-mail (SMTP)", INFO, "SMTP_HOST not set - e-mails are written to var/mail/*.eml")
    port = int(ctx.get("SMTP_PORT", 587) or 587)
    if not ctx.network:
        return Result("Integrations", "E-mail (SMTP)", SKIP, f"{host}:{port} (not probed: --no-network)")
    try:
        with socket.create_connection((str(host), port), timeout=ctx.timeout):
            pass
    except OSError as exc:
        return Result("Integrations", "E-mail (SMTP)", WARN, f"cannot connect to {host}:{port} ({short_error(exc)}) - mails stay queued",
                      "Check SMTP_HOST / SMTP_PORT and your firewall")
    return Result("Integrations", "E-mail (SMTP)", OK, f"{host}:{port} reachable")


def check_geoapify(ctx: Ctx) -> Result:
    if ctx.get("GEOAPIFY_API_KEY"):
        return Result("Integrations", "Maps (Geoapify)", OK, "API key set")
    return Result("Integrations", "Maps (Geoapify)", INFO, "no GEOAPIFY_API_KEY - OpenStreetMap tiles/search fallback",
                  "Free key: https://myprojects.geoapify.com")


def check_cashfree(ctx: Ctx) -> Result:
    env = ctx.get("CASHFREE_ENV", "sandbox")
    pg = bool(ctx.get("CASHFREE_APP_ID") and ctx.get("CASHFREE_SECRET_KEY"))
    payouts = bool(ctx.get("CASHFREE_PAYOUT_CLIENT_ID") and ctx.get("CASHFREE_PAYOUT_CLIENT_SECRET"))
    if not pg:
        return Result("Integrations", "Payments (Cashfree)", INFO, "TEST MODE - payments and payouts are simulated and clearly labelled",
                      "Add Cashfree sandbox keys to .env to test the real gateway")
    return Result("Integrations", "Payments (Cashfree)", OK if env == "sandbox" else WARN,
                  f"Cashfree {env}{' + payouts' if payouts else ' (payouts simulated)'}",
                  "" if env == "sandbox" else "PRODUCTION keys move real money - double-check before going live")


# ============================================================================= Server
def check_port(ctx: Ctx) -> Result:
    host = str(ctx.get("HOST", "127.0.0.1"))
    try:
        port = int(ctx.get("PORT", 10000))
    except (TypeError, ValueError):
        return Result("Server", "App port", FAIL, f"PORT={ctx.get('PORT')!r} is not a number", "Fix PORT in .env", critical=True)
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as s:
            s.bind((host, port))
    except OSError as exc:
        if getattr(exc, "errno", None) in (13, 10013):
            return Result("Server", "App port", WARN, f"no permission to use {host}:{port}", "Pick a PORT above 1024 in .env")
        if ctx.network:
            probe_host = "127.0.0.1" if host in ("0.0.0.0", "") else ("[::1]" if host in ("::", "::1") else host)
            status, data, _ = http_get(f"http://{probe_host}:{port}/healthz", timeout=2.0)
            if status in (200, 503) and isinstance(data, dict) and "database" in data:
                return Result("Server", "App port", INFO, f"IronPulse is already running on http://{probe_host}:{port}")
        return Result("Server", "App port", WARN, f"{host}:{port} is already in use",
                      "Stop the other program, or set another PORT (and PUBLIC_BASE_URL) in .env")
    return Result("Server", "App port", OK, f"{host}:{port} is free · open http://localhost:{port} after starting")


# ============================================================================= runner
def collect(ctx: Ctx) -> list[Result]:
    steps: list[tuple[str, str, Callable[[], Any]]] = [
        ("System", "Python version", check_python),
        ("System", "Virtual environment", check_venv),
        ("System", "Python packages", check_packages),
        ("Configuration", ".env file", lambda: check_env_file(ctx)),
        ("Configuration", "Required secrets", lambda: check_secrets(ctx)),
        ("Configuration", "Settings load", lambda: check_settings(ctx)),
        ("Configuration", "Production settings", lambda: check_production(ctx)),
        ("Database", "PostgreSQL connection", lambda: check_database(ctx)),
        ("Database", "Redis sessions", lambda: check_redis(ctx)),
        ("OCR", "Tesseract", lambda: check_tesseract(ctx)),
        ("OCR", "OCR engines", lambda: check_ocr_engines(ctx)),
        ("Assets", "Tailwind CLI", check_tailwind),
        ("Assets", "Built CSS/JS", check_dist_assets),
        ("Assets", "ffmpeg (imageio-ffmpeg)", check_ffmpeg),
        ("Assets", "Generated videos", check_videos),
        ("AI", "AI providers", lambda: check_ai(ctx)),
        ("Voice", "Kokoro TTS", lambda: check_kokoro(ctx)),
        ("Integrations", "E-mail (SMTP)", lambda: check_smtp(ctx)),
        ("Integrations", "Maps (Geoapify)", lambda: check_geoapify(ctx)),
        ("Integrations", "Payments (Cashfree)", lambda: check_cashfree(ctx)),
        ("Server", "App port", lambda: check_port(ctx)),
    ]
    results: list[Result] = []
    for group, name, fn in steps:
        try:
            out = fn()
        except Exception as exc:  # noqa: BLE001 - one broken probe never hides the others
            out = Result(group, name, WARN, f"check crashed: {short_error(exc)}")
        except SystemExit as exc:  # app.config.ConfigError from a probe that loads the settings itself
            out = Result(group, name, WARN, f"check stopped: {exc}")
        results.extend(out if isinstance(out, list) else [out])
    return results


TAGS = {OK: "[ OK ]", INFO: "[info]", WARN: "[WARN]", FAIL: "[FAIL]", SKIP: "[skip]"}
COLORS = {OK: "\033[32m", INFO: "\033[36m", WARN: "\033[33m", FAIL: "\033[31m", SKIP: "\033[2m"}


def _use_color(stream: Any) -> bool:
    if os.environ.get("NO_COLOR") or not getattr(stream, "isatty", lambda: False)():
        return False
    return os.name != "nt" or bool(os.environ.get("WT_SESSION") or os.environ.get("TERM"))


def render(report: Report, *, color: bool = False, width: int | None = None) -> str:
    width = width or max(80, min(shutil.get_terminal_size((110, 24)).columns, 140))
    name_w = max(len(r.name) for r in report.results) + 2 if report.results else 20
    detail_w = max(30, width - name_w - 10)
    lines = [f"IronPulse environment check - Python {platform.python_version()} on {platform.system()} {platform.machine()}", ""]
    groups = [g for g in GROUP_ORDER if any(r.group == g for r in report.results)]
    groups += sorted({r.group for r in report.results} - set(groups))
    for group in groups:
        lines.append(group)
        for r in (r for r in report.results if r.group == group):
            tag = TAGS.get(r.status, r.status)
            if color:
                tag = f"{COLORS.get(r.status, '')}{tag}\033[0m"
            wrapped = textwrap.wrap(r.detail, detail_w) or [""]
            lines.append(f"  {tag} {r.name.ljust(name_w)}{wrapped[0]}")
            lines.extend(" " * (9 + name_w) + more for more in wrapped[1:])
        lines.append("")
    fixes = [r for r in report.results if r.status in (FAIL, WARN) and r.fix]
    if fixes:
        lines.append("How to fix")
        for r in fixes:
            marker = "!!" if r.status == FAIL else " -"
            lines.extend(textwrap.wrap(f"{r.name}: {r.fix}", width - 4, initial_indent=f" {marker} ", subsequent_indent="    "))
        lines.append("")
    c = report.counts()
    verdict = ("NOT READY - fix the [FAIL] items marked !!" if report.critical_failures
               else "ready (some optional features are degraded)" if (report.failures or report.warnings) else "ready")
    lines.append(f"Summary: {c[OK]} ok · {c[INFO]} info · {c[WARN]} warnings · {c[FAIL]} failures · {c[SKIP]} skipped -> {verdict}")
    return "\n".join(lines)


def add_arguments(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    ap.add_argument("--no-network", action="store_true", help="skip AI, voice and SMTP probes (and the running-app probe)")
    ap.add_argument("--no-db", action="store_true", help="skip the PostgreSQL and Redis checks")
    ap.add_argument("--strict", action="store_true", help="exit code 1 on any warning or failure")
    ap.add_argument("--quiet", action="store_true", help="print nothing (use the exit code)")
    ap.add_argument("--timeout", type=float, default=3.0, metavar="SECONDS", help="per-probe network timeout (default 3)")
    ap.add_argument("--ai-probe", action="store_true",
                    help="also ask each Gemma server for a 1-token answer and show its latency (up to 60 s each)")


def run(args: argparse.Namespace, *, out: Any = None) -> Report:
    out = out or sys.stdout
    ctx = Ctx(network=not args.no_network, db=not args.no_db, timeout=max(0.5, args.timeout),
              ai_probe=bool(getattr(args, "ai_probe", False)))
    report = Report(collect(ctx), strict=args.strict)
    if args.json:
        print(json.dumps(report.as_dict(), indent=2), file=out)
    elif not args.quiet:
        print(render(report, color=_use_color(out)), file=out)
    return report


def main(argv: list[str] | None = None) -> Report:
    """Parse ``argv``, run every check, print the report and return it (``report.exit_code`` for scripts)."""
    ap = argparse.ArgumentParser(prog="check_env.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_arguments(ap)
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main().exit_code)
