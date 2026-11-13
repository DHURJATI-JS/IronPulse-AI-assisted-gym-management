"""AI plan advisor (signed-in users only), streamed so the first words appear in about a second.

POST /api/plans/ask {"question": str, "gym_slug": str|null}  -> text/event-stream of JSON events:
    {"type": "meta", "provider": "Gemma (gateway)" | null, "cached"?: true, "busy"?: true}
    {"type": "chunk", "text": "..."}
    {"type": "blocked", "code": "quota" | "cooldown" | "too_many" | "duplicate" | "spam", "detail": "...",
     "retry_after": seconds}                                  (instead of an answer; then done with "blocked": true)
    {"type": "done", "fallback": bool, "disclaimer": "...", "note"?: "...", "busy"?: true} | {"type": "done", "blocked": true}

The AI guard and the fair AI queue run inside ``ai.stream`` (docs/ai-guard-and-queue.md). The guard judges the
member's own words (``user_text``) in the scope of the gym they ask about (``dedupe_scope='gym:<id>'``), so the same
suggested question on a second gym's page is not a duplicate. A refusal is a 'blocked' event - the client starts its
cooldown from ``retry_after`` - except 'busy' (queue full or waited too long), which keeps the deterministic
recommendation. When the requester leaves (tab closed, logged out) the stream just ends, and an answer the router cut
short (``meta['truncated']``: out of time, interrupted, stopped) is shown but never cached. The request's DB session is
released before the stream starts, so a slow answer (or a wait in the AI queue) never holds a pooled connection.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Mapping
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, ValidationError
from starlette.requests import ClientDisconnect
from starlette.types import Receive, Scope, Send

from app.ai.client import AI_DISCLAIMER, AIUnavailableError, ai
from app.ai.errors import AICancelled, AIRejected
from app.ai.owner import AIOwner, current_owner
from app.core.deps import DB, AuthUser
from app.core.state import register_reset
from app.services.plan_advice import SYSTEM, build_context, build_prompt, deterministic_answer, guard_scope

log = logging.getLogger("ironpulse.ai.plans")
router = APIRouter()
_CACHE: OrderedDict[str, tuple[float, str, str]] = OrderedDict()  # key -> (expires, text, provider label)
CACHE_TTL = 600.0
CACHE_MAX = 300
MAX_TOKENS = 260
FIRST_TOKEN_TIMEOUT = 10.0  # per provider, counted once it holds its slot (never the wait in the AI queue)
TOTAL_TIMEOUT = 30.0  # the whole answer, from its provider's slot; a longer one is cut (meta['truncated'])
UNAVAILABLE_NOTE = "AI is unavailable right now - this is an automatic recommendation."
BUSY_NOTE = "AI is busy right now - here is a standard answer"
SSE_HEADERS = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
register_reset(_CACHE.clear, name="app.routers.plan_ai")  # tests: no answer outlives its test


class AskIn(BaseModel):
    question: str = Field(min_length=2, max_length=500)
    gym_slug: str | None = Field(default=None, max_length=140)


class EventStreamResponse(StreamingResponse):
    """A ``text/event-stream`` response that always closes its event generator when the response ends.

    Starlette merely stops iterating the generator when the client goes away while it is suspended at a ``yield`` (the
    ASGI spec 2.4 ``ClientDisconnect`` path, or a cancel delivered while a chunk was being sent). Closing it here runs
    the generator's cleanup at once - ``contextlib.aclosing`` around ``ai.stream`` - so the AI queue ticket, the guard
    admission and the provider stream are released now, not whenever the garbage collector finalises the generator.
    A client that went away (``ClientDisconnect``) is a closed tab, not a server error: it is not re-raised.
    Also used by the shop's product Q&A stream (app/routers/shop.py)."""

    def __init__(self, content: AsyncIterator[Any], *, headers: Mapping[str, str] | None = None) -> None:
        super().__init__(content, media_type="text/event-stream", headers={**SSE_HEADERS, **(headers or {})})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        except ClientDisconnect:
            log.debug("event stream: the client went away")
        finally:
            close = getattr(self.body_iterator, "aclose", None)
            if close is not None:
                try:
                    await close()  # a generator that already finished: no-op
                except Exception:  # noqa: BLE001 - cleanup must never turn a sent response into an error
                    log.debug("closing an event stream failed", exc_info=True)


def _sse(event: dict) -> bytes:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode()


def _remember(key: str, text: str, label: str) -> None:
    _CACHE[key] = (time.monotonic() + CACHE_TTL, text, label)
    _CACHE.move_to_end(key)
    while len(_CACHE) > CACHE_MAX:
        _CACHE.popitem(last=False)


def _fallback(text: str, *, busy: bool) -> list[bytes]:
    """The deterministic recommendation (AI offline, or the AI queue is too busy right now)."""
    meta: dict[str, Any] = {"type": "meta", "provider": None}
    done: dict[str, Any] = {"type": "done", "fallback": True, "disclaimer": AI_DISCLAIMER,
                            "note": BUSY_NOTE if busy else UNAVAILABLE_NOTE}
    if busy:
        meta["busy"] = done["busy"] = True
    return [_sse(meta), _sse({"type": "chunk", "text": text}), _sse(done)]


