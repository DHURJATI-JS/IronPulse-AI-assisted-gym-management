"""SecurityGateMiddleware - the first line of defence (pure ASGI, no DB access on the hot path).

For every HTTP request:
1. banned IP?            -> 403 ban page / JSON (with the expiry time)
2. body too large?       -> 413
3. rate limits           -> 429 + Retry-After (repeated violations escalate to a ban)
4. signature scanning    -> path, query, user-agent, and small urlencoded/JSON bodies
5. 404 flood detection   -> directory brute-forcing escalates to a ban

The rate limits, the threat scores and the rejection / 404 windows behind steps 3-5 are shared by every worker through
Redis when REDIS_URL is set (app/security/ratelimit.py, app/security/bans.py): N uvicorn workers enforce ONE allowance
per address, and an allowed request costs one Lua script round trip. Without Redis - or while it fails or is slower
than RATE_LIMIT_REDIS_TIMEOUT_MS - each worker judges from its own bounded memory, as a single process always has
(a warning at most once a minute). Bans are persisted in PostgreSQL; the other workers pick them up at their ban sync.
When the shared score bans an address, the worker that sees it blocks it at once and only the worker that claims the
ban persists it (``ThreatTracker.ban``): workers banning together never write a second ip_bans row (a repeat offence).

Keys: a client is limited, scored and banned by ``address_bucket`` - its IPv4 address, or its IPv6 /64 (one host owns
a whole /64 and could otherwise start afresh from every address of it). A whitelisted address is judged by itself.
Events and logs keep the full address.

Throttling is not an attack: a 429 of the 'global' or 'static' bucket is logged but adds no threat points, so a busy
gym behind one NAT is slowed down, never banned for it (the shared allowance is one per address, not one per worker).
429s of the abuse-prone buckets (sign-in, desk OTP, AI, live, uploads) escalate as before.

Forged evidence never bans (hunt F39): any web page, e-mail or chat link can make a visitor's browser request any URL
(``<img src="https://<app>/static/x.svg?f=/etc/passwd">``) or post any form here. What such a request carries - its
path, query and body, its 429s, its 404s - is refused and logged as before but adds no threat points and never bans
(app/security/net.py ``attributable``): before, one cross-site image banned the visitor's address for 24 hours, and
everyone behind the same gym Wi-Fi or carrier NAT with it. Requests without browser metadata (scripts, tools) and this
app's own pages' POSTs still score, and so does a scanner User-Agent on any request (a page can't set a browser's).
"""

from __future__ import annotations

import html
import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import get_settings
from app.models.enums import ThreatCategory
from app.security import scanner
from app.security.bans import ThreatTracker, ban_registry, security_events, threat_tracker
from app.security.crypto import token_hash
from app.security.net import attributable, client_ip_from_scope, header
from app.security.ratelimit import RateLimiter, address_bucket, bucket_for, limiter

SCAN_BODY_MAX = 64 * 1024
SCANNABLE_TYPES = ("application/x-www-form-urlencoded", "application/json", "text/plain")
NOT_FOUND_WINDOW = 300.0  # seconds: page/API 404s of one address are counted over this window ...
NOT_FOUND_LIMIT = 30  # ... and this many are directory brute-forcing (reported once, then the count starts over)
THROTTLE_ONLY_BUCKETS = ("global", "static")  # their 429s are logged, never threat points (see the module docstring)


def _session_hash(scope: Scope) -> str | None:
    from app.security.csrf import _read_cookie
    from app.security.sessions import session_cookie_name

    token = _read_cookie(scope, session_cookie_name())
    return token_hash(token) if token else None


def _wants_json(scope: Scope) -> bool:
    path = scope.get("path", "")
    accept = header(scope, b"accept") or ""
    return path.startswith("/api/") or ("application/json" in accept and "text/html" not in accept)


