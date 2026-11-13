"""Simulated AI for load tests: realistic latency, no model (README "Deploying to production" -> "Load tests with
simulated AI").

Selected only by naming ``simulated`` in AI_PROVIDER_ORDER - never by default, never as an automatic fallback - and
refused under ENVIRONMENT=production unless AI_SIMULATED_ALLOWED=true (app/config.py). ``SimulatedModel`` decides how
long a call takes and what it says; ``app.ai.client.SimulatedProvider`` answers through it like any other provider
(its own concurrency slots, the router's timeouts and circuit breaker), so the AI guard, the fair queue (lanes, per-user
caps, ``max_wait``) and cancellation around it are the real ones. ``SimulatedSpeech`` stands in for Kokoro behind
``app/services/tts.py`` (``tts.use_client``): the coach voice keeps its per-member cache, voice quota and 'tts' lane.

* **latency:** the first words after AI_SIM_FIRST_TOKEN_MS (uniform, e.g. ``400..1500``), then words at one rate per call
  drawn from AI_SIM_TOKENS_PER_S (``25..60``), each chunk +-25 %. A completion returns once the whole answer would have
  been generated. A voice line takes AI_SIM_TTS_MS.
* **words:** the same (system, prompt) always gets the same plausible coaching sentences (sha256-seeded), at most
  ``max_tokens`` words. A structured call (``schema=``) gets JSON built from the schema with conservative values:
  booleans false, optional lists left out, the most neutral enum choice - never an action, an approval or an alarm.
* **failures:** AI_SIM_FAILURE_RATE of the calls fail like an unreachable server (the circuit breaker counts them; a
  failed voice line trips the voice breaker). The health check fails as often, so a fully failing simulation stays
  "down" instead of flapping back "online" every minute.
* **bug-hunt aids** (environment variables, simulated provider only, ignored under ENVIRONMENT=production):
  AI_SIM_ECHO_NONCE=true makes every answer start with the nonce tokens (``nq`` + 8 of a-z0-9, e.g. ``nq7k2m9x4a``)
  found in the requester's own words, so a test can prove an answer belongs to its question and its user;
  AI_SIM_CAPTURE_PROMPTS=<path> appends every prompt the provider receives (system + prompt, the requesting user id and
  feature) to that JSONL file (buffered, flushed within FLUSH_SECONDS, mode 0600, at most AI_SIM_CAPTURE_MAX_MB), so a
  test can check which context reached the AI - and that no other member's data did.

Pure Python: no network, no settings import and no import of app.ai.client (which imports this module).
"""

from __future__ import annotations

import asyncio
import atexit
import hashlib
import json
import logging
import math
import os
import random
import re
import threading
from collections.abc import Awaitable, Callable
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any

log = logging.getLogger("ironpulse.ai.simulated")

NAME = "simulated"  # its name in AI_PROVIDER_ORDER (app.config.SIMULATED_AI)
LABEL = "Simulated AI (load test)"
MODEL = "simulated"
VOICE_ENGINE = "simulated"

ANSWER_WORDS = (40, 180)  # words of a free-text answer, before max_tokens caps it
CHUNK_JITTER = 0.25  # each streamed chunk takes its share of the call's rate +- 25 %
SPOKEN_WORDS_PER_SECOND = 2.5  # how long a voice line lasts (~150 words a minute)
# One silent MPEG-1 Layer III frame (128 kbit/s, 44.1 kHz, mono): header + zeroed side info and data = 417 bytes,
# 1152 samples. Players decode a run of them as silence.
MP3_FRAME = b"\xff\xfb\x90\xc4" + bytes(413)
MP3_FRAME_SECONDS = 1152 / 44100
MAX_AUDIO_SECONDS = 60.0
# bug-hunt aids (see the module docstring)
NONCE_RE = re.compile(r"(?<![a-z0-9])nq[a-z0-9]{8}(?![a-z0-9])")
MAX_NONCES = 20  # echoed per answer
FLUSH_SECONDS = 0.2  # captured prompts reach the file at most this long after the call
FLUSH_LINES = 64  # ... or as soon as this many wait
CAPTURE_MAX_MB = 256  # the capture file never grows beyond this (AI_SIM_CAPTURE_MAX_MB)

Sleep = Callable[[float], Awaitable[Any]]


def find_nonces(text: str | None) -> list[str]:
    """The nonce tokens in ``text`` (lower case), each once, in order, at most MAX_NONCES."""
    found: list[str] = []
    for token in NONCE_RE.findall((text or "").lower()):
        if token not in found:
            found.append(token)
            if len(found) >= MAX_NONCES:
                break
    return found