def _blocked(exc: AIRejected) -> list[bytes]:
    """The AI guard (or the queue's per-user limit) refused: say why and when to ask again - instead of an answer."""
    return [_sse({"type": "blocked", "code": exc.code, "detail": exc.message, "retry_after": exc.retry_after}),
            _sse({"type": "done", "blocked": True})]


async def answer_events(*, prompt: str, cache_key: str, fallback_text: str, user_text: str,
                        owner: AIOwner | None, dedupe_scope: str | None = None) -> AsyncIterator[bytes]:
    """The SSE events of one answer. Touches no ORM object: everything it needs is passed in, computed before the
    route released its DB session. ``user_text`` is the member's question as they typed it (the guard's spam,
    injection and duplicate checks) and ``dedupe_scope`` the gym it is about ('gym:<id>'; None on the generic page)."""
    hit = _CACHE.get(cache_key)
    if hit and hit[0] > time.monotonic():
        yield _sse({"type": "meta", "provider": hit[2], "cached": True})
        yield _sse({"type": "chunk", "text": hit[1]})
        yield _sse({"type": "done", "fallback": False, "disclaimer": AI_DISCLAIMER})
        return
    meta: dict[str, Any] = {}
    parts: list[str] = []
    try:
        # aclosing: however this generator ends - finished, failed, or closed at a yield because the client left -
        # ai.stream is closed right away, releasing its queue ticket, guard admission and provider stream
        async with contextlib.aclosing(ai.stream(
            feature="plans", system=SYSTEM, prompt=prompt, max_tokens=MAX_TOKENS,
            first_token_timeout=FIRST_TOKEN_TIMEOUT, total_timeout=TOTAL_TIMEOUT, meta=meta, user_text=user_text,
            owner=owner, dedupe_scope=dedupe_scope,
        )) as stream:
            async for chunk in stream:
                if not parts:
                    yield _sse({"type": "meta", "provider": meta.get("label")})
                parts.append(chunk)
                yield _sse({"type": "chunk", "text": chunk})
    except AIRejected as exc:  # refused before any compute was spent
        for event in _fallback(fallback_text, busy=True) if exc.code == "busy" else _blocked(exc):
            yield event
        return
    except AICancelled:  # the requester left (tab closed, logged out) before the first words: nobody is listening
        return
    except AIUnavailableError:  # every provider is down
        for event in _fallback(fallback_text, busy=False):
            yield event
        return
    except Exception:  # never let an AI hiccup break the widget
        log.exception("plan advisor stream failed")
        if not parts:
            for event in _fallback(fallback_text, busy=False):
                yield event
        return
    if meta.get("cancelled"):
        return  # the queue cut the answer short because the requester left: incomplete, never cached
    text = "".join(parts).strip()
    # ai.stream flags an answer it cut short (out of time, provider failed mid-answer): shown, never cached as complete.
    # The flag is set by the time the stream is closed (the aclosing block above), whatever its last words were.
    if text and not meta.get("truncated"):
        _remember(cache_key, text, meta.get("label") or "AI")
    yield _sse({"type": "done", "fallback": False, "disclaimer": AI_DISCLAIMER})


@router.post("/api/plans/ask")
async def ask_about_plans(request: Request, db: DB, user: AuthUser):
    try:
        body = AskIn.model_validate(await request.json())
    except (ValidationError, ValueError):
        return JSONResponse({"error": "Invalid input", "detail": "Ask a question of 2-500 characters."}, status_code=422)
    question = " ".join(body.question.split())
    if len(question) < 2:
        return JSONResponse({"error": "Invalid input", "detail": "Ask a question of 2-500 characters."}, status_code=422)
    ctx = await build_context(db, user, body.gym_slug)
    prompt = build_prompt(ctx, question)
    # an answer built from a member's own saved data (conditions, injuries, preferences) is cached for that member
    # alone - not even someone with an identical profile learns it was asked
    scope = f"member:{user.id}" if ctx.personal else "shared"
    events = answer_events(
        prompt=prompt,
        # shared only between identical prompts: the prompt holds member data (age, goal, conditions, distance to the
        # nearest branch), and a summarised key once let one member's personalised answer reach another member
        cache_key=hashlib.sha256(f"{scope}\n{SYSTEM}\n{prompt}".encode()).hexdigest(),
        fallback_text=deterministic_answer(ctx, question),
        user_text=question,  # the member's own words - never the prompt
        dedupe_scope=guard_scope(ctx),  # ... about this gym: the same question about another gym is not a duplicate
        owner=current_owner(),
    )
    await db.close()  # everything the stream needs is computed: no pooled connection is held while the AI answers
    return EventStreamResponse(events)
