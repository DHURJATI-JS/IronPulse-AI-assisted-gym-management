"""Platform self-check for the super-admin (WP-G).

``await run_selfcheck(db)`` -> ``list[CheckResult]`` (``check``, ``status`` ok | warn | fail, ``detail``,
``fix``). It never raises: every probe is isolated (database probes run inside a SAVEPOINT) and slow
probes have timeouts. Cross-platform: paths via pathlib, no shell commands.

``await platform_services(db)`` returns the live state of the supporting services shown on the AI page
(Redis session store, Kokoro TTS, the Gemma gateway status page backends, demo data).

Network probes (gateway status page, Kokoro TTS) are skipped in the test environment unless a test
injects ``HTTP_TRANSPORT`` (e.g. ``httpx.MockTransport``) - tests never touch the network.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import APP_DIR, get_settings
from app.models import User, Video
from app.models.enums import Role

log = logging.getLogger("ironpulse.selfcheck")

OK, WARN, FAIL = "ok", "warn", "fail"
GROUPS = ("Security", "Integrations", "AI & OCR", "Database & storage")
PLACEHOLDER_MARKERS = ("change", "replace", "example", "default", "secret-key", "insecure", "dummy", "xxxx")
DEFAULT_ADMIN_PASSWORDS = ("admin", "admin123", "Admin@123", "password", "changeme", "ironpulse", "superadmin",
                           "IronPulse123!")
GATEWAY_STATUS_TIMEOUT = 2.5
TTS_TIMEOUT = 2.0
DEMO_DOMAIN_FALLBACK = "demo.ironpulse.local"

# Tests inject an ``httpx.MockTransport`` here; in the test environment without it, network probes are skipped.
HTTP_TRANSPORT: Any = None


@dataclass(slots=True)
class CheckResult:
    key: str
    check: str
    status: str  # ok | warn | fail
    detail: str
    fix: str = ""
    group: str = "Security"

    def as_dict(self) -> dict[str, str]:
        return {"key": self.key, "check": self.check, "status": self.status, "detail": self.detail, "fix": self.fix,
                "group": self.group}


def _entropy_bits(value: str) -> float:
    if not value:
        return 0.0
    counts = Counter(value)
    n = len(value)
    per_char = -sum((c / n) * math.log2(c / n) for c in counts.values())
    return per_char * n


def _clip(value: Any, n: int = 120) -> str:
    text_ = " ".join(str(value if value is not None else "").split())
    return text_ if len(text_) <= n else text_[: n - 1] + "…"


# ===================================================================================== network probes
def network_allowed() -> bool:
    return HTTP_TRANSPORT is not None or not get_settings().is_test


def _http(timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, transport=HTTP_TRANSPORT, follow_redirects=False)


async def probe_gateway_status(timeout: float = GATEWAY_STATUS_TIMEOUT) -> dict[str, Any]:
    """The Gemma gateway's own status page (AI_GATEWAY_STATUS_URL): which llama.cpp backends are online."""
    url = get_settings().ai_gateway_status_url
    base = {"configured": bool(url), "ok": False, "skipped": False, "backends": [], "online": 0, "cpu": None,
            "latency_ms": None, "error": None, "url": url}
    if not url:
        return base
    if not network_allowed():
        return {**base, "ok": None, "skipped": True, "error": "not probed in the test environment"}
    started = time.perf_counter()
    try:
        async with _http(timeout) as client:
            resp = await client.get(url, headers={"Accept": "application/json"})
        base["latency_ms"] = int((time.perf_counter() - started) * 1000)
        if resp.status_code != 200:
            return {**base, "error": f"HTTP {resp.status_code}"}
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 - unreachable / timeout / not JSON
        return {**base, "error": exc.__class__.__name__}
    backends = []
    for b in (data.get("backends") if isinstance(data, dict) else None) or []:
        if not isinstance(b, dict):
            continue
        backends.append({
            "name": _clip(b.get("name") or "backend", 40), "url": _clip(b.get("url") or "", 80),
            "online": bool(b.get("online")), "busy": _clip(b.get("busy_slots") or "", 12),
            "overloaded": bool(b.get("overloaded")),
        })
        if len(backends) >= 12:
            break
    cpu = data.get("this_pc_cpu_percent") if isinstance(data, dict) else None
    try:
        cpu = round(float(cpu), 1) if cpu is not None else None
    except (TypeError, ValueError):
        cpu = None
    return {**base, "ok": True, "backends": backends, "online": sum(1 for b in backends if b["online"]), "cpu": cpu}


