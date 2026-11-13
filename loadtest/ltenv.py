"""Shared configuration of the IronPulse load-test kit - ONE place for names, ports, paths and the server environment.

Standard library only: imported by the seeder (project .venv), the Locust files and the monitor (.venv-loadtest) and
run by the launcher (``python loadtest/ltenv.py server-env``). Everything here points at load-test resources only:

* database ``ironpulse_loadtest`` (never the app's main database), created/dropped by loadtest/seed_loadtest.py
* Redis logical database 9 (never db 0 of the dev servers)            * server on 127.0.0.1:12000 (dev: 10000 / 11000)
* uploads + spooled e-mail under loadtest/var (never the project's var/)
* its own SECRET_KEY / ID_HASH_PEPPER / DATA_ENCRYPTION_KEY in loadtest/.secrets.env (generated once, 0600, gitignored;
  ENVIRONMENT=production refuses placeholder secrets, and the developer's .env is never used for them)

    python loadtest/ltenv.py server-env [--ai simulated|off|gateway] [--smtp spool|sink]   # `export` lines
    python loadtest/ltenv.py info                              # names, ports, paths (no secrets)
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import shlex
import sys
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent  # the project folder (its name ends with a space - always quote it)
LT_DIR = ROOT / "loadtest"
RESULTS_DIR = LT_DIR / "results"
VAR_DIR = LT_DIR / "var"  # uploads (+ spooled e-mail in var/mail) of the load-test server only
ACCOUNTS_FILE = LT_DIR / "accounts.json"  # written by the seeder - contains the shared test password (gitignored)
CATALOGUE_FILE = LT_DIR / "catalogue.json"  # ids / slugs that exist in ironpulse_loadtest (written by the seeder)
CURRENT_RUN_FILE = LT_DIR / ".current_run"  # name of the run directory the launcher started last
SERVER_PID_FILE = LT_DIR / ".server.pid"
MONITOR_PID_FILE = LT_DIR / ".monitor.pid"

DB_NAME = "ironpulse_loadtest"  # hard-wired: the seeder refuses to drop anything else
HOST = "127.0.0.1"
PORT = 12000
BASE_URL = f"http://{HOST}:{PORT}"
REDIS_DB = 9
WORKERS = 4

# Connection budget. PostgreSQL here has max_connections=100 (3 reserved) shared with the dev servers and the other
# teams' tests. The app's own default pool (app/config.py: 10 + 5 per worker, sized for 4 workers) is used as is:
# 4 x (10 + 5 + 1 LISTEN) = 64 connections; the database is capped at CONNECTION LIMIT 70 (64 + headroom for psql /
# the launcher's checks) so a run can never take more than that from the shared server.
DB_POOL_SIZE = int(os.environ.get("LT_DB_POOL_SIZE", "10"))
DB_MAX_OVERFLOW = int(os.environ.get("LT_DB_MAX_OVERFLOW", "5"))
DB_CONN_LIMIT = int(os.environ.get("LT_DB_CONN_LIMIT", "70"))

RUNTIME_HOME = Path(os.environ.get("IRONPULSE_HOME") or (Path.home() / ".ironpulse"))
PSQL = RUNTIME_HOME / "env" / "bin" / "psql"
REDIS_CLI = RUNTIME_HOME / "env" / "bin" / "redis-cli"
PROJECT_PYTHON = ROOT / ".venv" / "bin" / "python"
LOADTEST_PYTHON = ROOT / ".venv-loadtest" / "bin" / "python"

# Every lt_* account shares this password (test data in a throw-away database; also written to accounts.json).
PASSWORD = os.environ.get("LT_PASSWORD", "Lt-IronPulse#2026-load")
EMAIL_DOMAIN = "example.com"  # RFC 2606 reserved: nothing is ever deliverable
DEMO_EMAIL_DOMAIN = "demo.ironpulse.test"  # the demo dataset's accounts, re-homed onto the reserved .test TLD
SUPERADMIN_EMAIL = f"lt_superadmin@{EMAIL_DOMAIN}"  # the server's SUPERADMIN_EMAIL (bootstrap) - no simulated user
# One session per account (the app's SINGLE_SESSION_PER_ACCOUNT, default on): a sign-in ends the account's sessions
# everywhere else, so every simulated user needs an account of its own - the super-admins included.
N_SUPERADMINS = 10  # lt_superadmin_01 .. lt_superadmin_10 (LT_SUPERADMINS may not exceed this)
N_JUNK_ACCOUNTS = 500  # lt_junk_001 .. lt_junk_500: the signed-in half of the JunkUser class


def superadmin_email(index: int) -> str:
    return f"lt_superadmin_{index:02d}@{EMAIL_DOMAIN}"


# Client addresses (RFC 2544 benchmarking range 198.18.0.0/15). Signed-in accounts get a FIXED address each (people
# sign in from their usual address - no "new sign-in" notice storm); anonymous visitors, junk senders and abusers
# draw random ones from blocks of their own, so a ban of an abuser or junk sender can never hit a normal user.
ACCOUNT_NET = ipaddress.ip_network("198.18.0.0/16")
VISITOR_NET = ipaddress.ip_network("198.19.0.0/17")
JUNK_NET = ipaddress.ip_network("198.19.128.0/18")  # every JunkUser address (198.19.128.0 - 198.19.191.255):
JUNK_ANON_NET = ipaddress.ip_network("198.19.128.0/19")  # ... anonymous junk browsers
JUNK_ROTATE_NET = ipaddress.ip_network("198.19.160.0/19")  # ... a signed-in junk sender after its address was banned
ABUSER_NET = ipaddress.ip_network("198.19.240.0/20")
ACCOUNT_IP_OFFSET = {"member": 0, "owner": 30_000, "staff": 40_000, "superadmin": 50_000, "abuser": 55_000,
                     "junk": 60_000}


def account_ip(kind: str, index: int) -> str:
    """Fixed client address of account ``index`` (1-based) of ``kind``: 254 hosts per /24, never .0 / .255."""
    n = ACCOUNT_IP_OFFSET[kind] + index
    third, fourth = divmod(n, 254)
    return str(ACCOUNT_NET.network_address + third * 256 + fourth + 1)


def junk_ip(ip: str) -> bool:
    """Is ``ip`` one of the JunkUser class's addresses (its anonymous block, or a lt_junk_* account's fixed one)?
    monitor.py / analyze.py use it to keep the junk senders' server-side 5xx apart from the normal users'."""
    try:
        addr = ipaddress.ip_address(ip.strip())
    except ValueError:
        return False
    if addr in JUNK_NET:
        return True
    if addr not in ACCOUNT_NET:
        return False
    third, last = divmod(int(addr) - int(ACCOUNT_NET.network_address), 256)  # account_ip() backwards
    n = third * 254 + last - 1
    lo = ACCOUNT_IP_OFFSET["junk"]
    return 1 <= last <= 254 and lo < n <= lo + N_JUNK_ACCOUNTS


def random_ip(net: ipaddress.IPv4Network, rng) -> str:  # noqa: ANN001 - random.Random
    while True:
        addr = net.network_address + rng.randrange(1, net.num_addresses - 1)
        if str(addr).rsplit(".", 1)[1] not in ("0", "255"):
            return str(addr)


# ------------------------------------------------------------------------------------------------- .env + secrets
def read_dotenv(path: Path | None = None) -> dict[str, str]:
    """KEY=VALUE pairs of the project's .env (inline ' #' comments stripped). Read only - never written."""
    out: dict[str, str] = {}
    path = path or (ROOT / ".env")
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$", line)
        if not m:
            continue
        value = re.split(r"\s+#", m.group(2), maxsplit=1)[0].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[m.group(1).upper()] = value
    return out


