"""Exception handlers: friendly HTML pages for browsers, JSON for /api and fetch() callers."""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import quote

from asyncpg.exceptions import QueryCanceledError
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect

from app.ai.client import AICancelled, AIRejected, AIUnavailableError
from app.core.deps import wants_json
from app.db import db_timeout_kind
from app.services.storage import UploadError

log = logging.getLogger("ironpulse.errors")

DB_TIMEOUT_MESSAGE = "This page is taking too long - please try again."
DB_TIMEOUT_RETRY_AFTER = 5  # seconds (Retry-After)
CLIENT_CLOSED_REQUEST = 499  # nginx's "client closed request": the status of a request whose client went away

TITLES = {
    400: "Something is not quite right",
    401: "Please sign in",
    403: "Access denied",
    404: "Page not found",
    405: "Not allowed",
    409: "Conflict",
    413: "Upload too large",
    422: "Invalid input",
    429: "Slow down",
    500: "Something went wrong",
    503: "Temporarily unavailable",
}

AI_CANCELLED_MESSAGE = "The AI request was stopped because the page was closed or the connection was lost."

_OUT_OF_RANGE = ("out of int64 range", "out of int32 range", "out of int16 range",  # asyncpg encoding a parameter
                 "int too large to convert")  # Python: OverflowError "Python int too large to convert to C long"


def id_out_of_range(exc: BaseException) -> bool:
    """Is ``exc`` (or what caused it) an integer too large for a database column - a crafted id, never a bug?"""
    seen: set[int] = set()
    stack: list[BaseException | None] = [exc]
    while stack:
        e = stack.pop()
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        if isinstance(e, (OverflowError, DBAPIError)) or type(e).__name__ == "DataError":
            text = str(e)
            if any(marker in text for marker in _OUT_OF_RANGE):
                return True
        stack += [getattr(e, "orig", None), e.__cause__, e.__context__]
    return False


async def _error_page(request: Request, status: int, message: str, *, title: str | None = None,
                      headers: dict[str, str] | None = None):
    from app.core.templating import render

    title = title or TITLES.get(status, "Error")
    context = {"status": status, "title": title, "message": message}
    await _page_safe_user(request)
    try:
        response = render(request, "errors/error.html", context, status_code=status)
    except Exception:  # template failure must never hide the original error
        log.exception("error page rendering failed")
        response = None
        if getattr(request.state, "user", None) is not None:  # the signed-in header is the part that reads the database
            request.state.user = None
            try:
                response = render(request, "errors/error.html", context, status_code=status)
            except Exception:  # noqa: BLE001 - logged above; JSON below
                log.debug("the signed-out error page failed too", exc_info=True)
        if response is None:
            response = JSONResponse({"error": title, "detail": message}, status_code=status)
    for key, value in (headers or {}).items():
        response.headers[key] = value
    return response


PAGE_USER_COLUMNS = ("id", "role", "full_name", "email", "username", "photo_key", "id_status")  # base.html, navbar
PAGE_USER_RELOAD_SECONDS = 2.0


async def _page_safe_user(request: Request) -> None:
    """Make ``request.state.user`` readable by the error page. A flush or commit that failed (a UniqueViolation, a race)
    has already rolled the request's transaction back and EXPIRED every object of its session - SQLAlchemy does that at
    once - and get_db then detached them: reading ``user.role`` raised DetachedInstanceError and the browser got raw
    JSON. The same account is reloaded in a short session of its own (by the identity of the object this request
    already authenticated - never another account); when that fails too, the page renders signed out."""
    user = getattr(request.state, "user", None)
    if user is None:
        return
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy.exc import NoInspectionAvailable

    try:
        state = sa_inspect(user)
    except NoInspectionAvailable:
        return  # not a mapped object (a test double): nothing a session could have expired
    if not any(column in state.unloaded for column in PAGE_USER_COLUMNS):
        return
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload

    from app.db import get_sessionmaker
    from app.models import User

    fresh = None
    if isinstance(user, User) and state.identity:
        # loaded as the session lookup loads it (app/security/sessions.py): the page reads user.customer_profile too
        query = select(User).options(joinedload(User.customer_profile)).where(User.id == state.identity[0])
        try:
            async with asyncio.timeout(PAGE_USER_RELOAD_SECONDS):  # the database may be what failed
                async with get_sessionmaker()() as db:
                    fresh = (await db.execute(query)).unique().scalar_one_or_none()
        except Exception:  # noqa: BLE001 - the error page must render whatever happens here
            log.debug("could not reload the user for an error page", exc_info=True)
    request.state.user = fresh