async def probe_tts(timeout: float = TTS_TIMEOUT) -> dict[str, Any]:
    """Kokoro TTS (OpenAI-compatible) reachability: GET {TTS_BASE_URL}/models with a short timeout."""
    s = get_settings()
    base_url = (s.tts_base_url or "").rstrip("/")
    service: dict[str, Any] | None = None
    try:  # WP-K's service keeps its own circuit-breaker state (optional)
        from app.services.tts import tts as tts_service

        service = tts_service.status(admin=True)  # the admin view: cache and request counts (members never see them)
    except Exception:  # noqa: BLE001 - module missing or not ready
        service = None
    base = {"configured": bool(base_url), "ok": False, "skipped": False, "latency_ms": None, "error": None,
            "models": [], "url": base_url or None, "voice": s.tts_voice, "model": s.tts_model, "service": service}
    if not base_url:
        return base
    if not network_allowed():
        return {**base, "ok": None, "skipped": True, "error": "not probed in the test environment"}
    started = time.perf_counter()
    try:
        async with _http(timeout) as client:
            resp = await client.get(f"{base_url}/models", headers={"Authorization": f"Bearer {s.tts_api_key}"})
        base["latency_ms"] = int((time.perf_counter() - started) * 1000)
        if resp.status_code != 200:
            return {**base, "error": f"HTTP {resp.status_code}"}
        try:
            payload = resp.json()
            items = payload.get("data", []) if isinstance(payload, dict) else payload
            models = [_clip(m.get("id") if isinstance(m, dict) else m, 40) for m in (items or [])][:6]
        except ValueError:
            models = []
        return {**base, "ok": True, "models": models}
    except Exception as exc:  # noqa: BLE001
        return {**base, "error": exc.__class__.__name__}


async def probe_redis() -> dict[str, Any]:
    s = get_settings()
    if not s.redis_url:
        return {"configured": False, "ok": False, "error": None}
    try:
        from app.security.sessions import redis_status

        state = await asyncio.wait_for(redis_status(), timeout=3)
    except Exception as exc:  # noqa: BLE001
        return {"configured": True, "ok": False, "error": exc.__class__.__name__}
    return {"configured": bool(state.get("configured", True)), "ok": bool(state.get("ok")), "error": state.get("error")}


def demo_domain() -> str:
    """E-mail domain of the demo accounts that scripts/seed_demo.py loads (reserved .local domain, never real)."""
    return DEMO_DOMAIN_FALLBACK


async def demo_accounts(db: AsyncSession) -> int:
    domain = demo_domain()
    async with db.begin_nested():
        return int(await db.scalar(
            select(func.count()).select_from(User).where(User.email.ilike(f"%@{domain}"), User.deleted_at.is_(None))
        ) or 0)


async def platform_services(db: AsyncSession) -> dict[str, Any]:
    """Live state of the supporting services (network probes run concurrently, ~2.5 s worst case)."""
    redis, tts, gateway = await asyncio.gather(probe_redis(), probe_tts(), probe_gateway_status())
    try:
        demo = await demo_accounts(db)
    except Exception:  # noqa: BLE001
        demo = None
    return {"redis": redis, "tts": tts, "gateway": gateway,
            "demo": {"accounts": demo, "domain": demo_domain(), "production": get_settings().is_production}}


# ===================================================================================== security
def check_secret_key() -> CheckResult:
    s = get_settings()
    key = s.secret_key or ""
    bits = _entropy_bits(key)
    lowered = key.lower()
    if any(m in lowered for m in PLACEHOLDER_MARKERS) or len(set(key)) < 12:
        return CheckResult("secret_key", "Secret key strength", FAIL,
                           f"SECRET_KEY looks like a placeholder ({len(key)} characters, {len(set(key))} distinct).",
                           "Generate a new one: python scripts/make_env.py (or 64 random URL-safe characters) and restart.")
    if len(key) < 48 or bits < 160:
        return CheckResult("secret_key", "Secret key strength", WARN,
                           f"{len(key)} characters, about {int(bits)} bits of entropy.",
                           "Use at least 48 random characters (about 256 bits) for SECRET_KEY.")
    pepper = s.id_hash_pepper or ""
    if len(pepper) < 24:
        return CheckResult("secret_key", "Secret key strength", WARN,
                           f"SECRET_KEY is strong (~{int(bits)} bits) but ID_HASH_PEPPER has only {len(pepper)} characters.",
                           "Use at least 24 random characters for ID_HASH_PEPPER (set it once - changing it breaks duplicate-ID detection).")
    return CheckResult("secret_key", "Secret key strength", OK,
                       f"{len(key)} characters, about {int(bits)} bits of entropy; ID pepper set.")


