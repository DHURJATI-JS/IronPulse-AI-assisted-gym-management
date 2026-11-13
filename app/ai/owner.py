"""Who is spending AI compute right now - a per-request / per-job ``ContextVar``.

* ``load_user`` (app/core/deps.py) sets an ``AIOwner`` for every signed-in HTTP request (user id, role,
  session hash, client IP, the browser tab's page id from the ``X-Page-Id`` header, and a probe that tells
  whether the client has disconnected).
* The background job runner (app/services/jobs.py) sets one per job (``kind="job"``, the job's user and
  exit policy).
* Periodic/system work has no owner (``None``).

The AI router (app/ai/client.py), the per-user guard (app/ai/guard.py) and the AI queue (app/ai/scheduler.py)
read it, so AI call sites never have to pass identities around. ``OwnerContextMiddleware`` resets it around
every request: httpx's ``ASGITransport`` runs requests inside the test's own task, so without the reset an
owner could leak from one request into the next.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

PAGE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

OWNER_USER = "user"  # an interactive HTTP request of a signed-in person
OWNER_JOB = "job"  # a background job run on someone's behalf (or the system's, when user_id is None)
OWNER_SYSTEM = "system"


@dataclass(slots=True)
class AIOwner:
    user_id: int | None
    role: str | None = None
    session_hash: str | None = None
    ip: str | None = None
    page_id: str | None = None  # the browser tab (IP.pageId) that asked - lets a closed tab cancel its own work
    kind: str = OWNER_USER
    job_id: int | None = None
    exit_policy: str | None = None  # jobs: keep | cancel | pause. Requests use their feature's policy.
    disconnected: Callable[[], Awaitable[bool]] | None = None  # HTTP requests: Request.is_disconnected
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_user_request(self) -> bool:
        return self.kind == OWNER_USER and self.user_id is not None


_owner: ContextVar[AIOwner | None] = ContextVar("ironpulse_ai_owner", default=None)


def current_owner() -> AIOwner | None:
    return _owner.get()


def set_owner(owner: AIOwner | None) -> Token:
    return _owner.set(owner)


def reset_owner(token: Token) -> None:
    _owner.reset(token)


@contextmanager
def owner_scope(owner: AIOwner | None) -> Iterator[AIOwner | None]:
    """``with owner_scope(AIOwner(...)):`` - attribute the AI work inside the block to ``owner``."""
    token = _owner.set(owner)
    try:
        yield owner
    finally:
        _owner.reset(token)


def clean_page_id(value: Any) -> str | None:
    """A browser-generated tab id (``IP.pageId``) or None. Never trusted for authorisation - it only scopes
    cancellation *within* the caller's own session."""
    text = value.strip() if isinstance(value, str) else ""
    return text if PAGE_ID_RE.match(text) else None


class OwnerContextMiddleware:
    """Pure ASGI middleware: every HTTP/WebSocket request starts - and ends - with no AI owner."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        token = _owner.set(None)
        try:
            await self.app(scope, receive, send)
        finally:
            _owner.reset(token)
