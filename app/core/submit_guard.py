"""Double-submit protection for every state-changing request (POST / PUT / PATCH / DELETE).

Client side
* Every form rendered with the ``csrf()`` macro (app/templates/_ui.html) carries a hidden one-time ``submit_id`` - a new
  one per render (``new_submit_id``), so it works without JavaScript.
* app/static/src/js/00-core.js sends ``X-Submit-Id`` on every unsafe ``IP.api`` call (one per call) and disables the
  clicked form's submit buttons while it is being submitted (aria-busy, "Saving..."), re-enabling them on a
  back/forward-cache return, on an error answer and after a timeout.

Server side - ``SubmitGuardMiddleware`` (pure ASGI, registered in app/main.py inside the security gate):
* A request is identified by (browser, submit id, method, path, body fingerprint). The browser is the session cookie
  when signed in, else the CSRF cookie (sign-in and sign-up forms) - keys can never replay across browsers or users.
  The fingerprint is the form's fields (except csrf_token / page_id / submit_id; file parts by name), so changing a
  field and submitting the same rendered form again is a new request, while a double click, a resent POST or Back +
  submit with the same values is the same one. No submit id: not guarded (API clients, older pages).
* The FIRST request is processed. A duplicate that arrives while it runs waits (at most WAIT_SECONDS) and gets the same
  outcome; a duplicate after it finished gets the stored outcome replayed for WINDOW_SECONDS - the same 303 Location
  (with its Set-Cookie: a double-clicked sign-in ends with ONE session), the same JSON with its status, or the same
  page (its CSP nonce rewritten). 4xx / 5xx outcomes are not stored - nothing happened (validation, permission, rate
  limit) or the transaction rolled back - so a duplicate runs on its own. Anything not replayable (a stream, a file, a
  body above the size caps) is remembered as done: a duplicate gets 409 ``duplicate_submit`` (JSON) or a redirect back
  with a notice (pages).
* Store: Redis (``ip:<env>:<db>:sec:sg:<digest>`` - the shared namespace of app/security/ratelimit.py) with a
  per-process fallback (bounded) when REDIS_URL is unset or Redis fails. Fail-open: a store error never blocks a
  request (one warning a minute). Set-Cookie values are stored encrypted (DATA_ENCRYPTION_KEY): a sign-in's replay
  carries its session cookie.
* Never guarded (``EXEMPT``): payment webhooks, SSE streams, the live coach and TTS endpoints - they have their own
  idempotency and must never wait behind each other.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import secrets
import time
import zlib
from collections import OrderedDict
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import parse_qsl

from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger("ironpulse.submit_guard")

SUBMIT_FIELD = "submit_id"
SUBMIT_HEADER = b"x-submit-id"
SUBMIT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
WINDOW_SECONDS = 600  # a finished request's outcome is replayed this long
LEASE_SECONDS = 300  # a request in flight holds its key at most this long (a crashed worker frees it)
WAIT_SECONDS = 25.0  # a duplicate waits at most this long for the first request to finish
PEEK_BYTES = 64 * 1024  # body prefix read to find the submit id (form fields come before uploads)
MAX_JSON_BYTES = 16 * 1024  # replayable JSON / redirect body
MAX_HTML_BYTES = 256 * 1024  # replayable page (stored compressed)
LOCAL_MAX_ENTRIES = 20_000  # per-process fallback store
LOCAL_MAX_BYTES = 32 * 1024 * 1024
WARN_EVERY_SECONDS = 60.0
REDIS_RETRY_SECONDS = 10.0
_IGNORED_FIELDS = frozenset({"csrf_token", "page_id", SUBMIT_FIELD})
_MULTIPART_FIELD = re.compile(
    rb'Content-Disposition:\s*form-data;\s*name="([^"\r\n]{1,100})"(?:;\s*filename="([^"\r\n]{0,255})")?[^\r\n]*\r\n'
    rb"(?:[^\r\n]+\r\n)*\r\n",
    re.IGNORECASE,
)
_MULTIPART_ID = re.compile(rb'name="submit_id"\r\n(?:[^\r\n]+\r\n)*\r\n([A-Za-z0-9_-]{16,64})\r\n')

# Paths that are never guarded: they stream, carry their own idempotency, or are called many times a second.
EXEMPT_PREFIXES = ("/payments/", "/api/live/", "/api/tts", "/api/plans/ask", "/ws")
EXEMPT_RE = re.compile(r"^/api/shop/products/\d+/ask/stream$")

stats: dict[str, int] = {"claimed": 0, "replayed": 0, "waited": 0, "busy": 0, "done_unreplayable": 0, "store_errors": 0}


def new_submit_id() -> str:
    """A fresh one-time submit id (the ``csrf()`` macro renders one per form)."""
    return secrets.token_urlsafe(16)


def _install_template_global() -> None:
    try:
        from app.core.templating import env

        env.globals.setdefault("new_submit_id", new_submit_id)
    except Exception:  # noqa: BLE001 - templates without it simply render no submit id
        log.exception("could not register the new_submit_id template global")


_install_template_global()


def exempt(path: str) -> bool:
    return path.startswith(EXEMPT_PREFIXES) or bool(EXEMPT_RE.match(path))


# ------------------------------------------------------------------ stored outcomes
@dataclass(slots=True)
class Outcome:
    status: int
    headers: list[tuple[bytes, bytes]]
    cookies: list[bytes]
    body: bytes
    nonce: str = ""
    replayable: bool = True

    def dump(self) -> str:
        from app.security.crypto import encrypt_json

        return json.dumps({
            "st": "done", "s": self.status, "r": self.replayable, "n": self.nonce,
            "h": [[k.decode("latin-1"), v.decode("latin-1")] for k, v in self.headers],
            "c": encrypt_json([c.decode("latin-1") for c in self.cookies]) if self.cookies else "",
            "b": base64.b64encode(zlib.compress(self.body, 6)).decode() if self.body else "",
        }, separators=(",", ":"))

    @classmethod
    def load(cls, raw: str) -> "Outcome | None":
        from app.security.crypto import decrypt_json

        try:
            data = json.loads(raw)
            if data.get("st") != "done":
                return None
            cookies = [c.encode("latin-1") for c in (decrypt_json(data["c"]) if data.get("c") else [])]
            body = zlib.decompress(base64.b64decode(data["b"])) if data.get("b") else b""
            headers = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in data.get("h") or []]
            return cls(int(data["s"]), headers, cookies, body, str(data.get("n") or ""), bool(data.get("r", True)))
        except Exception:  # noqa: BLE001 - a record we cannot read (key rotated, corrupted) is no record
            log.warning("submit guard: unreadable stored outcome ignored")
            return None


PENDING = json.dumps({"st": "pending"})


# ------------------------------------------------------------------ stores
@dataclass(slots=True)
class _Entry:
    value: str
    expires: float
    event: asyncio.Event | None = field(default=None)


class LocalStore:
    """This worker's memory: the fallback without Redis (or while it fails). Bounded (entries and bytes, least recently
    used first); waiters in this process are woken at once."""

    def __init__(self, max_entries: int = LOCAL_MAX_ENTRIES, max_bytes: int = LOCAL_MAX_BYTES) -> None:
        self._data: OrderedDict[str, _Entry] = OrderedDict()
        self._bytes = 0
        self._max_entries = max_entries
        self._max_bytes = max_bytes

    def _get(self, key: str) -> _Entry | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        if entry.expires <= time.monotonic():
            self._drop(key)
            return None
        return entry

    def _drop(self, key: str) -> None:
        entry = self._data.pop(key, None)
        if entry is not None:
            self._bytes -= len(entry.value)
            if entry.event is not None:
                entry.event.set()

    def _put(self, key: str, value: str, ttl: float) -> None:
        old = self._data.pop(key, None)
        if old is not None:
            self._bytes -= len(old.value)
            if old.event is not None:
                old.event.set()
        self._data[key] = _Entry(value, time.monotonic() + ttl)
        self._bytes += len(value)
        while self._data and (len(self._data) > self._max_entries or self._bytes > self._max_bytes):
            self._drop(next(iter(self._data)))

    async def claim(self, key: str) -> bool:
        if self._get(key) is not None:
            return False
        self._put(key, PENDING, LEASE_SECONDS)
        return True

    async def get(self, key: str) -> str | None:
        entry = self._get(key)
        return entry.value if entry is not None else None

    async def wait(self, key: str, timeout: float) -> None:
        entry = self._get(key)
        if entry is None or entry.value != PENDING:
            return
        if entry.event is None:
            entry.event = asyncio.Event()
        try:
            await asyncio.wait_for(entry.event.wait(), timeout)
        except TimeoutError:
            pass

    async def complete(self, key: str, value: str) -> None:
        self._put(key, value, WINDOW_SECONDS)

    async def release(self, key: str) -> None:
        self._drop(key)

    def reset(self) -> None:
        for key in list(self._data):
            self._drop(key)


class RedisStore:
    """Shared by every worker. Any error raises - the caller falls back to ``LocalStore`` for that request."""

    async def _client(self) -> Any:
        from app.security.sessions import get_redis

        client = await get_redis()
        if client is None:
            raise ConnectionError("redis not available")
        return client

    async def claim(self, key: str) -> bool:
        return bool(await (await self._client()).set(key, PENDING, nx=True, ex=LEASE_SECONDS))

    async def get(self, key: str) -> str | None:
        return await (await self._client()).get(key)

    async def wait(self, key: str, timeout: float) -> None:
        await asyncio.sleep(min(timeout, 0.1))

    async def complete(self, key: str, value: str) -> None:
        await (await self._client()).set(key, value, ex=WINDOW_SECONDS)

    async def release(self, key: str) -> None:
        await (await self._client()).delete(key)


local_store = LocalStore()
redis_store = RedisStore()
_redis_retry_at = 0.0
_last_warning = 0.0


def _store_failed(exc: BaseException) -> None:
    global _redis_retry_at, _last_warning
    stats["store_errors"] += 1
    now = time.monotonic()
    _redis_retry_at = now + REDIS_RETRY_SECONDS
    if now - _last_warning >= WARN_EVERY_SECONDS:
        _last_warning = now
        log.warning("submit guard: Redis unavailable (%s) - duplicates are caught per worker for %.0f s",
                    exc.__class__.__name__, REDIS_RETRY_SECONDS)


def _store() -> Any:
    from app.config import get_settings

    if get_settings().redis_url and time.monotonic() >= _redis_retry_at:
        return redis_store
    return local_store


def reset() -> None:
    """Tests: forget this worker's guard state (the shared keys go with ``shared_store.reset``)."""
    global _redis_retry_at, _last_warning
    local_store.reset()
    _redis_retry_at = 0.0
    _last_warning = 0.0
    for k in stats:
        stats[k] = 0


