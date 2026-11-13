"""Text-to-speech for the live AI workout companion (WP-K).

Kokoro (the ``kokoro-fastapi`` Docker image, see ``docker/kokoro.compose.yml``) serves an
OpenAI-compatible ``POST /v1/audio/speech`` endpoint, so we call it through the OpenAI SDK::

    audio = await tts.synthesize("Nice work - rest 45 seconds.")   # MP3 bytes, or None

* results are cached in memory (LRU, ~200 phrases keyed by sha256(requester|voice|speed|text)) - cues such as
  "Rest 45 seconds" are spoken many times. Cache hits are free: never queued, never judged by the AI guard;
* the cache and the shared syntheses belong to ONE person (``requester_scope``): members never share a line. Coach
  lines carry first names and workout details, and a shared cache hit - instant, free, served even to a refused
  request - told one member which lines another had just heard; a shared synthesis could hand one member another's
  refusal. The same person's tabs and repeats still share their own cache and syntheses;
* a cache miss is judged first by the per-user AI guard (``guard.admit(owner, "tts")``: voice quotas and the account's
  AI pause, docs/ai-guard-and-queue.md section 3), then the synthesis takes a ticket in the AI queue's ``tts`` lane
  (``settings.ai_tts_concurrency`` Kokoro calls at once, fair between users, stopped when its page closes);
* concurrent requests of one person for the same phrase share ONE synthesis (a "flight"): it runs as a task of its own
  with a list of waiters, so a requester that goes away - its request cancelled, its page closed, its browser aborting
  the fetch (the coach interrupts a line) - never takes the audio from the others; the synthesis, and its place in the
  voice lane, is given up as soon as every waiter has gone. A refusal of the queue reaches only the requests of the
  person it belongs to;
* a circuit breaker marks the voice server down for 60 s after a failure, so a missing container
  never slows the workout down (the browser's own ``speechSynthesis`` voice is used instead);
* ``synthesize`` never raises: ``None`` means "fall back to the browser voice". Routes call ``synthesize_or_refuse``,
  which raises ``AIRejected`` for the requester's own refusal so the browser learns how long to back off;
* the client is injectable (``tts.use_client(fake)``) - tests never touch the network.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.ai import guard as guard_module  # the module, not the object: tests swap ``guard_module.guard``
from app.ai.errors import AICancelled, AIRejected, AIUnavailableError
from app.ai.owner import AIOwner, current_owner, owner_scope
from app.ai.scheduler import scheduler
from app.config import get_settings
from app.core.state import register_reset

log = logging.getLogger("ironpulse.tts")

FEATURE = "tts"  # AI policy (app/ai/policy.py): 'voice' quota group, realtime priority, the queue's 'tts' lane
CACHE_SIZE = 200
CACHE_MAX_BYTES = 48 * 1024 * 1024
DOWN_SECONDS = 60.0
MAX_TEXT = 400
# Kokoro voice ids ("af_heart", "bf_emma") and blends ("af_bella+af_sky")
VOICE_RE = re.compile(r"^[a-z]{2}_[a-z0-9]{2,24}(?:\+[a-z]{2}_[a-z0-9]{2,24}){0,2}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SPACES = re.compile(r"\s+")


def clean_text(text: str | None) -> str:
    """Collapse whitespace, drop control characters and cap the length (what we send to the engine)."""
    text = _CONTROL.sub(" ", text or "")
    return _SPACES.sub(" ", text).strip()[:MAX_TEXT]


def valid_voice(voice: str | None) -> bool:
    return bool(voice) and bool(VOICE_RE.fullmatch(voice or ""))


def requester_scope(owner: AIOwner | None) -> str:
    """Whose cached lines and syntheses a request may use: its own person's (``user:<id>``), never another member's.
    Requests without a person (system callers, scripts) share ``system``."""
    uid = owner.user_id if owner is not None else None
    return "system" if uid is None else f"user:{int(uid)}"


def cache_key(text: str, voice: str, speed: float, scope: str = "system") -> str:
    """Cache (and flight) key of one line for one requester scope (``requester_scope``)."""
    return hashlib.sha256(f"{scope}|{voice}|{speed:.3f}|{text}".encode()).hexdigest()


async def _read_audio(resp: Any) -> bytes:
    """Bytes from an OpenAI binary response (``HttpxBinaryResponseContent``) or a test double."""
    if isinstance(resp, (bytes, bytearray)):
        return bytes(resp)
    aread = getattr(resp, "aread", None)
    if callable(aread):
        data = await aread()
        if isinstance(data, (bytes, bytearray)):
            return bytes(data)
    content = getattr(resp, "content", None)
    if isinstance(content, (bytes, bytearray)):
        return bytes(content)
    raise TypeError("speech response contained no audio bytes")


# --------------------------------------------------------------------------- AI guard plumbing
_background: set[asyncio.Task] = set()  # strong references to guard.settle tasks


async def _admit(owner: AIOwner | None) -> Any:
    """``guard.admit`` for one synthesis: AIRejected (quota / cooldown) propagates. A guard that breaks in any other way
    is logged and the request admitted - a bug there must not take the voice away from everyone."""
    try:
        return await guard_module.guard.admit(owner, FEATURE, None)
    except AIUnavailableError:
        raise
    except Exception:
        log.exception("AI guard failed for tts - admitting the request")
        return None


def _settle_soon(admission: Any, *, ok: bool) -> asyncio.Task | None:
    """``guard.settle`` in a task of its own (safe in a ``finally`` of a task that is being cancelled)."""
    if admission is None:
        return None
    try:
        task = asyncio.get_running_loop().create_task(_do_settle(admission, ok), name="tts-guard-settle")
    except RuntimeError:  # no running loop (interpreter shutdown)
        return None
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


async def _settle(admission: Any, *, ok: bool) -> None:
    task = _settle_soon(admission, ok=ok)
    if task is not None:
        await asyncio.shield(task)


async def _do_settle(admission: Any, ok: bool) -> None:
    try:
        await guard_module.guard.settle(admission, ok=ok)
    except Exception:  # noqa: BLE001
        log.exception("AI guard settle failed (tts)")


async def _queue_refusal(owner: AIOwner | None, exc: AIRejected) -> AIRejected:
    """The queue refused a signed-in user's synthesis because they already have too many voice requests in flight:
    that earns guard strikes like the guard's own refusals (and comes back as 'cooldown' once the account is paused).
    'busy' is the system's fault and costs nothing. Never raises."""
    if exc.code != "too_many" or owner is None or not owner.is_user_request:
        return exc
    refusal = getattr(guard_module.guard, "refusal", None)
    if refusal is None:  # a stand-in guard (tests) without strike bookkeeping
        return exc
    try:
        out = await refusal(owner, FEATURE, exc)
    except Exception:  # noqa: BLE001 - bookkeeping must not change the refusal itself
        log.exception("AI guard could not record the queue refusal of tts")
        return exc
    return out if isinstance(out, AIRejected) else exc


