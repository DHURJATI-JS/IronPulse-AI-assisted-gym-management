"""IronPulse FastAPI application factory.

Middleware order (outermost first):
security-header fallback (around Starlette's own 500 handler) -> OwnerContext -> TrustedHost -> Talisman (CSP nonce +
security headers) -> SecurityGate (bans, rate limits, attack scanning) -> CSRF cookie -> CORS -> submit guard -> DB
release -> routes (app-wide dependencies: csrf_protect, load_user). The fallback gives the security headers to the
responses Talisman never sees: the Host check's 400, Talisman's own HTTP->HTTPS redirect and the 500 page.

csrf_protect runs BEFORE load_user: it may read a whole form body (a photo upload from a phone takes seconds), and
load_user's query opens the request's transaction - read after it, the upload kept a pooled connection idle in that
transaction (killed after DB_IDLE_TX_TIMEOUT_MS). The DB release middleware gives the request's connection back to
the pool when the response starts, not after the last byte reached the client (app/db.py release_request_session).
Static files carry Cache-Control (immutable for versioned URLs: app/core/static.py).
"""

from __future__ import annotations

import importlib
import logging
import os
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.ai.owner import OwnerContextMiddleware
from app.config import APP_DIR, get_settings
from app.core.deps import load_user
from app.core.errors import install_error_handlers
from app.core.static import CachedStaticFiles
from app.core.submit_guard import SubmitGuardMiddleware
from app.security.csrf import CSRFCookieMiddleware, csrf_protect
from app.security.gate import SecurityGateMiddleware
from app.security.net import header
from app.security.talisman import TalismanMiddleware, build_csp

log = logging.getLogger("ironpulse")

# A response produced before Talisman ran (the Host check's 400, the HTTPS redirect) has a fixed text body or none:
# nothing may load, frame it or read it.
FALLBACK_CSP = "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"


class SecurityHeadersFallback:
    """Outermost ASGI layer (``IronPulseApp.build_middleware_stack``): every HTTP response leaves with the security
    headers - also the ones no inner middleware decorated. TrustedHostMiddleware answers a bad Host before Talisman
    runs; Talisman answers its HTTP->HTTPS redirect before installing its header wrapper; Starlette sends the 500 page
    from ServerErrorMiddleware, outside every user middleware. A response that already has a Content-Security-Policy
    (everything Talisman decorated) passes untouched; any header already set is kept."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                present = {k.lower() for k, _ in headers}
                if b"content-security-policy" not in present:
                    message["headers"] = headers + [(k, v) for k, v in _fallback_headers(scope) if k not in present]
            await send(message)

        await self.app(scope, receive, send_wrapper)


def _fallback_headers(scope: Scope) -> list[tuple[bytes, bytes]]:
    s = get_settings()
    nonce = (scope.get("state") or {}).get("csp_nonce")
    # past Talisman (the 500 page): its own policy and nonce, so the page's styles and scripts still load
    csp = build_csp(nonce, header(scope, b"host")) if nonce else FALLBACK_CSP
    values = [
        ("content-security-policy", csp),
        ("x-content-type-options", "nosniff"),
        ("x-frame-options", "DENY"),
        ("referrer-policy", "strict-origin-when-cross-origin"),
        ("cross-origin-opener-policy", "same-origin"),
        ("cross-origin-resource-policy", "same-origin"),
        ("x-permitted-cross-domain-policies", "none"),
        ("cache-control", "no-store"),
    ]
    if s.force_https or s.cookie_secure:
        values.append(("strict-transport-security", "max-age=31536000; includeSubDomains"))
    return [(k.encode(), v.encode("latin-1")) for k, v in values]


class IronPulseApp(FastAPI):
    """FastAPI whose middleware stack is wrapped in ``SecurityHeadersFallback`` - outside Starlette's
    ServerErrorMiddleware, which a ``Middleware(...)`` entry can never be."""

    def build_middleware_stack(self) -> ASGIApp:
        return SecurityHeadersFallback(super().build_middleware_stack())


class ReleaseDBOnResponseMiddleware:
    """Innermost middleware: when the response starts, the request's database session ends its transaction and its
    connection goes back to the pool (``app.db.release_request_session``). FastAPI ends the ``get_db`` dependency only
    after the whole body was sent - to a slow client that kept a pooled connection idle in a transaction."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        from app.db import release_request_session

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                await release_request_session(scope)
            await send(message)

        await self.app(scope, receive, send_wrapper)