try:
    from app.core.state import register_reset

    register_reset(reset, name="app.core.submit_guard")
except Exception:  # noqa: BLE001  # pragma: no cover
    pass


# ------------------------------------------------------------------ request identity
def _cookies(scope: Scope) -> dict[str, str]:
    jar: dict[str, str] = {}
    for k, v in scope.get("headers") or []:
        if k == b"cookie":
            parsed = SimpleCookie()
            try:
                parsed.load(v.decode("latin-1"))
            except Exception:  # noqa: BLE001
                continue
            jar.update({name: morsel.value for name, morsel in parsed.items()})
    return jar


def _browser(scope: Scope) -> str | None:
    """The browser the request comes from: its session cookie, else its CSRF cookie (sign-in / sign-up forms)."""
    from app.security.csrf import csrf_cookie_name
    from app.security.sessions import session_cookie_name

    jar = _cookies(scope)
    token = jar.get(session_cookie_name())
    if token and len(token) <= 200:
        return "s:" + hashlib.sha256(token.encode()).hexdigest()
    csrf = jar.get(csrf_cookie_name())
    if csrf and 32 <= len(csrf) <= 64:
        return "c:" + hashlib.sha256(csrf.encode()).hexdigest()
    return None


def _header(scope: Scope, name: bytes) -> str:
    for k, v in scope.get("headers") or []:
        if k == name:
            return v.decode("latin-1")
    return ""