async def _respond(scope: Scope, send: Send, status: int, title: str, message: str, extra_headers: list | None = None) -> None:
    headers = [(b"cache-control", b"no-store")] + (extra_headers or [])
    if _wants_json(scope):
        body = json.dumps({"error": title, "detail": message}).encode()
        headers.append((b"content-type", b"application/json"))
    else:
        nonce = scope.get("state", {}).get("csp_nonce", "")
        body = (
            "<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' "
            "content='width=device-width,initial-scale=1'><title>" + html.escape(title) + "</title>"
            f"<style nonce='{nonce}'>body{{margin:0;min-height:100vh;display:grid;place-items:center;"
            "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Inter,Roboto,sans-serif;background:#0b0b0f;color:#f5f5f7}"
            ".c{max-width:520px;padding:40px;border-radius:28px;background:#1c1c1e;box-shadow:0 30px 80px rgba(0,0,0,.5)}"
            "h1{font-size:28px;margin:0 0 12px}p{color:#a1a1aa;line-height:1.6}"
            ".pill{display:inline-block;padding:6px 14px;border-radius:999px;background:#ff453a22;color:#ff6961;"
            "font-size:13px;font-weight:600;margin-bottom:18px}</style></head><body><div class='c'>"
            f"<span class='pill'>{status}</span><h1>{html.escape(title)}</h1><p>{html.escape(message)}</p>"
            "</div></body></html>"
        ).encode()
        headers.append((b"content-type", b"text/html; charset=utf-8"))
    headers.append((b"content-length", str(len(body)).encode()))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class SecurityGateMiddleware:
    def __init__(self, app: ASGIApp, *, rate_limiter: RateLimiter | None = None,
                 threats: ThreatTracker | None = None) -> None:
        self.app = app
        self.limiter = rate_limiter if rate_limiter is not None else limiter  # tests may give a gate its own
        self.threats = threats if threats is not None else threat_tracker

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        settings = get_settings()
        ip = client_ip_from_scope(scope)
        scope.setdefault("state", {})["client_ip"] = ip
        path: str = scope.get("path", "")
        method: str = scope.get("method", "GET")
        whitelisted = ip in settings.rate_limit_whitelist
        key = ip if whitelisted else address_bucket(ip)  # limits, threat score and ban key: the address or its /64

        # 1. bans ---------------------------------------------------------------------------
        ban = ban_registry.get(ip) or (ban_registry.get(key) if key != ip else None)
        if ban is not None:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 4403})
                return
            until = "permanently" if ban.until is None else f"until {ban.until:%d %b %Y %H:%M} UTC"
            await _respond(scope, send, 403, "Access blocked",
                           f"This network address has been banned {until} by the automated security system "
                           f"after suspicious activity. If you think this is a mistake, contact the gym administrator.")
            return
        if scope["type"] == "websocket":
            await self.app(scope, receive, send)
            return

        ua = header(scope, b"user-agent")
        ctype = header(scope, b"content-type") or ""
        own = attributable(scope, method)  # False: a browser was made to send it - refused / logged, never scored

        # 2. body size ----------------------------------------------------------------------
        max_body = settings.max_upload_bytes + 1024 * 1024
        clen_raw = header(scope, b"content-length")
        try:
            clen = int(clen_raw) if clen_raw else None
        except ValueError:
            clen = None
        if clen is not None and clen > max_body:
            await _respond(scope, send, 413, "Upload too large", f"Requests are limited to {settings.max_upload_mb} MB.")
            return

        # 3. rate limiting (one allowance per address, shared by every worker) ---------------
        if not whitelisted:
            bucket = bucket_for(method, path, ctype)
            checks = [bucket] if bucket == "static" else (["global"] + ([bucket] if bucket else []))
            verdict = await self.limiter.check(key, checks)
            if not verdict.allowed:
                recent = verdict.recent  # this address's rejections in the last minute (on every worker)
                # (a page on another site can make a visitor's browser post to the sign-in form again and again)
                escalates = verdict.bucket not in THROTTLE_ONLY_BUCKETS and own
                severity = (3 if recent > 40 else 2 if recent > 10 else 1) if escalates else 1
                sess = _session_hash(scope)
                security_events.record(ip, ThreatCategory.RATE_LIMIT, severity,
                                       f"rate limit '{verdict.bucket}' exceeded ({recent} rejections/min)",
                                       method=method, path=path, user_agent=ua, action="blocked", session_hash=sess)
                if escalates and await self.threats.add_async(key, severity) >= settings.threat_ban_score:
                    await self.threats.ban(key, severity, f"rate-limit abuse on '{verdict.bucket}'", sess)  # ONE persists
                await _respond(scope, send, 429, "Slow down",
                               "Too many requests. Please wait a moment and try again.",
                               [(b"retry-after", verdict.retry_after_header.encode())])
                return

        # 4. scanning ----------------------------------------------------------------------
        query = (scope.get("query_string") or b"").decode("latin-1")
        asked = scanner.scan_url(path, query)  # what the request asks for: any page or link can make a browser ask it
        replay: list[Message] | None = None
        if method in ("POST", "PUT", "PATCH") and ctype.startswith(SCANNABLE_TYPES) and clen is not None and clen <= SCAN_BODY_MAX:
            body, replay = await _buffer(receive)
            asked += scanner.scan_body(body)
        scored = scanner.scan_agent(method, ua) + (asked if own else [])  # (a page can't set a browser's User-Agent)
        unscored = [] if own else asked
        if scored or unscored:
            worst = max(f.severity for f in scored + unscored)
            sess = _session_hash(scope)
            score = await self.threats.add_async(key, *(f.severity for f in scored)) if scored else 0.0  # one update
            blocked = worst >= 4 or score >= settings.threat_ban_score
            for f in scored:
                security_events.record(ip, f.category, f.severity, f.detail, method=method, path=path,
                                       user_agent=ua, action="blocked" if blocked else "logged", session_hash=sess)
            for f in unscored:  # stored already reviewed: never scored, never triaged into a ban
                security_events.record(ip, f.category, f.severity, f.detail, method=method, path=path, user_agent=ua,
                                       action="blocked" if blocked else "logged", session_hash=sess, attributable=False)
            worst_scored = max((f.severity for f in scored), default=0)
            if scored and (score >= settings.threat_ban_score or worst_scored >= 5):
                await self.threats.ban(key, worst_scored, "; ".join(sorted({f.detail for f in scored}))[:300], sess)
                await _respond(scope, send, 403, "Access blocked",
                               "Malicious request patterns were detected and your address has been temporarily banned.")
                return
            if blocked:
                await _respond(scope, send, 403, "Request blocked",
                               "This request looked like an attack and was blocked by the security system.")
                return

        # 5. call the app (count 404s, enforce streamed body size) ------------------------
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            if replay:
                return replay.pop(0)
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > max_body:
                    raise _BodyTooLarge()
            return message

        status_holder = {"status": 200}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
            await send(message)

        try:
            await self.app(scope, limited_receive, send_wrapper)
        except _BodyTooLarge:
            await _respond(scope, send, 413, "Upload too large", f"Requests are limited to {settings.max_upload_mb} MB.")
            return

        # only page/API 404s count towards brute-force detection (never broken images or map tiles, nor what a page or
        # link made a browser request: 60 <img> tags on another site are not this visitor's brute-forcing); the
        # response is out already, so the shared window costs the client nothing
        if (status_holder["status"] == 404 and own
                and not path.startswith(("/static/", "/media/", "/maps/tiles/", "/favicon"))):
            recent = await self.limiter.note(key, "404", window=NOT_FOUND_WINDOW, clear_at=NOT_FOUND_LIMIT)
            if recent >= NOT_FOUND_LIMIT:
                security_events.record(ip, ThreatCategory.PROBE, 3, f"{recent} not-found responses in 5 minutes",
                                       method=method, path=path, user_agent=ua, action="logged")
                if await self.threats.add_async(key, 3) >= settings.threat_ban_score:
                    await self.threats.ban(key, 3, "directory brute-forcing (404 flood)", _session_hash(scope))


class _BodyTooLarge(Exception):
    pass


async def _buffer(receive: Receive) -> tuple[bytes, list[Message]]:
    chunks: list[bytes] = []
    messages: list[Message] = []
    while True:
        message = await receive()
        messages.append(message)
        if message["type"] == "http.request":
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        else:
            break
    return b"".join(chunks), messages
