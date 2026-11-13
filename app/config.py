"""Typed application settings, loaded from environment variables and the project's .env file.

All secrets live in .env (never in code). Access settings through ``get_settings()``.
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import logging
import math
import os
import shutil
import sys
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, PrivateAttr, ValidationError, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict, SettingsError

BASE_DIR = Path(__file__).resolve().parent.parent
APP_DIR = BASE_DIR / "app"

CommaList = Annotated[list[str], NoDecode]
TEST_DB_IDLE_TX_TIMEOUT_MS = 3_600_000  # db_idle_tx_timeout_ms under ENVIRONMENT=test unless set explicitly


def _split_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return []
        if value.startswith("["):
            return [str(v).strip() for v in json.loads(value) if str(v).strip()]
        return [part.strip() for part in value.split(",") if part.strip()]
    return [str(v).strip() for v in value if str(v).strip()]


# ---------------------------------------------------------------------------------- production mode + simulated AI
log = logging.getLogger("ironpulse.config")

SIMULATED_AI = "simulated"  # AI_PROVIDER_ORDER name of the load-test provider (app/ai/simulated.py) - never a default
# ENVIRONMENT=production turns these on unless the environment sets them explicitly (an explicit false is honoured and
# logged at every start: Settings.production_warnings). README "Deploying to production".
PRODUCTION_SECURE_DEFAULTS = ("cookie_secure", "force_https", "trust_proxy_headers")
SECRET_PLACEHOLDERS = frozenset({"change-me-run-setup-to-generate-a-64-char-secret"})  # .env.example
SECRET_PLACEHOLDER_MARKERS = ("change-me", "changeme", "change_me", "replace-me", "replace_me", "placeholder",
                              "example", "dummy", "insecure", "secret-key", "your-secret", "xxxxxxxx")
MIN_SECRET_DISTINCT = 6  # fewer different characters is a typed pattern ("aaaa...", "abcabc..."), not a random value
MIN_ADMIN_PASSWORD = 10  # what app.security.passwords asks of every account password
WEAK_ADMIN_PASSWORDS = frozenset({"admin", "admin123", "admin@123", "password", "changeme", "ironpulse", "superadmin",
                                  "ironpulse123!"})
SimRange = Annotated[tuple[float, float], NoDecode]  # AI_SIM_*: "400..1500" (uniform) or one number


def _parse_range(value: Any, *, positive: bool = False) -> tuple[float, float]:
    """``"400..1500"``, ``"800"`` or ``(400, 1500)`` -> (low, high) for the AI_SIM_* latency settings."""
    usage = "use a number or a range like 400..1500"
    if isinstance(value, str):
        text = value.strip()
        parts: list[Any] = [p.strip() for p in text.split("..")] if text else []
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        parts = [value]
    elif isinstance(value, (list, tuple)):
        parts = list(value)
    else:
        raise ValueError(usage)
    try:
        numbers = [float(p) for p in parts]
    except (TypeError, ValueError):
        raise ValueError(usage) from None
    if len(numbers) not in (1, 2):
        raise ValueError(usage)
    low, high = numbers[0], numbers[-1]
    if not all(math.isfinite(n) and n >= 0 for n in numbers):
        raise ValueError("must be a number of 0 or more")
    if low > high:
        raise ValueError("the range runs backwards - write the smaller number first, e.g. 400..1500")
    if positive and low <= 0:
        raise ValueError("must be above 0")
    return low, high


def is_loopback_host(host: str | None) -> bool:
    """localhost, 127.x.x.x or ::1: a bind address or Host name only this machine reaches."""
    name = (host or "").strip().strip("[]").lower()
    if name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def _weak_secret(value: str | None, min_length: int) -> str | None:
    """Why a secret is unfit for production (None: fine)."""
    text = (value or "").strip()
    if not text:
        return "is empty"
    if len(text) < min_length:
        return f"is too short ({len(text)} characters, at least {min_length})"
    if text in SECRET_PLACEHOLDERS or any(marker in text.lower() for marker in SECRET_PLACEHOLDER_MARKERS):
        return "is a placeholder"
    if len(set(text)) < MIN_SECRET_DISTINCT:
        return f"is not random (only {len(set(text))} different characters)"
    return None


def _bad_fernet_key(value: str | None) -> str | None:
    try:
        raw = base64.urlsafe_b64decode((value or "").strip().encode())
    except (binascii.Error, ValueError):
        return "is not a valid Fernet key"
    if len(raw) != 32:
        return "is not a valid Fernet key (32 url-safe base64-encoded bytes)"
    if len(set(raw)) < 16:
        return "is not random"
    return None


def _weak_admin_password(value: str | None) -> str | None:
    if not value:
        return None  # nothing to bootstrap: no super-admin is created from .env
    if len(value) < MIN_ADMIN_PASSWORD:
        return f"is too short ({len(value)} characters, at least {MIN_ADMIN_PASSWORD})"
    try:
        from app.security.passwords import COMMON_PASSWORDS as common  # lazy: only production settings get here
    except Exception:  # noqa: BLE001 - the list is a bonus; the other rules still apply
        common = set()
    if value.lower() in WEAK_ADMIN_PASSWORDS or value.lower() in common:
        return "is a well-known password"
    if len(set(value)) < MIN_SECRET_DISTINCT:
        return "is not random enough"
    return None


_MAPPED_V4 = ipaddress.ip_network("::ffff:0:0/96")  # a dual-stack listener reports IPv4 clients as ::ffff:a.b.c.d


_LOCAL_V6 = (ipaddress.ip_network("fc00::/7"), ipaddress.ip_network("fe80::/10"))  # ULA, link-local: never routed


def _catch_all_network(entry: str) -> bool:
    """A TRUSTED_PROXIES entry no reverse-proxy setup needs, from which a large part of the internet could pick its
    own client address: '*' (uvicorn's "trust everyone" - ``python -m app`` hands TRUSTED_PROXIES to its
    forwarded_allow_ips), IPv4 wider than a /8 (0.0.0.0/0, 0.0.0.0/1 ...), IPv6 wider than a /16 (::/0, 2000::/3 ...),
    or IPv4-mapped IPv6 wider than a mapped /8 (::ffff:0:0/96 is every IPv4 client of a dual-stack listener). Private
    IPv6 ranges (ULA fc00::/7, link-local fe80::/10 - Docker IPv6 networks) can't be reached from the internet: fine."""
    text = entry.strip()
    if text == "*":
        return True
    try:
        net = ipaddress.ip_network(text, strict=False)
    except ValueError:
        return False  # not an address: app.security.net ignores it (``_production_problems`` refuses it on its own)
    if net.version == 4:
        return net.prefixlen < 8
    if any(net.subnet_of(local) for local in _LOCAL_V6):
        return False
    return net.prefixlen < 16 or (net.overlaps(_MAPPED_V4) and net.prefixlen < _MAPPED_V4.prefixlen + 8)


