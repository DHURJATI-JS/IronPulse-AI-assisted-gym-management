"""AI provider router with strict, ordered failover.

    1. ``gateway``   - Gemma behind the user's OpenAI-compatible gateway (llama.cpp), via the OpenAI SDK
    2. ``docker``    - Gemma in a local Docker container (Ollama, OpenAI-compatible API)
    3. ``anthropic`` - Claude via the Anthropic SDK (only if ANTHROPIC_API_KEY is set)

If every provider fails, ``AIUnavailableError`` is raised and ``ai.status()`` reports ``offline`` -
the UI then shows "AI unavailable" while non-AI features (pre-recorded videos, rule-based safety
checks, payroll maths ...) keep working.

Every call is attributed to an owner (``current_owner()``, app/ai/owner.py) and goes through, in order
(docs/ai-guard-and-queue.md section 4):

    1. ``guard.admit``         per-user quotas, duplicate/spam checks (in the caller's ``dedupe_scope``), cooldowns
                               -> ``AIRejected`` before any work
    2. ``scheduler.acquire``   the fair AI queue (bounded concurrency, priorities, per-user fairness) -> may refuse
                               with ``AIRejected`` ('too_many' / 'busy') or stop with ``AICancelled``; while a signed-in
                               request waits there, its HTTP disconnect watcher already runs (browser gone ->
                               ``AICancelled('client_left')`` at once, the place in the queue is freed)
    3. the providers           ``complete()`` runs them in a child task the queue can cancel (page closed, user
                               left, HTTP client disconnected -> ``AICancelled``); ``stream()`` checks the ticket
                               between chunks and flags an answer it cut short (``meta['truncated']``)
    4. ``finally``             release the ticket, ``guard.settle`` and a ``CallLog`` row (user, owner kind, queue
                               time, cancelled)

What a call costs: work that never reached a provider is free - refused, cancelled while queued or while waiting for
a provider slot, a stream stopped before its first words - and so is a call every provider failed (a stream cut by a
provider failure included). Once a provider worked on it, the charge stands however the requester stopped it: a
stream whose first words went out, a completion whose provider slot was held, then dropped connection, closed tab or
cancelled caller (the same question may be asked again at once - a new, charged one). Dropping answers again and
again is an abuse signal (``guard.note_abort``: strike points beyond ``policy.ABORT_FREE``).

Text enters here once: ``system``, ``prompt``, ``user_text`` and ``dedupe_scope`` go through ``guard.clean_text``
(what UTF-8 cannot encode - lone surrogates - becomes U+FFFD) before the guard and before any provider SDK sees them.
A request a provider still cannot encode is that request's error: it never counts against the provider's circuit
breaker, which would take AI offline for everyone.

Waiting - in the queue or for a provider's concurrency slot - is never a provider failure: provider timeouts start
once a provider slot is held, and a slot that stays taken for a whole timeout just moves on to the next provider
without touching the circuit breaker. A provider's slots are handed out fairly (``_FairSlots``): when they are all
taken, the next free one goes to the waiting requester served least recently, so one person's second request never
jumps ahead of somebody else's first (Ollama runs one call at a time).

Failover (README "Local fallback LLM"): a provider that is down (connection refused, or no TCP connection within
``CONNECT_TIMEOUT_SECONDS``), erroring (HTTP 5xx), malformed (bad JSON, broken SSE) or too slow (no first words within
the first-token deadline) is skipped for the next one in AI_PROVIDER_ORDER. A stream that breaks before its first words
reached anybody moves on to the next provider; once words went out the answer ends with ``INTERRUPTED_MARKER`` and is
flagged ``meta['truncated']`` - two different answers are never spliced together. Each provider has a circuit breaker:
``ai_circuit_failures`` consecutive failures (a stream dying mid-answer included) skip it for
``ai_circuit_cooldown_seconds``; then the next call tries it again (half-open) and a success closes it.

Usage::

    from app.ai.client import ai, AIUnavailableError
    result = await ai.complete(feature="coach", system="...", prompt="...", schema=MyModel)
    result.data  # validated MyModel instance
"""

from __future__ import annotations

import asyncio
import base64
import io
import itertools
import logging
import os
import time
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel
from sqlalchemy import event as _sa_event
from sqlalchemy.orm import Session as _OrmSession

from app.ai import guard as guard_module  # module, not the object: tests swap ``guard_module.guard``
from app.ai import simulated  # the load-test provider's engine (pure: imports nothing from here)
from app.ai.errors import AICancelled, AIRejected, AIUnavailableError  # noqa: F401 - AIRejected re-exported
from app.ai.jsonutil import parse_model, schema_hint, strict_json_schema
from app.ai.owner import AIOwner, current_owner
from app.ai.policy import EXIT_KEEP
from app.ai.scheduler import Ticket, scheduler
from app.config import Settings, get_settings, simulated_ai_refusal, simulated_ai_warning, warn_at_startup
from app.core.state import register_reset

log = logging.getLogger("ironpulse.ai")
T = TypeVar("T", bound=BaseModel)

DISCONNECT_POLL_SECONDS = 0.5  # how often an HTTP request's AI call (queued or running) checks its client is there
# How long a provider may take to accept the TCP (+TLS) connection. A gateway PC that is switched off, or a Docker
# container that is not running, then fails over in about this long instead of the whole provider timeout (90-180 s).
# AI_CONNECT_TIMEOUT_SECONDS overrides it once the settings declare it; Claude (over the internet) gets at least
# CLOUD_CONNECT_TIMEOUT_SECONDS.
CONNECT_TIMEOUT_SECONDS = 2.0
FIRST_TOKEN_MARGIN = 5.0  # a raised first-token deadline leaves at least this much of the answer's budget for words
CLOUD_CONNECT_TIMEOUT_SECONDS = 5.0
FAIR_SLOT_MEMORY = 1024  # requesters whose last provider slot each provider remembers (fair hand-out of slots)
# The visible end of an answer AIRouter.stream() cut short. Consumers test ``meta.get('truncated')``, not these strings.
CUT_MARKER = " …"  # total_timeout used up
INTERRUPTED_MARKER = " … (the answer was interrupted)"  # the provider failed after its first words
# Stops a requester causes at will, mid-answer: the browser's connection dropped, its tab closed, the response stream
# that reads the answer was cancelled ('aborted'). Repeated, they earn strike points (guard.note_abort).
ABORT_REASONS = frozenset({"client_left", "page_closed", "aborted"})


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, availability: bool = True) -> None:
        super().__init__(message)
        self.availability = availability  # False = the provider is up but the output was unusable


def _availability(exc: BaseException) -> bool:
    """Does this failure of a provider attempt say the provider is down (counted towards its circuit breaker)? A request
    the SDK could not even encode (UnicodeEncodeError while building it: nothing was sent) is that request's own
    error, never the provider's - one person's bad input must not take AI offline for everyone."""
    if isinstance(exc, ProviderError):
        return exc.availability
    return not isinstance(exc, UnicodeEncodeError)


def connect_timeout_for(settings: Any, *, floor: float = 0.0) -> float:
    """Seconds a provider may take to accept its connection: AI_CONNECT_TIMEOUT_SECONDS when the settings declare it,
    else CONNECT_TIMEOUT_SECONDS; at least ``floor`` (Claude: CLOUD_CONNECT_TIMEOUT_SECONDS), within 0.2..30 s."""
    raw = getattr(settings, "ai_connect_timeout_seconds", None)
    try:
        value = float(raw) if raw is not None else CONNECT_TIMEOUT_SECONDS
    except (TypeError, ValueError):
        value = CONNECT_TIMEOUT_SECONDS
    if value != value:  # NaN
        value = CONNECT_TIMEOUT_SECONDS
    return max(floor, min(30.0, max(0.2, value)))


class ProviderBusy(ProviderError):
    """Every concurrency slot of the provider stayed taken for the whole timeout: the provider is busy, not down.
    The router moves on to the next provider and the circuit breaker never hears about it."""

    def __init__(self, message: str) -> None:
        super().__init__(message, availability=False)


class StreamCut(ProviderError):
    """A provider stream stopped at max_tokens (finish_reason 'length' / stop_reason 'max_tokens'): the answer is cut
    short. After its first words the router ends it with CUT_MARKER and flags it ``truncated`` (like total_timeout);
    before any words it is unusable output - the next provider is tried and the circuit breaker never hears of it."""

    def __init__(self, message: str = "the answer reached max_tokens") -> None:
        super().__init__(message, availability=False)


@dataclass(slots=True)
class AIResult:
    text: str
    data: Any = None
    provider: str = ""
    model: str = ""
    latency_ms: int = 0


@dataclass(slots=True)
class CallLog:
    feature: str
    provider: str
    model: str
    ok: bool
    latency_ms: int  # provider time only (waiting for the queue or for a provider slot is not included)
    error: str | None
    user_id: int | None = None  # who spent the compute (None = system work)
    owner_kind: str | None = None  # user | job | system
    queue_ms: int | None = None  # time the call waited in the AI queue before it could start
    cancelled: bool = False  # stopped because whoever asked left (page closed, disconnected, logged out ...)


call_log_queue: deque[CallLog] = deque(maxlen=5000)


# --------------------------------------------------------------------------- images
def prepare_image(data: bytes, max_side: int = 1024, quality: int = 85) -> bytes:
    """Downscale + re-encode to JPEG (keeps prompts small; strips metadata). Decoded only through
    ``storage.decode_image`` - the pixel count is checked on the header before any pixel is decoded, so bytes that
    reach AI vision can never be a decompression bomb (``storage.UploadError`` with a friendly text). Blocking: call it
    through ``storage.run_image_decode``."""
    from app.services.storage import decode_image

    im = decode_image(data, max_side)
    out = io.BytesIO()
    im.save(out, "JPEG", quality=quality, optimize=True)
    return out.getvalue()


