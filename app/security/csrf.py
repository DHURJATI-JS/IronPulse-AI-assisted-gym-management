"""CSRF protection: signed double-submit token bound to the CSRF cookie *and* the login session.

* ``CSRFCookieMiddleware`` guarantees every visitor has a random ``ip_csrf`` cookie.
* Templates embed ``csrf_token`` (hidden input / <meta>); JS sends it as ``X-CSRF-Token``.
* ``csrf_protect`` (an app-wide dependency) rejects unsafe requests without a valid token and with a
  foreign Origin header. Starlette caches the parsed form, so endpoints can still read Form fields.

Outdated pages are not attacks (hunt F15). A token is bound to the sign-in state its page was rendered in, so a page
left open while the browser signed in, out or in again in another tab - or renewed its session with a password change
- sends a token that no longer matches:
* a signed-out page (sign-in, sign-up, password reset, invitations, the account chooser) submitted after signing in
  in another tab is accepted with its signed-out token - it is still an HMAC over this browser's HttpOnly CSRF cookie,
  so a forged sign-in stays impossible - and its handler answers the signed-in browser ("already signed in", a
  re-sign-in);
* any other page of this app (its own Origin / ``Sec-Fetch-Site: same-origin``) gets a 303 back to the page it came from
  with "This page was out of date ... nothing was changed" (a fetch() call: a 403 saying so) - never the old 403 page
  whose "reload the page" re-posted the same stale token;
* neither ever adds threat points. Before, each stale submit was 3 points: five of them banned the member's address
  for 2 hours. A cross-site failure (another site's Origin / ``Sec-Fetch-Site``) is stored already reviewed and never
  scored either - a page on another site can make a visitor's browser send any number of them (F39). Only failures
  without browser metadata (scripts, tools) still count toward a ban.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import get_settings
from app.security.net import SAME_ORIGIN, UNKNOWN, origin_allowed, request_site
from app.security.sessions import cookie_name, session_cookie_name

SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
CSRF_EXEMPT_PATHS = {"/payments/cashfree/webhook"}
CSRF_COOKIE_BASE = "ip_csrf"
# pages rendered for signed-out visitors: a token of the signed-out state is accepted there after a sign-in elsewhere
SIGNED_OUT_FORMS = frozenset({
    "/auth/login", "/auth/signup", "/auth/forgot", "/auth/forgot-username", "/auth/reset", "/auth/invite",
    "/auth/staff-invite", "/auth/remembered", "/auth/remembered/forget",
})
STALE_STATE_KEY = "csrf_stale_redirect"  # scope["state"]: where CSRFCookieMiddleware sends an outdated form instead
CROSS_SITE_MESSAGE = "Cross-site request blocked."
STALE_MESSAGE = ("This page was out of date (you signed in or out in another tab, or changed your password), so nothing "
                 "was changed. Please try again.")
FAILED_MESSAGE = ("This request couldn't be verified, so nothing was changed. Go back to the previous page, reload it "
                  "and try again.")


def csrf_cookie_name() -> str:
    return cookie_name(CSRF_COOKIE_BASE)


def _read_cookie(scope: Scope, name: str) -> str | None:
    for k, v in scope.get("headers") or []:
        if k == b"cookie":
            jar = SimpleCookie()
            try:
                jar.load(v.decode("latin-1"))
            except Exception:
                return None
            if name in jar:
                return jar[name].value
    return None


def _stale_redirect_start(target: str) -> Message:
    """The response start of "back to the page you were on" with the out-of-date notice (a flash message)."""
    from app.core.templating import redirect

    response = redirect(target, STALE_MESSAGE, "warning")
    headers = [(k, v) for k, v in response.raw_headers if k != b"cache-control"] + [(b"cache-control", b"no-store")]
    return {"type": "http.response.start", "status": 303, "headers": headers}


class CSRFCookieMiddleware:
    """Gives every visitor a CSRF cookie - and turns the 403 of an outdated form of this app (``csrf_protect`` marks it
    in ``scope["state"]``) into a 303 back to the page it came from, with a notice. Nothing ran: the CSRF check stops
    the request before its handler."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        name = csrf_cookie_name()
        secret = _read_cookie(scope, name)
        new_secret = None
        if not secret or len(secret) < 32 or len(secret) > 64:
            secret = new_secret = secrets.token_urlsafe(32)
        state = scope.setdefault("state", {})
        state["csrf_secret"] = secret
        rewriting = False

        async def send_wrapper(message: Message) -> None:
            nonlocal rewriting
            if message["type"] == "http.response.start":
                target = state.get(STALE_STATE_KEY)
                if target and message.get("status") == 403:
                    rewriting = True
                    message = _stale_redirect_start(target)
                if new_secret:
                    settings = get_settings()
                    parts = [f"{name}={new_secret}", "Path=/", "HttpOnly", "SameSite=Lax", f"Max-Age={60 * 60 * 24 * 30}"]
                    if settings.cookie_secure:
                        parts.append("Secure")
                    headers = list(message.get("headers") or [])
                    headers.append((b"set-cookie", "; ".join(parts).encode("latin-1")))
                    message["headers"] = headers
            elif message["type"] == "http.response.body" and rewriting:
                if message.get("more_body", False):
                    return  # the 403 page's body is dropped: the redirect has none
                message = {"type": "http.response.body", "body": b"", "more_body": False}
            await send(message)

        await self.app(scope, receive, send_wrapper)