async def _peek(receive: Receive, limit: int) -> tuple[bytes, list[Message], bool]:
    """Read request body messages until ``limit`` bytes or the end: (prefix, the messages read, complete)."""
    messages: list[Message] = []
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        messages.append(message)
        if message["type"] != "http.request":
            return b"".join(chunks), messages, True
        body = message.get("body", b"") or b""
        chunks.append(body)
        size += len(body)
        if not message.get("more_body", False):
            return b"".join(chunks), messages, True
        if size >= limit:
            return b"".join(chunks), messages, False


def _form_identity(body: bytes, complete: bool, ctype: str) -> tuple[str | None, str]:
    """(submit id, fingerprint of the other fields) of a urlencoded / multipart body."""
    if ctype.startswith("application/x-www-form-urlencoded"):
        if not complete:
            return None, ""
        try:
            pairs = parse_qsl(body.decode("latin-1"), keep_blank_values=True, max_num_fields=2000)
        except ValueError:
            return None, ""
        sid = next((v for k, v in pairs if k == SUBMIT_FIELD), None)
        rest = sorted((k, v) for k, v in pairs if k not in _IGNORED_FIELDS)
        return sid, hashlib.sha256(json.dumps(rest).encode()).hexdigest()
    if ctype.startswith("multipart/form-data"):
        m = _MULTIPART_ID.search(body)
        if m is None:
            return None, ""
        h = hashlib.sha256()
        for part in _MULTIPART_FIELD.finditer(body):
            name, filename = part.group(1), part.group(2)
            if name.decode("latin-1") in _IGNORED_FIELDS:
                continue
            if filename is not None:
                h.update(b"F" + name + b"=" + filename + b"\0")
                continue
            start = part.end()
            end = body.find(b"\r\n--", start)
            h.update(b"T" + name + b"=" + (body[start:end] if end >= 0 else body[start:start + 4096]) + b"\0")
        return m.group(1).decode(), h.hexdigest()
    return None, ""