# --------------------------------------------------------------------------- provider slots
class _SlotTiming:
    """Filled in while a provider call waits for and holds a concurrency slot, so the router can keep the slot wait
    out of timeouts and latency figures. ``on_acquired`` lets the router restart its first-token clock; ``bucket`` is
    the requester (the queue ticket's (kind, user id)) the provider's slots are shared out fairly by."""

    __slots__ = ("wait", "acquired_at", "on_acquired", "bucket")

    def __init__(self, bucket: Any = None) -> None:
        self.wait = 0.0
        self.acquired_at: float | None = None
        self.on_acquired: Callable[[], None] | None = None
        self.bucket = bucket

    def acquired(self, waited: float) -> None:
        self.wait += waited
        self.acquired_at = time.perf_counter()
        if self.on_acquired is not None:
            try:
                self.on_acquired()
            except Exception:  # noqa: BLE001 - never lose a held slot over bookkeeping
                log.debug("slot callback failed", exc_info=True)


# set by the router around one provider attempt; providers report their slot through it (same task, same context)
_slot_timing: ContextVar[_SlotTiming | None] = ContextVar("ironpulse_ai_slot_timing", default=None)


@dataclass(slots=True, frozen=True)
class CallInfo:
    """Who asked what, visible to the provider of one attempt (``current_call()``). Only the simulated provider reads
    it (its bug-hunt aids: AI_SIM_ECHO_NONCE, AI_SIM_CAPTURE_PROMPTS); real providers get system + prompt only."""

    feature: str
    kind: str  # 'complete' | 'stream'
    user_id: int | None = None
    owner_kind: str | None = None
    user_text: str | None = None


_call_info: ContextVar[CallInfo | None] = ContextVar("ironpulse_ai_call_info", default=None)


def current_call() -> CallInfo | None:
    """The call the running provider attempt serves (None outside the router, e.g. a provider used directly)."""
    return _call_info.get()


class _FairSlots:
    """A provider's concurrency slots: ``asyncio.Semaphore`` (which it replaces - same ``acquire``/``release``/
    ``locked``), but a freed slot goes to the waiting requester (``bucket``: one user's requests, one user's jobs, the
    system) that was served least recently - FIFO inside one requester. With Ollama's single slot, one person's second
    request therefore never jumps ahead of another person's first. Cancelled or timed-out waiters leave the line; a slot
    granted to a waiter cancelled at that very moment is passed on, never lost."""

    def __init__(self, size: int) -> None:
        self._free = max(1, int(size))
        self._waiters: dict[Any, deque[asyncio.Future]] = {}
        self._served: OrderedDict[Any, int] = OrderedDict()  # bucket -> number of its latest grant (LRU, bounded)
        self._grants = itertools.count(1)
        self._arrivals = itertools.count(1)
        self._arrival: dict[asyncio.Future, int] = {}

    def locked(self) -> bool:
        return self._free == 0

    @property
    def _value(self) -> int:
        """Free slots, under ``asyncio.Semaphore``'s name (tests written against the semaphore read it)."""
        return self._free

    def waiting(self) -> int:
        return sum(len(q) for q in self._waiters.values())

    def _note(self, bucket: Any) -> None:
        self._served[bucket] = next(self._grants)
        self._served.move_to_end(bucket)
        while len(self._served) > FAIR_SLOT_MEMORY:
            self._served.popitem(last=False)

    async def acquire(self, bucket: Any = None) -> bool:
        if self._free > 0 and not self._waiters:
            self._free -= 1
            self._note(bucket)
            return True
        fut = asyncio.get_running_loop().create_future()
        self._arrival[fut] = next(self._arrivals)
        self._waiters.setdefault(bucket, deque()).append(fut)
        try:
            await fut
        except BaseException:
            if fut.done() and not fut.cancelled():  # granted while it was being cancelled: hand the slot on
                self.release()
            else:
                queue = self._waiters.get(bucket)
                if queue is not None:
                    try:
                        queue.remove(fut)
                    except ValueError:
                        pass
                    if not queue:
                        del self._waiters[bucket]
            raise
        finally:
            self._arrival.pop(fut, None)
        return True

    def release(self) -> None:
        while self._waiters:
            # the requester served longest ago (never = 0) goes first; ties: whoever has waited longest
            bucket = min(self._waiters, key=lambda b: (self._served.get(b, 0),
                                                       self._arrival.get(self._waiters[b][0], 0)))
            queue = self._waiters[bucket]
            fut = queue.popleft()
            if not queue:
                del self._waiters[bucket]
            if not fut.done():
                fut.set_result(None)
                self._note(bucket)
                return
        self._free += 1


class _ProviderSlot:
    """``async with provider.slot():`` - hold one of the provider's concurrency slots and report the wait."""

    __slots__ = ("provider",)

    def __init__(self, provider: BaseProvider) -> None:
        self.provider = provider

    async def __aenter__(self) -> None:
        timing = _slot_timing.get()
        started = time.perf_counter()
        await self.provider.semaphore.acquire(timing.bucket if timing is not None else None)
        if timing is not None:
            timing.acquired(time.perf_counter() - started)

    async def __aexit__(self, *exc: object) -> bool:
        self.provider.semaphore.release()
        return False


# --------------------------------------------------------------------------- providers
@dataclass
class ProviderState:
    healthy: bool | None = None  # None = unknown yet
    last_ok_at: float | None = None
    last_error: str | None = None
    last_checked_at: float | None = None
    consecutive_failures: int = 0
    open_until: float = 0.0
    calls: int = 0
    failures: int = 0
    detail: dict[str, Any] = field(default_factory=dict)


class BaseProvider:
    name = "base"
    label = "Provider"
    # a slow local model (the Docker fallback on a laptop CPU: 5-15 s to its first word) gets at least this long for
    # its first words in a stream - see ``first_token_allowance``; 0 = the feature's own deadline
    min_first_token_seconds = 0.0

    def __init__(self, settings: Settings, model: str, max_concurrency: int) -> None:
        self.settings = settings
        self.model = model
        self.semaphore = _FairSlots(max(1, max_concurrency))  # the provider's concurrency slots, shared out fairly
        self.state = ProviderState()

    def first_token_allowance(self, requested: float, total: float) -> float:
        """This provider's first-token deadline for a stream: the feature's, raised to ``min_first_token_seconds`` for a
        slow local model - never past the feature's whole-answer budget (``total`` minus FIRST_TOKEN_MARGIN s, so some
        words still fit; the whole answer stays bounded by ``total``, so the page's own timeout is never outrun)."""
        floor = min(float(self.min_first_token_seconds or 0.0), total - FIRST_TOKEN_MARGIN)
        return max(requested, floor)

    # -- to override
    @property
    def configured(self) -> bool:  # pragma: no cover - overridden
        return False

    async def _complete(self, *, system: str, prompt: str, images: list[bytes], max_tokens: int,
                        temperature: float, schema: type[BaseModel] | None) -> str:  # pragma: no cover
        raise NotImplementedError

    async def ping(self) -> bool:  # pragma: no cover
        return False

    async def stream_text(self, *, system: str, prompt: str, max_tokens: int, temperature: float):
        """Default streaming: one chunk with the whole completion (providers override with real streaming).

        Overrides take their concurrency slot with ``async with self.slot():`` - the router then starts the
        first-token clock once the slot is held and treats a slot that never frees up as 'busy', not 'down'."""
        async with self.slot():
            self.state.calls += 1
            text = await self._complete(system=system, prompt=prompt, images=[], max_tokens=max_tokens,
                                        temperature=temperature, schema=None)
        yield text

    def slot(self) -> _ProviderSlot:
        """Concurrency slot for one call. Streams hold it across their yields."""
        return _ProviderSlot(self)

    # -- circuit breaker
    def circuit_open(self) -> bool:
        return time.monotonic() < self.state.open_until

    def record_success(self) -> None:
        self.state.healthy = True
        self.state.last_ok_at = time.time()
        self.state.consecutive_failures = 0
        self.state.open_until = 0.0
        self.state.last_error = None

    def record_alive(self) -> None:
        """A stream's first words arrived: the provider is up - but the answer is not finished, so earlier failures
        still count until it is (a provider that keeps dying mid-answer must still open its circuit breaker)."""
        self.state.healthy = True
        self.state.last_ok_at = time.time()

    def record_failure(self, error: str, *, availability: bool = True) -> None:
        self.state.failures += 1
        self.state.last_error = error[:300]
        if not availability:
            return
        self.state.consecutive_failures += 1
        if self.state.consecutive_failures >= self.settings.ai_circuit_failures:
            self.state.healthy = False
            self.state.open_until = time.monotonic() + self.settings.ai_circuit_cooldown_seconds

    async def complete(self, *, timeout: float | None = None, **kwargs: Any) -> str:
        """One call under a concurrency slot. ``timeout`` bounds the call itself and starts once the slot is held;
        waiting longer than ``timeout`` for a slot raises ProviderBusy (busy, not down)."""
        timing = _slot_timing.get()
        bucket = timing.bucket if timing is not None else None
        started = time.perf_counter()
        if timeout is None:
            await self.semaphore.acquire(bucket)
        else:
            try:
                async with asyncio.timeout(timeout):
                    await self.semaphore.acquire(bucket)
            except TimeoutError as exc:
                raise ProviderBusy(f"busy: no free slot within {timeout:.0f}s") from exc
        try:
            if timing is not None:
                timing.acquired(time.perf_counter() - started)
            self.state.calls += 1
            if timeout is None:
                return await self._complete(**kwargs)
            try:
                async with asyncio.timeout(timeout):
                    return await self._complete(**kwargs)
            except TimeoutError as exc:
                raise ProviderError(f"no answer within {timeout:.0f}s") from exc
        finally:
            self.semaphore.release()

    def describe(self) -> dict[str, Any]:
        st = self.state
        if not self.configured:
            status = "not_configured"
        elif self.circuit_open() or st.healthy is False:
            status = "down"
        elif st.healthy:
            status = "up"
        else:
            status = "unknown"
        return {
            "name": self.name,
            "label": self.label,
            "model": self.model,
            "configured": self.configured,
            "status": status,
            "last_error": st.last_error,
            "last_ok_at": st.last_ok_at,
            "calls": st.calls,
            "failures": st.failures,
            "detail": st.detail,
        }


