"""AI error types shared by the provider router, the per-user guard and the AI queue.

Every class subclasses ``AIUnavailableError``, so the existing ``except AIUnavailableError`` fallbacks
(deterministic answers, "AI unavailable" banners) keep working unchanged:

    AIUnavailableError      no provider produced an answer (offline / every provider failed)
      +- AIRejected         refused before any compute: per-user quota, cooldown, duplicate, spam, queue full
      +- AICancelled        the requester left (page closed, logged out, disconnected) - the work was stopped

Routes that want to tell the user *why* check ``isinstance(exc, AIRejected)`` and use ``exc.payload()``;
``app/core/errors.py`` turns an uncaught ``AIRejected`` into a 429/409/422/503 JSON (with ``Retry-After``)
or an HTML error page.
"""

from __future__ import annotations

from typing import Any


class AIUnavailableError(RuntimeError):
    """Raised when no AI provider could produce a valid answer."""


class AIRejected(AIUnavailableError):
    """The request was refused before spending any AI compute."""

    STATUS = {
        "quota": 429,  # per-user limit for this kind of request reached
        "cooldown": 429,  # AI paused for this account after repeated abuse
        "too_many": 429,  # too many AI requests of this user queued/running at once
        "duplicate": 409,  # the same text was just sent
        "spam": 422,  # the text looks like spam (repeated characters, link floods ...)
        "busy": 503,  # the AI queue is full / the wait was too long - try again shortly
        "disabled": 403,  # AI is not available for this account
    }
    TITLES = {
        "quota": "AI limit reached",
        "cooldown": "AI paused",
        "too_many": "Too many AI requests",
        "duplicate": "Already asked",
        "spam": "Message blocked",
        "busy": "AI is busy",
        "disabled": "AI not available",
    }

    def __init__(self, code: str, message: str, *, retry_after: float = 0, feature: str | None = None) -> None:
        super().__init__(message)
        self.code = code if code in self.STATUS else "quota"
        self.message = message
        self.retry_after = max(0, int(round(retry_after)))
        self.feature = feature

    @property
    def status(self) -> int:
        return self.STATUS[self.code]

    @property
    def title(self) -> str:
        return self.TITLES[self.code]

    def payload(self) -> dict[str, Any]:
        """JSON body for API clients (IP.api exposes ``code`` and ``retry_after`` to the widgets)."""
        return {"error": self.title, "detail": self.message, "code": self.code, "retry_after": self.retry_after,
                "ai_available": True}

    def headers(self) -> dict[str, str]:
        return {"Retry-After": str(self.retry_after)} if self.retry_after else {}


class AICancelled(AIUnavailableError):
    """The work was stopped because whoever asked for it is gone (nobody would see the answer)."""

    def __init__(self, reason: str = "cancelled") -> None:
        super().__init__(f"AI work stopped ({reason})")
        self.reason = reason