async def _load_page_user(request: Request) -> None:
    """The signed-in user for an HTML error page raised before the app-wide ``load_user`` ran: csrf_protect runs
    first (app/main.py), so its 403 page would otherwise show a signed-out header. One short session of its own."""
    if getattr(request.state, "user_loaded", False) or request.scope.get("type") != "http":
        return
    from app.core.deps import load_user
    from app.db import get_sessionmaker

    try:
        async with get_sessionmaker()() as db:
            await load_user(request, db)
    except Exception:  # noqa: BLE001 - the error page must render whatever happens here
        log.debug("could not load the user for an error page", exc_info=True)


def install_error_handlers(app: FastAPI) -> None:
    from app.security.csrf import CSRFError

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        detail = exc.detail if isinstance(exc.detail, str) else TITLES.get(exc.status_code, "Error")
        if exc.status_code == 401 and not wants_json(request):
            nxt = quote(request.url.path + (f"?{request.url.query}" if request.url.query else ""), safe="")
            return RedirectResponse(f"/auth/login?next={nxt}", status_code=303)
        if wants_json(request):
            return JSONResponse({"error": TITLES.get(exc.status_code, "Error"), "detail": detail},
                                status_code=exc.status_code, headers=getattr(exc, "headers", None))
        if isinstance(exc, CSRFError):  # raised before load_user ran (app/main.py runs csrf_protect first)
            await _load_page_user(request)
        return await _error_page(request, exc.status_code, detail)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        errors = [
            {"field": ".".join(str(p) for p in e.get("loc", [])[1:]), "message": e.get("msg", "invalid")}
            for e in exc.errors()
        ]
        if wants_json(request):
            return JSONResponse({"error": "Invalid input", "fields": errors}, status_code=422)
        msg = "; ".join(f"{e['field'] or 'input'}: {e['message']}" for e in errors[:4])
        return await _error_page(request, 422, msg or "Some fields are missing or invalid.")

    @app.exception_handler(UploadError)
    async def upload_error(request: Request, exc: UploadError):
        if wants_json(request):
            return JSONResponse({"error": "Upload rejected", "detail": str(exc)}, status_code=400)
        return await _error_page(request, 400, str(exc))

    # Starlette picks the handler of the most specific class in the exception's MRO, so the two AIUnavailableError
    # subclasses below get their own answers and every other AIUnavailableError keeps the generic 503.
    @app.exception_handler(AIRejected)
    async def ai_rejected(request: Request, exc: AIRejected):
        """Refused before any compute (quota, cooldown, duplicate, spam, queue full): say why and when to retry.
        IP.api exposes ``code`` and ``retry_after``; clients never auto-retry a 429."""
        if wants_json(request):
            return JSONResponse(exc.payload(), status_code=exc.status, headers=exc.headers())
        return await _error_page(request, exc.status, exc.message, title=exc.title, headers=exc.headers())

    @app.exception_handler(AICancelled)
    async def ai_cancelled(request: Request, exc: AICancelled):
        """The requester left mid-call (usually nobody reads this answer any more)."""
        if wants_json(request):
            return JSONResponse({"error": "Cancelled", "detail": AI_CANCELLED_MESSAGE, "code": "cancelled"},
                                status_code=503)
        return await _error_page(request, 503, AI_CANCELLED_MESSAGE, title="Request stopped")

    @app.exception_handler(AIUnavailableError)
    async def ai_unavailable(request: Request, exc: AIUnavailableError):
        msg = "The AI assistant is unavailable right now (all AI providers are offline). Pre-recorded videos and all non-AI features still work."
        if wants_json(request):
            return JSONResponse({"error": "AI unavailable", "detail": msg, "ai_available": False}, status_code=503)
        return await _error_page(request, 503, msg)

    async def not_found_for_out_of_range(request: Request, exc: Exception):
        """An id no 64-bit column can hold (``/profile/9223372036854775808``, a 5,000-digit form field): asyncpg's
        "value out of int64 range" (a ``DBAPIError``) or Python's OverflowError converting it. Nothing can match it, so
        it is answered like any unknown id - 404, logged in one line - never a 500 with a traceback."""
        route = getattr(request.scope.get("route"), "path", None) or "-"
        log.info("out-of-range id on %s %s (route %s): %s", request.method, request.url.path, route,
                 (str(exc).splitlines() or [""])[0][:200])
        if wants_json(request):
            return JSONResponse({"error": TITLES[404], "detail": "Not found."}, status_code=404)
        return await _error_page(request, 404, "Not found.")

    async def overflow(request: Request, exc: Exception):
        if request.scope.get("type") == "http" and id_out_of_range(exc):
            return await not_found_for_out_of_range(request, exc)
        raise exc  # any other overflow is a bug: the generic 500 handler logs it, exactly as before

    app.add_exception_handler(OverflowError, overflow)

    async def db_timeout(request: Request, exc: Exception):
        """A query cancelled by ``statement_timeout`` or no pooled connection free within ``pool_timeout``
        (app/db.py): a 503 the visitor can retry instead of a 500. An id out of the 64-bit range: the 404 above. Any
        other database error goes on to the generic handler below, exactly as before."""
        kind = db_timeout_kind(exc)
        if kind is None and request.scope.get("type") == "http" and id_out_of_range(exc):
            return await not_found_for_out_of_range(request, exc)
        if kind is None or request.scope.get("type") != "http":  # a WebSocket cannot take an error page
            raise exc
        route = getattr(request.scope.get("route"), "path", None) or "-"
        log.warning("database %s on %s %s (route %s): %s", "statement timeout" if kind == "statement" else "pool timeout",
                    request.method, request.url.path, route, (str(exc).splitlines() or [""])[0][:200])
        headers = {"Retry-After": str(DB_TIMEOUT_RETRY_AFTER)}
        if wants_json(request):
            return JSONResponse({"error": TITLES[503], "detail": DB_TIMEOUT_MESSAGE, "code": "timeout"},
                                status_code=503, headers=headers)
        return await _error_page(request, 503, DB_TIMEOUT_MESSAGE, title="Taking too long", headers=headers)

    for exc_class in (DBAPIError, PoolTimeoutError, QueryCanceledError):
        app.add_exception_handler(exc_class, db_timeout)

    @app.exception_handler(ClientDisconnect)
    async def client_gone(request: Request, exc: ClientDisconnect):
        """The client went away while its request body was still being read - under overload, a client that gave up
        waiting (the stress test's ``POST /shop/cart/add``): nobody is left to answer and nothing is broken. One DEBUG
        line, no traceback; the request's transaction was rolled back by ``get_db`` like for any error. The 499 below
        never reaches anyone (uvicorn drops what is sent to a closed connection, and logs no access line for it)."""
        route = getattr(request.scope.get("route"), "path", None) or "-"
        log.debug("client went away during %s %s (route %s)", request.method, request.url.path, route)
        return Response(status_code=CLIENT_CLOSED_REQUEST)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        if wants_json(request):
            return JSONResponse({"error": "Server error", "detail": "Unexpected error - it has been logged."},
                                status_code=500)
        return await _error_page(request, 500, "An unexpected error occurred. It has been logged; please try again.")