class OpenAICompatProvider(BaseProvider):
    """Gemma via any OpenAI-compatible server (llama.cpp gateway, Ollama in Docker)."""

    def __init__(self, settings: Settings, *, name: str, label: str, base_url: str | None, api_key: str,
                 model: str, timeout: float, max_concurrency: int, status_url: str | None = None,
                 connect_timeout: float | None = None) -> None:
        super().__init__(settings, model, max_concurrency)
        self.name = name
        self.label = label
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or "none"
        self.timeout = timeout
        # a switched-off gateway PC or a stopped container fails over in ~2 s, not after the whole timeout
        self.connect_timeout = min(float(timeout), connect_timeout if connect_timeout is not None
                                   else connect_timeout_for(settings))
        self.status_url = status_url
        self._client: Any = None
        self._json_mode_supported = settings.ai_json_mode

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def http_timeout(self) -> Any:
        """The SDK's own Timeout type (the OpenAI SDK runs on httpx2, not httpx): the whole call, short connect."""
        import openai

        return openai.Timeout(self.timeout, connect=self.connect_timeout)

    def client(self) -> Any:
        if self._client is None:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(base_url=self.base_url, api_key=self.api_key, timeout=self.http_timeout(),
                                       max_retries=0)
        return self._client

    async def _complete(self, *, system: str, prompt: str, images: list[bytes], max_tokens: int,
                        temperature: float, schema: type[BaseModel] | None) -> str:
        import openai

        content: Any = prompt
        if images:
            content = [{"type": "text", "text": prompt}] + [
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(img).decode()}}
                for img in images
            ]
        messages = [{"role": "system", "content": system}, {"role": "user", "content": content}]
        kwargs: dict[str, Any] = {"model": self.model, "messages": messages, "max_tokens": max_tokens,
                                  "temperature": temperature}
        if schema is not None and self._json_mode_supported:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            resp = await self.client().chat.completions.create(**kwargs)
        except openai.BadRequestError as exc:
            if "response_format" in kwargs:  # server without JSON mode: retry once without it
                self._json_mode_supported = False
                kwargs.pop("response_format")
                resp = await self.client().chat.completions.create(**kwargs)
            else:
                raise ProviderError(f"bad request: {exc}", availability=False) from exc
        except (openai.APIConnectionError, openai.APITimeoutError) as exc:
            raise ProviderError(f"unreachable: {exc.__class__.__name__}") from exc
        except openai.APIStatusError as exc:
            raise ProviderError(f"HTTP {exc.status_code}") from exc
        if not resp.choices:
            raise ProviderError("empty response", availability=False)
        text = resp.choices[0].message.content or ""
        if not text.strip():
            raise ProviderError("empty completion", availability=False)
        return text

    async def stream_text(self, *, system: str, prompt: str, max_tokens: int, temperature: float):
        """Real token streaming from the OpenAI-compatible server (llama.cpp gateway / Ollama)."""
        import openai

        async with self.slot():
            self.state.calls += 1
            try:
                stream = await self.client().chat.completions.create(
                    model=self.model,
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                    max_tokens=max_tokens, temperature=temperature, stream=True,
                )
            except openai.BadRequestError as exc:
                raise ProviderError(f"bad request: {exc}", availability=False) from exc
            except (openai.APIConnectionError, openai.APITimeoutError) as exc:
                raise ProviderError(f"unreachable: {exc.__class__.__name__}") from exc
            except openai.APIStatusError as exc:
                raise ProviderError(f"HTTP {exc.status_code}") from exc
            # ``async with`` closes the HTTP response however this generator ends: normally, on an error, on a
            # cancellation, or when the consumer closes it at a ``yield`` - otherwise the gateway would keep
            # generating until the stream object happens to be garbage-collected
            async with stream:
                finish: str | None = None
                try:
                    async for event in stream:
                        if not event.choices:
                            continue
                        choice = event.choices[0]
                        finish = getattr(choice, "finish_reason", None) or finish
                        delta = getattr(choice, "delta", None)
                        piece = getattr(delta, "content", None) if delta is not None else None
                        if piece:
                            yield piece
                except (openai.APIConnectionError, openai.APITimeoutError) as exc:
                    raise ProviderError(f"unreachable: {exc.__class__.__name__}") from exc
                except openai.APIStatusError as exc:
                    raise ProviderError(f"HTTP {exc.status_code}") from exc
                if finish == "length":  # the model hit max_tokens mid-answer: say so instead of looking complete
                    raise StreamCut()

    async def ping(self) -> bool:
        if not self.configured:
            return False
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=min(5.0, self.connect_timeout))) as client:
                r = await client.get(f"{self.base_url}/models", headers={"Authorization": f"Bearer {self.api_key}"})
                ok = r.status_code == 200
                if ok and self.status_url:
                    try:
                        s = await client.get(self.status_url)
                        if s.status_code == 200:
                            data = s.json()
                            self.state.detail = {
                                "backends": [
                                    {"name": b.get("name"), "online": b.get("online"), "busy": b.get("busy_slots")}
                                    for b in data.get("backends", [])
                                ]
                            }
                    except Exception:
                        pass
                return ok
        except Exception as exc:
            self.state.last_error = f"health check failed: {exc.__class__.__name__}"
            return False


