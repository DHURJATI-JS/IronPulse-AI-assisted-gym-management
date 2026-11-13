"""WebSocket presence of a signed-in browser tab: ``/ws?page=<id>`` (00-core.js).

The real frontend opens a NEW socket with a new page id on every page of a signed-in user (IP.pageId is per document),
pings every 25 s and closes it with code 4001 on pagehide. Default here (LT_WS_MODE=per-page): exactly that - every
page view of a signed-in user closes the previous socket (in a background greenlet, like the browser's pagehide: the
next page never waits for it) and opens a new one; connect + "hello" time is reported as ``WS /ws (connect+hello)``.
A reader greenlet drains the server's events (notifications, chat, AI status), a pinger keeps the socket alive, and a
close by the server is reported as ``WS /ws (dropped by server)``. LT_WS_MODE=hold keeps ONE socket per user instead
(fewer handshakes - a comparison option only); off = no sockets.

websocket-client runs on gevent-patched sockets (Locust monkey-patches at import), so the reader and pinger are
cooperative greenlets.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import gevent
import websocket

PING_SECONDS = 25.0
CONNECT_TIMEOUT = 10.0
CLOSE_SIGNED_IN_ELSEWHERE = 4409  # one session per account: another sign-in ended this session (app/security/sessions.py)
CLOSE_LABELS = {4001: "page-hide", 4401: "no session", 4403: "bad origin / banned", 4429: "flood / too many sockets",
                4409: "signed in elsewhere - two simulated users share one account",
                1013: "try again later - server overloaded (load shedding)"}


def close_code(data: bytes | None) -> int:
    return int.from_bytes(data[:2], "big") if data and len(data) >= 2 else 1005


def _close_quietly(ws: websocket.WebSocket) -> None:
    try:
        ws.close(status=4001, reason=b"page-hide", timeout=2)
    except Exception:  # noqa: BLE001
        pass


class Presence:
    def __init__(self, user: Any, browser: Any) -> None:
        self.user = user
        self.browser = browser
        self.ws: websocket.WebSocket | None = None
        self.reader: gevent.Greenlet | None = None
        self.pinger: gevent.Greenlet | None = None
        self.closing = False
        self.messages = 0

    def _fire(self, name: str, started: float, exc: BaseException | None, length: int = 0) -> None:
        self.user.environment.events.request.fire(
            request_type="WS", name=name, response_time=(time.perf_counter() - started) * 1000.0, response_length=length,
            exception=exc, context=self.user.context(), response=None)

    def connect(self, page_id: str | None = None) -> websocket.WebSocket:
        """A new socket for ``page_id`` (raises on any failure; the caller reports it)."""
        b = self.browser
        url = f"ws://{b.host}/ws?page={page_id or b.page_id}"
        headers = [f"User-Agent: {b.ua}", f"X-Forwarded-For: {b.ip}", "X-Forwarded-Proto: https"]
        ws = websocket.create_connection(url, timeout=CONNECT_TIMEOUT, header=headers, cookie=b.cookie_header(),
                                         origin=b.origin, enable_multithread=True)
        while True:
            op, raw = ws.recv_data(control_frame=True)
            if op == websocket.ABNF.OPCODE_CLOSE:  # accept-then-close: the server says why (4401, 4403, 4409 ...)
                code = close_code(raw)
                _close_quietly(ws)
                if code == CLOSE_SIGNED_IN_ELSEWHERE:
                    b.elsewhere = True
                raise RuntimeError(f"closed before hello: {code} {CLOSE_LABELS.get(code, '')}".strip())
            if op in (websocket.ABNF.OPCODE_TEXT, websocket.ABNF.OPCODE_BINARY):
                break
        hello = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        data = json.loads(hello) if hello else {}
        if not isinstance(data, dict) or data.get("type") != "hello":
            _close_quietly(ws)
            raise RuntimeError(f"no hello from /ws: {str(hello)[:80]}")
        return ws

    def open(self) -> None:
        self.close()
        b = self.browser
        b.page_id = uuid.uuid4().hex
        started = time.perf_counter()
        try:
            ws = self.connect(b.page_id)
        except Exception as exc:  # noqa: BLE001 - report every failure kind (refused, 4401, timeout ...)
            self._fire("WS /ws (connect+hello)", started, exc)
            self.ws = None
            return
        self._fire("WS /ws (connect+hello)", started, None)
        ws.settimeout(None)
        self.ws = ws
        self.closing = False
        self.reader = gevent.spawn(self._read, ws)
        self.pinger = gevent.spawn(self._ping, ws)

    def _read(self, ws: websocket.WebSocket) -> None:
        while True:
            try:
                op, data = ws.recv_data(control_frame=False)
            except Exception as exc:  # noqa: BLE001
                if not self.closing and ws is self.ws:
                    started = time.perf_counter()
                    self._fire("WS /ws (dropped by server)", started, exc if not isinstance(exc, websocket.WebSocketConnectionClosedException)
                               else RuntimeError("connection closed by the server"))
                    self.ws = None
                return
            if op == websocket.ABNF.OPCODE_CLOSE:
                code = close_code(data)
                if not self.closing and ws is self.ws:
                    if code == CLOSE_SIGNED_IN_ELSEWHERE:  # another simulated user signed in with this account
                        self.browser.elsewhere = True
                    self._fire("WS /ws (signed in elsewhere)" if code == CLOSE_SIGNED_IN_ELSEWHERE else
                               "WS /ws (dropped by server)", time.perf_counter(),
                               RuntimeError(f"closed by server: {code} {CLOSE_LABELS.get(code, '')}".strip()))
                    self.ws = None
                return
            self.messages += 1

    def _ping(self, ws: websocket.WebSocket) -> None:
        while True:
            gevent.sleep(PING_SECONDS)
            if ws is not self.ws or self.closing:
                return
            try:
                ws.send('{"type":"ping"}')
            except Exception:  # noqa: BLE001 - the reader reports the drop
                return

    def close(self, *, wait: bool = False) -> None:
        """Page hide: stop the reader/pinger and close with 4001 - in the background (the next page does not wait for
        the close handshake), or right here with ``wait`` (the user stops)."""
        ws, self.ws = self.ws, None
        self.closing = True
        for g in (self.pinger, self.reader):
            if g is not None and not g.dead:
                g.kill(block=False)
        self.pinger = self.reader = None
        if ws is not None:
            if wait:
                _close_quietly(ws)
            else:
                gevent.spawn(_close_quietly, ws)


def ws_flood(user: Any, browser: Any, *, messages: int = 50, seconds: float = 10.0, name: str = "ABUSE WS /ws (flood)") -> None:
    """Abuse: one socket, ``messages`` pings within ``seconds`` (the server allows WS_FLOOD_MESSAGES=40 per 10 s and
    closes with 4429). Success = the server closed the socket with 4429 (or refused it: banned / 4403, 4401, 429/403
    on the handshake); failure = it kept accepting the flood."""
    started = time.perf_counter()
    p = Presence(user, browser)
    try:
        ws = p.connect(uuid.uuid4().hex)
    except Exception as exc:  # noqa: BLE001 - a refused handshake (ban, rate limit, no session) is the defence working
        refused = any(k in str(exc) for k in ("403", "429", "4401", "4403", "4429", "no hello"))
        p._fire(name, started, None if refused else exc)  # closed before the hello = refused (banned / no session)
        return
    pause = seconds / max(1, messages)
    code = None
    try:
        ws.settimeout(0.05)
        for _ in range(messages):
            try:
                ws.send('{"type":"ping"}')
            except Exception:  # noqa: BLE001 - closed by the server mid-flood
                break
            try:
                op, data = ws.recv_data(control_frame=True)
                if op == websocket.ABNF.OPCODE_CLOSE:
                    code = int.from_bytes(data[:2], "big") if data and len(data) >= 2 else 1005
                    break
            except websocket.WebSocketTimeoutException:
                pass
            except Exception:  # noqa: BLE001
                break
            gevent.sleep(pause)
        if code is None:
            ws.settimeout(2.0)
            try:
                for _ in range(20):
                    op, data = ws.recv_data(control_frame=True)
                    if op == websocket.ABNF.OPCODE_CLOSE:
                        code = int.from_bytes(data[:2], "big") if data and len(data) >= 2 else 1005
                        break
            except Exception:  # noqa: BLE001 - connection dropped without a close frame
                code = code or 1006
    finally:
        _close_quietly(ws)
    if code == 4429:
        p._fire(name, started, None)
    else:
        p._fire(name, started, RuntimeError(f"flood not stopped (close code {code})"))