def check_debug() -> CheckResult:
    s = get_settings()
    if s.is_production and s.debug:
        return CheckResult("debug", "Debug mode", FAIL, "DEBUG is on in production: stack traces and API docs are exposed.",
                           "Set DEBUG=false in .env and restart.")
    if s.debug:
        return CheckResult("debug", "Debug mode", WARN, f"DEBUG is on (environment: {s.environment}).",
                           "Fine while developing - set DEBUG=false before going live.")
    return CheckResult("debug", "Debug mode", OK, f"DEBUG is off (environment: {s.environment}).")


def check_cookies() -> CheckResult:
    s = get_settings()
    https = s.public_base_url.startswith("https://")
    if https and not s.cookie_secure:
        return CheckResult("cookies", "Secure cookies & HTTPS", FAIL,
                           f"PUBLIC_BASE_URL is {s.public_base_url} but COOKIE_SECURE is false - session cookies could leak over HTTP.",
                           "Set COOKIE_SECURE=true (and FORCE_HTTPS=true behind your TLS proxy).")
    if s.is_production and not https:
        return CheckResult("cookies", "Secure cookies & HTTPS", FAIL,
                           f"Production runs on {s.public_base_url} without HTTPS.",
                           "Serve the site over HTTPS, set PUBLIC_BASE_URL=https://… and COOKIE_SECURE=true.")
    if s.cookie_secure and not https:
        return CheckResult("cookies", "Secure cookies & HTTPS", WARN,
                           "COOKIE_SECURE is true but the base URL is plain HTTP - browsers may refuse the session cookie.",
                           "Use an https:// PUBLIC_BASE_URL or set COOKIE_SECURE=false for local development.")
    if https:
        return CheckResult("cookies", "Secure cookies & HTTPS", OK, "HTTPS base URL with Secure cookies.")
    return CheckResult("cookies", "Secure cookies & HTTPS", WARN,
                       f"Development over plain HTTP ({s.public_base_url}).",
                       "Before going live: HTTPS + COOKIE_SECURE=true + FORCE_HTTPS=true.")


def check_cors() -> CheckResult:
    s = get_settings()
    origins = s.cors_origins
    if "*" in origins:
        return CheckResult("cors", "CORS origins", FAIL, "CORS_ORIGINS contains '*' while credentials are allowed.",
                           "List the exact origins that need cross-site access (or leave CORS_ORIGINS empty).")
    insecure = [o for o in origins if o.startswith("http://") and "localhost" not in o and "127.0.0.1" not in o]
    if insecure and s.is_production:
        return CheckResult("cors", "CORS origins", WARN, "Plain-HTTP origins are allowed: " + ", ".join(insecure[:3]),
                           "Only allow https:// origins in production.")
    if not origins:
        return CheckResult("cors", "CORS origins", OK, "Same-origin only (no cross-site origins allowed).")
    return CheckResult("cors", "CORS origins", OK, "Allowed: " + ", ".join(origins[:4]) + ("…" if len(origins) > 4 else ""))


def check_allowed_hosts() -> CheckResult:
    s = get_settings()
    hosts = s.allowed_hosts
    if "*" in hosts or not hosts:
        return CheckResult("hosts", "Allowed hosts", FAIL, "ALLOWED_HOSTS accepts any Host header (host-header attacks).",
                           "Set ALLOWED_HOSTS to your domain names, e.g. gym.example.com,localhost.")
    wildcard = [h for h in hosts if h.startswith("*.")]
    detail = ", ".join(hosts[:5]) + ("…" if len(hosts) > 5 else "")
    if wildcard:
        return CheckResult("hosts", "Allowed hosts", WARN, f"Wildcard sub-domains allowed: {detail}",
                           "Prefer exact host names.")
    return CheckResult("hosts", "Allowed hosts", OK, detail)