def compute_csrf_token(csrf_secret: str, session_token: str | None) -> str:
    """Pure function: token = HMAC(secret_key, csrf_cookie | hash(session_cookie))."""
    binding = hashlib.sha256(session_token.encode()).hexdigest()[:24] if session_token else "anon"
    key = hmac.new(get_settings().secret_key.encode(), b"ironpulse:csrf", hashlib.sha256).digest()
    mac = hmac.new(key, f"{csrf_secret}|{binding}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")


def get_csrf_token(request: Request) -> str:
    secret = request.scope.get("state", {}).get("csrf_secret") or request.cookies.get(csrf_cookie_name()) or ""
    return compute_csrf_token(secret, request.cookies.get(session_cookie_name()))


def signed_out_csrf_token(request: Request) -> str:
    """The token for a page whose response clears the session cookie (the sign-in page telling a device it was signed
    out elsewhere): bound to the signed-out state the browser is in from then on, so its next POST validates."""
    secret = request.scope.get("state", {}).get("csrf_secret") or request.cookies.get(csrf_cookie_name()) or ""
    return compute_csrf_token(secret, None)


def _origin_ok(request: Request) -> bool:
    origin = request.headers.get("origin")
    if origin is None:
        return True
    origin = origin.strip()
    if not origin or origin == "null":
        return False  # literal "null" origins (sandboxed iframes, opaque pages) are rejected
    return origin_allowed(origin, request.headers.get("host"))


class CSRFError(HTTPException):
    def __init__(self, detail: str = FAILED_MESSAGE) -> None:
        super().__init__(status_code=403, detail=detail)


def _same(sent: str, expected: str) -> bool:
    return hmac.compare_digest(sent.encode(), expected.encode())


async def _sent_token(request: Request) -> tuple[str | None, bool]:
    """(the token the request carries, whether it came from a classic form field)."""
    sent = request.headers.get("x-csrf-token")
    if sent:
        return sent, False
    ctype = request.headers.get("content-type", "")
    if ctype.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
        try:
            form = await request.form()
        except Exception:
            form = None
        value: Any = form.get("csrf_token") if form is not None else None
        if isinstance(value, str) and value:
            return value, True
    return None, False


def _wants_page(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return not request.url.path.startswith("/api/") and not ("application/json" in accept and "text/html" not in accept)


def _back_to(request: Request) -> str:
    """Where an outdated form goes back to: the page it was on (its same-origin Referer: path + query), else home."""
    from app.core.utils import is_safe_redirect

    try:
        parts = urlsplit(request.headers.get("referer") or "")
    except ValueError:
        return "/"
    ours = {request.headers.get("host", ""), urlsplit(get_settings().public_base_url or "").netloc}
    if parts.netloc and parts.netloc not in ours:
        return "/"
    target = (parts.path or "") + (f"?{parts.query}" if parts.query else "")
    if len(target) > 1000 or any(ch in target for ch in "\r\n\t") or not is_safe_redirect(target):
        return "/"
    return target


async def csrf_protect(conn: HTTPConnection) -> None:
    """App-wide dependency (HTTP only; WebSockets are protected by the Origin check in /ws)."""
    if conn.scope.get("type") != "http" or not isinstance(conn, Request):
        return
    request = conn
    if request.method in SAFE_METHODS or request.url.path in CSRF_EXEMPT_PATHS:
        return
    site = request_site(request.scope)
    if not _origin_ok(request):
        _report(request, "foreign Origin header", site)
        raise CSRFError(CROSS_SITE_MESSAGE)
    sent, from_form = await _sent_token(request)
    if sent and _same(sent, get_csrf_token(request)):
        return
    if (sent and request.url.path in SIGNED_OUT_FORMS and request.cookies.get(session_cookie_name())
            and _same(sent, signed_out_csrf_token(request))):
        return  # a signed-out page sent after signing in in another tab: its handler answers the signed-in browser
    if site == SAME_ORIGIN:
        # one of this app's pages with a token of an earlier sign-in state: never threat points (module docstring)
        if sent and from_form and _wants_page(request):
            request.state.csrf_stale_redirect = _back_to(request)  # STALE_STATE_KEY: the middleware redirects
        raise CSRFError(STALE_MESSAGE if sent else FAILED_MESSAGE)
    _report(request, "missing or invalid CSRF token", site)
    raise CSRFError()


def _report(request: Request, detail: str, site: str) -> None:
    """A CSRF failure as a security event: scored (3 threat points) only when no browser says who made the request
    (UNKNOWN: a script or a tool); a cross-site one is stored already reviewed and never scored - a page on another site
    can make a visitor's browser send any number of them; this app's own outdated pages are not reported."""
    if site == SAME_ORIGIN:
        return
    try:
        from app.security.bans import security_events
        from app.models.enums import ThreatCategory

        security_events.record_from_request(request, ThreatCategory.CSRF, 2, detail, attributable=site == UNKNOWN)
    except Exception:  # never let reporting break request handling
        pass