def echo_prefix(nonces: list[str]) -> str:
    """What an echoing answer starts with: ``[nq... nq...] `` (empty without nonces)."""
    return f"[{' '.join(nonces)}] " if nonces else ""


class PromptCapture:
    """Appends one JSON line per prompt to ``path`` (AI_SIM_CAPTURE_PROMPTS). Lines are buffered and written together
    at most FLUSH_SECONDS later (or once FLUSH_LINES wait, or at exit) with a single O_APPEND write, so several worker
    processes can share one file. The file is created with mode 0600 (prompts hold member data) and stops growing at
    ``max_bytes``. ``flush()`` writes what is pending at once (tests, hunters)."""

    def __init__(self, path: str | os.PathLike[str], *, max_bytes: int = CAPTURE_MAX_MB * 1024 * 1024,
                 flush_seconds: float = FLUSH_SECONDS) -> None:
        self.path = Path(path).expanduser()
        self.max_bytes = max(1, int(max_bytes))
        self.flush_seconds = max(0.0, float(flush_seconds))
        self._lines: list[str] = []
        self._lock = threading.Lock()
        self._scheduled = False
        self._full = False
        self.written = 0  # records written by this process
        atexit.register(self.flush)

    def add(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        with self._lock:
            if self._full:
                return
            self._lines.append(line)
            pending = len(self._lines)
        if pending >= FLUSH_LINES or self.flush_seconds == 0:
            self.flush()
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # no event loop (a script): write now
            self.flush()
            return
        if not self._scheduled:
            self._scheduled = True
            loop.call_later(self.flush_seconds, self.flush)

    def flush(self) -> int:
        """Write the pending lines now; returns how many were written."""
        with self._lock:
            self._scheduled = False
            lines, self._lines = self._lines, []
            if not lines or self._full:
                return 0
            data = "".join(lines).encode("utf-8")
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    if os.fstat(fd).st_size + len(data) > self.max_bytes:
                        self._full = True
                        log.warning("AI_SIM_CAPTURE_PROMPTS: %s reached its size limit - no more prompts are captured",
                                    self.path)
                        return 0
                    view = memoryview(data)
                    while view:
                        view = view[os.write(fd, view):]
                finally:
                    os.close(fd)
            except OSError:
                log.exception("AI_SIM_CAPTURE_PROMPTS: could not write %s", self.path)
                return 0
            self.written += len(lines)
            return len(lines)

_SENTENCES = (
    "Start with five minutes of easy cardio to warm up your muscles and joints.",
    "Keep your core braced and move through a controlled range of motion.",
    "Rest about ninety seconds between sets so your form stays clean.",
    "Two or three sessions a week with a recovery day in between is a good rhythm.",
    "Drink water before, during and after training, especially on warm days.",
    "If an exercise causes sharp pain, stop and ask a trainer for a safer variation.",
    "Add weight only when the last two reps of every set still feel smooth.",
    "Sleep and regular meals matter as much as the workout itself.",
    "Finish with gentle stretching for the muscles you trained today.",
    "Write down your sets and reps so you can see your progress over the coming weeks.",
    "A brisk walk on rest days keeps you moving without adding much fatigue.",
    "Breathe out as you push or pull, and breathe in as you return to the start.",
    "Choose a pace at which you could still speak in short sentences.",
    "A trainer at your gym can check your technique during the first few sessions.",
    "Mix strength work with a few mobility drills for balanced fitness.",
    "Small, consistent steps usually beat occasional very hard sessions.",
    "Warm up each movement with a lighter set before your working sets.",
    "Keep your knees in line with your toes during squats and lunges.",
    "Lower each rep slowly to build control and strength.",
    "Plan your week ahead so training fits around work and family time.",
    "A plate with protein, vegetables and whole grains supports recovery.",
    "Take an extra rest day when you feel run down.",
    "Short sessions still count, and twenty focused minutes beat none.",
    "Pick one clear goal for the month and review it with your coach.",
)
# Enum values a structured answer prefers, in this order: the ones that change nothing (a review, no action).
_NEUTRAL_CHOICES = ("needs_review", "manual_review", "review", "none", "no_action", "ignore", "unknown", "pending",
                    "info", "low", "neutral", "calm", "ok", "no")
# Optional text fields worth filling (prose people read); other optional fields keep the schema's default.
_PROSE_FIELDS = frozenset({
    "answer", "reply", "message", "text", "summary", "assessment", "explanation", "advice", "reason", "rationale",
    "note", "notes", "comment", "feedback", "detail", "details", "description", "title", "focus", "tip", "tips",
    "recommendation", "suggestion", "headline", "intro", "closing",
})
_LONG_TEXT_FIELDS = frozenset({"answer", "reply", "message", "text", "summary", "assessment", "explanation", "advice"})


def _seeded(*parts: str) -> random.Random:
    digest = hashlib.sha256("\x00".join(parts).encode("utf-8", "replace")).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def _span(value: Any) -> tuple[float, float]:
    """(low, high) from a number or a pair (settings are already validated; this only normalises)."""
    if isinstance(value, (int, float)):
        return float(value), float(value)
    low, high = (float(v) for v in value)
    return (low, high) if low <= high else (high, low)


def count_tokens(text: str) -> int:
    """What generating ``text`` costs in simulated tokens: its words, or ~4 characters per token for JSON."""
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        return max(1, len(stripped) // 4)
    return max(1, len(text.split()))


def spoken_seconds(text: str, speed: float = 1.0) -> float:
    return len((text or "").split()) / SPOKEN_WORDS_PER_SECOND / max(0.25, float(speed or 1.0))


def silent_mp3(seconds: float) -> bytes:
    """Silent MP3 audio of about ``seconds`` (at least four frames, at most MAX_AUDIO_SECONDS)."""
    seconds = min(MAX_AUDIO_SECONDS, max(0.0, seconds))
    return MP3_FRAME * max(4, math.ceil(seconds / MP3_FRAME_SECONDS))


class SimulatedModel:
    """Latency, failures and answers of the simulated provider. The random source (``rng``) decides latency and
    failures; the prompt alone decides the words."""

    def __init__(self, *, first_token_ms: Any = (400.0, 1500.0), tokens_per_s: Any = (25.0, 60.0),
                 failure_rate: float = 0.0, tts_ms: Any = (300.0, 1200.0), rng: random.Random | None = None) -> None:
        self.first_token_ms = _span(first_token_ms)
        self.tokens_per_s = _span(tokens_per_s)
        self.failure_rate = min(1.0, max(0.0, float(failure_rate or 0.0)))
        self.tts_ms = _span(tts_ms)
        self.rng = rng if rng is not None else random.Random()

    @classmethod
    def from_settings(cls, settings: Any, *, rng: random.Random | None = None) -> SimulatedModel:
        return cls(first_token_ms=settings.ai_sim_first_token_ms, tokens_per_s=settings.ai_sim_tokens_per_s,
                   failure_rate=settings.ai_sim_failure_rate, tts_ms=settings.ai_sim_tts_ms, rng=rng)

    def describe(self) -> dict[str, Any]:
        return {"simulated": True, "first_token_ms": _text_span(self.first_token_ms),
                "tokens_per_s": _text_span(self.tokens_per_s), "failure_rate": self.failure_rate,
                "tts_ms": _text_span(self.tts_ms)}

    # ------------------------------------------------------------------ latency + failures (random)
    def first_token_seconds(self) -> float:
        return self.rng.uniform(*self.first_token_ms) / 1000.0

    def words_per_second(self) -> float:
        """One generation speed per call."""
        return max(0.1, self.rng.uniform(*self.tokens_per_s))

    def chunk_seconds(self, words: int, rate: float) -> float:
        return max(1, words) / rate * self.rng.uniform(1.0 - CHUNK_JITTER, 1.0 + CHUNK_JITTER)

    def generation_seconds(self, text: str, rate: float) -> float:
        """After the first words: the rest of a completion at ``rate`` tokens per second."""
        return max(0, count_tokens(text) - 1) / rate

    def speech_seconds(self) -> float:
        return self.rng.uniform(*self.tts_ms) / 1000.0

    def fails(self) -> bool:
        return self.failure_rate > 0.0 and self.rng.random() < self.failure_rate

    # ------------------------------------------------------------------ words (deterministic per prompt)
    def answer(self, system: str, prompt: str, max_tokens: int = 350) -> str:
        """Plausible coaching sentences, the same for the same (system, prompt), at most ``max_tokens`` words."""
        r = _seeded("text", system or "", prompt or "")
        target = min(max(1, int(max_tokens or 1)), r.randint(*ANSWER_WORDS))
        words: list[str] = []
        while len(words) < target:
            words.extend(r.choice(_SENTENCES).split())
        text = " ".join(words[:target])
        return text if text.endswith((".", "!", "?")) else text.rstrip(",;:-") + "."

    @staticmethod
    def chunks(text: str) -> list[str]:
        """``text`` cut into streamed pieces of one to three words (deterministic); joined they are ``text``."""
        words = text.split(" ")
        r = _seeded("chunks", text)
        out: list[str] = []
        i = 0
        while i < len(words):
            n = r.randint(1, 3)
            piece = " ".join(words[i:i + n])
            i += n
            out.append(piece + (" " if i < len(words) else ""))
        return out

    def json_answer(self, schema: Any, system: str, prompt: str, echo: list[str] | tuple[str, ...] = ()) -> str:
        """A JSON object for ``schema`` (a Pydantic model): its required fields and its prose fields filled with
        conservative values, the same for the same prompt. Falls back to the required fields alone when the prose
        does not validate. ``echo`` (nonces, AI_SIM_ECHO_NONCE): put in front of the first prose text field, when
        the result still validates."""
        name = getattr(schema, "__name__", "")
        try:
            spec = _json_schema(schema) if isinstance(schema, type) else schema.model_json_schema()
        except Exception:  # noqa: BLE001 - not a Pydantic model: a plain answer object
            return json.dumps({"answer": echo_prefix(list(echo)) + self.answer(system, prompt, 60)})
        defs = spec.get("$defs") or {}
        rich = _fill(spec, defs, _seeded("json", name, system or "", prompt or ""), prose=True)
        if _validates(schema, rich):
            return json.dumps(_with_echo(schema, rich, echo))
        plain = _fill(spec, defs, _seeded("json-plain", name, system or "", prompt or ""), prose=False)
        chosen = plain if _validates(schema, plain) else rich
        return json.dumps(_with_echo(schema, chosen, echo))


@lru_cache(maxsize=128)
def _json_schema(schema: type) -> dict[str, Any]:
    """A model's JSON schema, built once per class and only read afterwards. Pydantic rebuilds it on every call -
    0.5 to 1.5 ms of event-loop time per structured answer, which would load the server under test itself."""
    return schema.model_json_schema()


def _prose_path(value: Any, depth: int = 0) -> list[Any] | None:
    """Where the first prose text field of a filled answer is (a list of keys / indexes), or None."""
    if depth > 6:
        return None
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, str) and key.lower() in _PROSE_FIELDS:
                return [key]
        items = list(value.items())
    elif isinstance(value, list):
        items = list(enumerate(value))
    else:
        return None
    for key, item in items:
        sub = _prose_path(item, depth + 1)
        if sub is not None:
            return [key, *sub]
    return None


def _with_echo(schema: Any, value: Any, echo: Any) -> Any:
    """``value`` with the nonces in front of its first prose text field - unchanged when there is none, or when the
    longer text would no longer validate (a maxLength)."""
    nonces = list(echo or ())
    path = _prose_path(value) if nonces else None
    if path is None:
        return value
    copy = json.loads(json.dumps(value))
    target = copy
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = echo_prefix(nonces) + target[path[-1]]
    return copy if _validates(schema, copy) else value


def _text_span(span: tuple[float, float]) -> str:
    low, high = (int(v) if float(v).is_integer() else v for v in span)
    return f"{low}" if low == high else f"{low}..{high}"


def _validates(schema: Any, value: Any) -> bool:
    try:
        schema.model_validate(value)
    except Exception:  # noqa: BLE001
        return False
    return True


def _resolve(node: Any, defs: dict[str, Any]) -> dict[str, Any]:
    seen = 0
    while isinstance(node, dict) and "$ref" in node and seen < 20:
        node = defs.get(str(node["$ref"]).rsplit("/", 1)[-1], {})
        seen += 1
    return node if isinstance(node, dict) else {}


def _neutral(choices: list[Any]) -> Any:
    for preferred in _NEUTRAL_CHOICES:
        if preferred in choices:
            return preferred
    return choices[0] if choices else None


def _non_null(node: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any] | None:
    """The first non-null alternative of an anyOf/oneOf (or the node itself); None when only null is allowed."""
    for key in ("anyOf", "oneOf"):
        if key in node:
            options = [_resolve(o, defs) for o in node[key]]
            usable = [o for o in options if o.get("type") != "null"]
            return usable[0] if usable else None
    return node


def _is_prose(key: str, node: dict[str, Any], defs: dict[str, Any]) -> bool:
    target = _non_null(_resolve(node, defs), defs)
    return (key.lower() in _PROSE_FIELDS and target is not None and target.get("type") == "string"
            and not any(k in target for k in ("enum", "const", "pattern", "format")))


def _fill(node: Any, defs: dict[str, Any], r: random.Random, *, prose: bool, name: str = "", depth: int = 0) -> Any:
    node = _resolve(node, defs)
    if depth > 8:
        return None
    if "const" in node:
        return node["const"]
    if "enum" in node:
        return _neutral(list(node["enum"]))
    if "anyOf" in node or "oneOf" in node:
        target = _non_null(node, defs)
        return None if target is None else _fill(target, defs, r, prose=prose, name=name, depth=depth + 1)
    if node.get("allOf"):
        return _fill(node["allOf"][0], defs, r, prose=prose, name=name, depth=depth + 1)
    kind = node.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), None)
    if kind == "object" or "properties" in node:
        props = node.get("properties") or {}
        required = set(node.get("required") or ())
        out: dict[str, Any] = {}
        for key, sub in props.items():
            if key in required or (prose and _is_prose(key, sub, defs)):
                out[key] = _fill(sub, defs, r, prose=prose, name=key, depth=depth + 1)
        return out
    if kind == "array":
        n = min(8, int(node.get("minItems") or 0))
        return [_fill(node.get("items") or {}, defs, r, prose=prose, name=name, depth=depth + 1) for _ in range(n)]
    if kind == "string":
        return _string(node, r, name)
    if kind in ("integer", "number"):
        return _number(node, r, integer=kind == "integer")
    if kind == "boolean":
        return False
    if kind == "null":
        return None
    return ""