async def check_superadmin_password(db: AsyncSession) -> CheckResult:
    from app.security.passwords import COMMON_PASSWORDS, password_problems, verify_password

    s = get_settings()
    configured = s.superadmin_password or ""
    if configured:
        problems = password_problems(configured, email=s.superadmin_email)
        if configured.lower() in COMMON_PASSWORDS or configured in DEFAULT_ADMIN_PASSWORDS:
            problems.insert(0, "It is a well-known default password.")
        if problems:
            return CheckResult("superadmin_password", "Super-admin password", FAIL,
                               "SUPERADMIN_PASSWORD in .env is weak: " + " ".join(problems[:3]),
                               "Set a long random SUPERADMIN_PASSWORD in .env and restart - it applies to the platform "
                               "admin account at start-up (app/services/bootstrap.py).")
    async with db.begin_nested():
        hashes = (await db.execute(
            select(User.email, User.password_hash).where(User.role == Role.SUPERADMIN, User.deleted_at.is_(None))
        )).all()
    await db.commit()  # Argon2 below never runs inside an open transaction (no pooled connection idles in it)
    if not hashes:
        return CheckResult("superadmin_password", "Super-admin password", WARN, "No super-admin account exists yet.",
                           "Set SUPERADMIN_EMAIL and SUPERADMIN_PASSWORD in .env and restart (bootstrap creates it).")

    async def weak(pw_hash: str | None) -> str | None:
        for candidate in DEFAULT_ADMIN_PASSWORDS[:6]:
            if await verify_password(pw_hash, candidate):
                return candidate
        return None

    found = await asyncio.gather(*(weak(h) for _, h in hashes[:5]))
    bad = [email for (email, _), hit in zip(hashes, found) if hit]
    if bad:
        return CheckResult("superadmin_password", "Super-admin password", FAIL,
                           "These super-admin accounts still use a default password: " + ", ".join(bad),
                           "Set a new SUPERADMIN_PASSWORD in .env and restart now (or: python scripts/manage.py "
                           "create-superadmin --reset-password).")
    detail = f"{len(hashes)} super-admin account(s); no default passwords in use."
    if not configured:
        detail += " SUPERADMIN_PASSWORD is not set in .env (when set, it applies to the platform admin at every start)."
    return CheckResult("superadmin_password", "Super-admin password", OK, detail)


# ===================================================================================== integrations
def check_smtp() -> CheckResult:
    s = get_settings()
    if s.smtp_enabled:
        mode = "SSL" if s.smtp_ssl else ("STARTTLS" if s.smtp_starttls else "no TLS")
        status = OK if (s.smtp_ssl or s.smtp_starttls) else WARN
        return CheckResult("smtp", "E-mail (SMTP)", status, f"{s.smtp_host}:{s.smtp_port} ({mode}), from {s.smtp_from}",
                           "" if status == OK else "Enable STARTTLS or SSL so e-mails are encrypted in transit.",
                           group="Integrations")
    return CheckResult("smtp", "E-mail (SMTP)", WARN, "No SMTP server - e-mails are written to var/mail/*.eml instead.",
                       "Set SMTP_HOST, SMTP_PORT, SMTP_USERNAME and SMTP_PASSWORD in .env.", group="Integrations")


def check_geoapify() -> CheckResult:
    s = get_settings()
    if s.geoapify_enabled:
        return CheckResult("geoapify", "Maps & geocoding (Geoapify)", OK, "API key configured (map tiles, geocoding, address checks).",
                           group="Integrations")
    return CheckResult("geoapify", "Maps & geocoding (Geoapify)", WARN,
                       "No GEOAPIFY_API_KEY - OpenStreetMap tiles are used and address geocoding is limited.",
                       "Create a free key at geoapify.com and set GEOAPIFY_API_KEY.", group="Integrations")


def check_cashfree() -> CheckResult:
    s = get_settings()
    payouts = "payouts configured" if s.cashfree_payouts_enabled else "payouts simulated"
    if s.payments_test_mode:
        status = FAIL if s.is_production else WARN
        return CheckResult("cashfree", "Payments (Cashfree)", status,
                           f"TEST MODE - no Cashfree keys, every payment is simulated and labelled as a test ({payouts}).",
                           "Set CASHFREE_APP_ID and CASHFREE_SECRET_KEY (sandbox first, then production).",
                           group="Integrations")
    if s.cashfree_env == "sandbox":
        return CheckResult("cashfree", "Payments (Cashfree)", WARN, f"Cashfree SANDBOX - no real money moves ({payouts}).",
                           "Switch CASHFREE_ENV=production with live keys when you go live.", group="Integrations")
    return CheckResult("cashfree", "Payments (Cashfree)", OK, f"Cashfree PRODUCTION ({payouts}).", group="Integrations")


# ===================================================================================== AI & OCR
def check_tesseract() -> CheckResult:
    path = get_settings().resolved_tesseract_cmd()
    if path:
        return CheckResult("tesseract", "Tesseract OCR binary", OK, path, group="AI & OCR")
    return CheckResult("tesseract", "Tesseract OCR binary", WARN, "Tesseract was not found on this machine.",
                       "Install Tesseract (brew/apt/choco) or set TESSERACT_CMD to its full path.", group="AI & OCR")