def _copy(exc: AIRejected) -> AIRejected:
    """A fresh refusal for each waiter (one exception object must not be raised in several tasks)."""
    return AIRejected(exc.code, exc.message, retry_after=exc.retry_after, feature=exc.feature)


def _cancelling() -> int:
    task = asyncio.current_task()
    return task.cancelling() if task is not None else 0


def _retrieve(task: asyncio.Task) -> None:
    """Done-callback: a synthesis nobody waits for any more must not log 'exception was never retrieved'."""
    if not task.cancelled():
        task.exception()


def _same_page(a: AIOwner | None, b: AIOwner | None) -> bool:
    """Do two requests come from the same browser tab (or are they the same request)?"""
    if a is b:
        return True
    if a is None or b is None:
        return False
    return a.page_id is not None and a.page_id == b.page_id and a.user_id == b.user_id


# --------------------------------------------------------------------------- shared syntheses
@dataclass(eq=False, slots=True)
class _Waiter:
    """One request waiting for a shared synthesis."""

    owner: AIOwner | None
    creator: bool = False  # this request started the synthesis (its guard admission pays for it)
    gone: bool = False  # cancelled, or its page closed: no reason to keep synthesising for it


@dataclass(eq=False)
class _Flight:
    """One synthesis shared by every concurrent request of one person (``requester_scope``) for the same phrase.
    ``task`` runs on its own: a waiter that goes away only leaves the list, and the synthesis is cancelled once nobody
    is left."""

    key: str
    admission: Any = None  # guard admission of the request that started it (what it costs: ``TTSService._fly``)
    waiters: list[_Waiter] = field(default_factory=list)
    task: asyncio.Task | None = None
    refused_user_id: int | None = None  # the person whose ticket the queue refused (the refusal is theirs alone)
    speaking: bool = False  # Kokoro was given the line (its voice-lane slot was held): stopping it is no longer free

    def live(self) -> list[_Waiter]:
        return [w for w in self.waiters if not w.gone]

    def refused(self, waiter: _Waiter) -> bool:
        """Is the queue's refusal of this synthesis this waiter's own (its person's ticket was refused)?"""
        owner = waiter.owner
        return self.refused_user_id is not None and owner is not None and owner.user_id == self.refused_user_id

    def leave(self, owner: AIOwner | None) -> None:
        """The queue stopped ``owner``'s ticket (page closed, user left, logout): every waiter of that tab is gone."""
        for w in self.waiters:
            if not w.gone and _same_page(w.owner, owner):
                w.gone = True

    def stop_if_abandoned(self, reason: str) -> None:
        if not self.live() and self.task is not None and not self.task.done():
            self.task.cancel(msg=reason)

    @property
    def stopping(self) -> bool:
        return self.task is None or self.task.done() or self.task.cancelling() > 0


