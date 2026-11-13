"""Shared pytest fixtures.

Each test runs against a real PostgreSQL + pgvector database (``TEST_DATABASE_URL``; parallel work
packages use their own database, e.g. ``.../ironpulse_test_5``). Tables are created once per session
and TRUNCATEd after every test. Background workers are disabled; run jobs explicitly with
``await run_jobs()``. AI providers are replaced with fakes via the ``fake_ai`` / ``ai_down`` fixtures.
"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator
from typing import Any

# ---- environment must be prepared BEFORE anything from ``app`` is imported --------------------
os.environ["ENVIRONMENT"] = "test"
os.environ["RUN_BACKGROUND_JOBS"] = "false"
os.environ.setdefault("RATE_LIMIT_WHITELIST", "127.0.0.1")
os.environ.setdefault("PUBLIC_BASE_URL", "http://testserver")
os.environ["AI_PROVIDER_ORDER"] = os.environ.get("AI_PROVIDER_ORDER", "gateway,docker,anthropic")
_test_db = os.environ.get("TEST_DATABASE_URL")
if not _test_db:
    from app.config import Settings  # noqa: E402

    _test_db = Settings().test_database_url  # type: ignore[call-arg]
if not _test_db or "test" not in _test_db.rsplit("/", 1)[-1]:
    raise RuntimeError("Refusing to run tests: TEST_DATABASE_URL must point at a *test* database")
os.environ["DATABASE_URL"] = _test_db
os.environ["VAR_DIR"] = os.environ.get("VAR_DIR") or os.path.join(os.path.dirname(__file__), "..", "var", "test-" + _test_db.rsplit("/", 1)[-1])

# ---- Redis: never the dev servers' database 0 or the load test's 9 ---------------------------------------------------
# Every test that goes through the security gate writes rate-limit / threat keys, and the shared store's reset SCANs and
# UNLINKs this test database's namespace after each test: on the .env REDIS_URL that was the dev servers' db 0. Tests use
# TEST_REDIS_DB (default 15) of the same server; an explicit REDIS_URL naming another database is kept.
FORBIDDEN_REDIS_DBS = ("0", "9")


def _test_redis_url(url: str | None, *, explicit: bool) -> str | None:
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    if not url:
        return url
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    unix = parts.scheme == "unix"
    current = (query.get("db") if unix else (parts.path or "/").strip("/")) or "0"
    if explicit and current not in FORBIDDEN_REDIS_DBS:
        return url
    wanted = os.environ.get("TEST_REDIS_DB", "15").strip()
    if not wanted.isdigit() or wanted in FORBIDDEN_REDIS_DBS:
        raise RuntimeError("Refusing to run tests: TEST_REDIS_DB must be a Redis database other than 0 (dev servers) "
                           "and 9 (load test)")
    if unix:
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode({**query, "db": wanted}), parts.fragment))
    return urlunsplit((parts.scheme, parts.netloc, f"/{wanted}", parts.query, parts.fragment))


if "REDIS_URL" in os.environ:
    _redis_url = _test_redis_url(os.environ["REDIS_URL"], explicit=True)
else:
    from app.config import Settings  # noqa: E402

    _redis_url = _test_redis_url(Settings().redis_url, explicit=False)  # type: ignore[call-arg]
if _redis_url:
    os.environ["REDIS_URL"] = _redis_url

import httpx  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.config import get_settings, reset_settings_cache  # noqa: E402

reset_settings_cache()
from app import db as dbmod  # noqa: E402

dbmod.configure_engine(_test_db, poolclass=NullPool)

from app.ai.client import FakeProvider, ai  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402
from app.models import User, UserSession  # noqa: E402
from app.security.bans import ban_registry, security_events, threat_tracker  # noqa: E402
from app.security.crypto import new_token, token_hash  # noqa: E402
from app.security.csrf import compute_csrf_token, csrf_cookie_name  # noqa: E402
from app.security.ratelimit import limiter  # noqa: E402
from app.security.sessions import session_cookie_name  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
async def _schema() -> AsyncIterator[None]:
    await dbmod.drop_all()
    await dbmod.create_all()
    get_settings().var_dir.mkdir(parents=True, exist_ok=True)
    yield
    await dbmod.dispose_engine()


@pytest.fixture(autouse=True)
async def _clean_state() -> AsyncIterator[None]:
    from app.core.state import reset_all

    ban_registry.clear()
    threat_tracker.reset()
    limiter.reset()
    security_events.events.clear()
    security_events.bans.clear()
    ai.use_providers(None)
    await reset_all()  # AI guard counters, AI queue, presence timers, running job tasks (app/core/state.py)
    yield
    ai.use_providers(None)
    await reset_all()
    import app.models  # noqa: F401

    tables = ", ".join(f'"{t.name}"' for t in reversed(dbmod.Base.metadata.sorted_tables))
    async with dbmod.get_engine().begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture
async def db() -> AsyncIterator[Any]:
    async with dbmod.get_sessionmaker()() as session:
        yield session


@pytest.fixture
def app():
    return fastapi_app


def make_client(ip: str = "127.0.0.1") -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=fastapi_app, client=(ip, 51234))
    return httpx.AsyncClient(transport=transport, base_url="http://testserver", follow_redirects=False,
                             headers={"User-Agent": "pytest-ironpulse/1.0"})


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with make_client() as c:
        yield c


@pytest.fixture
async def anon_client_factory():
    clients: list[httpx.AsyncClient] = []

    def factory(ip: str = "203.0.113.10") -> httpx.AsyncClient:
        c = make_client(ip)
        clients.append(c)
        return c

    yield factory
    for c in clients:
        await c.aclose()


# ------------------------------------------------------------------ CSRF / auth helpers
async def prime(client: httpx.AsyncClient) -> None:
    """Make sure the client holds a CSRF cookie."""
    if csrf_cookie_name() not in client.cookies:
        await client.get("/healthz")


async def csrf_token(client: httpx.AsyncClient) -> str:
    await prime(client)
    return compute_csrf_token(client.cookies.get(csrf_cookie_name()), client.cookies.get(session_cookie_name()))


async def post_form(client: httpx.AsyncClient, url: str, data: dict[str, Any] | None = None,
                    files: dict[str, Any] | None = None, **kwargs: Any) -> httpx.Response:
    data = dict(data or {})
    data["csrf_token"] = await csrf_token(client)
    return await client.post(url, data=data, files=files, **kwargs)


async def api(client: httpx.AsyncClient, method: str, url: str, json: Any = None, **kwargs: Any) -> httpx.Response:
    headers = {"X-CSRF-Token": await csrf_token(client), "Accept": "application/json"}
    headers.update(kwargs.pop("headers", {}) or {})
    return await client.request(method, url, json=json, headers=headers, **kwargs)


async def login_as(client: httpx.AsyncClient, user: User) -> None:
    """Log in by creating a server-side session directly (independent of the login page)."""
    from datetime import timedelta

    from app.core.utils import utcnow

    token = new_token(32)
    async with dbmod.get_sessionmaker()() as s:
        s.add(UserSession(id=token_hash(token), user_id=user.id, expires_at=utcnow() + timedelta(hours=2),
                          ip="127.0.0.1", user_agent="pytest"))
        await s.commit()
    client.cookies.set(session_cookie_name(), token)


def meta_csrf(html: str) -> str | None:
    m = re.search(r'<meta name="csrf-token" content="([^"]+)"', html)
    return m.group(1) if m else None


# ------------------------------------------------------------------ AI fakes
@pytest.fixture
def fake_ai():
    """Usage: provider = fake_ai(responses=['{"a": 1}']) or fake_ai(handler=fn)."""

    def install(responses: list[str] | None = None, handler: Any = None, name: str = "gateway") -> FakeProvider:
        p = FakeProvider(name, responses=responses, handler=handler)
        ai.use_providers([p])
        return p

    yield install
    ai.use_providers(None)


@pytest.fixture
def ai_down():
    ai.use_providers([FakeProvider("gateway", fail=True), FakeProvider("docker", fail=True)])
    yield
    ai.use_providers(None)


async def run_jobs(limit: int = 50) -> int:
    from app.services.jobs import run_due_jobs

    total = 0
    while True:
        n = await run_due_jobs(limit=limit)
        total += n
        if n < limit:
            return total