async def check_ocr_engines() -> CheckResult:
    need = get_settings().ocr_min_engines
    try:
        from app.services import ocr

        result = await asyncio.wait_for(asyncio.to_thread(ocr.available_engines), timeout=15)
        if inspect.isawaitable(result):
            result = await asyncio.wait_for(result, timeout=15)
        engines = list(result or [])
    except NotImplementedError:
        return CheckResult("ocr_engines", "OCR engines", WARN, "The multi-engine OCR service is not installed yet.",
                           "Install the identity/OCR work package (app/services/ocr.py).", group="AI & OCR")
    except Exception as exc:  # noqa: BLE001
        return CheckResult("ocr_engines", "OCR engines", WARN, f"Could not list OCR engines ({exc.__class__.__name__}).",
                           "Check the OCR service logs.", group="AI & OCR")
    label = ", ".join(engines) if engines else "none"
    if len(engines) >= need:
        return CheckResult("ocr_engines", "OCR engines", OK, f"{len(engines)} available ({label}); {need} required to cross-validate.",
                           group="AI & OCR")
    return CheckResult("ocr_engines", "OCR engines", FAIL if not engines else WARN,
                       f"{len(engines)} available ({label}) but OCR_MIN_ENGINES={need} - ID cards fall back to manual review.",
                       "Install Tesseract and enable another engine (Apple Vision on macOS, Windows OCR, RapidOCR or a vision LLM).",
                       group="AI & OCR")


def _provider(name: str) -> object | None:
    from app.ai.client import ai

    return next((p for p in ai.providers if getattr(p, "name", None) == name), None)


async def _provider_check(key: str, label: str, name: str, *, probe: bool, missing_fix: str) -> CheckResult:
    provider = _provider(name)
    if provider is None or not provider.configured:  # type: ignore[attr-defined]
        return CheckResult(key, label, WARN, "Not configured (or not in AI_PROVIDER_ORDER).", missing_fix, group="AI & OCR")
    if probe:
        started = time.perf_counter()
        try:
            ok = await asyncio.wait_for(provider.ping(), timeout=8)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            ok = False
        ms = int((time.perf_counter() - started) * 1000)
        if ok:
            return CheckResult(key, label, OK, f"Reachable in {ms} ms · model {provider.model}.", group="AI & OCR")  # type: ignore[attr-defined]
        err = provider.state.last_error or "no answer"  # type: ignore[attr-defined]
        return CheckResult(key, label, WARN, f"Configured but unreachable ({err}).",
                           "Start the server/container and check the base URL and API key.", group="AI & OCR")
    info = provider.describe()  # type: ignore[attr-defined]
    status = info["status"]
    if status == "up":
        return CheckResult(key, label, OK, f"Healthy · model {info['model']}.", group="AI & OCR")
    if status == "down":
        return CheckResult(key, label, WARN, f"Configured but currently down ({info['last_error'] or 'health check failed'}).",
                           "Start the server/container; the AI router fails over to the next provider meanwhile.",
                           group="AI & OCR")
    return CheckResult(key, label, WARN, "Configured - not health-checked yet.",
                       "Use “Re-run with live probes” to ping it now.", group="AI & OCR")


async def check_ai_gateway(probe: bool) -> CheckResult:
    return await _provider_check("ai_gateway", "Gemma gateway (primary AI)", "gateway", probe=probe,
                                 missing_fix="Set AI_GATEWAY_BASE_URL and AI_GATEWAY_API_KEY.")


async def check_docker_ai(probe: bool) -> CheckResult:
    return await _provider_check("ai_docker", "Gemma in Docker (failsafe)", "docker", probe=probe,
                                 missing_fix="Run Ollama in Docker and set AI_DOCKER_BASE_URL / AI_DOCKER_MODEL.")


async def check_anthropic(probe: bool) -> CheckResult:
    s = get_settings()
    if not s.anthropic_api_key:
        return CheckResult("anthropic", "Claude (last-resort AI)", WARN,
                           "ANTHROPIC_API_KEY is not set - no cloud fallback when both Gemma providers are down.",
                           "Add ANTHROPIC_API_KEY to .env.", group="AI & OCR")
    return await _provider_check("anthropic", "Claude (last-resort AI)", "anthropic", probe=probe,
                                 missing_fix="Add 'anthropic' to AI_PROVIDER_ORDER.")


