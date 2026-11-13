"""Talisman-style security headers for FastAPI/Starlette (pure ASGI), with a per-request CSP nonce.

Equivalent to Flask-Talisman's defaults: strict Content-Security-Policy (nonce-based scripts and
styles, no inline handlers), HSTS (when HTTPS), X-Frame-Options, nosniff, Referrer-Policy,
Permissions-Policy, COOP/CORP, and optional HTTP->HTTPS redirect.
"""

from __future__ import annotations

import secrets

from starlette.datastructures import URL
from starlette.responses import RedirectResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import get_settings
from app.security.net import header, is_trusted_proxy

CASHFREE_SCRIPT = "https://sdk.cashfree.com"
CASHFREE_CONNECT = ("https://sandbox.cashfree.com", "https://api.cashfree.com", "https://sdk.cashfree.com")


def build_csp(nonce: str, host: str | None) -> str:
    settings = get_settings()
    script_src = ["'self'", f"'nonce-{nonce}'"]
    connect_src = ["'self'"]
    frame_src = ["'none'"]
    form_action = ["'self'"]
    if host:
        connect_src += [f"ws://{host}", f"wss://{host}"]
    if settings.cashfree_enabled:
        script_src.append(CASHFREE_SCRIPT)
        connect_src += list(CASHFREE_CONNECT)
        frame_src = ["https://*.cashfree.com"]
        form_action += ["https://*.cashfree.com"]
    directives = {
        "default-src": ["'self'"],
        "script-src": script_src,
        "style-src": ["'self'", f"'nonce-{nonce}'"],
        "img-src": ["'self'", "data:", "blob:"],
        "font-src": ["'self'", "data:"],
        "media-src": ["'self'", "blob:"],
        "connect-src": connect_src,
        "frame-src": frame_src,
        "worker-src": ["'self'", "blob:"],
        "manifest-src": ["'self'"],
        "object-src": ["'none'"],
        "base-uri": ["'self'"],
        "form-action": form_action,
        "frame-ancestors": ["'none'"],
    }
    if settings.force_https:
        directives["upgrade-insecure-requests"] = []
    return "; ".join(f"{k} {' '.join(v)}".strip() for k, v in directives.items())


class TalismanMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        settings = get_settings()

        if settings.force_https and scope.get("scheme") == "http":
            peer = (scope.get("client") or ("", 0))[0]
            from_proxy = settings.trust_proxy_headers and is_trusted_proxy(peer)
            proto = header(scope, b"x-forwarded-proto") if from_proxy else None
            if proto != "https":
                url = URL(scope=scope).replace(scheme="https")
                await RedirectResponse(str(url), status_code=308)(scope, receive, send)
                return

        nonce = secrets.token_urlsafe(16)
        scope.setdefault("state", {})["csp_nonce"] = nonce
        host = header(scope, b"host")
        path: str = scope.get("path", "")

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                existing = {k.lower() for k, _ in headers}

                def add(name: str, value: str) -> None:
                    if name.encode() not in existing:
                        headers.append((name.encode(), value.encode("latin-1")))

                add("content-security-policy", build_csp(nonce, host))
                add("x-content-type-options", "nosniff")
                add("x-frame-options", "DENY")
                add("referrer-policy", "strict-origin-when-cross-origin")
                add(
                    "permissions-policy",
                    "geolocation=(self), camera=(self), microphone=(self), payment=(self), usb=(), "
                    "magnetometer=(), gyroscope=(), accelerometer=(), interest-cohort=()",
                )
                add("cross-origin-opener-policy", "same-origin")
                add("cross-origin-resource-policy", "same-origin")
                add("x-permitted-cross-domain-policies", "none")
                if settings.force_https or settings.cookie_secure:
                    add("strict-transport-security", "max-age=31536000; includeSubDomains")
                if not path.startswith(("/static/", "/media/public/", "/maps/tiles/")):
                    add("cache-control", "no-store")
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)
