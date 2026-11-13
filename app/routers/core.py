"""Core endpoints: health, AI status, public media files, robots, and the real-time WebSocket."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from sqlalchemy import text

from app.ai.client import ai
from app.ai.owner import clean_page_id
from app.config import get_settings
from app.core.deps import DB, NotFound
from app.core.overload import warn_db_unavailable
from app.db import db_unavailable, get_sessionmaker
from app.services import storage
from app.services.presence import UNKNOWN_CLOSE, presence
from app.services.realtime import CloseRequest, hub, is_control_event, new_conn_id

log = logging.getLogger("ironpulse.core")
router = APIRouter()

# Feature modules register handlers for client->server WebSocket messages, e.g. "chat.typing".
WsHandler = Callable[[Any, dict[str, Any]], Awaitable[None]]  # (user, message) -> None
ws_handlers: dict[str, WsHandler] = {}

# /ws limits. Close codes (docs/ai-guard-and-queue.md section 7): 4001 page closing (client), 4401 no session /
# signed out, 4403 bad origin / banned, 4409 signed out because the account signed in on another device (no
# reconnect), 4429 flood or too many sockets, 1013 try again later (the session check could not reach the database:
# pool timeout, refused or lost connection - the page reconnects with a jittered back-off, 00-core.js).
WS_MAX_SOCKETS_PER_USER = 16  # more than anyone has tabs open: beyond it a socket is refused with 4429
WS_FLOOD_MESSAGES = 40  # messages per WS_FLOOD_WINDOW seconds per socket
WS_FLOOD_WINDOW = 10.0
WS_MAX_MESSAGE_CHARS = 4096
WS_CLOSE_FLOOD = 4429
WS_CLOSE_TRY_AGAIN = 1013  # RFC 6455 "Try Again Later": the server is overloaded right now
WS_TRY_AGAIN_REASON = "server busy - try again later"
_cap_logged: dict[int, float] = {}


def ws_handler(kind: str) -> Callable[[WsHandler], WsHandler]:
    def deco(fn: WsHandler) -> WsHandler:
        ws_handlers[kind] = fn
        return fn

    return deco


@router.get("/healthz", include_in_schema=False)
async def healthz(db: DB) -> JSONResponse:
    try:
        await db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False
    status = ai.status()
    return JSONResponse(
        {"ok": db_ok, "database": db_ok, "ai": {"state": status["state"], "active": status["active"]}},
        status_code=200 if db_ok else 503,
    )


@router.get("/api/ai/status")
async def ai_status() -> dict[str, Any]:
    s = ai.status()
    return {
        "available": s["available"],
        "state": s["state"],
        "active": s["active_label"],
        "providers": [{"name": p["label"], "status": p["status"]} for p in s["providers"]],
    }


@router.get("/media/{key:path}", include_in_schema=False)
async def public_media(key: str) -> FileResponse:
    if not key.startswith("public/"):
        raise NotFound()
    try:
        path = storage.path_for(key)
    except storage.UploadError:
        raise NotFound() from None
    if not path.is_file():
        raise NotFound()
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=604800, immutable"})


@router.get("/robots.txt", include_in_schema=False)
async def robots() -> PlainTextResponse:
    return PlainTextResponse("User-agent: *\nDisallow: /owner\nDisallow: /me\nDisallow: /staff\nDisallow: /superadmin\nDisallow: /api\n")


@router.get("/favicon.ico", include_in_schema=False)
async def favicon() -> RedirectResponse:
    return RedirectResponse("/static/img/favicon.svg", status_code=301)


def _origin_allowed(ws: WebSocket) -> bool:
    origin = ws.headers.get("origin")
    if origin is None:
        return True  # non-browser client; still needs a valid session cookie
    host = ws.headers.get("host", "")
    s = get_settings()
    allowed = {f"http://{host}", f"https://{host}", s.public_base_url, *s.cors_origins}
    return origin.rstrip("/") in {a.rstrip("/") for a in allowed}


def _report_flood(ws: WebSocket, session_hash: str | None, detail: str) -> None:
    """A socket broke the per-connection message limits: record it like any other rate-limit abuse (it feeds the
    IP's threat score and, at ``threat_ban_score``, a ban)."""
    try:
        from app.models.enums import ThreatCategory
        from app.security.bans import security_events, threat_tracker
        from app.security.net import client_ip_from_scope

        ip = client_ip_from_scope(ws.scope)
        security_events.record(ip, ThreatCategory.RATE_LIMIT, 2, "websocket flood", method="WS", path="/ws",
                               user_agent=ws.headers.get("user-agent"), action="blocked", session_hash=session_hash)
        # banned on the SHARED total through the claim (app/security/bans.py ThreatTracker.add), never a stale copy
        threat_tracker.add(ip, 2, reason=f"websocket flood ({detail})", session_hash=session_hash)
    except Exception:  # noqa: BLE001 - reporting must never keep a flooding socket open
        log.exception("could not record a websocket flood")


def _db_busy(exc: BaseException) -> str | None:
    """The database trouble the handshake answers with 1013 instead of raising: a pool or statement timeout, a refused
    or lost connection (``db_unavailable``; statement timeouts included - a session check is one primary-key read, so a
    cancelled one means overload, never a slow query). None for anything else (a bug: raised as before)."""
    return db_unavailable(exc, statement_timeouts=True)


def _try_again_later(kind: str, exc: BaseException, step: str) -> None:
    """One warning a minute for every socket turned away with 1013 (the next one says how many more there were)."""
    warn_db_unavailable(log, "WS /ws", kind, exc, f"{step} - socket closed with {WS_CLOSE_TRY_AGAIN} (try again later)")


async def _close_quietly(ws: WebSocket, code: int, reason: str | None = None, *, accept: bool = False) -> None:
    """(Accept, then) close with ``code`` so the browser receives it; a client that already left makes that fail -
    nothing to report then."""
    try:
        if accept:
            await ws.accept()
        await ws.close(code=code, reason=reason)
    except Exception:  # noqa: BLE001 - the client went away first
        log.debug("ws: close code %s not delivered (the client went away)", code)


def _log_cap(user_id: int, count: int) -> None:
    now = time.monotonic()
    if now - _cap_logged.get(user_id, -1e9) < 60:
        return
    if len(_cap_logged) > 1000:
        _cap_logged.clear()
    _cap_logged[user_id] = now
    log.info("ws: user %s already has %s sockets open - refusing another (4429)", user_id, count)


async def _pump(ws: WebSocket, queue: asyncio.Queue) -> None:
    """Forward hub events to the browser. A ``CloseRequest`` closes the socket (session signed out); other internal
    control items are dropped - they never reach the client."""
    while True:
        item = await queue.get()
        if isinstance(item, CloseRequest):
            try:
                await ws.close(code=item.code)
            except Exception:  # noqa: BLE001 - already closed
                pass
            return
        if not isinstance(item, dict) or is_control_event(item):
            continue
        try:
            await ws.send_json(item)
        except (TypeError, ValueError):
            log.warning("ws: dropped an event that is not JSON-serialisable (%s)", item.get("type"))
        except Exception:  # noqa: BLE001 - the socket is gone; the receive loop notices and cleans up
            return


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    from app.security.sessions import CLOSE_SIGNED_IN_ELSEWHERE, lookup_session, session_cookie_name

    # accept-then-close so the browser receives the close code (and stops reconnecting); no data is sent
    if not _origin_allowed(ws):  # Cross-Site WebSocket Hijacking protection
        await ws.accept()
        await ws.close(code=4403)
        return
    token = ws.cookies.get(session_cookie_name())
    try:
        async with get_sessionmaker()() as db:
            found = await lookup_session(db, token)
    except Exception as exc:
        trouble = _db_busy(exc)
        if trouble is None:
            raise
        # an overloaded database (the 1,050 / 1,600-user steps of the stress test): not this socket's fault and not a
        # bug - turned away with 1013, the page comes back after a jittered back-off (00-core.js)
        _try_again_later(trouble, exc, "session check")
        await _close_quietly(ws, WS_CLOSE_TRY_AGAIN, WS_TRY_AGAIN_REASON, accept=True)
        return
    sess = found.session
    if sess is None:  # a tab of a device signed out by a sign-in elsewhere is told so (4409) and stops reconnecting
        await ws.accept()
        await ws.close(code=CLOSE_SIGNED_IN_ELSEWHERE if found.ended is not None else 4401)
        return
    user = sess.user
    session_hash = sess.id
    page_id = clean_page_id(ws.query_params.get("page"))  # which tab (IP.pageId): 4001 cancels that tab's AI work
    open_sockets = hub.connection_count(user.id)
    if open_sockets >= WS_MAX_SOCKETS_PER_USER:
        _log_cap(user.id, open_sockets)
        await ws.accept()
        await ws.close(code=WS_CLOSE_FLOOD)
        return
    await ws.accept()
    conn_id = new_conn_id()
    queue = hub.subscribe(user.id, session_hash=session_hash, page_id=page_id, conn_id=conn_id)
    presence.connected(user.id, conn_id=conn_id, page_id=page_id, session_hash=session_hash)
    close_code: int | None = None
    sender: asyncio.Task | None = None
    try:
        # a revocation that committed between the lookup above and ``hub.subscribe`` has already closed the session's
        # sockets (hub.close_sessions) - before this one existed: look again, now that this socket is subscribed
        try:
            async with get_sessionmaker()() as db:
                again = await lookup_session(db, token)
        except Exception as exc:
            trouble = _db_busy(exc)
            if trouble is None:
                raise
            _try_again_later(trouble, exc, "session re-check")
            close_code = WS_CLOSE_TRY_AGAIN  # presence hears it below: a short absence its grace period covers
            await _close_quietly(ws, WS_CLOSE_TRY_AGAIN, WS_TRY_AGAIN_REASON)
            return
        if again.session is None:
            close_code = CLOSE_SIGNED_IN_ELSEWHERE if again.ended is not None else 4401
            await ws.close(code=close_code)
            return
        status = ai.status()
        await ws.send_json({"type": "hello", "user_id": user.id, "conn_id": conn_id, "page_id": page_id,
                            "ai": {"state": status["state"], "active": status["active_label"]}})
        sender = asyncio.create_task(_pump(ws, queue), name=f"ironpulse:ws-pump:{conn_id}")
        window_start, count = time.monotonic(), 0
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                close_code = message.get("code") or 1005
                break
            raw = message.get("text")
            size = len(raw) if raw is not None else len(message.get("bytes") or b"")
            now = time.monotonic()
            if now - window_start > WS_FLOOD_WINDOW:
                window_start, count = now, 0
            count += 1
            if count > WS_FLOOD_MESSAGES or size > WS_MAX_MESSAGE_CHARS:  # flooding -> report and drop the connection
                _report_flood(ws, session_hash, "message too large" if size > WS_MAX_MESSAGE_CHARS else "too many messages")
                close_code = WS_CLOSE_FLOOD
                await ws.close(code=WS_CLOSE_FLOOD)
                break
            if raw is None:  # binary frames are not part of the protocol
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            kind = str(msg.get("type", ""))
            if kind == "ping":
                await ws.send_json({"type": "pong"})
                continue
            handler = ws_handlers.get(kind)
            if handler is not None:
                try:
                    await handler(user, msg)
                except Exception:
                    log.exception("ws handler %s failed", kind)
    except WebSocketDisconnect as exc:
        close_code = exc.code
    except RuntimeError:
        pass
    finally:
        if sender is not None:
            sender.cancel()
        conn = hub.connection(queue)
        hub.unsubscribe(user.id, queue)
        if close_code is None and conn is not None:
            close_code = conn.close_code  # we closed it (session signed out)
        presence.disconnected(user.id, conn_id=conn_id, page_id=page_id,
                              code=close_code if close_code is not None else UNKNOWN_CLOSE)