def check_simulated_ai() -> CheckResult:
    """'Simulated AI (load tests)': the load-test provider answers locally with generated text and no model - only when
    named in AI_PROVIDER_ORDER (app/ai/simulated.py). Off is the normal state; on, every AI answer is made up."""
    from app.config import SIMULATED_AI, simulated_ai_refusal

    label = "Simulated AI (load tests)"
    s = get_settings()
    if SIMULATED_AI not in (s.ai_provider_order or []):
        return CheckResult("ai_simulated", label, OK, "Off - AI answers come from the real providers only.",
                           group="AI & OCR")
    alone = list(s.ai_provider_order) == [SIMULATED_AI]
    where = "the only provider" if alone else f"in the chain ({','.join(s.ai_provider_order)})"
    if s.is_production:
        refusal = simulated_ai_refusal(s)
        if refusal or not alone:
            return CheckResult("ai_simulated", label, FAIL, f"ON in production, {where}: members get generated answers.",
                               "Remove 'simulated' from AI_PROVIDER_ORDER (production allows it only alone, with "
                               "AI_SIMULATED_ALLOWED=true, for a load test) and restart.", group="AI & OCR")
        return CheckResult("ai_simulated", label, WARN, f"ON in production ({where}, AI_SIMULATED_ALLOWED=true): every AI "
                           "answer is generated locally - a load test is running.",
                           "Remove 'simulated' from AI_PROVIDER_ORDER after the test and restart.", group="AI & OCR")
    return CheckResult("ai_simulated", label, WARN, f"ON ({where}): AI answers are generated locally with realistic "
                       "latency and no model - for load tests only.",
                       "Remove 'simulated' from AI_PROVIDER_ORDER to use the real models again.", group="AI & OCR")


async def check_gateway_backends() -> CheckResult:
    label = "Gemma gateway backends"
    st = await probe_gateway_status()
    if not st["configured"]:
        return CheckResult("gateway_backends", label, WARN, "AI_GATEWAY_STATUS_URL is not set - backend health is not visible.",
                           "Set AI_GATEWAY_STATUS_URL to the gateway's /status page (e.g. http://192.168.0.101:8000/status).",
                           group="AI & OCR")
    if st["skipped"]:
        return CheckResult("gateway_backends", label, WARN, "Not probed (test environment).", group="AI & OCR")
    if not st["ok"]:
        return CheckResult("gateway_backends", label, WARN, f"Status page unreachable ({st['error']}).",
                           "Start the gateway and check AI_GATEWAY_STATUS_URL; the AI router fails over meanwhile.",
                           group="AI & OCR")
    backends = st["backends"]
    names = ", ".join(f"{b['name']} {'online' if b['online'] else 'offline'}" + (f" ({b['busy']} busy)" if b["busy"] else "")
                      for b in backends) or "no backends listed"
    cpu = f" · gateway CPU {st['cpu']}%" if st["cpu"] is not None else ""
    if not backends or st["online"] == 0:
        return CheckResult("gateway_backends", label, FAIL if backends else WARN, f"{names}{cpu}.",
                           "Start at least one llama.cpp backend behind the gateway.", group="AI & OCR")
    status = OK if st["online"] == len(backends) else WARN
    return CheckResult("gateway_backends", label, status, f"{st['online']} of {len(backends)} online: {names}{cpu}.",
                       "" if status == OK else "Restart the offline backend(s) for full capacity.", group="AI & OCR")


async def check_tts() -> CheckResult:
    label = "Voice (Kokoro TTS)"
    st = await probe_tts()
    if not st["configured"]:
        return CheckResult("tts", label, WARN, "TTS_BASE_URL is not set - the live coach uses the browser's built-in voice.",
                           "Run the kokoro-fastapi container and set TTS_BASE_URL (e.g. http://localhost:8880/v1).",
                           group="AI & OCR")
    if st["skipped"]:
        return CheckResult("tts", label, WARN, "Not probed (test environment).", group="AI & OCR")
    if st["ok"]:
        models = ", ".join(st["models"]) or st["model"]
        return CheckResult("tts", label, OK, f"Reachable in {st['latency_ms']} ms · {models} · voice {st['voice']}.",
                           group="AI & OCR")
    return CheckResult("tts", label, WARN, f"Unreachable ({st['error']}) - the browser's soft voice is used instead.",
                       "Start Kokoro: docker compose -f docker/kokoro.compose.yml up -d", group="AI & OCR")


# ===================================================================================== database & storage
async def check_database(db: AsyncSession) -> CheckResult:
    started = time.perf_counter()
    async with db.begin_nested():
        version = await db.scalar(text("SHOW server_version"))
    ms = (time.perf_counter() - started) * 1000
    status = OK if ms < 250 else WARN
    return CheckResult("database", "Database connectivity", status, f"PostgreSQL {version} · round trip {ms:.0f} ms.",
                       "" if status == OK else "The database is slow to answer - check its load and network.",
                       group="Database & storage")