def _string(node: dict[str, Any], r: random.Random, name: str) -> str:
    fmt = node.get("format")
    if fmt == "date":
        return "2026-01-15"
    if fmt == "date-time":
        return "2026-01-15T09:00:00Z"
    if fmt == "email":
        return "member@example.com"
    if fmt in ("uri", "url"):
        return "https://example.com/"
    if name.lower() in _LONG_TEXT_FIELDS:
        text = " ".join(r.choice(_SENTENCES) for _ in range(r.randint(1, 3)))
    else:
        text = " ".join(r.choice(_SENTENCES).rstrip(".").split()[: r.randint(3, 6)])
    low = int(node.get("minLength") or 0)
    while len(text) < low:
        text = f"{text} {r.choice(_SENTENCES)}"
    high = node.get("maxLength")
    if isinstance(high, int) and len(text) > high:
        text = text[:high].rstrip() or text[:high]
    return text


def _number(node: dict[str, Any], r: random.Random, *, integer: bool) -> int | float:
    step = 1 if integer else 1e-6
    low = node.get("minimum")
    high = node.get("maximum")
    if "exclusiveMinimum" in node:
        low = node["exclusiveMinimum"] + step
    if "exclusiveMaximum" in node:
        high = node["exclusiveMaximum"] - step
    low = 0 if low is None else low
    span = 10 if integer else 1
    high = low + span if high is None else max(low, high)
    value = r.uniform(low, min(high, low + span))
    if integer:
        return int(min(max(round(value), math.ceil(low)), math.floor(high)))
    return round(value, 3) if low <= round(value, 3) <= high else value


class SimulatedSpeech:
    """Kokoro stand-in for ``app.services.tts.tts.use_client``: ``audio.speech.create(...)`` waits a simulated synthesis
    time (AI_SIM_TTS_MS) and returns silent MP3 audio as long as the line takes to speak. Everything around it is the
    voice service's own: the per-member cache, the AI guard's voice quota, the queue's 'tts' lane and its breaker."""

    engine = VOICE_ENGINE

    def __init__(self, model: SimulatedModel, *, sleep: Sleep | None = None) -> None:
        self.model = model
        self._sleep = sleep or asyncio.sleep
        self.calls = 0
        self.audio = SimpleNamespace(speech=SimpleNamespace(create=self.create))

    async def create(self, *, model: str = "", voice: str = "", input: str = "", response_format: str = "mp3",  # noqa: A002 - the OpenAI SDK's keyword
                     speed: float = 1.0, **_: Any) -> bytes:
        self.calls += 1
        await self._sleep(self.model.speech_seconds())
        if self.model.fails():
            raise ConnectionError("simulated voice outage")
        return silent_mp3(spoken_seconds(input, speed))