def _key(browser: str, submit_id: str, method: str, path: str, fingerprint: str) -> str:
    from app.security.ratelimit import shared_key

    digest = hashlib.sha256(f"{browser}|{submit_id}|{method}|{path}|{fingerprint}".encode()).hexdigest()[:40]
    return shared_key("sg", digest)


def _wants_json(scope: Scope) -> bool:
    accept = _header(scope, b"accept")
    return scope.get("path", "").startswith("/api/") or ("application/json" in accept and "text/html" not in accept)


# ------------------------------------------------------------------ middleware
class SubmitGuardMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") not in UNSAFE_METHODS or exempt(scope.get("path", "")):
            await self.app(scope, receive, send)
            return
        browser = _browser(scope)
        header_id = _header(scope, SUBMIT_HEADER).strip()
        replay_msgs: list[Message] = []
        if header_id:
            submit_id, fingerprint = header_id, ""
        else:
            submit_id, fingerprint = None, ""
            ctype = _header(scope, b"content-type").lower()
            if browser is not None and ctype.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
                body, replay_msgs, complete = await _peek(receive, PEEK_BYTES)
                submit_id, fingerprint = _form_identity(body, complete, ctype)
        if replay_msgs:
            receive = _replaying(replay_msgs, receive)
        if browser is None or not submit_id or not SUBMIT_ID_RE.fullmatch(submit_id):
            await self.app(scope, receive, send)
            return

        key = _key(browser, submit_id, scope["method"], scope.get("path", ""), fingerprint)
        deadline = time.monotonic() + WAIT_SECONDS
        waited = False
        while True:
            store = _store()
            try:
                if await store.claim(key):
                    break  # ours: process it below
                raw = await store.get(key)
            except Exception as exc:  # noqa: BLE001 - fail open: this worker's memory decides
                _store_failed(exc)
                store = local_store
                if await store.claim(key):
                    break
                raw = await store.get(key)
            if raw is not None and raw != PENDING:
                outcome = Outcome.load(raw)
                if outcome is not None:
                    stats["replayed"] += 1
                    await _replay(scope, receive, send, outcome)
                    return
                await _safe(store.release(key))  # unreadable: forget it and claim it below
                raw = None
            if time.monotonic() >= deadline:
                stats["busy"] += 1
                await _busy(scope, receive, send)
                return
            if raw is None:
                await asyncio.sleep(0.01)  # released (the first one failed) or expired just now: claim it again
                continue
            if not waited:
                waited = True
                stats["waited"] += 1
            await store.wait(key, max(0.05, min(0.25, deadline - time.monotonic())))

        stats["claimed"] += 1
        await self._process(scope, receive, send, store, key)

    async def _process(self, scope: Scope, receive: Receive, send: Send, store: Any, key: str) -> None:
        captured = _Capture(scope)

        async def send_capture(message: Message) -> None:
            captured.feed(message)
            await send(message)

        try:
            await self.app(scope, receive, send_capture)
        except BaseException:
            await _safe(store.release(key))
            raise
        outcome = captured.outcome()
        if outcome is None:
            await _safe(store.release(key))
            return
        if not outcome.replayable:
            stats["done_unreplayable"] += 1
        try:
            await store.complete(key, outcome.dump())
        except Exception as exc:  # noqa: BLE001
            _store_failed(exc)
            await local_store.complete(key, outcome.dump())