async def check_pgvector(db: AsyncSession) -> CheckResult:
    async with db.begin_nested():
        rows = dict((await db.execute(text(
            "SELECT extname, extversion FROM pg_extension WHERE extname IN ('vector', 'citext', 'pg_trgm')"
        ))).all())
    missing = [e for e in ("vector", "citext", "pg_trgm") if e not in rows]
    if "vector" not in rows:
        return CheckResult("pgvector", "pgvector extension", FAIL, "The pgvector extension is missing.",
                           "Install pgvector and run CREATE EXTENSION vector (as a superuser).", group="Database & storage")
    detail = f"vector {rows['vector']}" + "".join(f" · {e} {rows[e]}" for e in ("citext", "pg_trgm") if e in rows)
    if missing:
        return CheckResult("pgvector", "pgvector extension", WARN, detail + " · missing: " + ", ".join(missing),
                           "CREATE EXTENSION for the missing extensions.", group="Database & storage")
    return CheckResult("pgvector", "pgvector extension", OK, detail, group="Database & storage")


async def check_redis() -> CheckResult:
    label = "Session store (Redis)"
    st = await probe_redis()
    if not st["configured"]:
        return CheckResult("redis", label, WARN, "REDIS_URL is not set - sessions live in PostgreSQL only (works, a little slower).",
                           "Optional: run Redis and set REDIS_URL=redis://localhost:6379/0.", group="Database & storage")
    if st["ok"]:
        return CheckResult("redis", label, OK, "Reachable - sessions are cached in Redis; PostgreSQL keeps the durable record.",
                           group="Database & storage")
    return CheckResult("redis", label, WARN,
                       f"Configured but unreachable ({st.get('error') or 'no answer'}) - sessions fall back to PostgreSQL.",
                       "Start Redis or fix REDIS_URL (password, host, port).", group="Database & storage")


async def check_demo_data(db: AsyncSession) -> CheckResult:
    label = "Demo data"
    n = await demo_accounts(db)
    production = get_settings().is_production
    if n and production:
        return CheckResult("demo_data", label, WARN, f"{n} demo account(s) (@{demo_domain()}) exist in production.",
                           "Delete the demo accounts and gyms before going live (SEED_DEMO_DATA=false).",
                           group="Database & storage")
    if n:
        return CheckResult("demo_data", label, OK, f"{n} demo account(s) loaded - credentials in var/demo_credentials.txt.",
                           group="Database & storage")
    if production:
        return CheckResult("demo_data", label, OK, "No demo accounts (as expected in production).", group="Database & storage")
    return CheckResult("demo_data", label, WARN, "No demo data - many screens look empty.",
                       "Run: python scripts/seed_demo.py (idempotent, clearly fictional data).", group="Database & storage")


async def check_uploads_writable() -> CheckResult:
    folder = get_settings().upload_dir

    def probe() -> None:
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / f".selfcheck-{uuid.uuid4().hex}.tmp"
        tmp.write_bytes(b"ok")
        tmp.unlink()

    try:
        await asyncio.to_thread(probe)
    except Exception as exc:  # noqa: BLE001
        return CheckResult("uploads", "Uploads folder writable", FAIL, f"{folder}: {exc.__class__.__name__}",
                           "Give the app user write access to the folder (VAR_DIR/uploads).", group="Database & storage")
    return CheckResult("uploads", "Uploads folder writable", OK, str(folder), group="Database & storage")


def check_assets() -> CheckResult:
    dist = APP_DIR / "static" / "dist"
    files = {name: dist / name for name in ("app.min.css", "app.min.js")}
    missing = [n for n, p in files.items() if not p.is_file() or p.stat().st_size == 0]
    if missing:
        return CheckResult("assets", "Built front-end assets", FAIL, "Missing: " + ", ".join(missing),
                           "Run: python scripts/build_assets.py", group="Database & storage")
    sizes = " · ".join(f"{n} {files[n].stat().st_size / 1024:.0f} KB" for n in files)
    return CheckResult("assets", "Built front-end assets", OK, sizes, group="Database & storage")