def setting(name: str, default: str = "") -> str:
    """A setting as the app sees it: a real environment variable wins over .env."""
    return os.environ.get(name, read_dotenv().get(name, default))


def _secret_file(name: str) -> str:
    try:
        return (RUNTIME_HOME / name).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def superuser_password() -> str:
    pw = os.environ.get("LT_PG_SUPERUSER_PASSWORD") or _secret_file("pg_superuser_password")
    if not pw:
        raise SystemExit(f"no PostgreSQL superuser password: {RUNTIME_HOME / 'pg_superuser_password'} is missing")
    return pw


def redis_password() -> str:
    pw = _secret_file("redis_password")
    if pw:
        return pw
    url = read_dotenv().get("REDIS_URL", "")
    return unquote(urlsplit(url).password or "") if url else ""


# The load-test server's OWN secrets (generated once, kept in loadtest/.secrets.env: mode 0600, gitignored, never .env).
# ENVIRONMENT=production refuses to start on a placeholder / short / non-random SECRET_KEY or ID_HASH_PEPPER and on a
# DATA_ENCRYPTION_KEY that is not a Fernet key (app/config.py _production_problems), and the developer's .env may hold
# development values. The seeder and the server read the same file, so data the seeder encrypted (Fernet) or hashed
# (HMAC with the pepper) stays readable by the server; delete the file only together with a re-seed.
SECRETS_FILE = LT_DIR / ".secrets.env"
SECRET_NAMES = ("SECRET_KEY", "ID_HASH_PEPPER", "DATA_ENCRYPTION_KEY")
_PLACEHOLDER_MARKERS = ("change", "replace", "placeholder", "example", "dummy", "insecure", "secret-key", "your-secret",
                        "xxxx")