class AnthropicProvider(BaseProvider):
    """Claude as the last-resort fallback (Anthropic SDK)."""

    name = "anthropic"
    label = "Claude (Anthropic)"

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings, settings.anthropic_model, settings.anthropic_max_concurrency)
        self._client: Any = None
        self._structured_ok = True

    @property
    def configured(self) -> bool:
        return bool(self.settings.anthropic_api_key)

    def client(self) -> Any:
        if self._client is None:
            from anthropic import AsyncAnthropic

            import anthropic

            timeout = float(self.settings.anthropic_timeout)
            connect = min(timeout, connect_timeout_for(self.settings, floor=CLOUD_CONNECT_TIMEOUT_SECONDS))
            self._client = AsyncAnthropic(  # the SDK's own Timeout type (it runs on httpx2, not httpx)
                api_key=self.settings.anthropic_api_key, timeout=anthropic.Timeout(timeout, connect=connect),
                max_retries=0,
            )
        return self._client

    async def _complete(self, *, system: str, prompt: str, images: list[bytes], max_tokens: int,
                        temperature: float, schema: type[BaseModel] | None) -> str:
        import anthropic

        content: list[dict[str, Any]] = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.standard_b64encode(img).decode()}}
            for img in images
        ]
        content.append({"type": "text", "text": prompt})
        output_config: dict[str, Any] = {"effort": self.settings.anthropic_effort}
        if schema is not None and self._structured_ok:
            output_config["format"] = {"type": "json_schema", "schema": strict_json_schema(schema)}
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max(max_tokens, 2048),
            "system": system,
            "messages": [{"role": "user", "content": content}],
            "output_config": output_config,
            # server-side refusal fallback (routes a policy decline to Anthropic's recommended model)
            "betas": ["server-side-fallback-2026-07-01"],
            "fallbacks": "default",
        }
        try:
            resp = await self.client().beta.messages.create(**kwargs)
        except anthropic.BadRequestError as exc:
            if "format" in output_config:  # schema not accepted -> rely on prompt instructions
                self._structured_ok = False
                output_config.pop("format")
                resp = await self.client().beta.messages.create(**kwargs)
            else:
                raise ProviderError(f"bad request: {exc.message}", availability=False) from exc
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise ProviderError("authentication failed") from exc
        except anthropic.RateLimitError as exc:
            raise ProviderError("rate limited") from exc
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
            raise ProviderError(f"unreachable: {exc.__class__.__name__}") from exc
        except anthropic.APIStatusError as exc:
            raise ProviderError(f"HTTP {exc.status_code}") from exc
        if resp.stop_reason == "refusal":
            raise ProviderError("request declined by the model", availability=False)
        text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
        if not text.strip():
            raise ProviderError("empty completion", availability=False)
        return text

    async def stream_text(self, *, system: str, prompt: str, max_tokens: int, temperature: float):
        """Real streaming (``beta.messages.stream``) with the same request as ``_complete``. The stream manager
        closes the HTTP stream when this generator ends for any reason - a cancel stops the request at once."""
        import anthropic

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max(max_tokens, 2048),
            "system": system,
            "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
            "output_config": {"effort": self.settings.anthropic_effort},
            "betas": ["server-side-fallback-2026-07-01"],
            "fallbacks": "default",
        }
        async with self.slot():
            self.state.calls += 1
            try:
                async with self.client().beta.messages.stream(**kwargs) as stream:
                    async for text in stream.text_stream:
                        if text:
                            yield text
                    try:
                        final = await stream.get_final_message()
                    except AssertionError:  # the stream never started a message: nothing to inspect
                        final = None
                    if getattr(final, "stop_reason", None) == "refusal":
                        raise ProviderError("request declined by the model", availability=False)
                    if getattr(final, "stop_reason", None) == "max_tokens":
                        raise StreamCut()
            except anthropic.BadRequestError as exc:
                raise ProviderError(f"bad request: {exc.message}", availability=False) from exc
            except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
                raise ProviderError("authentication failed") from exc
            except anthropic.RateLimitError as exc:
                raise ProviderError("rate limited") from exc
            except (anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
                raise ProviderError(f"unreachable: {exc.__class__.__name__}") from exc
            except anthropic.APIStatusError as exc:
                raise ProviderError(f"HTTP {exc.status_code}") from exc

    async def ping(self) -> bool:
        if not self.configured:
            return False
        try:
            await self.client().models.retrieve(self.model, timeout=8.0)
            return True
        except Exception as exc:
            self.state.last_error = f"health check failed: {exc.__class__.__name__}"
            return False


class SimulatedProvider(BaseProvider):
    """Load tests: answers with realistic latency and no model (app/ai/simulated.py, settings AI_SIM_*).

    Built like ``FakeProvider`` but configured from settings, and in the chain only when AI_PROVIDER_ORDER names
    ``simulated`` - never by default; ENVIRONMENT=production also needs AI_SIMULATED_ALLOWED=true. It holds one of its
    AI_SIM_MAX_CONCURRENCY slots per call like a real server, the router's timeouts and circuit breaker apply to it, and
    the guard and the fair queue around it are the real ones - a load test exercises them for real. Nothing is kept
    per call (no prompt log), so it can run for hours."""

    name = simulated.NAME
    label = simulated.LABEL

    def __init__(self, settings: Settings, *, rng: Any = None, echo_nonces: bool | None = None,
                 capture_path: str | None = None) -> None:
        super().__init__(settings, simulated.MODEL, settings.ai_sim_max_concurrency)
        self.sim = simulated.SimulatedModel.from_settings(settings, rng=rng)
        # bug-hunt aids: the keyword arguments (tests) win over the settings / environment; never in production
        echo = echo_nonces if echo_nonces is not None else _sim_flag(settings, "ai_sim_echo_nonce")
        path = capture_path if capture_path is not None else _sim_text(settings, "ai_sim_capture_prompts")
        if settings.is_production and (echo or path):
            log.warning("AI_SIM_ECHO_NONCE / AI_SIM_CAPTURE_PROMPTS are bug-hunt aids and are ignored under "
                        "ENVIRONMENT=production")
            echo, path = False, ""
        self.echo_nonces = bool(echo)
        self.capture: simulated.PromptCapture | None = None
        if path:
            limit = _sim_text(settings, "ai_sim_capture_max_mb")
            try:
                max_mb = max(1, int(float(limit))) if limit else simulated.CAPTURE_MAX_MB
            except ValueError:
                max_mb = simulated.CAPTURE_MAX_MB
            self.capture = simulated.PromptCapture(path, max_bytes=max_mb * 1024 * 1024)
            warn_at_startup(log, f"AI_SIM_CAPTURE_PROMPTS: every prompt of the simulated AI provider is appended to "
                                 f"{self.capture.path} (bug hunts only - it holds member data)")
        self.state.detail = self.sim.describe() | {"echo_nonces": self.echo_nonces,
                                                   "capture_prompts": bool(self.capture)}

    @property
    def configured(self) -> bool:
        return True

    def _observe(self, kind: str, *, system: str, prompt: str, max_tokens: int, schema: type[BaseModel] | None = None,
                 images: int = 0) -> list[str]:
        """Bug-hunt aids for one prompt: capture it (AI_SIM_CAPTURE_PROMPTS) and return the nonces to echo
        (AI_SIM_ECHO_NONCE: those in the requester's own words - ``user_text`` when the feature passed it, else the
        prompt)."""
        call = current_call()
        if self.capture is not None:
            self.capture.add({
                "ts": time.time(), "pid": os.getpid(), "provider": self.name, "kind": kind,
                "feature": call.feature if call else None, "user_id": call.user_id if call else None,
                "owner_kind": call.owner_kind if call else None, "user_text": call.user_text if call else None,
                "schema": getattr(schema, "__name__", None), "max_tokens": max_tokens, "images": images,
                "system": system, "prompt": prompt,
            })
        if not self.echo_nonces:
            return []
        source = call.user_text if call is not None and call.user_text else prompt
        return simulated.find_nonces(source)

    async def _complete(self, *, system: str, prompt: str, images: list[bytes], max_tokens: int,
                        temperature: float, schema: type[BaseModel] | None) -> str:
        echo = self._observe("complete", system=system, prompt=prompt, max_tokens=max_tokens, schema=schema,
                             images=len(images))
        text = (self.sim.json_answer(schema, system, prompt, echo) if schema is not None
                else simulated.echo_prefix(echo) + self.sim.answer(system, prompt, max_tokens))
        rate = self.sim.words_per_second()
        await asyncio.sleep(self.sim.first_token_seconds())
        if self.sim.fails():
            raise ProviderError("simulated outage")
        await asyncio.sleep(self.sim.generation_seconds(text, rate))
        return text

    async def stream_text(self, *, system: str, prompt: str, max_tokens: int, temperature: float):
        """Real streaming: the slot is held across the yields, the first words come after the first-token delay, the
        rest in chunks of one to three words at the call's rate. Closing the generator frees the slot at once."""
        async with self.slot():
            self.state.calls += 1
            echo = self._observe("stream", system=system, prompt=prompt, max_tokens=max_tokens)
            text = self.sim.answer(system, prompt, max_tokens)
            rate = self.sim.words_per_second()
            await asyncio.sleep(self.sim.first_token_seconds())
            if self.sim.fails():
                raise ProviderError("simulated outage")
            if echo:  # the nonces come first, in one piece, so a reader sees them with the first words
                yield simulated.echo_prefix(echo)
            for i, piece in enumerate(self.sim.chunks(text)):
                if i:
                    await asyncio.sleep(self.sim.chunk_seconds(len(piece.split()), rate))
                yield piece

    async def ping(self) -> bool:
        """Fails as often as the calls do (AI_SIM_FAILURE_RATE, like an unreachable server): a fully failing
        simulation stays down instead of the health job closing its breaker - and announcing 'AI online' - every
        minute."""
        return not self.sim.fails()

    def voice(self) -> simulated.SimulatedSpeech:
        """The matching Kokoro stand-in (same latency source) for ``app.services.tts.tts.use_client``."""
        return simulated.SimulatedSpeech(self.sim)


def _sim_text(settings: Any, name: str) -> str:
    """A simulated-provider option: the setting (.env / environment at start-up) when it is set, else the process's
    environment variable now (a bug hunt may switch an aid on for one server process)."""
    value = getattr(settings, name, None)
    text = "" if value is None else str(value).strip()
    return text or os.environ.get(name.upper(), "").strip()


def _sim_flag(settings: Any, name: str) -> bool:
    if getattr(settings, name, None) is True:
        return True
    return os.environ.get(name.upper(), "").strip().lower() in ("1", "true", "yes", "on")


def _simulated_provider(settings: Settings) -> SimulatedProvider | None:
    """The simulated provider for a chain that names it - or None in production without AI_SIMULATED_ALLOWED (the
    settings already refuse to load then; this is the safety net), and in production next to real providers (a load
    test runs it alone: behind a real model it would answer members with generated text whenever the model fails).
    Logs the loud warning at every start."""
    refusal = simulated_ai_refusal(settings)
    if refusal is None and settings.is_production and list(settings.ai_provider_order) != [simulated.NAME]:
        refusal = (f"AI_PROVIDER_ORDER={','.join(settings.ai_provider_order)} puts the simulated AI next to real "
                   "providers - production runs it only alone (AI_PROVIDER_ORDER=simulated, for a load test)")
    if refusal is not None:
        log.error("%s - the simulated AI provider is left out of the chain", refusal)
        return None
    warn_at_startup(log, simulated_ai_warning(settings) or "")
    return SimulatedProvider(settings)


# --------------------------------------------------------------------------- guard / queue plumbing
_background: set[asyncio.Task] = set()  # strong references to fire-and-forget tasks (guard.settle, note_abort)


def _clean(text: Any) -> Any:
    """``guard.clean_text``, applied once as text enters the router: what UTF-8 cannot encode (lone surrogates from a
    JSON "\\ud800" escape) becomes U+FFFD, so neither the guard nor a provider SDK can choke on it."""
    return guard_module.clean_text(text)


class _Progress:
    """How far one ``complete()`` got. ``reached``: a provider attempt held its provider slot - the provider was
    working on the call from then on, so a requester who stops it no longer gets it for free."""

    __slots__ = ("reached",)

    def __init__(self) -> None:
        self.reached = False

    def mark(self) -> None:
        self.reached = True


def _note_abort_soon(owner: AIOwner | None, feature: str) -> None:
    """``guard.note_abort`` (a dropped call that reached a provider) in a task of its own - synchronous, so it is safe
    in a ``finally`` inside a cancelled scope. Skipped for stand-in guards without it and for anything but a signed-in
    person's request."""
    note = getattr(guard_module.guard, "note_abort", None)
    if note is None or owner is None or not owner.is_user_request:
        return
    try:
        task = asyncio.get_running_loop().create_task(_do_note_abort(note, owner, feature), name="ai-guard-abort")
    except RuntimeError:  # no running loop (interpreter shutdown)
        return
    _background.add(task)
    task.add_done_callback(_background.discard)


async def _do_note_abort(note: Callable[..., Awaitable[Any]], owner: AIOwner, feature: str) -> None:
    try:
        await note(owner, feature)
    except Exception:  # noqa: BLE001
        log.exception("AI guard: could not note the dropped %s call of user %s", feature, owner.user_id)


async def _admit(owner: AIOwner | None, feature: str, text: str | None, scope: str | None = None) -> Any:
    """``guard.admit``: AIRejected (and any AIUnavailableError) propagates unchanged. A guard that breaks in any
    other way is logged and the request admitted - a bug there must not switch AI off for everyone. ``scope`` is only
    passed when the caller gave one (stand-in guards written before it existed keep working)."""
    try:
        if scope is None:
            return await guard_module.guard.admit(owner, feature, text)
        return await guard_module.guard.admit(owner, feature, text, scope=scope)
    except AIUnavailableError:
        raise
    except Exception:
        log.exception("AI guard failed for %s - admitting the request", feature)
        return None


def _settle_soon(admission: Any, *, ok: bool, again: bool = False) -> asyncio.Task | None:
    """Start ``guard.settle`` in a task of its own and return it. Synchronous, so it is safe in a ``finally`` that
    may run inside a cancelled anyio scope (Starlette re-cancels every await there). ``again`` (with ``ok``): the call
    is charged but its answer was cut short by its requester - its duplicate fingerprint is forgotten, so asking the
    same thing again at once is a new (charged) question, not a 'duplicate'."""
    if admission is None:
        return None
    try:
        task = asyncio.get_running_loop().create_task(_do_settle(admission, ok, again), name="ai-guard-settle")
    except RuntimeError:  # no running loop (interpreter shutdown)
        return None
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


async def _settle(admission: Any, *, ok: bool, again: bool = False) -> None:
    """``guard.settle`` that completes even if the caller is cancelled while it runs."""
    task = _settle_soon(admission, ok=ok, again=again)
    if task is not None:
        await asyncio.shield(task)


async def _do_settle(admission: Any, ok: bool, again: bool = False) -> None:
    try:
        await guard_module.guard.settle(admission, ok=ok)
        forget = getattr(guard_module.guard, "forget_fingerprint", None) if ok and again else None
        if forget is not None:  # stand-in guards (tests) may not have it
            await forget(admission)
    except Exception:  # noqa: BLE001
        log.exception("AI guard settle failed")


async def _drain_background() -> None:
    """Tests (``app.core.state.reset_all``): let pending ``guard.settle`` tasks finish before the next test starts,
    so a late refund never lands in the next test's counters (user ids restart after TRUNCATE)."""
    pending = [t for t in _background if not t.done()]
    if not pending:
        return
    try:
        await asyncio.wait(pending, timeout=5)
    except Exception:  # noqa: BLE001 - tasks of another (closed) event loop
        _background.clear()


register_reset(_drain_background, name="app.ai.client")


def _forget_call_logs() -> None:
    """Tests: call logs one test never flushed must not become the next test's AICallLog rows (in production the
    background runner flushes them every few seconds)."""
    call_log_queue.clear()


register_reset(_forget_call_logs, name="app.ai.client.call_log_queue")


async def _refusal(owner: AIOwner | None, feature: str, exc: AIRejected) -> AIRejected:
    """The queue refused a signed-in user's request because they already have too many AI calls in flight
    ('too_many': flooding). That earns guard strikes like the guard's own refusals (docs section 3); returns the error
    to raise - the guard turns it into 'cooldown' once the account is paused. 'busy' is the system's fault and costs
    nothing. Never raises."""
    if exc.code != "too_many" or owner is None or not owner.is_user_request:
        return exc
    refusal = getattr(guard_module.guard, "refusal", None)
    if refusal is None:  # a stand-in guard (tests) without strike bookkeeping
        return exc
    try:
        out = await refusal(owner, feature, exc)
    except Exception:  # noqa: BLE001 - bookkeeping must not change the refusal itself
        log.exception("AI guard could not record the queue refusal of %s", feature)
        return exc
    return out if isinstance(out, AIRejected) else exc


@contextmanager
def _cancel_hook(ticket: Ticket | None, clock: asyncio.Timeout, loop: asyncio.AbstractEventLoop) -> Iterator[None]:
    """While a stream waits for its provider's words: ``scheduler.cancel`` (page closed, user left, logout) expires
    the wait's timeout at once (``ticket.on_cancel``), instead of when the next chunk happens to arrive. The provider
    stream is interrupted where it waits (its HTTP request aborted, its slot freed) and the router sees the ticket's
    ``cancel_reason`` - never a provider failure."""
    if ticket is None:
        yield
        return

    def expire() -> None:
        try:
            clock.reschedule(loop.time())
        except RuntimeError:  # already expiring or left: nothing to interrupt
            pass

    ticket.on_cancel = expire
    try:
        if ticket.cancel_reason is not None:
            expire()  # cancelled just before the wait began
        yield
    finally:
        if ticket.on_cancel is expire:
            ticket.on_cancel = None


def _cancelling() -> int:
    task = asyncio.current_task()
    return task.cancelling() if task is not None else 0


def _retrieve(task: asyncio.Task) -> None:
    """Done-callback: mark a child task's exception as retrieved even if nobody awaited it."""
    if not task.cancelled():
        task.exception()


def _provider_ms(started: float, timing: _SlotTiming) -> int:
    """Provider time of one attempt: wall time minus the time spent waiting for provider slots."""
    return max(0, int((time.perf_counter() - started - timing.wait) * 1000))


def _log_call(ticket: Ticket, feature: str, provider: BaseProvider, *, ok: bool, latency_ms: int,
              error: str | None, cancelled: bool = False) -> None:
    """Append a CallLog (synchronously - this may run while the task is being cancelled)."""
    call_log_queue.append(CallLog(feature, provider.name, provider.model, ok, latency_ms,
                                  error[:500] if error else None, user_id=ticket.user_id, owner_kind=ticket.kind,
                                  queue_ms=ticket.queue_ms, cancelled=cancelled))


def _watch_disconnect(owner: AIOwner | None, ticket: Ticket, *, while_running: bool = True) -> asyncio.Task | None:
    """HTTP requests: stop the AI work when the browser goes away - while the ticket waits in the AI queue and, with
    ``while_running``, while its provider work runs. Work that must finish anyway (exit policy 'keep': safety
    checks, verification ...) is not watched."""
    probe = owner.disconnected if owner is not None else None
    if probe is None or ticket.exit_policy == EXIT_KEEP:
        return None
    return asyncio.create_task(_disconnect_watcher(probe, ticket, while_running), name=f"ai-watch:{ticket.id}")


async def _disconnect_watcher(probe: Callable[[], Awaitable[bool]], ticket: Ticket, while_running: bool = True) -> None:
    """Polls ``probe`` every DISCONNECT_POLL_SECONDS while the ticket is in a watched state; on a disconnect the
    queue cancels the ticket ('client_left'): a queued one leaves the queue and its waiter raises AICancelled at once,
    a running one has its provider work stopped."""
    watched = ("queued", "running") if while_running else ("queued",)
    while ticket.state in watched:
        await asyncio.sleep(DISCONNECT_POLL_SECONDS)
        if ticket.state not in watched:
            return
        try:
            gone = await probe()
        except Exception:  # noqa: BLE001 - a broken probe must not stop the AI call
            log.debug("disconnect probe failed for ticket %s", ticket.id, exc_info=True)
            return
        if gone:
            log.info("AI %s for user %s stopped (%s): the client disconnected", ticket.feature, ticket.user_id,
                     ticket.state)
            scheduler.cancel(ticket_id=ticket.id, reason="client_left")
            return


class _QueueWatch:
    """``scheduler.acquire(on_queued=...)`` hook: starts the HTTP disconnect watcher the moment a request has to wait
    in the AI queue, so a browser that goes away frees its place at once (AICancelled('client_left')) instead of
    being noticed only once the provider work has started. ``while_running`` keeps the same watcher on the provider
    work afterwards (``complete``); streams stop watching once they run - their response notices a lost client."""

    __slots__ = ("owner", "task", "while_running")

    def __init__(self, owner: AIOwner | None, *, while_running: bool) -> None:
        self.owner = owner
        self.while_running = while_running
        self.task: asyncio.Task | None = None

    def __call__(self, ticket: Ticket) -> None:
        if self.task is None:
            self.task = _watch_disconnect(self.owner, ticket, while_running=self.while_running)

    def stop(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()


async def _run_ticket(ticket: Ticket, owner: AIOwner | None, work: Coroutine[Any, Any, Any],
                      watcher: asyncio.Task | None = None) -> Any:
    """Run the provider work of a running ticket in a child task the queue can cancel, and release the ticket
    however it ends (docs section 4, steps 3-4). ``watcher``: the disconnect watcher that already ran while the
    ticket waited in the queue (it keeps watching); without one, a new watcher starts here.

    * the queue (or the disconnect watcher) cancelled the child while our caller is fine -> ``AICancelled``: a bare
      CancelledError in a request handler means uvicorn ERROR logs and a DB session that is not rolled back;
    * our caller was cancelled (SSE disconnect, a timeout wrapper, shutdown) -> asyncio cancels the awaited child
      too, we wait for it to unwind (HTTP request aborted, provider slot freed) and re-raise CancelledError."""
    outcome = "failed"
    child = asyncio.create_task(work, name=f"ai:{ticket.feature}:{ticket.id}")
    child.add_done_callback(_retrieve)
    try:
        scheduler.attach(ticket, child)
        if watcher is None or watcher.done():
            watcher = _watch_disconnect(owner, ticket)
        try:
            result = await child
        except asyncio.CancelledError:
            outcome = "cancelled"
            if child.cancelled() and _cancelling() == 0:
                raise AICancelled(ticket.cancel_reason or "cancelled") from None
            raise
        outcome = "done"
        return result
    finally:
        if watcher is not None:
            watcher.cancel()
        if not child.done():  # never leave provider work running behind us
            child.cancel()
        scheduler.release(ticket, outcome=outcome)


async def _first_chunk(agen: Any, timing: _SlotTiming, first_token_timeout: float,
                       ticket: Ticket | None = None, call: CallInfo | None = None) -> Any:
    """First chunk of a provider stream. The first-token clock (re)starts once the provider slot is held; a slot
    that stays taken for a whole ``first_token_timeout`` means the provider is busy (ProviderBusy), not down. A queue
    cancel of ``ticket`` ends the wait at once with AICancelled. ``call`` is what the provider sees as
    ``current_call()`` while its stream starts."""
    loop = asyncio.get_running_loop()
    token = _slot_timing.set(timing)
    call_token = _call_info.set(call)
    try:
        async with asyncio.timeout(first_token_timeout) as clock:
            def restart() -> None:
                if ticket is not None and ticket.cancel_reason is not None:
                    return  # cancelled: the clock is running out on purpose
                try:
                    clock.reschedule(loop.time() + first_token_timeout)
                except RuntimeError:  # already expiring - the timeout wins
                    pass

            timing.on_acquired = restart
            with _cancel_hook(ticket, clock, loop):
                return await agen.__anext__()
    except TimeoutError as exc:
        if ticket is not None and ticket.cancel_reason is not None:
            raise AICancelled(ticket.cancel_reason) from None
        if timing.acquired_at is None:
            raise ProviderBusy(f"busy: no free slot within {first_token_timeout:.0f}s") from exc
        raise
    finally:
        timing.on_acquired = None
        _call_info.reset(call_token)
        _slot_timing.reset(token)


async def _next_chunk(agen: Any, timeout: float, ticket: Ticket) -> Any:
    """The next chunk of a provider stream within ``timeout`` seconds (TimeoutError otherwise, or at once when the
    queue cancels ``ticket`` - the caller tells the two apart by ``ticket.cancel_reason``)."""
    loop = asyncio.get_running_loop()
    async with asyncio.timeout(timeout) as clock:
        with _cancel_hook(ticket, clock, loop):
            return await agen.__anext__()


# --------------------------------------------------------------------------- router
def build_provider(settings: Settings, name: str) -> BaseProvider:
    """One provider of the chain from the settings: 'gateway' (Gemma behind the LAN gateway), 'docker' (Gemma in
    Ollama, the local failsafe) or 'anthropic' (Claude). ``manage.py ai-test`` builds them the same way."""
    s = settings
    if name == "gateway":
        return OpenAICompatProvider(
            s, name="gateway", label="Gemma (gateway)", base_url=s.ai_gateway_base_url, api_key=s.ai_gateway_api_key,
            model=s.ai_gateway_model, timeout=s.ai_gateway_timeout, max_concurrency=s.ai_gateway_max_concurrency,
            status_url=s.ai_gateway_status_url,
        )
    if name == "docker":
        docker = OpenAICompatProvider(
            s, name="docker", label="Gemma (Docker failsafe)", base_url=s.ai_docker_base_url,
            api_key=s.ai_docker_api_key, model=s.ai_docker_model, timeout=s.ai_docker_timeout,
            max_concurrency=s.ai_docker_max_concurrency,
        )
        # the local CPU model needs longer for its first words than the gateway's GPU (features ask for 8-12 s)
        docker.min_first_token_seconds = float(getattr(s, "ai_docker_first_token_seconds", 25.0) or 0.0)
        return docker
    if name == "anthropic":
        return AnthropicProvider(s)
    raise ValueError(f"unknown AI provider {name!r}")


JSON_INSTRUCTIONS = (
    "\n\nOUTPUT FORMAT: respond with ONE JSON object only - no markdown fences, no commentary. "
    "It must validate against this JSON Schema:\n{schema}"
)


class AIRouter:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._providers: list[BaseProvider] = self._build()
        self._override: list[BaseProvider] | None = None

    def _build(self) -> list[BaseProvider]:
        s = self.settings
        catalog: dict[str, BaseProvider] = {name: build_provider(s, name) for name in ("gateway", "docker", "anthropic")}
        if simulated.NAME in s.ai_provider_order:  # load tests: only when named, never part of the default chain
            sim = _simulated_provider(s)
            if sim is not None:
                catalog[sim.name] = sim
        # each provider once (settings already dedupe; a model_copy() skips that validation)
        return [catalog[name] for name in dict.fromkeys(s.ai_provider_order) if name in catalog]

    # ---- test hooks
    def use_providers(self, providers: list[BaseProvider] | None) -> None:
        """Replace the provider chain (tests). ``None`` restores the configured chain."""
        self._override = providers

    @property
    def providers(self) -> list[BaseProvider]:
        return self._override if self._override is not None else self._providers

    # ---- status
    def status(self) -> dict[str, Any]:
        configured = [p for p in self.providers if p.configured]
        usable = [p for p in configured if not p.circuit_open() and p.state.healthy is not False]
        if not usable:
            state = "offline"
        elif usable[0] is configured[0]:
            state = "online"
        else:
            state = "degraded"
        active = usable[0] if usable else None
        return {
            "available": bool(usable),
            "state": state,
            "active": active.name if active else None,
            "active_label": active.label if active else None,
            "providers": [p.describe() for p in self.providers],
        }

    @property
    def available(self) -> bool:
        return self.status()["available"]

    async def refresh_health(self) -> dict[str, Any]:
        """The health job (every AI_HEALTH_INTERVAL_SECONDS): a provider that answers its ping closes its breaker (it
        came back), one that does not is skipped for its cooldown."""
        async def check(p: BaseProvider) -> None:
            if not p.configured:
                return
            ok = await p.ping()
            p.state.last_checked_at = time.time()
            if ok:
                p.record_success()
            else:
                p.state.healthy = False
                p.state.open_until = time.monotonic() + p.settings.ai_circuit_cooldown_seconds

        await asyncio.gather(*(check(p) for p in self.providers), return_exceptions=True)
        return self.status()

    # ---- streaming (interactive answers: first words appear within ~1 s)
    async def stream(
        self,
        *,
        feature: str,
        system: str,
        prompt: str,
        max_tokens: int = 350,
        temperature: float = 0.3,
        first_token_timeout: float = 12.0,
        total_timeout: float = 45.0,
        meta: dict[str, Any] | None = None,
        user_text: str | None = None,
        owner: AIOwner | None = None,
        dedupe_scope: str | None = None,
    ) -> AsyncIterator[str]:
        """Async generator yielding text chunks. Fails over to the next provider only if a provider
        fails *before* its first token; raises AIUnavailableError when nobody can answer.
        ``meta`` (optional dict) receives {"provider", "model", "label"} of the provider that answered,
        ``"truncated": True`` when the router cut the answer short after its first words - total_timeout used up
        or the model reached max_tokens (the text ends with " …"), the provider failed mid-answer (" … (the answer was
        interrupted)"; never continued by another provider) or the stream was cancelled - and ``"cancelled": reason``
        when the queue stopped it (the requester left). A cut answer is shown but must never be cached as complete:
        test ``meta.get("truncated")``, never the marker strings.

        ``user_text`` is the person's raw words (only the guard reads them) and ``dedupe_scope`` the subject they
        are about ('gym:12', 'product:7'): the same question about another subject is not a duplicate.

        Guard and queue come first (AIRejected / AICancelled propagate from the first ``__anext__``); a signed-in
        request that waits in the queue is watched for a disconnect meanwhile. The generator runs in the consumer's
        task (an SSE response), so there is no child task: a ticket cancelled by the queue interrupts the wait for
        the provider's next words at once (``ticket.on_cancel``) - before the first chunk that is AICancelled, later
        the stream just ends - and the ticket is released synchronously in ``finally``. ``total_timeout`` is enforced
        even while no chunk arrives; ``first_token_timeout`` starts once the provider slot is held.

        Charged (``guard.settle(ok=True)``) once the first words went out, however the stream ends after that -
        finished, cut by ``total_timeout``, its reader gone, its tab closed - except when the provider fails mid-answer.
        A stream that never said a word costs nothing. A stream its requester cut short may be asked again at once (a
        new, charged question), and the stop is noted as a dropped call (``guard.note_abort``)."""
        owner = owner if owner is not None else current_owner()
        system, prompt = _clean(system), _clean(prompt)
        user_text, dedupe_scope = _clean(user_text), _clean(dedupe_scope)
        if not any(p.configured and not p.circuit_open() for p in self.providers):
            raise AIUnavailableError("No AI provider is available")
        admission = await _admit(owner, feature, user_text, dedupe_scope)
        watch = _QueueWatch(owner, while_running=False)
        try:
            ticket = await scheduler.acquire(owner, feature, on_queued=watch)
        except BaseException as exc:
            watch.stop()
            _settle_soon(admission, ok=False)
            if isinstance(exc, AIRejected):
                refused = await _refusal(owner, feature, exc)
                if refused is not exc:
                    raise refused from None
            raise
        watch.stop()  # running: the response streaming the words notices a client that went away
        outcome = "failed"
        interrupted: str | None = None  # the provider failed after its first words (the answer ends with the marker)
        answered = complete = False  # the first words went out / the provider's stream reached its end
        active: tuple[BaseProvider, Any, float, _SlotTiming] | None = None  # the provider stream that is open
        call = CallInfo(feature=feature, kind="stream", user_id=ticket.user_id, owner_kind=ticket.kind,
                        user_text=user_text)
        try:
            errors: list[str] = []
            for provider in [p for p in self.providers if p.configured and not p.circuit_open()]:
                if ticket.cancel_reason is not None:
                    raise AICancelled(ticket.cancel_reason)
                started, timing = time.perf_counter(), _SlotTiming(ticket.bucket)
                agen = provider.stream_text(system=system, prompt=prompt, max_tokens=max_tokens,
                                            temperature=temperature)
                active = (provider, agen, started, timing)
                try:
                    first = await _first_chunk(agen, timing, provider.first_token_allowance(first_token_timeout,
                                                                                            total_timeout), ticket, call)
                except AICancelled:  # the queue stopped us while we waited: not the provider's fault
                    raise
                except StopAsyncIteration:  # the stream ended without a word (no choices, a body that is not SSE)
                    active = None
                    provider.record_failure("empty stream", availability=False)
                    errors.append(f"{provider.name}: empty stream")
                    _log_call(ticket, feature, provider, ok=False, latency_ms=_provider_ms(started, timing),
                              error=errors[-1])
                    continue
                except ProviderBusy as exc:  # every slot stayed taken: busy, not down - try the next provider
                    active = None
                    await _aclose(agen)
                    errors.append(f"{provider.name}: {exc}")
                    log.info("AI provider %s busy for %s: %s", provider.name, feature, exc)
                    continue
                except Exception as exc:  # timeout / connection / HTTP error before any output -> next provider
                    if ticket.cancel_reason is not None:  # interrupted by a queue cancel, whatever it raised
                        raise AICancelled(ticket.cancel_reason) from None
                    active = None
                    await _aclose(agen)
                    provider.record_failure(f"{exc.__class__.__name__}: {exc}", availability=_availability(exc))
                    errors.append(f"{provider.name}: {exc.__class__.__name__}")
                    _log_call(ticket, feature, provider, ok=False, latency_ms=_provider_ms(started, timing),
                              error=errors[-1])
                    continue
                provider.record_alive()  # up - but only a finished answer resets its failure count (below)
                if ticket.cancel_reason is not None:  # stopped while the first words were on their way
                    raise AICancelled(ticket.cancel_reason)
                if meta is not None:
                    meta["provider"], meta["model"], meta["label"] = provider.name, provider.model, provider.label
                answered = True
                yield first
                deadline = (timing.acquired_at or started) + total_timeout
                timed_out = False  # total_timeout used up: the provider works, it is just slow
                while ticket.cancel_reason is None:
                    remaining = deadline - time.perf_counter()
                    if remaining <= 0:
                        timed_out = True
                        yield CUT_MARKER
                        break
                    try:
                        chunk = await _next_chunk(agen, remaining, ticket)
                    except StopAsyncIteration:
                        complete = True
                        break
                    except TimeoutError:
                        if ticket.cancel_reason is not None:  # a queue cancel interrupted the wait: just end
                            break
                        timed_out = True
                        yield CUT_MARKER  # total_timeout reached while waiting for more words
                        break
                    except StreamCut:  # the model reached max_tokens: a cut answer, flagged like a timeout
                        if ticket.cancel_reason is not None:
                            break
                        timed_out = True
                        yield CUT_MARKER
                        break
                    except Exception as exc:  # mid-stream failure: keep what was said, mark it - never splice
                        if ticket.cancel_reason is not None:  # another provider's answer onto these words
                            break
                        log.warning("AI stream from %s interrupted: %s", provider.name, exc)
                        interrupted = f"interrupted mid-answer: {exc.__class__.__name__}: {exc}"[:300]
                        # a provider that keeps dying mid-answer must open its breaker like one that is down
                        provider.record_failure(interrupted, availability=_availability(exc))
                        yield INTERRUPTED_MARKER
                        break
                    if ticket.cancel_reason is None:
                        yield chunk
                if complete or timed_out:
                    provider.record_success()
                if ticket.cancel_reason is None:
                    outcome = "done"
                else:  # the requester is gone: stop yielding (the provider stream is closed below)
                    outcome = "cancelled"
                    if meta is not None:  # lets a consumer tell a cut answer from a complete one (no caching)
                        meta["cancelled"] = ticket.cancel_reason
                return
            raise AIUnavailableError("; ".join(errors) or "AI unavailable")
        except BaseException as exc:
            if isinstance(exc, (asyncio.CancelledError, GeneratorExit, AICancelled)):
                outcome = "cancelled"
            raise
        finally:
            # synchronous bookkeeping first: inside a cancelled anyio scope every await below is re-cancelled
            if meta is not None and answered and not (complete and outcome == "done"):
                meta["truncated"] = True  # words went out, but not the provider's whole answer
            reason = ticket.cancel_reason or "aborted"  # read before release() fills it in
            if active is not None:
                provider, _agen, started, timing = active
                if outcome == "cancelled":
                    _log_call(ticket, feature, provider, ok=False, latency_ms=_provider_ms(started, timing),
                              error=f"cancelled ({reason})", cancelled=True)
                elif outcome == "done":  # an answer that broke off mid-way is logged as the provider's failure
                    _log_call(ticket, feature, provider, ok=not interrupted, latency_ms=_provider_ms(started, timing),
                              error=interrupted)
            scheduler.release(ticket, outcome=outcome)
            # Once words went out the provider did the work: charged, however the stream ended - except a provider
            # failure mid-answer, which is refunded with its fingerprint (asking again at once is not a duplicate).
            # Cut short by its requester: charged, and the same question may be asked again at once (charged again).
            # Nothing said yet (refused, stopped before the first words, every provider failed): free.
            cut = answered and outcome == "cancelled"
            _settle_soon(admission, ok=answered and not interrupted, again=cut)
            if cut and reason in ABORT_REASONS:
                _note_abort_soon(owner, feature)  # dropped mid-answer: repeated, an abuse signal
            if active is not None:
                await _aclose(active[1])  # aborts the provider's HTTP stream if we stopped at a yield

    # ---- completion
    async def complete(
        self,
        *,
        feature: str,
        system: str,
        prompt: str,
        schema: type[T] | None = None,
        images: list[bytes] | None = None,
        max_tokens: int = 900,
        temperature: float = 0.2,
        image_max_side: int = 1024,
        timeout: float | None = None,
        user_text: str | None = None,
        owner: AIOwner | None = None,
        dedupe_scope: str | None = None,
    ) -> AIResult:
        """Try providers in order. Returns AIResult (``data`` is the validated schema instance).

        ``timeout`` (seconds) caps each provider attempt so interactive features answer promptly:
        a slow provider is abandoned and the next one is tried. The clock starts once the provider's concurrency
        slot is held; waiting in the AI queue or for a slot never counts against it.
        ``user_text`` is the raw user input (only the guard reads it), ``dedupe_scope`` the subject it is about
        ('gym:12', 'product:7' - the guard's duplicate check is per scope); ``owner`` defaults to ``current_owner()``.
        A signed-in HTTP request is watched for a disconnect from the moment it waits in the AI queue.
        Raises AIRejected (guard / queue refusal), AICancelled (the requester left) or AIUnavailableError when
        every provider failed or none is configured/healthy - all three are AIUnavailableError.

        Charged (``guard.settle(ok=True)``) when answered, and also when the requester stops it - AICancelled or a
        cancelled caller - after a provider slot was held: the provider was working on it (the same request may then
        be made again at once, charged again). Stopped before that (still queued, waiting for a provider slot), refused,
        or failed by every provider: refunded. A dropped connection or a closed tab after a provider slot was held is
        noted as a dropped call (``guard.note_abort``)."""
        owner = owner if owner is not None else current_owner()
        system, prompt = _clean(system), _clean(prompt)
        user_text, dedupe_scope = _clean(user_text), _clean(dedupe_scope)
        configured = [p for p in self.providers if p.configured]
        if not configured:
            raise AIUnavailableError("No AI provider is configured")
        if all(p.circuit_open() for p in configured):  # fail fast: nothing to admit or queue for
            log.info("AI unavailable for %s: all circuits open", feature)
            raise AIUnavailableError("; ".join(f"{p.name}: temporarily disabled ({p.state.last_error})"
                                               for p in configured))
        admission = await _admit(owner, feature, user_text, dedupe_scope)
        ok = False
        stopped = False  # the requester's side stopped the call: AICancelled, or our caller was cancelled
        left: str | None = None  # AICancelled's reason (client_left, page_closed, left, logout ...)
        progress = _Progress()
        watch = _QueueWatch(owner, while_running=True)
        try:
            try:
                ticket = await scheduler.acquire(owner, feature, on_queued=watch)
            except AIRejected as exc:  # 'too_many' earns guard strikes (and may become 'cooldown')
                refused = await _refusal(owner, feature, exc)
                if refused is not exc:
                    raise refused from None
                raise
            work = self._complete_chain(ticket, feature=feature, system=system, prompt=prompt, schema=schema,
                                        images=images or [], max_tokens=max_tokens, temperature=temperature,
                                        image_max_side=image_max_side, timeout=timeout, progress=progress,
                                        user_text=user_text)
            result = await _run_ticket(ticket, owner, work, watch.task)
            ok = True
            return result
        except AICancelled as exc:
            stopped, left = True, exc.reason
            raise
        except asyncio.CancelledError:
            stopped = True
            raise
        finally:
            watch.stop()
            # the provider worked on a call its requester stopped: charged (asking the same again at once is a new,
            # charged question - they never got this answer). Never reached a provider: free.
            dropped = stopped and progress.reached
            if dropped and left in ABORT_REASONS:
                _note_abort_soon(owner, feature)  # dropped mid-answer: repeated, an abuse signal
            await _settle(admission, ok=ok or dropped, again=dropped)

    async def _complete_chain(self, ticket: Ticket, *, feature: str, system: str, prompt: str,
                              schema: type[T] | None, images: list[bytes], max_tokens: int, temperature: float,
                              image_max_side: int, timeout: float | None,
                              progress: _Progress | None = None, user_text: str | None = None) -> AIResult:
        """The provider loop of ``complete()`` - runs in the ticket's child task. ``progress`` learns when a provider
        attempt holds its slot (the call reached a provider)."""
        from app.services.storage import run_image_decode

        # at most IMAGE_DECODE_SLOTS image decodes at once per process, as for uploads
        prepared: list[bytes] = [await run_image_decode(prepare_image, img, image_max_side) for img in images]
        sys_prompt = system + (JSON_INSTRUCTIONS.format(schema=schema_hint(schema)) if schema else "")
        errors: list[str] = []
        candidates = [p for p in self.providers if p.configured]
        if not candidates:
            raise AIUnavailableError("No AI provider is configured")
        call = CallInfo(feature=feature, kind="complete", user_id=ticket.user_id, owner_kind=ticket.kind,
                        user_text=user_text)
        tried_any = False
        for provider in candidates:
            if provider.circuit_open():
                errors.append(f"{provider.name}: temporarily disabled ({provider.state.last_error})")
                continue
            tried_any = True
            started, timing = time.perf_counter(), _SlotTiming(ticket.bucket)
            if progress is not None:
                timing.on_acquired = progress.mark
            token = _slot_timing.set(timing)
            call_token = _call_info.set(call)

            async def _call(p: str, temp: float) -> str:
                return await provider.complete(system=sys_prompt, prompt=p, images=prepared, max_tokens=max_tokens,
                                               temperature=temp, schema=schema, timeout=timeout)

            try:
                text = await _call(prompt, temperature)
                data = None
                if schema is not None:
                    try:
                        data = parse_model(schema, text)
                    except ValueError as first_err:
                        repair = (
                            f"{prompt}\n\nYour previous answer was not valid ({first_err}). "
                            f"Previous answer:\n{text[:3000]}\n\nReturn ONLY the corrected JSON object."
                        )
                        text = await _call(repair, 0.0)
                        try:
                            data = parse_model(schema, text)
                        except ValueError as exc:
                            raise ProviderError(f"invalid JSON after repair: {exc}", availability=False) from exc
                latency = _provider_ms(started, timing)
                provider.record_success()
                _log_call(ticket, feature, provider, ok=True, latency_ms=latency, error=None)
                return AIResult(text=text, data=data, provider=provider.name, model=provider.model, latency_ms=latency)
            except ProviderBusy as exc:  # every slot stayed taken: busy, not down - try the next provider
                errors.append(f"{provider.name}: {exc}")
                log.info("AI provider %s busy for %s: %s", provider.name, feature, exc)
                continue
            except ProviderError as exc:
                provider.record_failure(str(exc), availability=exc.availability)
                errors.append(f"{provider.name}: {exc}")
            except asyncio.CancelledError:  # the queue stopped us, or our caller went away: not a provider failure
                _log_call(ticket, feature, provider, ok=False, latency_ms=_provider_ms(started, timing),
                          error=f"cancelled ({ticket.cancel_reason or 'aborted'})", cancelled=True)
                raise
            except Exception as exc:  # anything unexpected is an availability failure - unless the request was bad
                provider.record_failure(f"{exc.__class__.__name__}: {exc}", availability=_availability(exc))
                errors.append(f"{provider.name}: {exc.__class__.__name__}")
            finally:
                _call_info.reset(call_token)
                _slot_timing.reset(token)
            _log_call(ticket, feature, provider, ok=False, latency_ms=_provider_ms(started, timing), error=errors[-1])
            log.warning("AI provider %s failed for %s: %s", provider.name, feature, errors[-1])
        if not tried_any:
            log.info("AI unavailable for %s: all circuits open", feature)
        raise AIUnavailableError("; ".join(errors) or "AI unavailable")

    async def complete_json(self, *, feature: str, system: str, prompt: str, schema: type[T], **kwargs: Any) -> T:
        result = await self.complete(feature=feature, system=system, prompt=prompt, schema=schema, **kwargs)
        return result.data


async def _aclose(agen: Any) -> None:
    try:
        await agen.aclose()
    except Exception:  # noqa: BLE001
        pass


ai = AIRouter()


def install_simulated_voice(router: AIRouter) -> bool:
    """With the simulated provider in ``router``'s chain and AI_SIM_TTS on, the coach voice is simulated too: Kokoro is
    replaced through the voice service's own client hook (``tts.use_client``), so its per-member cache, the AI guard's
    voice quota and the queue's 'tts' lane stay the real ones. Not under ENVIRONMENT=test (tests inject their own voice
    client). Returns whether it was installed; never raises (the app must start whatever happens here)."""
    s = router.settings
    sim = next((p for p in router.providers if isinstance(p, SimulatedProvider)), None)
    if sim is None or not s.ai_sim_tts or s.is_test:
        return False
    try:
        from app.services.tts import tts  # lazy: the voice service imports the guard and the queue, never this module

        tts.use_client(sim.voice())
    except Exception:  # noqa: BLE001
        log.exception("the simulated voice could not be installed - the coach voice keeps its configured server")
        return False
    warn_at_startup(log, f"!!! SIMULATED VOICE ACTIVE: coach voice lines are silent MP3 audio after "
                         f"{sim.sim.describe()['tts_ms']} ms (AI_SIM_TTS_MS) - no Kokoro runs. AI_SIM_TTS=false keeps "
                         "the configured voice server !!!")
    return True


install_simulated_voice(ai)

AI_DISCLAIMER ="AI can make mistakes. Double-check important information - this is not medical, legal or financial advice."


async def probe_provider(provider: BaseProvider, *, timeout: float = 60.0) -> dict[str, Any]:
    """A one-token answer straight from ``provider`` - no guard, no queue, no circuit breaker: admin tools only
    (``manage.py ai-test``). Returns {'ok', 'first_ms', 'total_ms', 'text', 'error'}."""
    out: dict[str, Any] = {"ok": False, "first_ms": None, "total_ms": None, "text": "", "error": None}
    if not provider.configured:
        out["error"] = "not configured"
        return out
    started = time.perf_counter()
    parts: list[str] = []
    agen = provider.stream_text(system="You are a health check. Reply with one word.", prompt="Say OK.",
                                max_tokens=1, temperature=0.0)
    try:
        async with asyncio.timeout(timeout):
            try:
                async for piece in agen:
                    if out["first_ms"] is None:
                        out["first_ms"] = int((time.perf_counter() - started) * 1000)
                    parts.append(piece)
            except StreamCut:  # max_tokens=1 reached: exactly what was asked for
                pass
        out["ok"] = bool("".join(parts).strip())
        if not out["ok"]:
            out["error"] = "empty answer"
    except TimeoutError:
        out["error"] = f"no answer within {timeout:.0f} s"
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        out["error"] = f"{exc.__class__.__name__}: {exc}"[:300]
    finally:
        await _aclose(agen)
    out["total_ms"] = int((time.perf_counter() - started) * 1000)
    out["text"] = "".join(parts).strip()[:80]
    return out


CALL_LOG_FLUSH_MAX = 500  # queued call logs written per flush (every 2 s); the rest wait for the next one
_CALL_LOG_INFO = "ironpulse.ai_call_log_flush"  # Session.info: the call-log batches the open transaction is writing


def _requeue_call_logs(batches: list[list[CallLog]]) -> None:
    logs = [c for b in batches for c in b]
    if logs:
        call_log_queue.extendleft(reversed(logs))  # oldest first again (a full queue drops its newest)
        log.warning("AI call log flush was not committed - %s entries put back in the queue", len(logs))


@_sa_event.listens_for(_OrmSession, "after_commit")
def _call_logs_committed(session: Any) -> None:
    if session.get_nested_transaction() is None:  # the COMMIT, not a released SAVEPOINT
        session.info.pop(_CALL_LOG_INFO, None)


@_sa_event.listens_for(_OrmSession, "after_transaction_end")
def _call_logs_not_committed(session: Any, transaction: Any) -> None:
    """The session's outermost transaction ended without the COMMIT above (rollback, failed COMMIT, closed session):
    the batch goes back to the front of the queue - a database hiccup never loses a window of AI call logs."""
    if transaction.nested or transaction.parent is not None:
        return
    batches = session.info.pop(_CALL_LOG_INFO, None)
    if batches:
        try:
            _requeue_call_logs(batches)
        except Exception:  # noqa: BLE001 - never break a rollback
            log.exception("could not put an uncommitted AI call log flush back in the queue")


async def flush_ai_call_logs(db: Any) -> int:
    """Queue the next CALL_LOG_FLUSH_MAX call logs as ``AICallLog`` rows (the caller commits). Every text is made storable
    first (a NUL byte in a provider's error message is kept as ``\\x00``, never a refused row), and nothing taken off
    the queue is lost: unless the caller's transaction COMMITs, the batch goes back to the front of the queue."""
    from app.models import AICallLog
    from app.security.bans import storable

    batch: list[CallLog] = []
    while call_log_queue and len(batch) < CALL_LOG_FLUSH_MAX:
        batch.append(call_log_queue.popleft())
    if not batch:
        return 0
    try:
        rows = [AICallLog(feature=storable(c.feature, 40), provider=storable(c.provider, 20),
                          model=storable(c.model, 80), ok=c.ok, latency_ms=c.latency_ms,
                          error=storable(c.error, 500) or None, user_id=c.user_id,
                          owner_kind=storable(c.owner_kind, 10) or None, queue_ms=c.queue_ms,
                          cancelled=bool(c.cancelled)) for c in batch]
    except BaseException:
        _requeue_call_logs([batch])  # nothing was added to the session
        raise
    db.info.setdefault(_CALL_LOG_INFO, []).append(batch)
    db.add_all(rows)  # begins the session's transaction: its end (COMMIT or not) settles the batch
    return len(batch)


# --------------------------------------------------------------------------- test double
class FakeProvider(BaseProvider):
    """Deterministic provider for tests: ``FakeProvider(responses=[...])`` or ``fail=True``.

    ``handler(**kwargs)`` (sync or async) computes the answer - an async handler that sleeps simulates a slow,
    cancellable provider. ``delay`` (s) is slept before every answer. ``chunks`` makes ``stream_text`` stream those
    pieces one by one (sleeping ``delay`` before each); ``streams_closed`` counts provider streams that ended."""

    def __init__(self, name: str = "fake", *, responses: list[str] | None = None, fail: bool = False,
                 handler: Any = None, delay: float = 0.0, chunks: list[str] | None = None,
                 max_concurrency: int = 8) -> None:
        super().__init__(get_settings(), model="fake-model", max_concurrency=max_concurrency)
        self.name = name
        self.label = f"Fake ({name})"
        self.responses = list(responses or [])
        self.fail = fail
        self.handler = handler
        self.delay = delay
        self.chunks = list(chunks) if chunks is not None else None
        self.prompts: list[dict[str, Any]] = []
        self.streams_closed = 0

    @property
    def configured(self) -> bool:
        return True

    async def _complete(self, **kwargs: Any) -> str:
        self.prompts.append(kwargs)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise ProviderError("simulated outage")
        if self.handler is not None:
            out = self.handler(**kwargs)
            return await out if asyncio.iscoroutine(out) else out
        if self.responses:
            return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        return "{}"

    async def stream_text(self, *, system: str, prompt: str, max_tokens: int, temperature: float):
        if self.chunks is None:  # one chunk with the whole completion, like BaseProvider
            async with self.slot():
                self.state.calls += 1
                text = await self._complete(system=system, prompt=prompt, images=[], max_tokens=max_tokens,
                                            temperature=temperature, schema=None)
            yield text
            return
        async with self.slot():
            self.state.calls += 1
            self.prompts.append({"system": system, "prompt": prompt, "images": [], "max_tokens": max_tokens,
                                 "temperature": temperature, "schema": None})
            try:
                if self.fail:
                    raise ProviderError("simulated outage")
                for piece in self.chunks:
                    if self.delay:
                        await asyncio.sleep(self.delay)
                    yield piece
            finally:
                self.streams_closed += 1

    async def ping(self) -> bool:
        return not self.fail