async def _safe(coro: Any) -> None:
    try:
        await coro
    except Exception as exc:  # noqa: BLE001
        _store_failed(exc)


def _replaying(messages: list[Message], receive: Receive) -> Receive:
    pending = list(messages)

    async def replay() -> Message:
        if pending:
            return pending.pop(0)
        return await receive()

    return replay


class _Capture:
    """Watches the response going out and keeps what a duplicate needs to get the same outcome."""

    def __init__(self, scope: Scope) -> None:
        self.nonce = str((scope.get("state") or {}).get("csp_nonce") or "")
        self.status = 0
        self.headers: list[tuple[bytes, bytes]] = []
        self.cookies: list[bytes] = []
        self.chunks: list[bytes] = []
        self.size = 0
        self.mode = ""  # skip | keep | opaque
        self.cap = 0
        self.finished = False

    def feed(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            self.status = int(message["status"])
            ctype = ""
            for k, v in message.get("headers") or []:
                name = k.lower()
                if name == b"set-cookie":
                    self.cookies.append(v)
                elif name != b"content-length":
                    self.headers.append((name, v))
                if name == b"content-type":
                    ctype = v.decode("latin-1").lower()
            if self.status >= 400 or self.status < 200:
                self.mode = "skip"
            elif 300 <= self.status < 400 or ctype.startswith("application/json"):
                self.mode, self.cap = "keep", MAX_JSON_BYTES
            elif ctype.startswith("text/html"):
                self.mode, self.cap = "keep", MAX_HTML_BYTES
            else:
                self.mode = "opaque"
        elif message["type"] == "http.response.body":
            body = message.get("body", b"") or b""
            if self.mode == "keep":
                self.size += len(body)
                if self.size > self.cap:
                    self.mode, self.chunks = "opaque", []
                else:
                    self.chunks.append(body)
            if not message.get("more_body", False):
                self.finished = True

    def outcome(self) -> Outcome | None:
        if not self.finished or self.mode in ("", "skip"):
            return None
        if self.mode == "opaque":
            return Outcome(self.status, [], [], b"", replayable=False)
        return Outcome(self.status, self.headers, self.cookies, b"".join(self.chunks), self.nonce)


async def _replay(scope: Scope, receive: Receive, send: Send, outcome: Outcome) -> None:
    if not outcome.replayable:
        await _already_done(scope, receive, send)
        return
    body = outcome.body
    nonce = str((scope.get("state") or {}).get("csp_nonce") or "")
    if outcome.nonce and nonce and body:
        body = body.replace(outcome.nonce.encode(), nonce.encode())  # the replayed page's scripts run under this CSP
    headers = list(outcome.headers) + [(b"set-cookie", c) for c in outcome.cookies]
    headers += [(b"content-length", str(len(body)).encode()), (b"x-submit-replay", b"1")]
    await send({"type": "http.response.start", "status": outcome.status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


def _back(scope: Scope) -> str:
    """Where a page form came from (same-origin Referer path), for the 'already submitted' notice."""
    from urllib.parse import urlsplit

    ref = _header(scope, b"referer")
    host = _header(scope, b"host")
    if ref:
        parts = urlsplit(ref)
        if parts.netloc == host and parts.path.startswith("/") and not parts.path.startswith("//"):
            return parts.path + (f"?{parts.query}" if parts.query else "")
    return "/"


async def _notice(scope: Scope, receive: Receive, send: Send, status: int, code: str, message: str,
                  retry_after: int | None = None) -> None:
    from starlette.responses import JSONResponse

    from app.core.templating import redirect

    if _wants_json(scope):
        payload: dict[str, Any] = {"error": "Already submitted" if code == "duplicate_submit" else "Still working",
                                   "detail": message, "code": code}
        headers = {"Cache-Control": "no-store"}
        if retry_after:
            payload["retry_after"] = retry_after
            headers["Retry-After"] = str(retry_after)
        await JSONResponse(payload, status_code=status, headers=headers)(scope, receive, send)
        return
    await redirect(_back(scope), message, "info")(scope, receive, send)


async def _already_done(scope: Scope, receive: Receive, send: Send) -> None:
    await _notice(scope, receive, send, 409, "duplicate_submit",
                  "That was already sent - nothing was submitted twice.")


async def _busy(scope: Scope, receive: Receive, send: Send) -> None:
    await _notice(scope, receive, send, 409, "submit_in_progress",
                  "Your first request is still being processed - reload the page in a moment to see the result.",
                  retry_after=2)