async def check_videos(db: AsyncSession) -> CheckResult:
    async with db.begin_nested():
        names = (await db.execute(select(Video.file_name).where(Video.is_active.is_(True)))).scalars().all()
    folder = APP_DIR / "static" / "videos"
    present = await asyncio.to_thread(lambda: sum(1 for n in names if n and (folder / n).is_file()))
    if not names:
        return CheckResult("videos", "Generated workout videos", WARN, "No pre-recorded videos in the catalogue yet.",
                           "Generate them (scripts/generate_videos.py) - videos are included in every plan.",
                           group="Database & storage")
    if present < len(names):
        return CheckResult("videos", "Generated workout videos", WARN,
                           f"{present} of {len(names)} video files exist in static/videos.",
                           "Re-run the video generator to create the missing files.", group="Database & storage")
    return CheckResult("videos", "Generated workout videos", OK, f"{len(names)} videos, all files present.",
                       group="Database & storage")


# ===================================================================================== runner
async def _guard(key: str, label: str, group: str, fn: Callable[[], Awaitable[CheckResult] | CheckResult]) -> CheckResult:
    try:
        result = fn()
        if inspect.isawaitable(result):
            result = await result
        return result  # type: ignore[return-value]
    except Exception as exc:  # noqa: BLE001 - a broken probe must not hide the others
        log.warning("self-check %s failed: %s", key, exc)
        return CheckResult(key, label, FAIL, f"The check itself failed: {exc.__class__.__name__}: {str(exc)[:160]}",
                           "See the server log for details.", group=group)


ORDER = ["secret_key", "superadmin_password", "debug", "cookies", "cors", "hosts", "smtp", "geoapify", "cashfree",
         "ai_gateway", "gateway_backends", "ai_docker", "anthropic", "ai_simulated", "tts", "tesseract", "ocr_engines",
         "database",
         "pgvector", "redis", "uploads", "assets", "videos", "demo_data"]


async def run_selfcheck(db: AsyncSession, *, probe: bool = False) -> list[CheckResult]:
    """All checks in display order. ``probe=True`` pings the AI providers live (slower)."""
    # pure / filesystem / network probes may run concurrently; database probes share one session -> sequential
    parallel = await asyncio.gather(
        _guard("secret_key", "Secret key strength", "Security", check_secret_key),
        _guard("debug", "Debug mode", "Security", check_debug),
        _guard("cookies", "Secure cookies & HTTPS", "Security", check_cookies),
        _guard("cors", "CORS origins", "Security", check_cors),
        _guard("hosts", "Allowed hosts", "Security", check_allowed_hosts),
        _guard("smtp", "E-mail (SMTP)", "Integrations", check_smtp),
        _guard("geoapify", "Maps & geocoding (Geoapify)", "Integrations", check_geoapify),
        _guard("cashfree", "Payments (Cashfree)", "Integrations", check_cashfree),
        _guard("tesseract", "Tesseract OCR binary", "AI & OCR", check_tesseract),
        _guard("ocr_engines", "OCR engines", "AI & OCR", check_ocr_engines),
        _guard("ai_gateway", "Gemma gateway (primary AI)", "AI & OCR", lambda: check_ai_gateway(probe)),
        _guard("gateway_backends", "Gemma gateway backends", "AI & OCR", check_gateway_backends),
        _guard("ai_docker", "Gemma in Docker (failsafe)", "AI & OCR", lambda: check_docker_ai(probe)),
        _guard("anthropic", "Claude (last-resort AI)", "AI & OCR", lambda: check_anthropic(probe)),
        _guard("ai_simulated", "Simulated AI (load tests)", "AI & OCR", check_simulated_ai),
        _guard("tts", "Voice (Kokoro TTS)", "AI & OCR", check_tts),
        _guard("redis", "Session store (Redis)", "Database & storage", check_redis),
        _guard("uploads", "Uploads folder writable", "Database & storage", check_uploads_writable),
        _guard("assets", "Built front-end assets", "Database & storage", check_assets),
    )
    sequential = [
        await _guard("superadmin_password", "Super-admin password", "Security", lambda: check_superadmin_password(db)),
        await _guard("database", "Database connectivity", "Database & storage", lambda: check_database(db)),
        await _guard("pgvector", "pgvector extension", "Database & storage", lambda: check_pgvector(db)),
        await _guard("videos", "Generated workout videos", "Database & storage", lambda: check_videos(db)),
        await _guard("demo_data", "Demo data", "Database & storage", lambda: check_demo_data(db)),
    ]
    results = list(parallel) + sequential
    order = {g: i for i, g in enumerate(GROUPS)}
    pos = {k: i for i, k in enumerate(ORDER)}
    results.sort(key=lambda r: (order.get(r.group, 99), pos.get(r.key, 99)))
    return results


def summarize(results: list[CheckResult]) -> dict[str, int]:
    counts = Counter(r.status for r in results)
    return {OK: counts.get(OK, 0), WARN: counts.get(WARN, 0), FAIL: counts.get(FAIL, 0), "total": len(results)}