def _random_token(nbytes: int) -> str:
    import secrets  # noqa: PLC0415

    while True:
        value = secrets.token_urlsafe(nbytes)
        if len(set(value)) >= 16 and not any(m in value.lower() for m in _PLACEHOLDER_MARKERS):
            return value


def _fernet_key() -> str:
    import base64  # noqa: PLC0415

    while True:
        raw = os.urandom(32)
        if len(set(raw)) >= 24:  # the app wants 32 random bytes (>= 16 distinct)
            return base64.urlsafe_b64encode(raw).decode("ascii")


def _secret_ok(name: str, value: str) -> bool:
    if name == "DATA_ENCRYPTION_KEY":
        import base64  # noqa: PLC0415

        try:
            return len(base64.urlsafe_b64decode(value.encode("ascii"))) == 32
        except (ValueError, UnicodeEncodeError):
            return False
    return len(value) >= (32 if name == "SECRET_KEY" else 16) and len(set(value)) >= 16 and \
        not any(m in value.lower() for m in _PLACEHOLDER_MARKERS)


def loadtest_secrets() -> dict[str, str]:
    """SECRET_KEY / ID_HASH_PEPPER / DATA_ENCRYPTION_KEY of the load-test server (created on first use)."""
    found = read_dotenv(SECRETS_FILE)
    out = {k: found[k] for k in SECRET_NAMES if _secret_ok(k, found.get(k, ""))}
    if len(out) == len(SECRET_NAMES):
        return out
    out.setdefault("SECRET_KEY", _random_token(48))
    out.setdefault("ID_HASH_PEPPER", _random_token(32))
    out.setdefault("DATA_ENCRYPTION_KEY", _fernet_key())
    text = ("# IronPulse load-test server secrets - generated by loadtest/ltenv.py, used by the seeder and the server.\n"
            "# Never commit; never copy into .env. Deleting it requires a re-seed (encrypted / hashed seed data).\n"
            + "".join(f"{k}={out[k]}\n" for k in SECRET_NAMES))
    tmp = SECRETS_FILE.with_name(SECRETS_FILE.name + f".{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, SECRETS_FILE)
    return out


def _main_db_url() -> str:
    url = read_dotenv().get("DATABASE_URL") or ""
    if not url:
        raise SystemExit("DATABASE_URL is missing in .env - the load test reuses its role/password/host")
    return url


def db_parts() -> dict[str, str | int]:
    """Role, password, host and port of the app's DATABASE_URL (.env) - only its database name is replaced."""
    u = urlsplit(_main_db_url().replace("postgresql+asyncpg://", "postgresql://", 1))
    return {"user": unquote(u.username or "ironpulse"), "password": unquote(u.password or ""),
            "host": u.hostname or "localhost", "port": u.port or 5432}


def loadtest_db_url(driver: str = "postgresql+asyncpg") -> str:
    p = db_parts()
    auth = f"{quote(str(p['user']), safe='')}:{quote(str(p['password']), safe='')}"
    return f"{driver}://{auth}@{p['host']}:{p['port']}/{DB_NAME}"


def superuser_dsn(database: str = "postgres") -> str:
    p = db_parts()
    return f"postgresql://postgres:{quote(superuser_password(), safe='')}@{p['host']}:{p['port']}/{database}"


def redis_url() -> str:
    return f"redis://:{quote(redis_password(), safe='')}@127.0.0.1:6379/{REDIS_DB}"


def assert_loadtest_target(url: str) -> None:
    """Hard stop when a URL does not point at the load-test database."""
    name = urlsplit(url.replace("+asyncpg", "")).path.lstrip("/")
    if name != DB_NAME:
        raise SystemExit(f"refusing to touch database {name!r} - the load-test kit only uses {DB_NAME!r}")


# ------------------------------------------------------------------------------------------------- server environment
# AI modes of the load-test server (README "AI"):
#   simulated (default) - the app's own load-test provider (app/ai/simulated.py: AI_PROVIDER_ORDER=simulated,
#                         AI_SIMULATED_ALLOWED=true): in-process, no network, no model, realistic latency - the AI guard,
#                         the fair queue, provider slots and the circuit breaker run exactly as with a real provider
#   off                 - no provider at all: the app answers with its deterministic fallbacks before the guard runs
#   gateway             - the LAN Gemma gateway from .env (a shared machine: only with its owner's OK)
AI_MODES = ("simulated", "off", "gateway")
DEFAULT_AI = os.environ.get("LT_AI", "simulated")
SMTP_MODES = ("spool", "sink")  # spool: mails written to loadtest/var/mail; sink: loadtest/smtp_sink.py on :2525
SMTP_SINK_PORT = 2525

# Load-relevant settings pinned to the app's OWN defaults (app/config.py), never to whatever the developer's .env says
# (it sets e.g. RATE_LIMIT_AI_PER_MINUTE=20 where the app default is 60). ``app_defaults()`` reads the current defaults
# from app.config.Settings when the project interpreter runs this; the values below are the fallback (the defaults on
# 2026-11-10). LT_<NAME>=value overrides one for an experiment (recorded in server_config.json).
PINNED_DEFAULTS: dict[str, str] = {
    "SESSION_TTL_HOURS": "168", "DB_POOL_SIZE": "10", "DB_MAX_OVERFLOW": "5", "DB_STATEMENT_TIMEOUT_MS": "15000",
    "DB_IDLE_TX_TIMEOUT_MS": "60000", "DB_POOL_TIMEOUT_S": "10.0", "DB_LONG_STATEMENT_TIMEOUT_MS": "300000",
    "DB_BACKGROUND_IDLE_TX_TIMEOUT_MS": "600000", "DB_ECHO": "false",
    "RATE_LIMIT_PER_MINUTE": "240", "RATE_LIMIT_AUTH_PER_MINUTE": "10", "RATE_LIMIT_AI_PER_MINUTE": "60",
    "RATE_LIMIT_LIVE_PER_MINUTE": "120", "RATE_LIMIT_UPLOAD_PER_MINUTE": "30", "RATE_LIMIT_STATIC_PER_MINUTE": "1500",
    "RATE_LIMIT_REDIS_TIMEOUT_MS": "100", "MAX_UPLOAD_MB": "8", "THREAT_BAN_SCORE": "12.0", "LOGIN_MAX_FAILURES": "5",
    "LOGIN_LOCKOUT_MINUTES": "15",
    "AI_GUARD_ENABLED": "true", "AI_QUOTA_SCALE": "1.0", "AI_QUEUE_CONCURRENCY": "4", "AI_TTS_CONCURRENCY": "2",
    "AI_QUEUE_MAX_PER_USER": "4", "AI_QUEUE_MAX_TOTAL": "200", "PRESENCE_GRACE_SECONDS": "45", "JOBS_CONCURRENCY": "2",
    "AI_CIRCUIT_FAILURES": "2", "AI_CIRCUIT_COOLDOWN_SECONDS": "45", "AI_HEALTH_INTERVAL_SECONDS": "60",
    "AI_GATEWAY_MAX_CONCURRENCY": "4", "AI_GATEWAY_TIMEOUT": "90.0", "AI_JSON_MODE": "true",
    "AI_SIM_FIRST_TOKEN_MS": "400..1500", "AI_SIM_TOKENS_PER_S": "25..60", "AI_SIM_FAILURE_RATE": "0.0",
    "AI_SIM_MAX_CONCURRENCY": "4", "AI_SIM_TTS": "true", "AI_SIM_TTS_MS": "300..1200",
    "RETENTION_HOUR_UTC": "21", "RETENTION_MAX_SECONDS": "600", "RETENTION_BATCH": "1000",
}


def _fmt_default(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, tuple) and len(value) == 2:  # AI_SIM_* ranges
        lo, hi = (int(v) if float(v).is_integer() else v for v in value)
        return f"{lo}..{hi}"
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value)
    return "" if value is None else str(value)