def _is_network(entry: str) -> bool:
    try:
        ipaddress.ip_network(entry.strip(), strict=False)
    except ValueError:
        return False
    return True


def uvicorn_forwarded_allow_ips(s: "Settings") -> str:
    """TRUSTED_PROXIES as uvicorn's forwarded_allow_ips: only entries that are addresses or networks. uvicorn reads '*'
    (or a name it can't parse) as "trust every client" while the app ignores such entries - they are never passed on;
    without any, 127.0.0.1 (never uvicorn's own $FORWARDED_ALLOW_IPS fallback, which images often set to '*')."""
    nets = [p.strip() for p in s.trusted_proxies if p.strip() != "*" and _is_network(p)]
    return ",".join(dict.fromkeys(nets)) or "127.0.0.1"


def _host_allowed(host: str, patterns: list[str]) -> bool:
    """Would the Host check (Starlette's TrustedHostMiddleware in app.main) answer a request for ``host``: an exact
    name, a '*.domain' wildcard, '*', or the 'www.' name it redirects to."""
    host = host.strip().lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"  # an IP literal keeps its brackets in a Host header
    for pattern in (p.strip().lower() for p in patterns):
        if pattern in ("*", host, f"www.{host}") or (pattern.startswith("*.") and host.endswith(pattern[1:])):
            return True
    return False


def _unusable_host(pattern: str) -> bool:
    """An ALLOWED_HOSTS entry no request can ever match: the Host header is compared without scheme, path or port
    (``https://gym.example.com``, ``gym.example.com:443``; an IPv6 literal has several colons and is left alone)."""
    text = pattern.strip()
    return "/" in text or text.count(":") == 1


# Host-name suffixes under which ANYONE can get a name and point it at any address: an ALLOWED_HOSTS wildcard for one of
# them lets an attacker's own domain pass the Host check (DNS rebinding). No Public Suffix List is bundled, so: every
# top-level domain (one label) except the special-use names, the usual country second levels (co.in, com.au, org.uk
# ...) and well-known dynamic-DNS / any-IP DNS services.
SPECIAL_USE_SUFFIXES = frozenset({"localhost", "local", "internal", "test", "example", "invalid", "home.arpa"})
COUNTRY_SECOND_LEVELS = frozenset({"ac", "biz", "co", "com", "edu", "firm", "gen", "go", "gob", "gov", "govt", "ind",
                                   "info", "int", "ltd", "me", "mil", "ne", "net", "nic", "nom", "or", "org", "plc",
                                   "res", "sch", "web"})
OPEN_DNS_SUFFIXES = frozenset({"nip.io", "sslip.io", "xip.io", "duckdns.org", "ddns.net", "dynu.net", "freeddns.org",
                               "hopto.org", "zapto.org", "sytes.net", "no-ip.org", "no-ip.com", "no-ip.biz", "mooo.com"})


def public_suffix_wildcard(pattern: str) -> str | None:
    """The public suffix a '*.<suffix>' ALLOWED_HOSTS entry opens to everybody (``*.com`` -> 'com', ``*.co.in`` ->
    'co.in'), or None for a wildcard of one's own domain (``*.gym.example.com``) or any other entry."""
    text = pattern.strip().lower().rstrip(".")
    if not text.startswith("*."):
        return None
    suffix = text[2:]
    labels = suffix.split(".")
    if suffix in SPECIAL_USE_SUFFIXES:
        return None
    if len(labels) < 2 or suffix in OPEN_DNS_SUFFIXES:
        return suffix
    if len(labels) == 2 and labels[0] in COUNTRY_SECOND_LEVELS and len(labels[1]) == 2 and labels[1].isalpha():
        return suffix
    return None


def plain_http_origins(origins: list[str]) -> list[str]:
    """CORS origins served over plain HTTP (loopback excepted): anyone on such a page's network path can rewrite it."""
    out = []
    for origin in origins:
        parts = urlsplit(origin.strip())
        if parts.scheme.lower() == "http" and not is_loopback_host(parts.hostname or ""):
            out.append(origin.strip())
    return out