def _watch_requester(flight: _Flight, waiter: _Waiter) -> asyncio.Task | None:
    """HTTP requests: notice a browser that aborted its fetch - the coach interrupts a line with a newer one, or the
    page moves on. That request's share of the synthesis is given up at once, and with nobody left the synthesis and
    its place in the voice lane: a ticket kept for audio nobody will play made the person's next lines 'too_many' (and
    earned strikes for them). Requests without a disconnect probe (system callers) are not watched."""
    probe = waiter.owner.disconnected if waiter.owner is not None else None
    if probe is None:
        return None
    try:
        return asyncio.get_running_loop().create_task(_requester_watch(flight, waiter, probe), name="tts-watch")
    except RuntimeError:  # no running loop (interpreter shutdown)
        return None


async def _requester_watch(flight: _Flight, waiter: _Waiter, probe: Callable[[], Awaitable[bool]]) -> None:
    from app.ai.client import DISCONNECT_POLL_SECONDS  # the AI router's cadence for the same check

    while not waiter.gone and not flight.stopping:
        await asyncio.sleep(DISCONNECT_POLL_SECONDS)
        if waiter.gone or flight.stopping:
            return
        try:
            left = await probe()
        except Exception:  # noqa: BLE001 - a broken probe must not stop the synthesis
            log.debug("TTS disconnect probe failed", exc_info=True)
            return
        if left:
            log.info("TTS line of user %s abandoned: the client disconnected", getattr(waiter.owner, "user_id", None))
            waiter.gone = True
            flight.stop_if_abandoned("client_left")
            return


@dataclass
class TTSStats:
    requests: int = 0
    hits: int = 0
    joined: int = 0  # requests that shared a synthesis already underway
    refused: int = 0  # refused by the AI guard or the queue (the browser voice was used)
    synthesized: int = 0
    failures: int = 0
    last_error: str | None = None
    last_ok_at: float | None = None