def app_defaults() -> tuple[dict[str, str], str]:
    """(pinned settings, source): the app's current defaults when app.config imports (project interpreter), else the
    fallback table above. Only class-level field defaults are read - nothing is instantiated, .env is not read."""
    out = dict(PINNED_DEFAULTS)
    source = "fallback table in loadtest/ltenv.py (app.config not importable here)"
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from app.config import Settings  # noqa: PLC0415 - optional: only the project interpreter has pydantic

        fields = Settings.model_fields
        for name in list(out):
            f = fields.get(name.lower())
            if f is not None and f.default is not None and repr(f.default) != "PydanticUndefined":
                out[name] = _fmt_default(f.default)
        source = "app/config.py defaults (read at launch)"
    except Exception:  # noqa: BLE001 - any import problem: keep the fallback table
        pass
    for name in list(out):
        override = os.environ.get(f"LT_{name}")
        if override is not None:
            out[name] = override
    if os.environ.get("LT_DB_POOL_SIZE"):
        out["DB_POOL_SIZE"] = str(DB_POOL_SIZE)
    if os.environ.get("LT_DB_MAX_OVERFLOW"):
        out["DB_MAX_OVERFLOW"] = str(DB_MAX_OVERFLOW)
    return out, source


def server_env(ai: str = DEFAULT_AI, smtp: str = "spool") -> dict[str, str]:
    """Environment overrides of the load-test server. Real environment variables beat .env (pydantic-settings), so
    .env stays untouched; everything that could reach the outside world is switched off explicitly."""
    if ai not in AI_MODES:
        raise SystemExit(f"--ai must be one of {AI_MODES}")
    if smtp not in SMTP_MODES:
        raise SystemExit(f"--smtp must be one of {SMTP_MODES}")
    pinned, _source = app_defaults()
    env = dict(pinned)
    env.update({
        # ---- production mode behind a TLS-terminating proxy (the Locust client plays that proxy). ENVIRONMENT=
        # production turns COOKIE_SECURE / FORCE_HTTPS / TRUST_PROXY_HEADERS on by itself now; they are still set
        # explicitly so the run does not depend on that default (and the .env's development values never apply)
        "ENVIRONMENT": "production",
        "DEBUG": "false",
        "RELOAD": "false",
        "HOST": HOST,
        "PORT": str(PORT),
        "PUBLIC_BASE_URL": f"https://{HOST}:{PORT}",
        "ALLOWED_HOSTS": f"{HOST},localhost",
        "CORS_ORIGINS": "",
        "COOKIE_SECURE": "true",  # __Host- cookies, Secure flag
        "FORCE_HTTPS": "true",  # plain http without X-Forwarded-Proto: https -> 308; HSTS on
        "TRUST_PROXY_HEADERS": "true",
        "TRUSTED_PROXIES": "127.0.0.1",
        "RATE_LIMIT_WHITELIST": "",  # nobody is exempt - per-IP limits, bans and the AI guard work as in production
        # ---- own database / Redis db / files
        "DATABASE_URL": loadtest_db_url(),
        "DATABASE_ADMIN_URL": "",
        "TEST_DATABASE_URL": "",
        "DB_POOL_SIZE": str(DB_POOL_SIZE) if os.environ.get("LT_DB_POOL_SIZE") else pinned["DB_POOL_SIZE"],
        "DB_MAX_OVERFLOW": str(DB_MAX_OVERFLOW) if os.environ.get("LT_DB_MAX_OVERFLOW") else pinned["DB_MAX_OVERFLOW"],
        "REDIS_URL": redis_url(),
        "REALTIME_PG_BRIDGE": "true",  # several workers: live events fan out through LISTEN/NOTIFY
        "RUN_BACKGROUND_JOBS": "true",
        "SEED_DEMO_DATA": "false",
        "AUTO_CREATE_TABLES": "true",
        "AUTO_MIGRATE": "true",
        "VAR_DIR": str(VAR_DIR),
        "LOADTEST_RESULTS_DIR": str(RESULTS_DIR),
        "SUPERADMIN_EMAIL": SUPERADMIN_EMAIL,
        "SUPERADMIN_PASSWORD": PASSWORD,
        **loadtest_secrets(),  # own SECRET_KEY / ID_HASH_PEPPER / DATA_ENCRYPTION_KEY (never the developer's .env)
        # ---- no outbound side effects
        "SMTP_HOST": "",  # e-mail is spooled to loadtest/var/mail, never sent (see smtp="sink" below)
        "SMTP_USERNAME": "",
        "SMTP_PASSWORD": "",
        "GEOAPIFY_API_KEY": "",  # no geocoding / tile calls ...
        "MAP_OSM_FALLBACK": "false",  # ... and no OpenStreetMap fallback either (tiles answer a local transparent PNG)
        "CASHFREE_APP_ID": "",  # payments stay in TEST MODE
        "CASHFREE_SECRET_KEY": "",
        "CASHFREE_PAYOUT_CLIENT_ID": "",
        "CASHFREE_PAYOUT_CLIENT_SECRET": "",
        "ANTHROPIC_API_KEY": "",  # never paid AI
        "AI_DOCKER_BASE_URL": "",  # the local Ollama failsafe is not running on this machine
        "TTS_BASE_URL": "",  # Kokoro is not running; the browser voice is the app's fallback
        "AI_SIMULATED_ALLOWED": "false",
    })
    if ai == "simulated":
        env.update({"AI_PROVIDER_ORDER": "simulated", "AI_SIMULATED_ALLOWED": "true",
                    "AI_GATEWAY_BASE_URL": "", "AI_GATEWAY_STATUS_URL": ""})
    elif ai == "off":
        env.update({"AI_PROVIDER_ORDER": "gateway,docker,anthropic",  # every one unconfigured below -> AI unavailable
                    "AI_GATEWAY_BASE_URL": "", "AI_GATEWAY_STATUS_URL": ""})
    else:  # gateway: the URL / key / model from .env, never anthropic or docker
        env.update({"AI_PROVIDER_ORDER": "gateway"})
    if smtp == "sink":  # the real SMTP delivery path into a local sink that discards every mail (loadtest/smtp_sink.py)
        env.update({"SMTP_HOST": HOST, "SMTP_PORT": str(SMTP_SINK_PORT), "SMTP_STARTTLS": "false", "SMTP_SSL": "false",
                    "SMTP_TIMEOUT": "10"})
    return env


SECRET_NAME = re.compile(r"key|secret|password|pepper|token|url", re.I)


def info() -> dict[str, object]:
    return {
        "database": DB_NAME, "server": BASE_URL, "workers": WORKERS, "redis_db": REDIS_DB, "var_dir": str(VAR_DIR),
        "db_pool": f"{DB_POOL_SIZE}+{DB_MAX_OVERFLOW} per worker", "db_connection_limit": DB_CONN_LIMIT,
        "accounts": str(ACCOUNTS_FILE), "catalogue": str(CATALOGUE_FILE), "results": str(RESULTS_DIR),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    se = sub.add_parser("server-env", help="print `export KEY=value` lines for the load-test server")
    se.add_argument("--ai", choices=AI_MODES, default=DEFAULT_AI)
    se.add_argument("--smtp", choices=SMTP_MODES, default="spool")
    sub.add_parser("info", help="print names, ports and paths")
    args = ap.parse_args(argv)
    if args.cmd == "server-env":
        for key, value in server_env(args.ai, args.smtp).items():
            print(f"export {key}={shlex.quote(value)}")
    else:
        print(json.dumps(info(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