class ProductionConfigError(ValueError):
    """What Settings validation raises under ENVIRONMENT=production: ``problems`` holds one line per problem, each
    naming the variable to set (``describe_errors`` passes them on unchanged)."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = list(problems)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        hide_input_in_errors=True,  # a validation error never prints the values (secrets, database passwords)
    )

    # ------------------------------------------------------------------ app
    app_name: str = "IronPulse"
    environment: Literal["development", "production", "test"] = "development"
    debug: bool = False
    host: str = "127.0.0.1"
    port: int = 11000
    public_base_url: str = f"http://localhost:{port}"
    allowed_hosts: CommaList = Field(default_factory=lambda: ["localhost", "127.0.0.1", "::1"])
    cors_origins: CommaList = Field(default_factory=list)
    # COOKIE_SECURE, FORCE_HTTPS (+ HSTS) and TRUST_PROXY_HEADERS default to true under ENVIRONMENT=production unless
    # set explicitly (_production_mode); these defaults are the development ones
    cookie_secure: bool = False
    force_https: bool = False
    trust_proxy_headers: bool = False
    # reverse proxies whose X-Forwarded-For we believe (addresses or CIDR ranges); used only with trust_proxy_headers
    trusted_proxies: CommaList = Field(default_factory=lambda: ["127.0.0.1", "::1"])
    session_ttl_hours: int = 24 * 7
    run_background_jobs: bool = True
    seed_demo_data: bool = True
    realtime_pg_bridge: bool = False  # true when running several uvicorn workers (LISTEN/NOTIFY fan-out)
    reload: bool = False  # auto-restart on code changes (development only)

    # ------------------------------------------------------------------ secrets
    secret_key: str = Field(min_length=32)
    data_encryption_key: str = Field(min_length=40)
    id_hash_pepper: str = Field(min_length=16)

    # ------------------------------------------------------------------ database
    database_url: str = "postgresql+asyncpg://ironpulse:ironpulse@localhost:5432/ironpulse"
    database_admin_url: str | None = None
    test_database_url: str | None = None
    # Connection pool PER WORKER PROCESS, shared by its requests and its background work: at most
    # db_pool_size + db_max_overflow connections (+1 LISTEN connection with REALTIME_PG_BRIDGE). A request that finds
    # none free waits up to db_pool_timeout_s, then gets the friendly 503. Defaults sized so 4 workers fit a default
    # PostgreSQL (max_connections 100, 3 reserved): 4 x (10 + 5 + 1) = 64 of 97 connections, under the 80 % the startup
    # check allows (app.db.ConnectionBudget; ARCHITECTURE.md "Database sizing" has the formula and PgBouncer advice).
    db_pool_size: int = 10
    db_max_overflow: int = 5
    db_echo: bool = False
    auto_create_tables: bool = True
    auto_migrate: bool = True  # additive schema sync on startup (new columns/indexes/constraints; never drops)
    # Server-side limits on every pooled connection (app/db.py; 0 = leave the server/role default). A request whose
    # query runs longer, or that waits longer for a free connection, gets a friendly 503 (app/core/errors.py).
    db_statement_timeout_ms: int = Field(15_000, ge=0)
    # idle_in_transaction_session_timeout. ENVIRONMENT=test defaults to 1 h instead (_test_db_idle_limit): pytest
    # fixtures keep one session open across a whole test, and some tests crawl every page for minutes
    db_idle_tx_timeout_ms: int = Field(60_000, ge=0)
    db_pool_timeout_s: float = Field(10.0, gt=0)  # wait for a free pooled connection
    db_long_statement_timeout_ms: int = Field(300_000, ge=0)  # default of app.db.long_statement() (batch work, CSV)
    # background sessions (app.db.session_scope: jobs, periodic tasks, worker loops, scripts) may sit idle in a read
    # transaction while an AI provider or OCR engine answers; applied only when longer than db_idle_tx_timeout_ms
    db_background_idle_tx_timeout_ms: int = Field(600_000, ge=0)

    # ------------------------------------------------------------------ data retention (app/services/retention.py)
    # Nightly batched deletes of old operational rows. 0 days = keep that table for ever.
    retention_batch: int = Field(1_000, ge=1, le=50_000)  # rows deleted per transaction
    retention_security_days: int = Field(90, ge=0)  # security_events
    retention_audit_days: int = Field(365, ge=0)  # audit_logs
    retention_ai_log_days: int = Field(30, ge=0)  # ai_call_logs
    retention_email_days: int = Field(30, ge=0)  # email_outbox rows already sent / logged / failed
    retention_jobs_days: int = Field(14, ge=0)  # jobs done / failed / cancelled (never queued, running or paused)
    retention_notification_days: int = Field(90, ge=0)  # notifications already read (unread ones are kept)
    # ip_bans lifted / expired this long ago (never an active ban; at least the 30-day repeat-offence window is kept)
    retention_ip_ban_days: int = Field(90, ge=0)
    # auth_tokens used / superseded this long ago, and e-mail-verification / password-reset links expired this long ago
    # (an unused invite stays: the owner's member and staff pages show "invite sent ..." from it)
    retention_auth_token_days: int = Field(30, ge=0)
    retention_session_days: int = Field(30, ge=0)  # user_sessions expired this long ago (live ones are never touched)
    retention_hour_utc: int = Field(21, ge=0, le=23)  # runs once per UTC day, at the first hourly tick from this hour
    retention_max_seconds: int = Field(600, ge=0)  # time budget per run (0 = none); the next tick continues

    # ------------------------------------------------------------------ Redis (optional session store)
    # Sessions: Redis maps the session token hash -> user id (fast, shared by all workers); the user's
    # latest name/e-mail/photo is always read from PostgreSQL. Empty = PostgreSQL-only sessions.
    redis_url: str | None = None

    # ------------------------------------------------------------------ AI providers (strict order)
    ai_provider_order: CommaList = Field(default_factory=lambda: ["gateway", "docker", "anthropic"])
    ai_gateway_base_url: str | None = "http://192.168.0.101:8000/v1"
    ai_gateway_api_key: str = "change-me-gateway-key"
    ai_gateway_model: str = "gemma"
    ai_gateway_status_url: str | None = None
    ai_gateway_timeout: float = 90.0
    ai_gateway_max_concurrency: int = 4
    ai_docker_base_url: str | None = "http://localhost:11434/v1"
    ai_docker_api_key: str = "ollama"
    ai_docker_model: str = "gemma3:4b"
    ai_docker_timeout: float = 180.0
    ai_docker_max_concurrency: int = 1
    # the local CPU model gets at least this long for its first words in a streamed answer (features ask for 8-12 s; a
    # laptop's Ollama needs 5-15 s) - never beyond the feature's whole-answer budget (app/ai/client.py)
    ai_docker_first_token_seconds: float = Field(25.0, ge=0, le=120)
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-opus-5-5"
    anthropic_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    anthropic_timeout: float = 120.0
    anthropic_max_concurrency: int = 4
    # ------------------------------------------------------------------ voice (live AI workout companion)
    # Kokoro TTS behind an OpenAI-compatible server (e.g. the kokoro-fastapi Docker container on port 8880).
    # When unreachable the browser's built-in soft voice (speechSynthesis) is used instead.
    tts_base_url: str | None = "http://localhost:8880/v1"
    tts_api_key: str = "not-needed"
    tts_model: str = "kokoro"
    tts_voice: str = "af_heart"
    tts_speed: float = 0.95
    tts_timeout: float = 20.0
    ai_health_interval_seconds: int = 60

    # ------------------------------------------------------------------ AI spam protection + queue (app/ai/guard.py,
    # app/ai/scheduler.py, app/services/presence.py - see docs/ai-guard-and-queue.md)
    ai_guard_enabled: bool = True  # per-user quotas, duplicate/spam checks and cooldowns
    ai_quota_scale: float = 1.0  # multiplies every per-user AI quota (2.0 = twice as generous)
    ai_queue_concurrency: int = 4  # LLM calls running at once per process; the rest wait in a fair queue
    ai_tts_concurrency: int = 2  # Kokoro syntheses running at once per process
    ai_queue_max_per_user: int = 4  # queued + running AI calls one user may have at once
    ai_queue_max_total: int = 200  # whole-queue cap; beyond it requests fail fast with "AI is busy"
    presence_grace_seconds: int = 45  # a user counts as gone this long after their last page closed
    jobs_concurrency: int = 2  # background jobs executed at once per process
    ai_circuit_failures: int = 2
    ai_circuit_cooldown_seconds: int = 45
    ai_json_mode: bool = True

    # ------------------------------------------------------------------ simulated AI (load tests only - app/ai/simulated.py)
    # Used only when AI_PROVIDER_ORDER names 'simulated' (never by default): answers are generated locally with
    # realistic latency and pass the same AI guard, fair queue, provider slots and circuit breaker as a real provider.
    # ENVIRONMENT=production refuses to start with it unless AI_SIMULATED_ALLOWED=true, and then warns at every start.
    ai_simulated_allowed: bool = False
    ai_sim_first_token_ms: SimRange = (400.0, 1500.0)  # time to the first words, uniform ("400..1500" or one number)
    ai_sim_tokens_per_s: SimRange = (25.0, 60.0)  # generation speed, one rate per call ("25..60" or one number)
    ai_sim_failure_rate: float = Field(0.0, ge=0.0, le=1.0)  # share of calls failing like an unreachable server
    ai_sim_max_concurrency: int = Field(4, ge=1)  # provider slots, like AI_GATEWAY_MAX_CONCURRENCY
    ai_sim_tts: bool = True  # with 'simulated' selected the coach voice (Kokoro lane) is simulated too
    ai_sim_tts_ms: SimRange = (300.0, 1200.0)  # time to synthesise one voice line ("300..1200" or one number)
    # bug-hunt aids of the simulated provider (ignored in production, app/ai/simulated.py): every answer starts with the
    # nonces found in the requester's own text; every prompt is appended to this JSONL file (mode 0600, capped)
    ai_sim_echo_nonce: bool = False
    ai_sim_capture_prompts: str = ""
    ai_sim_capture_max_mb: int = Field(256, ge=1)
    # how long a provider gets to accept the connection before the next one is asked (Claude: at least 5 s)
    ai_connect_timeout_seconds: float = Field(2.0, gt=0, le=30)

    # ------------------------------------------------------------------ OCR
    tesseract_cmd: str | None = None
    tesseract_langs: str = "eng"
    ocr_engines: CommaList = Field(
        default_factory=lambda: ["tesseract", "apple_vision", "windows_ocr", "rapidocr", "vision_llm"]
    )
    ocr_min_engines: int = 2

    # ------------------------------------------------------------------ maps
    geoapify_api_key: str | None = None
    map_osm_fallback: bool = True

    # ------------------------------------------------------------------ payments (Cashfree)
    cashfree_env: Literal["sandbox", "production"] = "sandbox"
    cashfree_app_id: str | None = None
    cashfree_secret_key: str | None = None
    cashfree_api_version: str = "2025-01-01"
    cashfree_payout_client_id: str | None = None
    cashfree_payout_client_secret: str | None = None
    cashfree_payout_api_version: str = "2024-01-01"

    # ------------------------------------------------------------------ e-mail
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from: str = "IronPulse <no-reply@ironpulse.local>"
    smtp_starttls: bool = True
    smtp_ssl: bool = False
    smtp_timeout: float = 20.0

    # ------------------------------------------------------------------ security limits
    rate_limit_per_minute: int = 240
    rate_limit_auth_per_minute: int = 10
    rate_limit_ai_per_minute: int = 60  # per address (a gym's NAT shares it); per-user AI quotas: app/ai/policy.py
    rate_limit_live_per_minute: int = 120  # live workouts: progress events, commands, chat messages + coach voice
    rate_limit_upload_per_minute: int = 30
    rate_limit_static_per_minute: int = 1500
    rate_limit_whitelist: CommaList = Field(default_factory=list)
    # With REDIS_URL the rate limits, threat scores and unknown-account sign-in lockout are shared by every worker
    # (app/security/ratelimit.py); a Redis call slower than this falls back to the worker's own memory for a moment
    rate_limit_redis_timeout_ms: int = Field(250, ge=1, le=5000)
    max_upload_mb: int = 8
    threat_ban_score: float = 12.0
    login_max_failures: int = 5
    login_lockout_minutes: int = 15

    # ------------------------------------------------------------------ business rules
    offline_max_distance_km: float = 30.0
    presence_radius_km: float = 1.5
    trainer_min_gap_hours: float = 6.0
    trainer_max_branch_distance_km: float = 50.0
    trainer_max_clients: int = Field(150, ge=1)  # active clients per trainer for automatic assignment (checkout)
    max_open_hours_per_day: float = 14.0
    min_branch_photos: int = 3
    account_retention_days: int = 30
    otp_ttl_minutes: int = 10
    otp_max_attempts: int = 5
    tasks_max_open: int = Field(500, ge=1)  # open tasks one person may hold (TASKS_MAX_OPEN; app/services/tasks.py)

    # ------------------------------------------------------------------ bootstrap super-admin
    superadmin_email: str = "admin@ironpulse.local"
    superadmin_password: str | None = None

    # ------------------------------------------------------------------ paths
    var_dir: Path = BASE_DIR / "var"
    loadtest_results_dir: Path | None = None  # LOADTEST_RESULTS_DIR; None = <project root>/loadtest/results (/superadmin/load-test)

    # ------------------------------------------------------------------ sign-in sessions (app/security/sessions.py)
    # true: every successful sign-in ends the account's other sessions - the newest wins and the other devices are told
    # "signed in on another device"; false: an account may stay signed in on several devices at once
    single_session_per_account: bool = True

    # ------------------------------------------------------------------ realtime (app/services/realtime.py)
    # a direct (session-pooled) database URL for the cross-worker LISTEN bridge - needed behind PgBouncer in
    # transaction mode; empty = DATABASE_URL
    realtime_pg_bridge_url: str | None = None

    # ------------------------------------------------------------------ validators
    @field_validator(
        "allowed_hosts", "cors_origins", "ai_provider_order", "ocr_engines", "rate_limit_whitelist", "trusted_proxies",
        mode="before"
    )
    @classmethod
    def _lists(cls, value: Any) -> list[str]:
        return _split_list(value)

    @field_validator("ai_provider_order")
    @classmethod
    def _known_providers(cls, value: list[str]) -> list[str]:
        known = {"gateway", "docker", "anthropic", SIMULATED_AI}  # 'simulated' only when named: never in the default
        # each provider once, at its first place: a repeated name would make every failing call try it twice
        cleaned = list(dict.fromkeys(v.lower() for v in value if v.lower() in known))
        return cleaned or ["gateway", "docker", "anthropic"]

    @field_validator("allowed_hosts")
    @classmethod
    def _host_patterns(cls, value: list[str]) -> list[str]:
        """The wildcard shapes the Host check accepts. Starlette's TrustedHostMiddleware asserts them only once the app
        builds its middleware (the first request): any other '*' would end every request in a 500, not stop the
        start."""
        for pattern in value:
            if "*" in pattern[1:] or (pattern.startswith("*") and pattern != "*" and not pattern.startswith("*.")):
                raise ValueError(f"'{pattern}' - a wildcard must look like *.example.com")
        return value

    @field_validator("ai_sim_first_token_ms", "ai_sim_tokens_per_s", "ai_sim_tts_ms", mode="before")
    @classmethod
    def _sim_range(cls, value: Any, info: ValidationInfo) -> tuple[float, float]:
        return _parse_range(value, positive=info.field_name == "ai_sim_tokens_per_s")

    @field_validator("database_url", "test_database_url", "database_admin_url")
    @classmethod
    def _asyncpg_driver(cls, value: str | None) -> str | None:
        if value and value.startswith(("postgresql://", "postgres://")):
            value = "postgresql+asyncpg://" + value.split("://", 1)[1]
        return value

    @field_validator("public_base_url")
    @classmethod
    def _strip_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @model_validator(mode="before")
    @classmethod
    def _test_db_idle_limit(cls, data: Any) -> Any:
        """ENVIRONMENT=test without an explicit DB_IDLE_TX_TIMEOUT_MS: a 1 h idle-in-transaction limit (a test's
        ``db`` fixture session stays open across all of its requests)."""
        if isinstance(data, dict) and str(data.get("environment") or "").lower() == "test" \
                and "db_idle_tx_timeout_ms" not in data:
            data = {**data, "db_idle_tx_timeout_ms": TEST_DB_IDLE_TX_TIMEOUT_MS}
        return data

    @model_validator(mode="after")
    def _derived(self) -> "Settings":
        if self.environment == "production":
            self.debug = False
        return self

    _explicit_fields: frozenset[str] = PrivateAttr(default_factory=frozenset)  # set by the environment / .env / kwargs

    @model_validator(mode="after")
    def _production_mode(self) -> "Settings":
        """ENVIRONMENT=production is secure by default (README "Deploying to production"): COOKIE_SECURE, FORCE_HTTPS
        (+ HSTS) and TRUST_PROXY_HEADERS are on unless set explicitly - an explicit false is honoured and logged at every
        start (``production_warnings``) - debug and auto-reload are off, and settings that would make the site unsafe
        (``_production_problems``: placeholder secrets, host checks - '*' or a public-suffix wildcard such as '*.com' -,
        '*', 'null' or plain-http:// origins, catch-all proxies, a weak bootstrap password, the simulated AI without
        AI_SIMULATED_ALLOWED) stop the start with one line each naming the variable to set."""
        explicit = frozenset(self.model_fields_set)  # before the defaults below mark their fields as set
        self._explicit_fields = explicit
        if self.environment != "production":
            return self
        for name in PRODUCTION_SECURE_DEFAULTS:
            if name not in explicit:
                setattr(self, name, True)
        self.debug = False
        self.reload = False
        problems = _production_problems(self)
        if problems:
            raise ProductionConfigError(problems)
        return self

    # ------------------------------------------------------------------ helpers
    @property
    def is_test(self) -> bool:
        return self.environment == "test"

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    def production_warnings(self) -> list[str]:
        """What a production operator should hear at every start (``get_settings`` logs it, ``manage.py run`` prints
        it, ``manage.py check`` lists it): explicit opt-outs of the secure defaults and settings that only half work.
        Empty outside production. Unsafe settings stop the start instead (``_production_mode``)."""
        if not self.is_production:
            return []
        out: list[str] = []
        if not self.cookie_secure:
            out.append("COOKIE_SECURE=false in production: session and CSRF cookies are not marked Secure and also "
                       "travel over plain HTTP - remove the line (production default: true) once the site is served "
                       "over HTTPS")
        if not self.force_https:
            hsts = "" if self.cookie_secure else ", and no HSTS header is sent"
            out.append("FORCE_HTTPS=false in production: plain-HTTP requests are served instead of redirected to "
                       f"HTTPS{hsts} - remove the line (production default: true)")
        if not self.trust_proxy_headers:
            loop = " and FORCE_HTTPS redirects in a loop" if self.force_https else ""
            out.append("TRUST_PROXY_HEADERS=false in production: behind a reverse proxy every visitor gets the proxy's "
                       f"address (one shared rate limit, one ban for everyone){loop} - remove the line (production "
                       "default: true; X-Forwarded-For/-Proto are believed only from TRUSTED_PROXIES) unless clients "
                       "connect to the app directly")
        hosts_unset = ("allowed_hosts" not in self._explicit_fields
                       and all(is_loopback_host(h) for h in self.allowed_hosts))
        if hosts_unset:
            out.append("ALLOWED_HOSTS is not set: only requests for localhost are answered (400 'Invalid host header' "
                       "for any other name) - set ALLOWED_HOSTS to the site's host name(s), e.g. gym.example.com")
        plain = plain_http_origins(self.cors_origins)
        if plain and not self.cookie_secure:  # COOKIE_SECURE on refuses them (``_production_problems``)
            out.append(f"CORS_ORIGINS={plain[0]} is a plain-http:// origin: anyone on that page's network path can "
                       "rewrite it and send signed-in requests from it - use https:// origins once the site is served "
                       "over HTTPS")
        unusable = [h for h in self.allowed_hosts if _unusable_host(h)]
        if unusable:
            out.append(f"ALLOWED_HOSTS contains '{unusable[0]}', which never matches a request (the Host header is "
                       "compared without scheme, port or path) - list bare host names, e.g. "
                       "ALLOWED_HOSTS=gym.example.com")
        base = urlsplit(self.public_base_url)
        base_host = base.hostname or ""
        if is_loopback_host(base_host):
            # fine for a trial on this machine (loopback HOST, localhost answered); not once the site has real names
            if not is_loopback_host(self.host) or not _host_allowed(base_host, self.allowed_hosts):
                out.append(f"PUBLIC_BASE_URL={self.public_base_url} points at this machine: links in e-mails do not "
                           "work for anyone else - set PUBLIC_BASE_URL to the site's address, e.g. "
                           "https://gym.example.com")
        else:
            if base_host and not hosts_unset and not _host_allowed(base_host, self.allowed_hosts):
                name = f"[{base_host}]" if ":" in base_host else base_host
                out.append(f"PUBLIC_BASE_URL={self.public_base_url} names a host ALLOWED_HOSTS does not accept: links "
                           f"in e-mails would be answered 400 'Invalid host header' - add {name} to ALLOWED_HOSTS")
        return out

    @property
    def upload_dir(self) -> Path:
        return self.var_dir / "uploads"

    @property
    def mail_dir(self) -> Path:
        return self.var_dir / "mail"

    @property
    def log_dir(self) -> Path:
        return self.var_dir / "logs"

    @property
    def cashfree_enabled(self) -> bool:
        return bool(self.cashfree_app_id and self.cashfree_secret_key)

    @property
    def cashfree_payouts_enabled(self) -> bool:
        return bool(self.cashfree_payout_client_id and self.cashfree_payout_client_secret)

    @property
    def payments_test_mode(self) -> bool:
        """True when no Cashfree keys are configured: payments are simulated and clearly labelled."""
        return not self.cashfree_enabled

    @property
    def cashfree_pg_base(self) -> str:
        return "https://api.cashfree.com/pg" if self.cashfree_env == "production" else "https://sandbox.cashfree.com/pg"

    @property
    def cashfree_payout_base(self) -> str:
        return (
            "https://api.cashfree.com/payout" if self.cashfree_env == "production" else "https://sandbox.cashfree.com/payout"
        )

    @property
    def smtp_enabled(self) -> bool:
        return bool(self.smtp_host)

    @property
    def geoapify_enabled(self) -> bool:
        return bool(self.geoapify_api_key)

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    def resolved_tesseract_cmd(self) -> str | None:
        """Locate the Tesseract binary on Windows, macOS or Linux."""
        candidates: list[str] = []
        if self.tesseract_cmd:
            candidates.append(self.tesseract_cmd)
        found = shutil.which("tesseract")
        if found:
            candidates.append(found)
        if sys.platform == "win32":
            candidates += [
                r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
            ]
        else:
            candidates += [
                str(Path.home() / ".ironpulse" / "env" / "bin" / "tesseract"),
                "/opt/homebrew/bin/tesseract",
                "/usr/local/bin/tesseract",
                "/usr/bin/tesseract",
            ]
        for c in candidates:
            if c and Path(c).exists():
                return c
        return None

    # ------------------------------------------------------------------ caps (app/core/caps.py holds the CAPS table)
    # Gyms one person may be a member of at once, counted as DISTINCT gyms over active, suspended and unfinished
    # (pending) memberships: online-capable (online or combo plans) and in-person-capable (gym or combo plans).
    max_online_gyms: int = Field(3, ge=1, le=100)
    max_offline_gyms: int = Field(1, ge=1, le=100)
    # Tune any other cap of app/core/caps.py: "name=number,name=number" (e.g. branches_per_gym=300)
    caps_overrides: str = ""

    # ------------------------------------------------------------------ trust & safety (app/services/trust_safety.py)
    # staff removed for a VIOLATION this many times across ANY gyms within VIOLATION_WINDOW_DAYS -> the account is
    # banned platform-wide automatically (any SEVERE removal bans at once)
    violation_autoban_threshold: int = Field(2, ge=1, le=50)
    violation_window_days: int = Field(365, ge=1, le=3650)
    # a SEVERE removal also bans the account's recent public network addresses for this many days ...
    severe_ip_ban_days: int = Field(30, ge=1, le=3650)
    # ... except an address more than this many OTHER active accounts used recently (gym Wi-Fi, carrier NAT): flagged
    shared_ip_threshold: int = Field(3, ge=0, le=10_000)

    # ------------------------------------------------------------------ platform admin sign-in (app/security/admin_login.py)
    # super-admin accounts sign in ONLY at /superadmin/login. SUPERADMIN_ALLOWED_IPS: addresses / CIDR ranges allowed to
    # open that page and the /superadmin console (empty = any address; the client address honours TRUSTED_PROXIES)
    superadmin_allowed_ips: CommaList = Field(default_factory=list)
    # failed platform-admin sign-ins per client address within the window ban that address (loopback never: counted only)
    superadmin_login_max_failures: int = Field(5, ge=1, le=100)
    superadmin_login_window_minutes: int = Field(15, ge=1, le=1440)
    superadmin_login_ban_hours: int = Field(24, ge=1, le=8760)

    # ------------------------------------------------------------------ platform governance (app/services/governance.py)
    # a gym scheduled for deletion stays suspended (and restorable) for this many days before the final deletion
    gym_delete_cooling_off_days: int = Field(7, ge=0, le=365)
    # destructive governance actions (bans, deletions, refunds, payouts) one super-admin may run per hour
    governance_actions_per_hour: int = Field(30, ge=1, le=1000)

    @field_validator("superadmin_allowed_ips", mode="before")
    @classmethod
    def _admin_ip_list(cls, value: Any) -> list[str]:
        return _split_list(value)

    # ------------------------------------------------------------------ public load-test page (app/routers/public_loadtest.py)
    # GET /load-test: a curated summary of the latest load-test run for everyone, linked from the navbar as "Load test
    # results" for anonymous visitors and every signed-in role; false = the page is a 404 and the link is hidden (the
    # full report stays at /superadmin/load-test either way) - docs/load-test-page.md
    public_loadtest_page: bool = True


# ---------------------------------------------------------------------------------- loading + startup reports
class ConfigError(SystemExit):
    """IronPulse cannot run with these settings. ``problems``: one line each, naming the variable to set, never a value.

    A SystemExit, so every way of starting the app - ``python -m app``, ``uvicorn app.main:app`` with any number of
    workers, the scripts - stops with those lines (``get_settings`` prints them) instead of a traceback. Exit status 3
    is uvicorn's STARTUP_FAILURE: a multi-worker supervisor then stops instead of restarting its workers forever. Catch
    it explicitly where a broken configuration is a finding, not the end (``manage.py check``)."""

    EXIT_STATUS = 3

    def __init__(self, problems: Any = ()) -> None:
        super().__init__(self.EXIT_STATUS)
        items = [problems] if isinstance(problems, str) else list(problems) if isinstance(problems, (list, tuple)) else []
        self.problems = [str(p) for p in items if p] or ["the settings are invalid"]

    def __str__(self) -> str:
        return "; ".join(self.problems)

    def __reduce__(self) -> tuple[Any, ...]:
        return (type(self), (self.problems,))

    def report(self, stream: Any = None) -> None:
        """One line per problem on stderr (what the server log - and the load-test launcher - shows)."""
        out = stream if stream is not None else sys.stderr
        for problem in self.problems:
            print(f"[ironpulse] configuration error: {problem}", file=out, flush=True)


GENERATED_SECRETS = frozenset({"SECRET_KEY", "DATA_ENCRYPTION_KEY", "ID_HASH_PEPPER", "SUPERADMIN_PASSWORD"})


def describe_errors(exc: ValidationError) -> list[str]:
    """One line per problem of a Settings ValidationError, naming the environment variable (never its value)."""
    lines: list[str] = []
    for err in exc.errors(include_url=False, include_input=False):
        ctx = err.get("ctx") or {}
        if isinstance(ctx.get("error"), ProductionConfigError):
            lines.extend(ctx["error"].problems)
            continue
        loc = err.get("loc") or ()
        var = str(loc[0]).upper() if loc else "the settings"
        kind = str(err.get("type") or "")
        if kind == "missing":
            text = "is not set"
        elif kind == "string_too_short":
            text = f"is too short (at least {ctx.get('min_length')} characters)"
        else:
            msg = str(err.get("msg") or "invalid value")
            text = "is invalid: " + (msg[len("Value error, "):] if msg.startswith("Value error, ") else msg)
        hint = " (python scripts/make_env.py generates it)" if var in GENERATED_SECRETS else ""
        lines.append(f"{var} {text} - set {var} in .env or the environment{hint}")
    return lines


def build_settings(**values: Any) -> Settings:
    """``Settings(**values)`` whose validation errors become a ``ConfigError`` - one line per problem, no values,
    nothing printed. ``get_settings`` is the cached version that also reports."""
    try:
        return Settings(**values)  # type: ignore[call-arg]
    except ValidationError as exc:
        raise ConfigError(describe_errors(exc)) from None
    except SettingsError as exc:  # a value pydantic-settings could not even decode (e.g. a broken JSON list)
        raise ConfigError([f"{exc} - fix it in .env or the environment"]) from None


def _production_problems(s: Settings) -> list[str]:
    """Settings that would make a production site unsafe: each one stops the start (``Settings._production_mode``)."""
    problems: list[str] = []
    for var, value, min_length in (("SECRET_KEY", s.secret_key, 32), ("ID_HASH_PEPPER", s.id_hash_pepper, 16)):
        why = _weak_secret(value, min_length)
        if why:
            problems.append(f"{var} {why} - set {var} to a long random value (python scripts/make_env.py generates "
                            "one)")
    why = _bad_fernet_key(s.data_encryption_key)
    if why:
        problems.append(f"DATA_ENCRYPTION_KEY {why} - set DATA_ENCRYPTION_KEY to a Fernet key (python "
                        "scripts/make_env.py generates one; keep it once files are encrypted)")
    hosts = [h.strip() for h in s.allowed_hosts if h.strip()]
    example = "e.g. ALLOWED_HOSTS=gym.example.com"
    if not hosts:
        problems.append(f"ALLOWED_HOSTS is empty - set ALLOWED_HOSTS to the site's host name(s), {example}")
    elif "*" in hosts:
        problems.append(f"ALLOWED_HOSTS=* accepts any Host header - set ALLOWED_HOSTS to the site's host name(s), "
                        f"{example}")
    elif all(_unusable_host(h) for h in hosts):  # every request would get 400 'Invalid host header'
        problems.append(f"ALLOWED_HOSTS has no entry a request can match ('{hosts[0]}': the Host header is compared "
                        f"without scheme, port or path) - list bare host names, {example}")
    elif all(is_loopback_host(h) for h in hosts) and not is_loopback_host(s.host):
        problems.append(f"ALLOWED_HOSTS allows only localhost (the development default) but HOST={s.host} accepts "
                        f"connections from the network - set ALLOWED_HOSTS to the site's host name(s), {example}")
    wide = [h for h in hosts if public_suffix_wildcard(h)]
    if wide:
        problems.append(f"ALLOWED_HOSTS={wide[0]} accepts every name under the public suffix "
                        f"'.{public_suffix_wildcard(wide[0])}' - anyone can register one and point it at this server "
                        "(DNS rebinding); name your own domain, e.g. ALLOWED_HOSTS=*.gym.example.com or gym.example.com")
    plain = plain_http_origins(s.cors_origins)
    if plain and s.cookie_secure:
        problems.append(f"CORS_ORIGINS={plain[0]} is a plain-http:// origin - anyone on that page's network path can "
                        "rewrite it and send signed-in requests from it; list https:// origins only (or leave "
                        "CORS_ORIGINS empty)")
    if "*" in s.cors_origins:
        problems.append("CORS_ORIGINS=* lets any website send signed-in requests - set CORS_ORIGINS to the exact "
                        "origins that need access, or leave it empty")
    if any(o.strip().lower() == "null" for o in s.cors_origins):  # sandboxed frames and file:// pages send it
        problems.append("CORS_ORIGINS=null lets a sandboxed frame on any website send signed-in requests - remove "
                        "'null' from CORS_ORIGINS")
    if s.trust_proxy_headers:
        open_nets = [p.strip() for p in s.trusted_proxies if _catch_all_network(p)]
        if open_nets:
            problems.append(f"TRUSTED_PROXIES={open_nets[0]} believes X-Forwarded-For from (almost) every client "
                            "(anyone could choose their address and dodge bans) - set TRUSTED_PROXIES to your reverse "
                            "proxy's address(es)")
        names = [p.strip() for p in s.trusted_proxies if p.strip() != "*" and not _is_network(p)]
        if names:  # the app ignores it while uvicorn would take it literally: say so instead of half-trusting it
            problems.append(f"TRUSTED_PROXIES={names[0]} is not an IP address or network - list your reverse proxy's "
                            "address(es) or range(s), e.g. TRUSTED_PROXIES=10.0.0.5 or 172.16.0.0/12")
    fwd = os.environ.get("FORWARDED_ALLOW_IPS", "").strip()
    if fwd and any(_catch_all_network(p) for p in fwd.split(",")):
        problems.append("FORWARDED_ALLOW_IPS=* makes uvicorn believe X-Forwarded-For from every client - unset it "
                        "(IronPulse hands TRUSTED_PROXIES to uvicorn itself)")
    if urlsplit(s.public_base_url).scheme == "https":
        for name, on in (("COOKIE_SECURE", s.cookie_secure), ("FORCE_HTTPS", s.force_https)):
            if not on:
                why = ("session cookies would also travel over plain HTTP" if name == "COOKIE_SECURE" else
                       "plain-HTTP requests would be served, with no HSTS")
                problems.append(f"{name}=false but PUBLIC_BASE_URL is https:// - {why}; remove the {name} line "
                                "(an older .env.example pinned it to false)")
    elif s.cookie_secure and not is_loopback_host(urlsplit(s.public_base_url).hostname or ""):
        problems.append(f"PUBLIC_BASE_URL={s.public_base_url} is not https:// while COOKIE_SECURE is on - links in "
                        "e-mails (sign-in, password reset) would travel over plain HTTP; set PUBLIC_BASE_URL=https://<the "
                        "site's address> (an intranet trial over plain HTTP sets COOKIE_SECURE=false and "
                        "FORCE_HTTPS=false explicitly)")
    why = _weak_admin_password(s.superadmin_password)
    if why:
        problems.append(f"SUPERADMIN_PASSWORD {why} - set a long random SUPERADMIN_PASSWORD (python scripts/make_env.py "
                        "generates one), or remove it once the super-admin exists")
    refusal = simulated_ai_refusal(s)
    if refusal:
        problems.append(refusal)
    return problems


def _range_text(span: Any) -> str:
    low, high = (int(v) if float(v).is_integer() else v for v in span)
    return f"{low}" if low == high else f"{low}..{high}"


def simulated_ai_requested(s: Settings) -> bool:
    return SIMULATED_AI in (s.ai_provider_order or [])


def simulated_ai_refusal(s: Settings) -> str | None:
    """Production never runs the simulated provider without AI_SIMULATED_ALLOWED=true: the settings refuse to load
    (this line), and the AI router leaves it out of the chain all the same."""
    if simulated_ai_requested(s) and s.is_production and not s.ai_simulated_allowed:
        return ("AI_PROVIDER_ORDER names the simulated AI provider (generated answers, no model) - remove 'simulated' "
                "from AI_PROVIDER_ORDER, or set AI_SIMULATED_ALLOWED=true for a load test")
    if simulated_ai_requested(s) and s.is_production and list(s.ai_provider_order) != [SIMULATED_AI]:
        return (f"AI_PROVIDER_ORDER={','.join(s.ai_provider_order)} would answer members with generated text whenever "
                "the model fails - production runs the simulated AI provider only alone (AI_PROVIDER_ORDER=simulated, "
                "for a load test): remove 'simulated' or the real providers")
    return None


def simulated_ai_warning(s: Settings) -> str | None:
    """The loud line logged at every start of every process while the simulated provider is in the chain."""
    if not simulated_ai_requested(s) or simulated_ai_refusal(s):
        return None
    text = (f"SIMULATED AI PROVIDER ACTIVE (AI_PROVIDER_ORDER={','.join(s.ai_provider_order)}): AI answers are "
            f"generated locally - first words after {_range_text(s.ai_sim_first_token_ms)} ms, "
            f"{_range_text(s.ai_sim_tokens_per_s)} words/s, {s.ai_sim_failure_rate:.0%} failures - and no model runs."
            " For load tests only")
    if s.is_production:
        text += ("; ENVIRONMENT=production with AI_SIMULATED_ALLOWED=true: members get simulated answers - remove "
                 "'simulated' from AI_PROVIDER_ORDER after the test")
    return f"!!! {text} !!!"


def startup_warnings(s: Settings) -> list[str]:
    """Everything an operator should hear when a server starts with ``s``: the production warnings and the simulated
    AI (``manage.py run`` prints them once; every process logs them itself)."""
    out = list(s.production_warnings())
    simulated = simulated_ai_warning(s)
    if simulated:
        out.append(simulated)
    return out


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The process's settings (cached). Unusable settings end the process with one clear line per problem
    (``ConfigError``, printed here); the production warnings are logged once per process - at every start of every
    worker."""
    try:
        settings = build_settings()
    except ConfigError as exc:
        exc.report()
        raise
    for line in settings.production_warnings():
        warn_at_startup(log, line)
    return settings


STARTUP_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"  # = app.main._configure_logging


def warn_at_startup(logger: logging.Logger, message: str) -> None:
    """``logger.warning(message)`` that keeps its level and source even before logging is configured: a uvicorn worker
    imports the settings (and builds the AI router) before app.main sets logging up, and Python's last-resort handler
    would print the bare text - a log scan for WARNING would never see it."""
    if logger.hasHandlers():
        logger.warning(message)
        return
    handler = logging.StreamHandler()  # stderr, like the last-resort handler; only for this one line
    handler.setFormatter(logging.Formatter(STARTUP_LOG_FORMAT))
    logger.addHandler(handler)
    try:
        logger.warning(message)
    finally:
        logger.removeHandler(handler)


def reset_settings_cache() -> None:
    get_settings.cache_clear()
