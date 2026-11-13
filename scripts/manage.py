"""IronPulse management CLI - one entry point for every maintenance task (Windows / macOS / Linux).

    python scripts/manage.py <command> [options]          run it with the project's .venv interpreter
    python scripts/manage.py <command> --help             details for one command

Database   init-db         create the database, role, extensions (pgvector, citext, pg_trgm) and tables
           reset-db --yes  drop and recreate every table (DELETES ALL DATA)
           seed            load the demo data (accounts are written to var/demo_credentials.txt)
           create-superadmin   create a platform super-admin, or reset its password
Security   unban <ip>      lift every ban of a network address from this computer's terminal (e.g. after failed
                           platform-admin sign-ins - /superadmin/login bans an address after 5 failures)
Assets     build-assets    Tailwind CSS + JS bundle  ->  app/static/dist
           gen-videos      generate the pre-recorded workout videos
Run & QA   run             start the web app (python -m app; --reload, --workers N)
           test            run the test-suite (pytest; extra arguments are passed through)
           stress          run the stress test (scripts/stress_test.py)
           check           diagnose the installation (scripts/check_env.py)
Config     make-env        create/complete .env with fresh secrets
           configure       write the database/Redis/AI connection settings for a setup mode
           env             read or write single .env values (used by the setup scripts)
AI         ai-status       every provider of the chain: up, model pulled, a 1-token latency probe
           ai-test         one tiny real answer from each provider (--provider docker ...) through the app's own code
           ai-fallback     start the local AI fallback (Ollama in Docker / native) in the background (run.sh does)
           ai-chat         chat with Gemma through the OpenAI-compatible gateway from the terminal

Every command uses the interpreter that runs this file (sys.executable) for its subprocesses.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import importlib.util
import ipaddress
import os
import re
import secrets
import shlex
import stat
import string
import subprocess
import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
DB_MODES = ("docker", "runtime", "native", "existing")
FALLBACK_DISCLAIMER = "AI can make mistakes. Double-check important information - this is not medical, legal or financial advice."
DEFAULT_CHAT_SYSTEM = (
    "You are the IronPulse gym assistant, powered by Gemma. Answer clearly and briefly. You are not a doctor: for "
    "pain, injuries or medical conditions recommend seeing a qualified professional."
)
PASSTHROUGH = {"build-assets", "gen-videos", "test", "stress", "make-env"}


class CommandError(Exception):
    """A user-facing error: printed without a traceback, exit code 1 (or ``code``)."""

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


def say(msg: str) -> None:
    print(f"[manage] {msg}", flush=True)


def load_script(name: str) -> Any:
    """Import scripts/<name>.py (shared with check_env so each sibling script is loaded once)."""
    key = f"ironpulse_scripts_{name}"
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, SCRIPTS / f"{name}.py")
    if spec is None or spec.loader is None:
        raise CommandError(f"scripts/{name}.py is missing")
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(key, None)
        raise
    return module


def run_python(args: Iterable[str], *, env: dict[str, str] | None = None) -> int:
    """Run ``sys.executable <args>`` in the project folder and return its exit code."""
    cmd = [sys.executable, *args]
    try:
        return subprocess.call(cmd, cwd=str(ROOT), env=env)
    except KeyboardInterrupt:
        return 130


def startup_warnings(settings: Any) -> list[str]:
    """What the server will warn about at startup (``app.config.startup_warnings``: explicit opt-outs of the production
    defaults, the simulated AI provider), printed once by ``run`` before the workers start. Never fails the start."""
    try:
        from app.config import startup_warnings as collect

        return [str(line) for line in collect(settings)]
    except Exception:  # noqa: BLE001 - settings stand-ins (tests) or an older app: no warnings
        return []


def forwarded_allow_ips(settings: Any) -> str:
    """TRUSTED_PROXIES as uvicorn's --forwarded-allow-ips: only entries that are addresses or networks. uvicorn reads
    '*' (or anything it cannot parse as an address) as 'trust every client', while the app ignores such entries - so
    they are never passed on. Without one, uvicorn would fall back to $FORWARDED_ALLOW_IPS (container images often
    set it to '*'): 127.0.0.1 then."""
    nets: list[str] = []
    for entry in getattr(settings, "trusted_proxies", None) or []:
        text = str(entry).strip()
        try:
            ipaddress.ip_network(text, strict=False)
        except ValueError:
            continue
        nets.append(text)
    return ",".join(dict.fromkeys(nets)) or "127.0.0.1"


def load_settings() -> Any:
    try:
        from app.config import get_settings

        return get_settings()
    except Exception as exc:  # noqa: BLE001 - pydantic ValidationError, missing .env ...
        first = " ".join(str(exc).strip().splitlines()[:4])
        raise CommandError(f"the app settings could not be loaded ({exc.__class__.__name__}: {first[:300]}). "
                           "Run: python scripts/make_env.py  and check .env") from exc


# ============================================================================= .env helpers
def _env_text(path: Path) -> str:
    if not path.is_file():
        raise CommandError(f"{path} does not exist - create it first: python scripts/make_env.py")
    return path.read_text(encoding="utf-8")


def _write_env(path: Path, text: str, updates: dict[str, str]) -> None:
    make_env = load_script("make_env")
    path.write_text(make_env.set_values(text, updates), encoding="utf-8")
    if os.name != "nt":
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def new_secret() -> str:
    return secrets.token_urlsafe(24)


def runtime_secret(home: Path, name: str) -> str:
    """Read (or create, mode 600) a password file of the user-space runtime (~/.ironpulse/<name>)."""
    path = home / name
    if path.is_file():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    home.mkdir(parents=True, exist_ok=True)
    value = secrets.token_hex(16)
    path.write_text(value, encoding="utf-8")
    if os.name != "nt":
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return value


def _replace_db(url: str, database: str) -> str:
    init_db = load_script("init_db")
    return init_db.with_database(url, database)


def configure_env(
    env_path: Path,
    mode: str,
    *,
    database_url: str | None = None,
    admin_url: str | None = None,
    test_database_url: str | None = None,
    redis: str = "auto",
    tesseract: str | None = None,
    runtime_home: Path | None = None,
    db_port: int | None = None,
) -> dict[str, str]:
    """Write the connection settings for a setup mode into ``env_path``. Returns the changed keys.

    docker   PostgreSQL on localhost:5433 + Redis on :6380 (docker/compose.yml); passwords generated once
    runtime  user-space PostgreSQL on :5432 + Redis on :6379 (~/.ironpulse password files)
    native   system PostgreSQL on :5432 (role "ironpulse"); DATABASE_ADMIN_URL from --admin-url
    existing your own server (--database-url)
    Existing passwords are kept, so running it again never breaks a working installation.
    """
    if mode not in DB_MODES:
        raise CommandError(f"unknown mode {mode!r} - use one of {', '.join(DB_MODES)}", code=2)
    init_db = load_script("init_db")
    make_env = load_script("make_env")
    text = _env_text(env_path)
    current = make_env.parse(text)
    updates: dict[str, str] = {"IRONPULSE_DB_MODE": mode}

    def secret(key: str) -> str:
        value = current.get(key) or updates.get(key)
        if not value:
            value = new_secret()
            updates[key] = value
        return value

    home = runtime_home or init_db.runtime_home()
    if mode == "docker":
        port = db_port or 5433
        pg_pw, app_pw = secret("POSTGRES_PASSWORD"), secret("APP_DB_PASSWORD")
        updates["DATABASE_URL"] = f"postgresql+asyncpg://ironpulse:{app_pw}@localhost:{port}/ironpulse"
        updates["TEST_DATABASE_URL"] = f"postgresql+asyncpg://ironpulse:{app_pw}@localhost:{port}/ironpulse_test"
        updates["DATABASE_ADMIN_URL"] = f"postgresql+asyncpg://postgres:{pg_pw}@localhost:{port}/postgres"
        if redis in ("auto", "docker"):
            updates["REDIS_URL"] = f"redis://:{secret('REDIS_PASSWORD')}@localhost:6380/0"
        updates["AI_DOCKER_BASE_URL"] = current.get("AI_DOCKER_BASE_URL") or "http://localhost:11434/v1"
        updates["AI_DOCKER_API_KEY"] = current.get("AI_DOCKER_API_KEY") or "ollama"
        updates["AI_DOCKER_MODEL"] = current.get("AI_DOCKER_MODEL") or "gemma3:4b"
        updates["TTS_BASE_URL"] = current.get("TTS_BASE_URL") or "http://localhost:8880/v1"
    elif mode == "runtime":
        port = db_port or int(os.environ.get("IRONPULSE_PGPORT") or 5432)
        app_pw = runtime_secret(home, "app_db_password")
        runtime_secret(home, "pg_superuser_password")  # initdb uses it (scripts/runtime_postgres.sh init)
        updates["DATABASE_URL"] = f"postgresql+asyncpg://ironpulse:{app_pw}@localhost:{port}/ironpulse"
        updates["TEST_DATABASE_URL"] = f"postgresql+asyncpg://ironpulse:{app_pw}@localhost:{port}/ironpulse_test"
        updates["DATABASE_ADMIN_URL"] = ""  # init-db reads the superuser password file directly
        if redis in ("auto", "runtime"):
            redis_port = int(os.environ.get("IRONPULSE_REDIS_PORT") or 6379)
            updates["REDIS_URL"] = f"redis://:{runtime_secret(home, 'redis_password')}@127.0.0.1:{redis_port}/0"
    elif mode == "native":
        port = db_port or 5432
        app_pw = secret("APP_DB_PASSWORD")
        updates["DATABASE_URL"] = f"postgresql+asyncpg://ironpulse:{app_pw}@localhost:{port}/ironpulse"
        updates["TEST_DATABASE_URL"] = f"postgresql+asyncpg://ironpulse:{app_pw}@localhost:{port}/ironpulse_test"
        if admin_url:
            updates["DATABASE_ADMIN_URL"] = init_db.normalize_url(admin_url)
    else:  # existing
        url = init_db.normalize_url(database_url or current.get("DATABASE_URL") or "")
        if not url:
            raise CommandError("--database-url is required for --db existing", code=2)
        try:
            parsed = init_db.parse_url(url)
            if not parsed.database:
                raise ValueError("no database name")
        except Exception as exc:  # noqa: BLE001
            raise CommandError(f"invalid --database-url ({exc})", code=2) from exc
        updates["DATABASE_URL"] = url
        updates["TEST_DATABASE_URL"] = init_db.normalize_url(test_database_url) if test_database_url else _replace_db(url, f"{parsed.database}_test")
        if admin_url:
            updates["DATABASE_ADMIN_URL"] = init_db.normalize_url(admin_url)
    if redis.startswith("redis://") or redis.startswith("rediss://"):
        updates["REDIS_URL"] = redis
    elif redis == "none":
        updates["REDIS_URL"] = ""
    if tesseract:
        updates["TESSERACT_CMD"] = tesseract
    changed = {k: v for k, v in updates.items() if current.get(k, None) != v}
    if changed:
        _write_env(env_path, text, changed)
    return changed


SECRET_KEYS = ("PASSWORD", "SECRET", "KEY", "PEPPER", "TOKEN")


def redact_value(key: str, value: str) -> str:
    if not value:
        return "(empty)"
    if "://" in value and "@" in value:
        scheme, rest = value.split("://", 1)
        creds, host = rest.rsplit("@", 1)
        user = creds.split(":", 1)[0]
        return f"{scheme}://{user}:***@{host}"
    if any(word in key for word in SECRET_KEYS):
        return "***"
    return value


# ============================================================================= create-superadmin
def generate_password(length: int = 20) -> str:
    alphabet = string.ascii_letters + string.digits + "-_.!@#%^*"
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw) and any(c.isdigit() for c in pw)
                and any(not c.isalnum() for c in pw)):
            return pw


async def create_superadmin(db: Any, *, email: str, password: str, name: str = "Platform Admin",
                            reset: bool = False) -> dict[str, Any]:
    """Create a platform super-admin (or reset an existing super-admin's password). The caller commits."""
    from sqlalchemy import select

    from app.core.utils import utcnow
    from app.models import User
    from app.models.enums import IdStatus, NotificationCategory, Role, Severity
    from app.security.passwords import hash_password, password_problems
    from app.security.sessions import revoke_all_sessions
    from app.services.audit import audit
    from app.services.identity_lookup import generate_username
    from app.services.notify import notify

    email = (email or "").strip().lower()
    if len(email) > 254 or not EMAIL_RE.match(email):
        raise CommandError("enter a valid e-mail address (for example admin@example.com)", code=2)
    name = " ".join((name or "").split())[:120] or "Platform Admin"
    problems = password_problems(password or "", email=email, name=name)
    if problems:
        raise CommandError("password rejected: " + " ".join(problems), code=2)

    user = await db.scalar(select(User).where(User.email == email))
    if user is not None:
        if user.role != Role.SUPERADMIN:
            raise CommandError(f"{email} already belongs to a {user.role.value} account - use a separate e-mail for the super-admin")
        if user.deleted_at is not None:
            raise CommandError(f"{email} belongs to a deleted account that is still inside its retention window")
        if not reset:
            raise CommandError(f"a super-admin with {email} already exists - add --reset-password to set a new password")
        user.password_hash = await hash_password(password)
        user.failed_login_count = 0
        user.locked_until = None
        user.is_active = True
        await revoke_all_sessions(db, user.id)
        await notify(db, user, "Your password was reset",
                     "Your IronPulse super-admin password was reset from the server command line (scripts/manage.py) and "
                     "every session was signed out. If you did not expect this, contact the server administrator now.",
                     category=NotificationCategory.SECURITY, severity=Severity.WARNING, link="/superadmin/login")
        await audit(db, None, "superadmin.password_reset", "user", user.id, detail={"via": "cli", "email": email})
        return {"action": "reset", "user_id": user.id, "email": email, "username": user.username}

    username = await generate_username(db, name, email)
    user = User(email=email, full_name=name, role=Role.SUPERADMIN, password_hash=await hash_password(password),
                email_verified_at=utcnow(), id_status=IdStatus.VERIFIED, timezone="UTC", username=username)
    db.add(user)
    await db.flush()
    await notify(db, user, "Your super-admin account is ready",
                 f"A platform super-admin account was created for you from the server command line. Sign in with {email} "
                 "at /superadmin/login (platform admins sign in only there) to open the security centre.",
                 category=NotificationCategory.ACCOUNT, severity=Severity.SUCCESS, link="/superadmin")
    await audit(db, None, "superadmin.created", "user", user.id, detail={"via": "cli", "email": email})
    return {"action": "created", "user_id": user.id, "email": email, "username": username}


# ============================================================================= AI chat helpers
def ai_target(settings: Any, provider: str, *, base_url: str | None = None, model: str | None = None) -> dict[str, str]:
    if provider == "docker":
        return {"label": "Gemma in Docker (Ollama)", "base_url": base_url or settings.ai_docker_base_url or "",
                "api_key": settings.ai_docker_api_key or "ollama", "model": model or settings.ai_docker_model}
    return {"label": "Gemma gateway", "base_url": base_url or settings.ai_gateway_base_url or "",
            "api_key": settings.ai_gateway_api_key or "none", "model": model or settings.ai_gateway_model}


def trim_history(messages: list[dict[str, str]], *, max_chars: int = 12000, max_messages: int = 16) -> list[dict[str, str]]:
    """Keep the system prompt + the latest turns (Gemma has an 8k-token context)."""
    head = messages[:1] if messages and messages[0]["role"] == "system" else []
    rest = messages[len(head):][-max_messages:]
    while len(rest) > 1 and sum(len(m["content"]) for m in rest) > max_chars:
        rest = rest[1:]
    while rest and rest[0]["role"] != "user":
        rest = rest[1:]
    return head + rest


def chat_once(client: Any, model: str, messages: list[dict[str, str]], *, stream: bool = True, max_tokens: int = 600,
              temperature: float = 0.6, write: Callable[[str], Any] | None = None) -> str:
    """Send the conversation through the OpenAI SDK; stream the answer to ``write``; return the full text."""
    write = write or (lambda s: (sys.stdout.write(s), sys.stdout.flush()))
    if not stream:
        resp = client.chat.completions.create(model=model, messages=messages, max_tokens=max_tokens, temperature=temperature)
        text = (resp.choices[0].message.content or "") if getattr(resp, "choices", None) else ""
        write(text + "\n")
        return text
    parts: list[str] = []
    events = client.chat.completions.create(model=model, messages=messages, max_tokens=max_tokens,
                                            temperature=temperature, stream=True)
    for event in events:
        choices = getattr(event, "choices", None) or []
        if not choices:
            continue
        delta = getattr(choices[0], "delta", None)
        piece = getattr(delta, "content", None) if delta is not None else None
        if piece:
            parts.append(piece)
            write(piece)
    write("\n")
    return "".join(parts)


def describe_ai_error(exc: BaseException, target: dict[str, str]) -> str:
    try:
        import openai
    except ImportError:  # pragma: no cover
        return str(exc)
    if isinstance(exc, openai.APITimeoutError):
        return f"{target['label']} at {target['base_url']} timed out - it may be busy; try again or raise --timeout"
    if isinstance(exc, openai.APIConnectionError):
        return (f"cannot reach {target['base_url']} - is the {target['label']} running and reachable from this computer? "
                "Check with: python scripts/manage.py ai-status")
    if isinstance(exc, openai.AuthenticationError):
        return "the server rejected the API key - check AI_GATEWAY_API_KEY in .env"
    if isinstance(exc, openai.NotFoundError):
        return f"model '{target['model']}' was not found on {target['base_url']} - check the model name (ai-status lists them)"
    if isinstance(exc, openai.APIStatusError):
        return f"HTTP {exc.status_code} from {target['base_url']}: {str(exc)[:200]}"
    return f"{exc.__class__.__name__}: {str(exc)[:200]}"


# ============================================================================= commands
def cmd_init_db(args: argparse.Namespace, extra: list[str]) -> int:
    return load_script("init_db").run(args)


def cmd_reset_db(args: argparse.Namespace, extra: list[str]) -> int:
    if not args.yes:
        say("reset-db DROPS EVERY TABLE and recreates them empty - all members, gyms, payments and uploads metadata are lost.")
        say("Re-run with --yes if that is really what you want:  python scripts/manage.py reset-db --yes")
        return 2
    settings = load_settings()
    init_db = load_script("init_db")
    if settings.is_production and not args.force:
        raise CommandError("ENVIRONMENT=production - refusing to reset. Add --force if you are absolutely sure.", code=2)
    say(f"resetting {init_db.redact(settings.database_url)} ...")

    async def _reset() -> None:
        from app.db import create_all, dispose_engine, drop_all

        try:
            await drop_all()
            await create_all()
        finally:
            await dispose_engine()

    asyncio.run(_reset())
    say("done - every table is empty. Load demo data with: python scripts/manage.py seed")
    return 0


def seed_available() -> tuple[bool, str]:
    """The demo data is a data file (scripts/demo/dataset.json) loaded by scripts/seed_demo.py - nothing in app/."""
    for rel in ("seed_demo.py", "demo/loader.py", "demo/dataset.json"):
        if not (SCRIPTS / rel).is_file():
            return False, f"scripts/{rel} is missing"
    return True, ""


def cmd_seed(args: argparse.Namespace, extra: list[str]) -> int:
    ok, why = seed_available()
    if not ok:
        say(f"the demo seed is not available yet ({why}). Nothing was changed.")
        return 1
    say("loading demo data (idempotent) ...")
    return run_python([str(SCRIPTS / "seed_demo.py")])


def cmd_create_superadmin(args: argparse.Namespace, extra: list[str]) -> int:
    settings = load_settings()
    email = args.email or settings.superadmin_email
    generated = False
    if args.password:
        say("note: a password typed on the command line can end up in your shell history.")
        password = args.password
    elif args.generate_password or not sys.stdin.isatty():
        password, generated = generate_password(), True
    else:
        password = getpass.getpass(f"New password for {email}: ")
        if password != getpass.getpass("Repeat the password: "):
            raise CommandError("the passwords do not match", code=2)

    async def _go() -> dict[str, Any]:
        from app.db import dispose_engine, session_scope

        try:
            async with session_scope() as db:
                return await create_superadmin(db, email=email, password=password, name=args.name, reset=args.reset_password)
        finally:
            await dispose_engine()

    result = asyncio.run(_go())
    verb = "created" if result["action"] == "created" else "password reset for"
    say(f"super-admin {verb} {result['email']} (@{result.get('username') or '-'}) - sign in at "
        f"{settings.public_base_url}/superadmin/login")
    if generated:
        say(f"generated password (shown once, store it safely): {password}")
    return 0


async def unban_address(db: Any, ip: str, *, note: str = "") -> list[Any]:
    """Lift every active ban of ``ip`` (and of its IPv6 /64) and clear its failed platform-admin sign-ins, threat score
    and ban claim (app/security/admin_login.py ``unban``). The caller commits. Raises CommandError for a bad address."""
    from app.security import admin_login

    raw = (ip or "").strip().strip("[]")
    try:
        if "/" in raw:
            raw = str(ipaddress.ip_network(raw, strict=False))
        else:
            raw = str(ipaddress.ip_address(raw))
    except ValueError:
        raise CommandError(f"{ip!r} is not an IP address", code=2) from None
    return await admin_login.unban(db, raw, note=note or "lifted from the server's terminal (manage.py unban)")


def cmd_unban(args: argparse.Namespace, extra: list[str]) -> int:
    async def _go() -> list[Any]:
        from app.db import dispose_engine, session_scope

        try:
            async with session_scope() as db:
                return await unban_address(db, args.ip, note=args.note or "")
        finally:
            await dispose_engine()

    lifted = asyncio.run(_go())
    if not lifted:
        say(f"no active ban for {args.ip} - nothing to lift (its failed admin sign-ins were cleared)")
        return 0
    say(f"lifted {len(lifted)} ban{'s' if len(lifted) != 1 else ''} of {args.ip} - every worker lets it in again "
        "within 30 seconds (their next ban sync)")
    return 0


def cmd_build_assets(args: argparse.Namespace, extra: list[str]) -> int:
    return run_python([str(SCRIPTS / "build_assets.py"), *extra])


def cmd_gen_videos(args: argparse.Namespace, extra: list[str]) -> int:
    script = SCRIPTS / "generate_videos.py"
    if not script.is_file():
        say("the video generator (scripts/generate_videos.py) is not installed yet - nothing to do. "
            "The app works without videos; the video pages show an empty state until they exist.")
        return 1
    return run_python([str(script), *extra])


def cmd_check(args: argparse.Namespace, extra: list[str]) -> int:
    return load_script("check_env").run(args).exit_code


def cmd_run(args: argparse.Namespace, extra: list[str]) -> int:
    env = dict(os.environ)
    if args.host:
        env["HOST"] = args.host
    if args.port:
        env["PORT"] = str(args.port)
    if args.workers and args.workers > 1:
        if args.reload:
            raise CommandError("--reload cannot be combined with --workers", code=2)
        settings = load_settings()
        host = args.host or settings.host
        port = args.port or settings.port
        env["REALTIME_PG_BRIDGE"] = "true"  # live events fan out between worker processes via LISTEN/NOTIFY
        if not getattr(settings, "redis_url", None):  # docs/ai-guard-and-queue.md section 5 (each worker logs it too)
            say("warning: several workers without REDIS_URL - presence is not shared between them, so leaving the "
                "site stops a member's AI work only when they sign out (see README); rate limits, threat scores and "
                f"the sign-in lockout are kept per worker ({args.workers} x the allowance)")
        for line in startup_warnings(settings):  # production opt-outs, the simulated AI (each worker logs them too)
            say(f"warning: {line}")
        say(f"starting {args.workers} workers on http://{host}:{port}")
        cmd = ["-m", "uvicorn", "app.main:app", "--host", str(host), "--port", str(port), "--workers", str(args.workers),
               "--no-server-header", "--proxy-headers" if settings.trust_proxy_headers else "--no-proxy-headers"]
        if settings.trust_proxy_headers:  # uvicorn's own trust list = ours (it would read $FORWARDED_ALLOW_IPS else)
            cmd += ["--forwarded-allow-ips", forwarded_allow_ips(settings)]
        return run_python(cmd, env=env)
    if args.reload:
        env["RELOAD"] = "true"
    return run_python(["-m", "app"], env=env)


def cmd_test(args: argparse.Namespace, extra: list[str]) -> int:
    return run_python(["-m", "pytest", *extra])


def cmd_stress(args: argparse.Namespace, extra: list[str]) -> int:
    script = SCRIPTS / "stress_test.py"
    if not script.is_file():
        say("the stress test (scripts/stress_test.py) is not installed yet.")
        return 1
    return run_python([str(script), *extra])


def cmd_make_env(args: argparse.Namespace, extra: list[str]) -> int:
    return run_python([str(SCRIPTS / "make_env.py"), *extra])


def cmd_configure(args: argparse.Namespace, extra: list[str]) -> int:
    env_path = Path(args.env_file)
    changed = configure_env(
        env_path, args.db, database_url=args.database_url, admin_url=args.admin_url,
        test_database_url=args.test_database_url, redis=args.redis, tesseract=args.tesseract,
        runtime_home=Path(args.runtime_home) if args.runtime_home else None, db_port=args.db_port,
    )
    if not changed:
        say(f"{env_path.name} already configured for '{args.db}' - nothing changed")
    for key in sorted(changed):
        say(f"{key} = {redact_value(key, changed[key])}")
    return 0


def cmd_env(args: argparse.Namespace, extra: list[str]) -> int:
    env_path = Path(args.env_file)
    make_env = load_script("make_env")
    if args.action == "get":
        values = make_env.parse(env_path.read_text(encoding="utf-8")) if env_path.is_file() else {}
        for key in args.items:
            value = os.environ.get(key, "") if args.effective else ""
            print(value or values.get(key, ""))
        return 0
    text = _env_text(env_path)
    current = make_env.parse(text)
    updates: dict[str, str] = {}
    if args.action == "ensure":
        for key in args.items:
            if not KEY_RE.match(key):
                raise CommandError(f"invalid key {key!r}", code=2)
            if not current.get(key):
                updates[key] = new_secret()
    else:  # set
        for item in args.items:
            key, sep, value = item.partition("=")
            if not sep or not KEY_RE.match(key):
                raise CommandError(f"expected KEY=VALUE, got {item!r}", code=2)
            updates[key] = value
    if updates:
        _write_env(env_path, text, updates)
        for key in sorted(updates):
            say(f"{key} = {redact_value(key, updates[key])}")
    return 0


def cmd_ai_status(args: argparse.Namespace, extra: list[str]) -> int:
    check_env = load_script("check_env")
    ctx = check_env.Ctx(network=True, db=False, timeout=args.timeout, ai_probe=not args.no_probe,
                        probe_timeout=args.probe_timeout)
    results = check_env.check_ai(ctx) + [check_env.check_kokoro(ctx)]
    report = check_env.Report(results)
    print(check_env.render(report, color=check_env._use_color(sys.stdout)))  # noqa: SLF001
    providers = [r for r in results if r.name in ("Gemma gateway", "Gemma in Docker", "Claude (Anthropic)")]
    return 0 if any(r.status == check_env.OK for r in providers) else 1


AI_PROVIDERS = ("gateway", "docker", "anthropic", "simulated")


def cmd_ai_test(args: argparse.Namespace, extra: list[str]) -> int:
    """One tiny real answer from each provider, through the app's own provider code (its connect timeout, its SSE
    parsing) - no guard, no queue, no circuit breaker. Exit code 0 when every tested provider answered."""
    settings = load_settings()
    from app.ai import client

    names = [n for n in settings.ai_provider_order if n in AI_PROVIDERS] if args.provider == "all" else [args.provider]
    rows: list[tuple[str, str, dict[str, Any]]] = []

    async def go() -> None:
        for name in names:
            if name == "simulated":
                provider = client.SimulatedProvider(settings)
            else:
                provider = client.build_provider(settings, name)
            result = await client.probe_provider(provider, timeout=args.timeout)
            rows.append((name, f"{provider.label} · {provider.model}", result))

    asyncio.run(go())
    ok = True
    for name, what, r in rows:
        if r["ok"]:
            say(f"{name:9} OK    {what} · first token {r['first_ms']} ms, done in {r['total_ms']} ms "
                f"(answer: {r['text']!r})")
        else:
            ok = False
            say(f"{name:9} FAIL  {what} · {r['error']} (after {r['total_ms']} ms)")
    if not rows:
        say("no provider to test (AI_PROVIDER_ORDER)")
    return 0 if ok and rows else 1


def cmd_ai_fallback(args: argparse.Namespace, extra: list[str]) -> int:
    load_script("ai_fallback").run(dry_run=args.dry_run, as_json=args.json, disabled=args.no_ai_docker)
    return 0  # never fails the start: the app runs without the local fallback too


def cmd_ai_chat(args: argparse.Namespace, extra: list[str]) -> int:
    settings = load_settings()
    target = ai_target(settings, args.provider, base_url=args.base_url, model=args.model)
    if not target["base_url"]:
        raise CommandError(f"no base URL configured for the {target['label']} (AI_GATEWAY_BASE_URL / AI_DOCKER_BASE_URL)")
    from openai import OpenAI

    try:
        from app.ai.client import AI_DISCLAIMER as disclaimer
    except Exception:  # noqa: BLE001
        disclaimer = FALLBACK_DISCLAIMER
    client = OpenAI(base_url=target["base_url"], api_key=target["api_key"], timeout=args.timeout, max_retries=0)
    system = args.system or DEFAULT_CHAT_SYSTEM
    messages: list[dict[str, str]] = [{"role": "system", "content": system}]
    options = {"stream": not args.no_stream, "max_tokens": max(16, min(args.max_tokens, 1500)),
               "temperature": max(0.0, min(args.temperature, 1.5))}

    prompt = " ".join(args.prompt).strip()
    if not prompt and not sys.stdin.isatty():
        prompt = sys.stdin.read().strip()
    if prompt:
        messages.append({"role": "user", "content": prompt})
        try:
            chat_once(client, target["model"], messages, **options)
        except Exception as exc:  # noqa: BLE001
            raise CommandError(describe_ai_error(exc, target)) from exc
        return 0

    print(f"Chatting with '{target['model']}' - {target['label']} at {target['base_url']} (OpenAI-compatible API).")
    print(f"{disclaimer}\nType /reset to start over, /exit to quit.\n")
    while True:
        try:
            line = input("you > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line in ("/exit", "/quit", "exit", "quit"):
            return 0
        if line == "/reset":
            messages = messages[:1]
            print("(conversation cleared)")
            continue
        messages.append({"role": "user", "content": line})
        print("gemma > ", end="", flush=True)
        try:
            reply = chat_once(client, target["model"], messages, **options)
        except KeyboardInterrupt:
            print("\n(interrupted)")
            messages.pop()
            continue
        except Exception as exc:  # noqa: BLE001
            print(f"\n[error] {describe_ai_error(exc, target)}")
            messages.pop()
            continue
        messages.append({"role": "assistant", "content": reply})
        messages = trim_history(messages)


# ============================================================================= parser
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="manage.py", description=__doc__, allow_abbrev=False,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", metavar="<command>")

    def add(name: str, handler: Callable[..., int], help_text: str, description: str = "") -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text, description=description or help_text, allow_abbrev=False,
                           formatter_class=argparse.RawDescriptionHelpFormatter)
        p.set_defaults(handler=handler)
        return p

    init_db = load_script("init_db")
    check_env = load_script("check_env")

    p = add("init-db", cmd_init_db, "create the database, role, extensions and tables (idempotent)",
            "Create the PostgreSQL database if it is missing (DATABASE_ADMIN_URL superuser, or the app URL against the\n"
            "'postgres' maintenance database), CREATE EXTENSION vector, citext, pg_trgm, then app.db.create_all().\n"
            "Prints OS-specific pgvector install hints when the extension is not available.")
    init_db.add_arguments(p)

    p = add("reset-db", cmd_reset_db, "drop and recreate every table (DELETES ALL DATA)")
    p.add_argument("--yes", action="store_true", help="confirm that all data may be deleted")
    p.add_argument("--force", action="store_true", help="also allow it when ENVIRONMENT=production")

    add("seed", cmd_seed, "load the demo data (runs scripts/seed_demo.py; credentials -> var/demo_credentials.txt)")

    p = add("create-superadmin", cmd_create_superadmin, "create a platform super-admin or reset its password",
            "Create a platform super-admin (security centre) or, with --reset-password, set a new password for an\n"
            "existing one (signs out every session). Without --password you are prompted; when no terminal is attached\n"
            "a strong password is generated and printed once.")
    p.add_argument("--email", help="e-mail address (default: SUPERADMIN_EMAIL from .env)")
    p.add_argument("--name", default="Platform Admin", help="display name (default: Platform Admin)")
    p.add_argument("--password", help="password (prefer the prompt - command lines end up in shell history)")
    p.add_argument("--generate-password", action="store_true", help="generate a strong password and print it once")
    p.add_argument("--reset-password", action="store_true", help="the super-admin exists: set a new password")

    p = add("unban", cmd_unban, "lift every ban of a network address (run it on the server itself)",
            "Lift every active ban of an IP address - for example your own, after five failed sign-ins at\n"
            "/superadmin/login - and clear its failed sign-in count. An IPv6 address lifts its /64 too. The running\n"
            "server's workers pick the lift up at their next ban sync (within 30 seconds).\n"
            "  python scripts/manage.py unban 203.0.113.7")
    p.add_argument("ip", help="the banned address (IPv4 or IPv6)")
    p.add_argument("--note", default="", help="why it is lifted (kept in the audit log)")

    add("build-assets", cmd_build_assets, "build Tailwind CSS + the JS bundle (extra options go to build_assets.py)",
        "Build app/static/dist/app.min.css (Tailwind v4 standalone CLI, downloaded for this OS when missing) and\n"
        "app/static/dist/app.min.js. Extra options are passed to scripts/build_assets.py, e.g. --no-download, --js-only.")
    add("gen-videos", cmd_gen_videos, "generate the pre-recorded workout videos (scripts/generate_videos.py)")

    p = add("check", cmd_check, "diagnose the installation (scripts/check_env.py)",
            "Diagnostics table: Python, venv, packages, .env secrets, PostgreSQL + pgvector, Redis, Tesseract, OCR engines,\n"
            "Tailwind + built assets, ffmpeg, videos, AI providers (Gemma gateway + status page, Docker, Claude), Kokoro,\n"
            "SMTP, Geoapify, Cashfree and the app port. Exit code 1 on critical failures.")
    check_env.add_arguments(p)

    p = add("run", cmd_run, "start the web app (python -m app)")
    p.add_argument("--reload", action="store_true", help="restart automatically when code changes (development)")
    p.add_argument("--workers", type=int, default=1, metavar="N", help="run N uvicorn worker processes")
    p.add_argument("--host", help="override HOST from .env (0.0.0.0 = reachable from your LAN)")
    p.add_argument("--port", type=int, help="override PORT from .env")

    add("test", cmd_test, "run the tests with pytest (extra arguments are passed to pytest)",
        "Run pytest with this interpreter. Each parallel work package uses its own database:\n"
        "  TEST_DATABASE_URL=postgresql+asyncpg://.../ironpulse_test_3 python scripts/manage.py test tests/test_x.py -q")
    add("stress", cmd_stress, "run the stress test (extra arguments go to scripts/stress_test.py)")
    add("make-env", cmd_make_env, "create/complete .env with fresh secrets (extra arguments go to make_env.py, e.g. --set K=V)")

    p = add("configure", cmd_configure, "write the database/Redis/AI connection settings for a setup mode",
            "Modes:  docker   PostgreSQL :5433 + Redis :6380 from docker/compose.yml (passwords generated once)\n"
            "        runtime  user-space PostgreSQL :5432 + Redis :6379 under ~/.ironpulse (macOS/Linux)\n"
            "        native   PostgreSQL installed on this computer (role 'ironpulse')\n"
            "        existing your own server: --database-url URL")
    p.add_argument("--db", required=True, choices=DB_MODES, help="setup mode")
    p.add_argument("--database-url", metavar="URL", help="existing mode: the app database URL")
    p.add_argument("--test-database-url", metavar="URL", help="existing mode: test database URL (default <db>_test)")
    p.add_argument("--admin-url", metavar="URL", help="superuser URL for init-db (stored as DATABASE_ADMIN_URL)")
    p.add_argument("--redis", default="auto", metavar="auto|none|URL", help="Redis session store (default: per mode)")
    p.add_argument("--tesseract", metavar="PATH", help="store TESSERACT_CMD")
    p.add_argument("--db-port", type=int, metavar="PORT", help="PostgreSQL port (default: 5433 docker, 5432 otherwise)")
    p.add_argument("--runtime-home", metavar="DIR", help="runtime folder (default ~/.ironpulse)")
    p.add_argument("--env-file", default=str(ROOT / ".env"), metavar="PATH", help="the .env file to edit")

    p = add("env", cmd_env, "read or write .env values (get KEY | ensure KEY... | set KEY=VALUE...)",
            "get KEY...        print the values (empty line when missing)\n"
            "ensure KEY...     generate a random secret for each key that is missing or empty\n"
            "set KEY=VALUE...  write values (inline comments are kept)")
    p.add_argument("action", choices=("get", "ensure", "set"))
    p.add_argument("items", nargs="+", metavar="KEY")
    p.add_argument("--effective", action="store_true", help="get: prefer a real environment variable over .env")
    p.add_argument("--env-file", default=str(ROOT / ".env"), metavar="PATH")

    p = add("ai-status", cmd_ai_status, "every AI provider of the chain: up, model pulled, a 1-token latency probe",
            "Gemma gateway (+ its status page), Gemma in Docker (Ollama: running? model pulled?), Claude (key set) and\n"
            "the failover chain. The Gemma servers also get a 1-token answer to measure their latency (--no-probe skips\n"
            "it; Claude is never asked here - use ai-test --provider anthropic).")
    p.add_argument("--timeout", type=float, default=4.0, metavar="SECONDS", help="per check (default 4)")
    p.add_argument("--no-probe", action="store_true", help="skip the 1-token latency probe")
    p.add_argument("--probe-timeout", type=float, default=60.0, metavar="SECONDS",
                   help="the latency probe's limit (default 60: a cold Ollama loads the model first)")

    p = add("ai-test", cmd_ai_test, "one tiny real answer from each AI provider (or --provider NAME)",
            "Sends 'Say OK.' (max 1 token) straight to each provider of AI_PROVIDER_ORDER through the app's provider\n"
            "code and prints the first-token latency. --provider anthropic spends a few tokens of your Claude budget.\n"
            "  python scripts/manage.py ai-test --provider docker")
    p.add_argument("--provider", choices=("all", *AI_PROVIDERS), default="all")
    p.add_argument("--timeout", type=float, default=120.0, metavar="SECONDS")

    p = add("ai-fallback", cmd_ai_fallback, "start the local AI fallback (Gemma in Ollama) in the background",
            "What run.sh / run.ps1 do before starting the app: when AI_PROVIDER_ORDER has 'docker' and AI_DOCKER_BASE_URL\n"
            "is this computer, start Ollama (Docker: docker compose up -d ollama ollama-pull; or a native ollama) in the\n"
            "background and print one line. Never blocks; log: var/logs/ai-fallback.log.")
    p.add_argument("--dry-run", action="store_true", help="print the decision and commands; start nothing")
    p.add_argument("--json", action="store_true", help="print the decision as JSON")
    p.add_argument("--no-ai-docker", action="store_true", help="skip it")

    p = add("ai-chat", cmd_ai_chat, "chat with Gemma (OpenAI-compatible gateway, e.g. http://192.168.0.101:8000/v1)",
            "Chat with Gemma from the terminal through the OpenAI SDK. Uses AI_GATEWAY_BASE_URL / AI_GATEWAY_API_KEY /\n"
            "AI_GATEWAY_MODEL from .env (or --provider docker for the Ollama failsafe). Answers stream as they arrive.\n"
            "  python scripts/manage.py ai-chat                        interactive (/reset, /exit)\n"
            "  python scripts/manage.py ai-chat \"Plan a 20 minute warm-up\"\n"
            "  python scripts/manage.py ai-chat --base-url http://192.168.0.101:8000/v1 \"Hello\"")
    p.add_argument("prompt", nargs="*", help="one-shot question (omit for an interactive chat)")
    p.add_argument("--provider", choices=("gateway", "docker"), default="gateway")
    p.add_argument("--base-url", metavar="URL", help="override the OpenAI-compatible base URL")
    p.add_argument("--model", help="override the model name")
    p.add_argument("--system", help="system prompt")
    p.add_argument("--max-tokens", type=int, default=600)
    p.add_argument("--temperature", type=float, default=0.6)
    p.add_argument("--timeout", type=float, default=60.0, metavar="SECONDS")
    p.add_argument("--no-stream", action="store_true", help="wait for the whole answer instead of streaming")
    return ap


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    args, extra = parser.parse_known_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0 if not argv else 2
    if extra and args.command not in PASSTHROUGH:
        parser.error(f"unrecognized arguments for '{args.command}': {shlex.join(extra)}")
    try:
        return int(args.handler(args, extra) or 0)
    except CommandError as exc:
        print(f"[manage] error: {exc}", file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