class TTSService:
    def __init__(self) -> None:
        self._cache: OrderedDict[str, bytes] = OrderedDict()
        self._cache_bytes = 0
        self._inflight: dict[str, _Flight] = {}
        self._client: Any = None
        self._injected: Any = None
        self._down_until = 0.0
        self.stats = TTSStats()

    # ---------------------------------------------------------------- configuration / test hooks
    def use_client(self, client: Any | None) -> None:
        """Inject a client exposing ``audio.speech.create(...)`` (tests). ``None`` restores the real one."""
        self._injected = client
        self.reset()

    def reset(self) -> list[asyncio.Task]:
        """Forget the cache, the circuit breaker and the counters, and stop syntheses still underway (tests).
        Returns the stopped tasks."""
        stopped: list[asyncio.Task] = []
        for flight in list(self._inflight.values()):
            task = flight.task
            if task is None or task.done():
                continue
            try:
                task.cancel(msg="reset")
            except RuntimeError:  # its event loop is gone
                continue
            stopped.append(task)
        self._inflight.clear()
        self._cache.clear()
        self._cache_bytes = 0
        self._down_until = 0.0
        self.stats = TTSStats()
        return stopped

    @property
    def configured(self) -> bool:
        if self._injected is not None:
            return True
        s = get_settings()
        # tests never talk to a real voice server unless a fake client was injected
        return bool(s.tts_base_url) and not s.is_test

    def _get_client(self) -> Any:
        if self._injected is not None:
            return self._injected
        if self._client is None:
            from openai import AsyncOpenAI

            s = get_settings()
            self._client = AsyncOpenAI(base_url=s.tts_base_url, api_key=s.tts_api_key or "not-needed",
                                       timeout=s.tts_timeout, max_retries=0)
        return self._client

    # ---------------------------------------------------------------- circuit breaker
    @property
    def is_down(self) -> bool:
        return time.monotonic() < self._down_until

    def _trip(self, error: str) -> None:
        self.stats.failures += 1
        self.stats.last_error = error[:200]
        self._down_until = time.monotonic() + DOWN_SECONDS
        log.warning("TTS unavailable (%s) - using the browser voice for %.0f s", error[:120], DOWN_SECONDS)

    def status(self, *, admin: bool = False) -> dict[str, Any]:
        """The voice engine's state for the browser (live coach pages, GET /api/tts/status, refusal bodies). It says
        nothing about other people's voice use; ``admin=True`` (the admin panel) adds the cache and request counts."""
        s = get_settings()
        if not self.configured:
            state = "not_configured"
        elif self.is_down:
            state = "down"
        elif self.stats.last_ok_at is not None:
            state = "up"
        else:
            state = "unknown"
        # the engine actually answering: an injected client names its own (the simulated voice of a load test says
        # "simulated", app/ai/simulated.py SimulatedSpeech.engine) - never "kokoro" when Kokoro isn't the one speaking
        engine = getattr(self._injected, "engine", None) if self._injected is not None else None
        out: dict[str, Any] = {
            "engine": engine if isinstance(engine, str) and engine else "kokoro",
            "model": s.tts_model,
            "voice": s.tts_voice,
            "speed": s.tts_speed,
            "state": state,
            "available": state in ("up", "unknown"),
            "fallback": "browser",
            "retry_in_s": max(0, round(self._down_until - time.monotonic())) if state == "down" else 0,
        }
        if admin:
            st = self.stats
            out.update(cached=len(self._cache), cached_bytes=self._cache_bytes, requests=st.requests, hits=st.hits,
                       joined=st.joined, refused=st.refused, synthesized=st.synthesized, failures=st.failures)
        return out

    # ---------------------------------------------------------------- cache
    def _cache_get(self, key: str) -> bytes | None:
        data = self._cache.get(key)
        if data is not None:
            self._cache.move_to_end(key)
        return data

    def _cache_put(self, key: str, data: bytes) -> None:
        if key in self._cache:
            self._cache_bytes -= len(self._cache.pop(key))
        self._cache[key] = data
        self._cache_bytes += len(data)
        while self._cache and (len(self._cache) > CACHE_SIZE or self._cache_bytes > CACHE_MAX_BYTES):
            _, old = self._cache.popitem(last=False)
            self._cache_bytes -= len(old)

    # ---------------------------------------------------------------- synthesis
    async def synthesize(self, text: str, voice: str | None = None) -> bytes | None:
        """MP3 bytes for ``text`` - or None (not configured, circuit open, engine error, refused by the AI guard or the
        queue, requester gone): use the browser voice. Never raises."""
        try:
            return await self.synthesize_or_refuse(text, voice)
        except AIRejected:
            return None

    async def synthesize_or_refuse(self, text: str, voice: str | None = None) -> bytes | None:
        """Like ``synthesize``, but a refusal of *this* request - the per-user voice quota, AI paused on the account,
        too many voice requests at once, the voice lane busy - raises ``AIRejected``, so the route can tell the browser
        how long to back off. Hits of the requester's own cache and requests that share their own synthesis already
        underway cost nothing; only the request that starts a synthesis is judged by the AI guard (and charged once
        Kokoro works on it - see ``_fly``). Another member's lines are never visible here: they are cached and
        synthesised under that member's own keys (``requester_scope``)."""
        s = get_settings()
        text = clean_text(text)
        if not text or not self.configured:
            return None
        voice = voice if valid_voice(voice) else s.tts_voice
        speed = float(s.tts_speed or 1.0)
        owner = current_owner()
        key = cache_key(text, voice, speed, requester_scope(owner))
        self.stats.requests += 1
        cached = self._cache_get(key)
        if cached is not None:
            self.stats.hits += 1
            return cached
        if self.is_down:
            return None
        flight = self._joinable(key)
        if flight is None:
            admission: Any = None
            refusal: AIRejected | None = None
            try:
                admission = await _admit(owner)
            except AIRejected as exc:
                refusal = exc
            # admit() awaited (Redis): by now the phrase may be in this person's cache, or underway for another of their
            # requests (a second tab) - both are free for them, refused or not
            cached = self._cache_get(key)
            flight = self._joinable(key)
            if cached is not None or flight is not None:
                await _settle(admission, ok=False)  # nothing is synthesised for this request: no charge
                if cached is not None:
                    self.stats.hits += 1
                    return cached
            elif refusal is not None:
                self.stats.refused += 1
                raise refusal
            elif self.is_down:
                await _settle(admission, ok=False)
                return None
            else:
                waiter = _Waiter(owner, creator=True)
                return await self._wait(self._launch(key, text, voice, speed, waiter, admission), waiter)
        self.stats.joined += 1
        waiter = _Waiter(owner)
        flight.waiters.append(waiter)
        return await self._wait(flight, waiter)

    def _joinable(self, key: str) -> _Flight | None:
        flight = self._inflight.get(key)
        return None if flight is None or flight.stopping else flight

    def _launch(self, key: str, text: str, voice: str, speed: float, waiter: _Waiter, admission: Any) -> _Flight:
        flight = _Flight(key=key, admission=admission, waiters=[waiter])
        task = asyncio.get_running_loop().create_task(self._fly(flight, text, voice, speed), name=f"tts:{key[:12]}")
        task.add_done_callback(_retrieve)
        flight.task = task
        self._inflight[key] = flight
        return flight

    async def _wait(self, flight: _Flight, waiter: _Waiter) -> bytes | None:
        """This request's share of a synthesis. Cancelling the request - or its browser aborting the fetch - only
        removes it from the waiters (the synthesis stops once nobody is left)."""
        task = flight.task
        assert task is not None
        watcher = _watch_requester(flight, waiter)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled() and _cancelling() == 0:
                return None  # the synthesis was stopped (nobody else wanted it, or a reset): the browser voice
            waiter.gone = True  # this request was cancelled - whoever else waits keeps the synthesis
            flight.stop_if_abandoned("abandoned")
            raise
        except AIRejected as exc:
            # the queue refused the synthesis' ticket: 'busy' (the voice lane is full) concerns everyone; any other
            # refusal (too many voice requests at once, AI paused) belongs to the person whose ticket was refused and
            # reaches only their requests - never decided by who started the synthesis. The others: browser voice
            if exc.code == "busy" or flight.refused(waiter):
                self.stats.refused += 1
                raise _copy(exc) from None
            return None
        except Exception:  # noqa: BLE001 - AICancelled (nobody left to hear it) or anything unexpected: browser voice
            return None
        finally:
            if watcher is not None and not watcher.done():
                watcher.cancel()

    async def _fly(self, flight: _Flight, text: str, voice: str, speed: float) -> bytes | None:
        """The shared synthesis (a task of its own): queue ticket -> Kokoro -> cache, then settle the admission.

        What it costs the request that started it - the rule of ``ai.complete`` (docs/ai-guard-and-queue.md section
        12): charged when audio came out, and also when every requester left once Kokoro was working on the line (the
        coach page aborted the fetch, the page closed): Kokoro did the work, and dropping lines mid-synthesis must not
        be a way around the voice quota. Free: refused, stopped while it waited for a voice-lane slot, an engine
        failure. Interrupting a line is ordinary coach behaviour, so it never earns strikes."""
        ok = False
        try:
            data = await self._in_lane(flight, text, voice, speed)
            if data is not None:
                self._cache_put(flight.key, data)
                ok = True
            return data
        except asyncio.CancelledError:
            ok = flight.speaking  # stopped mid-synthesis: charged; stopped before Kokoro got it: free
            raise
        finally:
            if self._inflight.get(flight.key) is flight:
                del self._inflight[flight.key]
            _settle_soon(flight.admission, ok=ok)

    async def _in_lane(self, flight: _Flight, text: str, voice: str, speed: float) -> bytes | None:
        """Kokoro inside a ticket of the AI queue's 'tts' lane. The ticket is the first live waiter's: when that
        requester's page closes while the phrase still waits for a slot, the next waiter's ticket takes over, and a
        cancel while Kokoro is speaking only stops the synthesis when nobody else waits for it."""
        while True:
            live = flight.live()
            if not live:
                raise AICancelled("left")
            owner = live[0].owner
            try:
                ticket = await scheduler.acquire(owner, FEATURE)
            except AICancelled:
                flight.leave(owner)
                continue
            except AIRejected as exc:
                flight.refused_user_id = owner.user_id if owner is not None else None
                raise await _queue_refusal(owner, exc) from None
            break
        outcome = "failed"

        def on_cancel() -> None:
            flight.leave(owner)
            flight.stop_if_abandoned(ticket.cancel_reason or "cancelled")

        ticket.on_cancel = on_cancel  # never attach the task itself: other waiters may still want the audio
        try:
            if ticket.cancel_reason is not None:  # stopped between the grant and here
                on_cancel()
            flight.speaking = _cancelling() == 0  # Kokoro gets the line now (unless that stop is already under way)
            with owner_scope(owner):  # the synthesis is attributed to the requester whose ticket runs it
                data = await self._fetch(text, voice, speed)
            outcome = "done" if data is not None else "failed"
            return data
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            ticket.on_cancel = None
            scheduler.release(ticket, outcome=outcome)

    async def _fetch(self, text: str, voice: str, speed: float) -> bytes | None:
        s = get_settings()
        try:
            client = self._get_client()
            resp = await asyncio.wait_for(
                client.audio.speech.create(model=s.tts_model, voice=voice, input=text, response_format="mp3",
                                           speed=speed),
                timeout=float(s.tts_timeout) + 2.0,
            )
            data = await _read_audio(resp)
            if len(data) < 32:
                raise ValueError("empty audio")
        except asyncio.CancelledError:  # stopped (everyone left): not an engine failure
            raise
        except Exception as exc:  # noqa: BLE001 - never raise to the route
            self._trip(f"{exc.__class__.__name__}: {exc}")
            return None
        self.stats.synthesized += 1
        self.stats.last_ok_at = time.time()
        return data

    async def probe(self) -> dict[str, Any]:
        """Synthesise a tiny phrase when the engine state is still unknown (cached afterwards)."""
        if self.configured and not self.is_down and self.stats.last_ok_at is None:
            await self.synthesize("Ready.")
        return self.status()


tts = TTSService()


async def _reset_for_tests() -> None:
    """``app.core.state.reset_all``: stop syntheses of the previous test and let their guard refunds land first."""
    stopped = tts.reset()
    try:
        if stopped:
            await asyncio.wait(stopped, timeout=5)
        pending = [t for t in _background if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=5)
    except Exception:  # noqa: BLE001 - tasks of another (closed) event loop
        _background.clear()


register_reset(_reset_for_tests, name="app.services.tts")
