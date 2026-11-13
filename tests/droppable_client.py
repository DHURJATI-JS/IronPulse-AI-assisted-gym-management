"""A test client whose connection can drop in the middle of a request - a browser closing the assistant sheet or
the tab while the AI is still answering.

httpx's ``ASGITransport`` never reports a disconnect, so ``Request.is_disconnected()`` (the probe the AI owner of a
signed-in request carries, app/core/deps.py) would always say "still here". This wraps the real app: once ``gone`` is
set, ``receive`` returns ``{"type": "http.disconnect"}`` - exactly what uvicorn does when the browser aborts - so the
AI router's disconnect watcher (app/ai/client.py) and the routes' own checks see the client leave."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx


def droppable_client(gone: asyncio.Event, ip: str = "127.0.0.1") -> httpx.AsyncClient:
    from app.main import app as fastapi_app

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await fastapi_app(scope, receive, send)
            return
        body_done = False

        async def receive_or_disconnect() -> dict[str, Any]:
            nonlocal body_done
            if body_done:
                if not gone.is_set():
                    await gone.wait()  # is_disconnected() polls inside a cancelled scope: this wait is interrupted
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request" and not message.get("more_body", False):
                body_done = True
            return message

        await fastapi_app(scope, receive_or_disconnect, send)

    transport = httpx.ASGITransport(app=app, client=(ip, 51234))
    return httpx.AsyncClient(transport=transport, base_url="http://testserver", follow_redirects=False,
                             headers={"User-Agent": "pytest-ironpulse/1.0"})