# Every feature router module. Each exposes ``router = APIRouter()``.
ROUTER_MODULES = (
    "app.routers.core",
    "app.routers.public",
    "app.routers.auth",
    "app.routers.account",
    "app.routers.identity",
    "app.routers.owner_gyms",
    "app.routers.maps",
    "app.routers.owner_staff",
    "app.routers.staff_portal",
    "app.routers.owner_finance",
    "app.routers.owner_plans",
    "app.routers.checkout",
    "app.routers.customer_health",
    "app.routers.customer_coach",
    "app.routers.assistant",
    "app.routers.owner_customers",
    "app.routers.chat",
    "app.routers.superadmin",
    "app.routers.superadmin_loadtest",
    "app.routers.public_loadtest",  # GET /load-test: the public, curated load-test summary (PUBLIC_LOADTEST_PAGE)
    "app.routers.superadmin_trust",
    "app.routers.superadmin_login",  # /superadmin/login: the only sign-in of platform admins (app/security/admin_login.py)
    "app.routers.superadmin_governance",  # /superadmin/governance: bans, gym deletion, settlements (services/governance.py)
    "app.routers.superadmin_insights",  # /superadmin/insights: the site overview and "Ask the AI"
    "app.routers.live_coach",
    "app.routers.shop",
    "app.routers.owner_shop",
    "app.routers.profiles",
    "app.routers.auth_extras",
    "app.routers.plan_ai",
)

# Modules that only register background jobs / periodic tasks / ws handlers.
SIDE_EFFECT_MODULES = (
    "app.core.nav",
    "app.services.accounts",
)


def _configure_logging() -> None:
    s = get_settings()
    logging.basicConfig(
        level=logging.DEBUG if s.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.db import create_all, dispose_engine, session_scope, startup_lock
    from app.services.bootstrap import bootstrap
    from app.services.presence import presence
    from app.services.realtime import hub
    from app.services.worker import runner

    s = get_settings()
    s.var_dir.mkdir(parents=True, exist_ok=True)
    if not s.is_test:
        # one process at a time: workers starting together raced each other's CREATE TABLE / CREATE INDEX / super-admin
        # INSERT into UniqueViolations, and one worker failing to start stops uvicorn's whole multi-worker server
        async with startup_lock():
            if s.auto_create_tables:
                await create_all()
            async with session_scope() as db:
                await bootstrap(db)
    if s.run_background_jobs and not s.is_test:
        runner.start()
    if getattr(s, "realtime_pg_bridge", False):
        # LISTEN needs a session-pooled (direct) connection: behind PgBouncer's transaction pooling DATABASE_URL cannot
        # carry it, so REALTIME_PG_BRIDGE_URL (when set) names a direct one
        await hub.start_bridge(s.realtime_pg_bridge_url or s.database_url)
    scheme = "https" if os.environ.get("IRONPULSE_LAN") == "1" else "http"  # LAN mode (scripts/lan.py) serves TLS itself
    log.info("%s ready on %s://%s:%s (env=%s)", s.app_name, scheme, s.host, s.port, s.environment)
    try:
        yield
    finally:
        presence.begin_shutdown()  # sockets closing from here on are a restart, not people leaving
        await runner.stop()
        await presence.drain(timeout=2.0)  # presence callbacks already running finish before the engine goes
        await hub.stop_bridge()
        await dispose_engine()


def create_app() -> FastAPI:
    _configure_logging()
    s = get_settings()
    middleware = [
        Middleware(OwnerContextMiddleware),  # outermost: no AI owner leaks between requests
        Middleware(TrustedHostMiddleware, allowed_hosts=s.allowed_hosts + (["testserver"] if s.is_test else [])),
        Middleware(TalismanMiddleware),
        Middleware(SecurityGateMiddleware),
        Middleware(CSRFCookieMiddleware),
        Middleware(
            CORSMiddleware,
            allow_origins=s.cors_origins,  # empty = same-origin only
            allow_credentials=True,
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type", "X-CSRF-Token", "X-Page-Id", "X-Submit-Id"],  # X-Submit-Id: IP.api
            max_age=600,
        ),
        Middleware(SubmitGuardMiddleware),  # double-submit protection: one effect per (browser, submit id) - app/core/submit_guard.py
        Middleware(ReleaseDBOnResponseMiddleware),  # innermost
    ]
    app = IronPulseApp(
        title=s.app_name,
        lifespan=lifespan,
        middleware=middleware,
        # csrf_protect first: a form body it reads is read before load_user opens the request's transaction
        dependencies=[Depends(csrf_protect), Depends(load_user)],
        docs_url="/api/docs" if s.debug else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if s.debug else None,
    )
    install_error_handlers(app)
    app.mount("/static", CachedStaticFiles(directory=str(APP_DIR / "static")), name="static")
    for name in SIDE_EFFECT_MODULES:
        importlib.import_module(name)
    for name in ROUTER_MODULES:
        module = importlib.import_module(name)
        app.include_router(module.router)
    return app


app = create_app()
