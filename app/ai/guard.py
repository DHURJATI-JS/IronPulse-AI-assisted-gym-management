"""Per-user AI spam protection: quotas, duplicate and spam checks, strikes and escalating cooldowns.

Contract: docs/ai-guard-and-queue.md, section 3. Every quota, strike value and cooldown length comes from
app/ai/policy.py - change limits there, never here.

``guard.admit(owner, feature, text, scope=None)`` judges one AI request *before* any compute is spent, in this order:

1. an active cooldown on the account                                      -> AIRejected("cooldown")
2. text checks, when the caller passes the user's raw words: longer than MAX_TEXT_CHARS, one character or one
   word repeated over and over, a flood of links, mostly symbols, only a handful of distinct characters
                                                                          -> AIRejected("spam")
   Prompt-injection phrases are *noted* (a strike and a log line) but never refused.
3. the same normalised text from the same user for the same feature and dedupe ``scope`` (the subject asked about:
   'gym:12', 'product:7') within ``policy.dedupe_seconds``                -> AIRejected("duplicate")
4. the per-user quota windows (minute / hour / day, ``policy.cost`` units, tier from role + AI plan)
                                                                          -> AIRejected("quota")

Safety features (``policy.safety``) skip all four - a health alarm is never refused - but their usage is still
counted. Unguarded features, background jobs and system work are not judged at all (``admit`` returns None). Work the
quotas never refuse can still be bounded per person with ``reserve`` / ``release`` (a daily allowance, e.g. the AI safety
reviews of health readings - past it the caller falls back to its rules, nothing is refused).

Every refusal earns strike points (rolling STRIKE_WINDOW); a burst of refusals adds a "flood" strike; crossing a
COOLDOWN_LADDER level pauses AI for the whole account (the REPEAT_COOLDOWN_AFTER-th pause within a day lasts
REPEAT_COOLDOWN_SECONDS), and long pauses are reported once to the IP threat/ban system. ``settle(admission,
ok=False)`` refunds a call whose work never reached a provider or that the provider failed, so nobody pays for AI
they never got; the router (app/ai/client.py) decides which, and keeps the charge of a call its requester dropped
mid-answer (``forget_fingerprint`` lets them ask it again) - ``note_abort`` turns repeated drops into strike points.
A background job that was admitted at the click carries ``Admission.to_payload()`` and settles it with
``guard.settle_payload(payload, ok)`` - whichever settle comes first wins.

Text never breaks or bypasses the guard: ``clean_text`` replaces what UTF-8 cannot encode (lone surrogates from a
JSON "\\ud800" escape) before anything reads it, and words that still cannot be read are skipped - the request is
then judged without them (cooldown and quotas still apply). ``admit`` judges in a task of its own that its caller's
cancellation cannot interrupt: whatever it decides after the charge is settled even when nobody waits any more.

Storage: Redis (shared by every process and both app copies) under ``ip:<env>:<db>:ai:``, or an in-process store
with the same semantics whenever Redis is not configured or an operation fails - the limits never fail open. The
common admit path is ONE pipelined Redis round trip and touches PostgreSQL only for the cached AI-entitlement
lookup; that round trip also reads the entitlement's version (``forget_entitlement`` bumps it), so a subscription
change committed by any process is seen at the next request. Refusals, cooldowns and escalations are counted per
UTC day for the admin panel (``snapshot()``), and the guard's refusals are ranked per account in the same round trips
(``offenders()``: the site overview's "frequent limit offenders").
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import math
import os
import re
import secrets
import time
import unicodedata
from collections import Counter
from collections.abc import Awaitable, Callable, Coroutine, Generator, Iterable, Mapping, MutableMapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, NoReturn

from app.ai import policy as ai_policy
from app.ai.errors import AIRejected
from app.ai.owner import OWNER_USER, AIOwner
from app.config import get_settings
from app.core.state import register_reset

log = logging.getLogger("ironpulse.ai.guard")

__all__ = [
    "Admission", "AIGuard", "MemoryStore", "RedisStore", "Reservation", "clean_scope", "clean_text", "fold_text", "guard",
    "human_wait", "injection_marker", "instruction_marker", "normalize_text", "spam_reason", "text_fingerprint",
]

# --------------------------------------------------------------------------- text heuristics
# Shape thresholds of the spam check (docs section 3). Quotas, strikes and cooldowns live in app/ai/policy.py.
RUN_LENGTH = 25  # one character this many times in a row is spam ...
RUN_EXEMPT = frozenset(".-_=")  # ... except dotted leaders, underlines and rules pasted from documents
TOKEN_REPEATS = 15  # one word at least this many times ...
TOKEN_SHARE = 0.5  # ... that also makes up more than half of all the words
MAX_LINKS = 3
SYMBOLS_MIN_CHARS = 40  # "mostly symbols" is only judged for texts longer than this
MIN_WORD_SHARE = 0.25  # letters/digits (with their combining marks) among the visible characters
VARIETY_MIN_CHARS = 80  # "only a handful of distinct characters" is only judged for texts longer than this
MIN_VARIETY = 0.08  # distinct visible characters per visible character ...
VARIETY_SPAN = 120  # ... over at most this many characters: long sincere texts reuse ~40 letters and must pass
SHORT_REPLY_CHARS = 12  # 'ok', 'yes', 'thanks!!' legitimately repeat in a conversation, so for normalised texts
SHORT_REPLY_DEDUPE = 5.0  # shorter than this the duplicate window shrinks to a double-submit guard (seconds)

# --------------------------------------------------------------------------- bookkeeping
ENTITLEMENT_TTL = 300.0  # seconds a customer's "my plan includes AI" answer is cached in memory
ENTITLEMENT_RETRY = 30.0  # after a failed lookup the stricter 'member' tier is assumed for this long
ENTITLEMENT_TIMEOUT = 5.0
# forget_entitlement() bumps a shared version key per user (and one for "everyone"); a cached answer read at another
# version is looked up again. The key outlives every cache entry older than the bump (> ENTITLEMENT_TTL) and starts
# from a random base, so a version that expired and comes back never repeats an old value.
ENTITLEMENT_VERSION_TTL = 86400
TIER_MEMORY_SECONDS = 86400  # status() shows the tier a user was last judged with for this long
DAY = 86400
COOLDOWN_GRACE = 60  # a cooldown record outlives its end by this much (activity is judged by its 'until')
REFUSAL_CODES = ("quota", "duplicate", "spam", "cooldown")  # the guard's own refusals, counted per UTC day
STATS_TTL = 3 * DAY  # the per-day counters of snapshot() live this long
PAYLOAD_VERSION = 1  # Admission.to_payload() format

_GROUP_WORDING = {  # quota group -> (what was used up, what the person may do again)
    "chat": ("AI chat messages", "send more"),
    "qa": ("AI questions", "ask again"),
    "gen": ("AI generations", "generate more"),
    "notes": ("AI-written notes", "get more"),
    "vision": ("AI photo checks", "try again"),
    "voice": ("AI voice replies", "use the AI voice again"),
}
_PERIOD = {60: "this minute", 3600: "this hour", 86400: "today"}
_COOLDOWN_REASONS = {  # strike code that crossed a ladder level -> "... because of <reason>."
    "duplicate": "the same message being sent again and again",
    "spam": "messages that looked like spam",
    "injection": "repeated attempts to override the AI's instructions",
    "abort": "AI answers being abandoned half-way again and again",
}
_DEFAULT_COOLDOWN_REASON = "too many requests in a short time"
DUPLICATE_MESSAGE = "You just asked that - give it a moment."

Op = tuple[str, tuple[Any, ...], dict[str, Any]]


def _op(name: str, *args: Any, **kwargs: Any) -> Op:
    """One store command (a redis-py method name and its arguments) - both stores execute the same lists."""
    return name, args, kwargs


# =========================================================================== text helpers
_LINK_RE = re.compile(r"(?:\bhttps?://|\bwww\.)\S+", re.IGNORECASE)
_RUN_RE = re.compile(r"(\S)\1{%d,}" % (RUN_LENGTH - 1))
_INJECTION_RE = re.compile(
    r"\b(?:ignore|disregard|override)\s+(?:all\s+|any\s+|the\s+|your\s+)?"
    r"(?:(?:previous|prior|above|earlier|preceding|system|original)\s+)?(?:instructions?|prompts?|rules)\b"
    r"|\bsystem\s+prompt\b|\byou\s+are\s+now\b|\bdeveloper\s+mode\b|\bjail\s*break|\bdo\s+anything\s+now\b"
    r"|\b(?:reveal|print|show|repeat)\s+(?:me\s+)?(?:your|the)\s+(?:(?:system|hidden|initial|original)\s+)?"
    r"(?:prompt|instructions)\b",
    re.IGNORECASE,
)


def _is_punct(ch: str) -> bool:
    return unicodedata.category(ch).startswith("P")


def _is_edge_noise(ch: str) -> bool:
    return ch.isspace() or _is_punct(ch)


def _trim(text: str, noise: Callable[[str], bool]) -> str:
    start, end = 0, len(text)
    while start < end and noise(text[start]):
        start += 1
    while end > start and noise(text[end - 1]):
        end -= 1
    return text[start:end]


def normalize_text(text: str) -> str:
    """The form duplicates are compared in: NFKC, casefolded, whitespace collapsed, surrounding punctuation
    stripped. A text that is *only* punctuation ('?', '!!') keeps it, so '?' and '!' stay different."""
    folded = " ".join(unicodedata.normalize("NFKC", text).casefold().split())
    return _trim(folded, _is_edge_noise) or folded


_SURROGATE_RE = re.compile("[\ud800-\udfff]")


def clean_text(value: Any) -> str | None:
    """Text every AI layer can use: None stays None, anything else becomes a str, and what UTF-8 cannot encode - lone
    UTF-16 surrogates, which Python's json makes of a "\\ud800" escape - becomes U+FFFD (a valid surrogate pair becomes
    its character). The AI router applies it once where text enters (before the guard and before the provider) and
    the guard applies it to what it is given, so no text can make either of them raise. Clean text costs one regex
    scan and comes back as the same object."""
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    if _SURROGATE_RE.search(text) is None:
        return text
    return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def clean_scope(scope: Any) -> str | None:
    """A dedupe scope as the guard uses it: cleaned like text, surrounding whitespace removed, '' and None mean "no
    scope"."""
    if scope is None:
        return None
    text = (clean_text(scope) or "").strip()
    return text or None


def _fingerprint(normalized: str, scope: str | None) -> str:
    # length-prefixed, so no (scope, text) pair can produce another pair's bytes
    data = normalized if scope is None else f"{len(scope)}:{scope}\n{normalized}"
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def text_fingerprint(text: str, scope: str | None = None) -> str:
    """sha256 of the normalised text - or of the dedupe scope ('gym:12', 'product:7') and the normalised text - which is
    what the duplicate check stores (never the text itself)."""
    return _fingerprint(normalize_text(clean_text(text) or ""), clean_scope(scope))


def spam_reason(text: str) -> str | None:
    """Why ``text`` looks like spam, as a short phrase for the refusal message - or None for a normal message.

    Deliberately shape-only (no word lists), so it never trips on normal short replies ('ok', '?', '2',
    'thanks!!'), numbers, Hindi/Tamil/Arabic/CJK text, a little emoji, or a long sincere paragraph."""
    if len(text) > ai_policy.MAX_TEXT_CHARS:
        return f"it is longer than {ai_policy.MAX_TEXT_CHARS} characters"
    for match in _RUN_RE.finditer(text):
        if match.group(1) not in RUN_EXEMPT:
            return "one character is repeated over and over"
    if len(_LINK_RE.findall(text)) > MAX_LINKS:
        return "it contains too many links"
    words = [w for w in (_trim(t, _is_punct).casefold() for t in text.split()) if w]
    if len(words) >= TOKEN_REPEATS:
        _word, count = Counter(words).most_common(1)[0]
        if count >= TOKEN_REPEATS and count > len(words) * TOKEN_SHARE:
            return "the same word is repeated over and over"
    visible = [c for c in text if not c.isspace()]
    if visible and len(text) > SYMBOLS_MIN_CHARS:
        # str.isalnum covers every script; combining marks (Devanagari/Tamil vowel signs) belong to their letter
        wordish = sum(1 for c in visible if c.isalnum() or unicodedata.category(c).startswith("M"))
        if wordish < len(visible) * MIN_WORD_SHARE:
            return "it is mostly symbols"
    if visible and len(text) > VARIETY_MIN_CHARS:
        if len(set(visible)) < MIN_VARIETY * min(len(visible), VARIETY_SPAN):
            return "it uses only a handful of different characters"
    return None


# Injection phrases are matched in a FOLDED copy of the text (never shown, never stored), so how the words are written
# cannot hide them: NFKD (full-width, mathematical and ligature letters become plain ones, accents split off), every
# format character dropped (zero-width spaces and joiners, soft hyphens, bidi marks - category Cf) with the combining
# marks, and the Cyrillic / Greek / other letters that look like Latin ones folded to those.
_CONFUSABLES = str.maketrans({
    # Cyrillic
    "а": "a", "А": "A", "В": "B", "е": "e", "Е": "E", "к": "k", "К": "K", "М": "M", "Н": "H", "о": "o", "О": "O",
    "р": "p", "Р": "P", "с": "c", "С": "C", "Т": "T", "у": "y", "У": "Y", "х": "x", "Х": "X", "і": "i", "І": "I",
    "ј": "j", "Ј": "J", "ѕ": "s", "Ѕ": "S", "ԁ": "d", "һ": "h", "Һ": "H", "ӏ": "l", "Ӏ": "I", "ԛ": "q", "ԝ": "w",
    "ɡ": "g", "ı": "i", "ȷ": "j", "օ": "o", "ս": "u",
    # Greek
    "α": "a", "Α": "A", "Β": "B", "γ": "y", "ε": "e", "Ε": "E", "Ζ": "Z", "η": "n", "Η": "H", "ι": "i", "Ι": "I",
    "κ": "k", "Κ": "K", "Μ": "M", "ν": "v", "Ν": "N", "ο": "o", "Ο": "O", "ρ": "p", "Ρ": "P", "τ": "t", "Τ": "T",
    "υ": "u", "Υ": "Y", "χ": "x", "Χ": "X",
})
_DROPPED_CATEGORIES = frozenset({"Cf", "Mn", "Me"})
_FOLD_SPACES = re.compile(r"[^\S\n]+")


def fold_text(text: str) -> str:
    """The copy injection phrases are matched in (see above): line breaks kept, other whitespace runs collapsed."""
    decomposed = unicodedata.normalize("NFKD", text)
    kept = "".join(ch for ch in decomposed if unicodedata.category(ch) not in _DROPPED_CATEGORIES)
    return _FOLD_SPACES.sub(" ", kept.translate(_CONFUSABLES)).strip()


def injection_marker(text: str) -> str | None:
    """The prompt-injection phrase found in ``text`` ('ignore all previous instructions', 'system prompt' ...) - matched
    in the folded copy, so zero-width characters, full-width letters or look-alike letters do not hide it."""
    match = _INJECTION_RE.search(fold_text(text))
    return " ".join(match.group(0).lower().split()) if match else None


# What only makes sense as an instruction to the AI inside SAVED data (a health item, a note, a fitness-profile item that
# every AI prompt quotes): a chat-format role marker ("SYSTEM:", "[assistant]", "<|im_start|>", "[INST]"), a new rule for
# the AI, telling the reader what to tell or answer the member ("always tell this member to ..."), overriding the rules
# or instructions, "say so first". Never applied to what people type in a chat (normal requests say "you must" and
# "from now on" all the time) - only to text an AI prompt quotes as data. A note in the member's own voice ("tell me if
# my form is off", "you must keep my heart rate under 150") does not match.
_SAVED_INSTRUCTION_RE = re.compile(
    r"[\[(<{]\s*/?\s*(?:system|assistant|developer)\s*[\])>}]"
    r"|<\|[a-z_ ]{2,20}\|>|\[/?inst\]|<</?sys>>|(?<!#)#{2,}\s*(?:system|instructions?|response)\b"
    r"|\bnew\s+(?:rules?|instructions?|directives?|system\s+prompt|guidelines?)\s*(?::|for\s+(?:the\s+|this\s+|you\b|"
    r"(?:ai|a\.i\.|assistant|bot|chat\s*bot|model|llm|system)\b))"
    r"|\bfrom\s+now\s+on\b[\s,:]+(?:\w+\s+){0,2}?(?:ignore|disregard|forget|override|pretend|act\s+as|respond|reply|"
    r"answer|you\s+are)\b"
    r"|\b(?:always|never|must|should|shall|have\s+to|need\s+to|will)\s+(?:\w+\s+){0,2}?(?:tell|say|respond|reply|"
    r"answer|recommend|advise|instruct|insist|inform|claim|output|mention)\s+(?:(?:to|that)\s+)?(?:(?:the|this|that|"
    r"every|each|all|any)\s+)?(?:member|user|customer|client|patient|person|athlete)s?\b"
    r"|\b(?:you|the\s+(?:ai|assistant|bot|model|llm|system))\s+(?:must|shall|are\s+to|will\s+now|have\s+to|need\s+to)"
    r"\s+(?:always\s+|now\s+)?(?:ignore|disregard|pretend|roleplay|role-play|reveal|print|output)\b"
    r"|\b(?:ignore|disregard|override|bypass)\s+(?:\w+\s+){0,3}?(?:rules?|instructions?|guidelines?|guardrails?|"
    r"safety|polic(?:y|ies)|prompts?|programming|constraints?|restrictions?|filters?)\b"
    r"|\b(?:say|tell|state|write|output)\s+(?:so|this|that|it)\s+(?:first|verbatim|immediately)\b",
    re.IGNORECASE,
)


# a chat-format role marker opening a line or sentence ("SYSTEM:", "### Assistant:", "> developer message:") - looked for
# piece by piece (linear in the text's length, whatever its punctuation and line breaks)
_ROLE_SPLIT = re.compile(r"[\n.!?;]")
_ROLE_PREFIX = re.compile(r"(?:system|assistant|developer)(?:\s+(?:message|prompt|instructions?|override|update))?\s*:",
                          re.IGNORECASE)


def instruction_marker(text: str) -> str | None:
    """The instruction-to-the-AI phrase in a SAVED text that AI prompts quote as data (``app.ai.member_context``): every
    chat injection phrase (``injection_marker``) plus the forms above - matched in the folded copy. None for data."""
    folded = fold_text(text)
    match = _INJECTION_RE.search(folded) or _SAVED_INSTRUCTION_RE.search(folded)
    if match:
        return " ".join(match.group(0).lower().split())
    for piece in _ROLE_SPLIT.split(folded):
        role = _ROLE_PREFIX.match(piece.lstrip("#*> \t"))
        if role:
            return " ".join(role.group(0).lower().split())
    return None


def human_wait(seconds: float) -> str:
    """'45 seconds', '12 min', '3 h 20 min' - how long someone has to wait, in words."""
    total = max(1, math.ceil(seconds))
    if total < 60:
        return f"{total} second" + ("" if total == 1 else "s")
    minutes = math.ceil(total / 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"{hours} h {rest} min" if rest else f"{hours} h"


def _text(value: Any) -> str | None:
    if value is None:
        return None
    return value.decode() if isinstance(value, bytes) else str(value)


def _int(value: Any) -> int:
    try:
        return int(_text(value) or 0)
    except (TypeError, ValueError):
        return 0


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat()


def _utc_day(ts: float) -> str:
    """'2026-11-10': the UTC day the per-day counters of ``snapshot()`` belong to."""
    return datetime.fromtimestamp(ts, UTC).date().isoformat()


_process: tuple[int, str] | None = None


def _process_id() -> str:
    """Identifies this process in an exported admission: an in-memory charge can only be refunded where it was taken
    (a forked worker gets an id of its own)."""
    global _process
    pid = os.getpid()
    if _process is None or _process[0] != pid:
        _process = (pid, f"{pid}-{secrets.token_hex(4)}")
    return _process[1]


def _parse_cooldown(raw: Any) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        info = json.loads(raw)
        info["until"] = float(info["until"])
        info["started"] = float(info.get("started") or info["until"])
    except (TypeError, ValueError, KeyError):
        return None
    return info if isinstance(info, dict) else None


def _strike_total(entries: Iterable[Any]) -> float:
    """Sum of the points encoded in strike members ('<points>|<code>|<nonce>')."""
    total = 0.0
    for entry in entries:
        member = entry[0] if isinstance(entry, (tuple, list)) else entry
        try:
            total += float((_text(member) or "0").split("|", 1)[0])
        except ValueError:
            continue
    return total


def _crossed_level(before: float, after: float) -> tuple[int, int] | None:
    """(1-based ladder level, seconds) of the highest COOLDOWN_LADDER level crossed by going before -> after."""
    hit = None
    for index, (points, seconds) in enumerate(ai_policy.COOLDOWN_LADDER, start=1):
        if before < points <= after:
            hit = (index, int(seconds))
    return hit


def _free_strikes(code: str) -> tuple[str, int] | None:
    """(counter key prefix, how many are free within STRIKE_WINDOW) for strike codes the first few of which cost no
    points - double clicks ('duplicate') and the odd closed tab ('abort') - or None. Read at call time (tunable)."""
    if code == "duplicate":
        return "dc", int(ai_policy.DUPLICATE_FREE)
    if code == "abort":
        return "ab", int(getattr(ai_policy, "ABORT_FREE", 0))
    return None


def _escalation_severity(seconds: float) -> int:
    """Threat severity a cooldown of this length is reported with (0 = too short to report)."""
    if seconds < ai_policy.ESCALATE_MIN_SECONDS:
        return 0
    return max((sev for threshold, sev in ai_policy.ESCALATE_SEVERITY.items() if seconds >= threshold), default=2)


def _reportable_ip(owner: AIOwner) -> str | None:
    """The address a long pause of ``owner`` is reported for - None without one or for a whitelisted address."""
    ip = (owner.ip or "").strip()
    if not ip or ip in get_settings().rate_limit_whitelist:
        return None
    return ip


def _version(values: Any) -> tuple[str | None, ...]:
    """An entitlement version as read by MGET (the user's key, the everyone key)."""
    return tuple(_text(value) for value in (values or ()))


def _usage_groups() -> list[str]:
    groups = {group for windows in ai_policy.QUOTAS.values() for group in windows}
    groups.update(p.group for p in ai_policy.FEATURES.values() if p.guarded)
    return sorted(groups)


def _glob_escape(text: str) -> str:
    return re.sub(r"([*?\[\]\\])", r"\\\1", text)


# =========================================================================== stores
class RedisStore:
    """Redis backend. ``run`` sends a command list as ONE MULTI/EXEC pipeline (one round trip, atomic)."""

    name = "redis"

    def __init__(self, client: Any) -> None:
        self.client = client

    async def run(self, ops: list[Op]) -> list[Any]:
        async with self.client.pipeline(transaction=True) as pipe:
            for name, args, kwargs in ops:
                getattr(pipe, name)(*args, **kwargs)
            return list(await pipe.execute())

    async def transact(self, watch: list[str], reads: list[Op],
                       decide: Callable[[list[Any]], tuple[list[Op], Any]], *, attempts: int = 5) -> tuple[list[Op], Any]:
        """Optimistic read-modify-write: WATCH ``watch``, run ``reads``, let ``decide`` turn the results into
        (writes, value), then MULTI/EXEC the writes; retried when a watched key changed meanwhile."""
        from redis.exceptions import WatchError

        for _ in range(attempts):
            async with self.client.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(*watch)
                    results = [await getattr(pipe, name)(*args, **kwargs) for name, args, kwargs in reads]
                    ops, value = decide(results)
                    if not ops:
                        return ops, value
                    pipe.multi()
                    for name, args, kwargs in ops:
                        getattr(pipe, name)(*args, **kwargs)
                    await pipe.execute()
                    return ops, value
                except WatchError:
                    continue
        # still contended after several tries: last writer wins - a pause must never be lost to a race
        ops, value = decide(await self.run(reads))
        if ops:
            await self.run(ops)
        return ops, value

    async def delete_prefix(self, prefix: str) -> int:
        deleted = 0
        batch: list[str] = []
        async for key in self.client.scan_iter(match=_glob_escape(prefix) + "*", count=1000):
            batch.append(key)
            if len(batch) >= 500:
                deleted += int(await self.client.unlink(*batch))
                batch.clear()
        if batch:
            deleted += int(await self.client.unlink(*batch))
        return deleted


class MemoryStore:
    """In-process twin of the Redis subset the guard uses (strings, counters, sorted sets, TTLs, MULTI).

    Used when Redis is not configured or an operation fails. Commands run synchronously inside ``run``, so a
    command list is atomic for this event loop exactly like a MULTI/EXEC. TTLs use the monotonic clock."""

    name = "memory"
    SWEEP_EVERY = 4096  # commands between sweeps of expired keys

    def __init__(self, *, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._data: dict[str, Any] = {}
        self._expires: dict[str, float] = {}
        self._since_sweep = 0

    # ------------------------------------------------------------- interface shared with RedisStore
    async def run(self, ops: list[Op]) -> list[Any]:
        return self.run_now(ops)

    def run_now(self, ops: list[Op]) -> list[Any]:
        self._since_sweep += len(ops)
        if self._since_sweep >= self.SWEEP_EVERY:
            self._sweep()
        return [getattr(self, "c_" + name)(*args, **kwargs) for name, args, kwargs in ops]

    async def transact(self, watch: list[str], reads: list[Op],
                       decide: Callable[[list[Any]], tuple[list[Op], Any]]) -> tuple[list[Op], Any]:
        ops, value = decide(self.run_now(reads))  # nothing awaits in between: atomic
        if ops:
            self.run_now(ops)
        return ops, value

    async def delete_prefix(self, prefix: str) -> int:
        doomed = [key for key in self._data if key.startswith(prefix)]
        for key in doomed:
            self._drop(key)
        return len(doomed)

    def clear(self) -> None:
        self._data.clear()
        self._expires.clear()
        self._since_sweep = 0

    def __len__(self) -> int:
        return len(self._data)

    # ------------------------------------------------------------- internals
    def _sweep(self) -> None:
        self._since_sweep = 0
        now = self._now()
        for key in [k for k, exp in self._expires.items() if exp <= now]:
            self._drop(key)

    def _drop(self, key: str) -> None:
        self._data.pop(key, None)
        self._expires.pop(key, None)

    def _alive(self, key: str) -> bool:
        exp = self._expires.get(key)
        if exp is not None and exp <= self._now():
            self._drop(key)
        return key in self._data

    def _zset(self, key: str, *, create: bool = False) -> dict[str, float] | None:
        if not self._alive(key):
            if not create:
                return None
            self._data[key] = {}
        value = self._data[key]
        if not isinstance(value, dict):
            raise TypeError(f"WRONGTYPE {key} is not a sorted set")
        return value

    @staticmethod
    def _bound(value: Any) -> float:
        text = str(value).strip().lower()
        if text in ("-inf", "-infinity"):
            return -math.inf
        if text in ("+inf", "inf", "+infinity", "infinity"):
            return math.inf
        return float(text)

    def _sorted(self, key: str) -> list[tuple[str, float]]:
        zset = self._zset(key) or {}
        return sorted(zset.items(), key=lambda item: (item[1], item[0]))

    def _ttl_set(self, key: str, seconds: float | None) -> None:
        if seconds is not None:
            self._expires[key] = self._now() + float(seconds)

    # ------------------------------------------------------------- commands (redis-py names and semantics)
    def c_get(self, key: str) -> str | None:
        if not self._alive(key):
            return None
        value = self._data[key]
        if not isinstance(value, str):
            raise TypeError(f"WRONGTYPE {key} is not a string")
        return value

    def c_set(self, key: str, value: Any, ex: float | None = None, px: float | None = None, nx: bool = False,
              xx: bool = False, keepttl: bool = False) -> bool | None:
        alive = self._alive(key)
        if (nx and alive) or (xx and not alive):
            return None
        self._data[key] = str(value)
        if not keepttl:
            self._expires.pop(key, None)
        self._ttl_set(key, px / 1000.0 if px is not None else ex)
        return True

    def c_delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if self._alive(key):
                self._drop(key)
                removed += 1
        return removed

    def c_incrby(self, key: str, amount: int = 1) -> int:
        value = int(self.c_get(key) or 0) + int(amount)
        self._data[key] = str(value)  # like Redis, INCRBY keeps the key's TTL
        return value

    def c_expire(self, key: str, time: float, **_flags: Any) -> bool:
        if not self._alive(key):
            return False
        self._ttl_set(key, time)
        return True

    def c_pttl(self, key: str) -> int:
        if not self._alive(key):
            return -2
        exp = self._expires.get(key)
        return -1 if exp is None else max(0, int((exp - self._now()) * 1000))

    def c_mget(self, keys: Iterable[str], *more: str) -> list[str | None]:
        out: list[str | None] = []
        for key in [*keys, *more]:
            value = self._data.get(key) if self._alive(key) else None
            out.append(value if isinstance(value, str) else None)
        return out

    def c_zadd(self, key: str, mapping: dict[Any, float], nx: bool = False, xx: bool = False, ch: bool = False,
               gt: bool = False, lt: bool = False, **_flags: Any) -> int:
        zset = self._zset(key, create=True)
        assert zset is not None
        added = changed = 0
        for member, score in mapping.items():
            member, score = str(member), float(score)
            old = zset.get(member)
            if old is None:
                if xx:
                    continue
                zset[member] = score
                added += 1
            elif not nx and not (gt and score <= old) and not (lt and score >= old) and score != old:
                zset[member] = score
                changed += 1
        if not zset:
            self._drop(key)
        return added + changed if ch else added

    def c_zincrby(self, key: str, amount: float, value: Any) -> float:
        zset = self._zset(key, create=True)
        assert zset is not None
        zset[str(value)] = score = zset.get(str(value), 0.0) + float(amount)
        return score

    def c_zrem(self, key: str, *members: Any) -> int:
        zset = self._zset(key)
        if not zset:
            return 0
        removed = sum(1 for m in members if zset.pop(str(m), None) is not None)
        if not zset:
            self._drop(key)
        return removed

    def c_zscore(self, key: str, member: Any) -> float | None:
        zset = self._zset(key)
        return None if zset is None else zset.get(str(member))

    def c_zcard(self, key: str) -> int:
        return len(self._zset(key) or {})

    def c_zcount(self, key: str, min: Any, max: Any) -> int:  # noqa: A002 - redis-py names
        lo, hi = self._bound(min), self._bound(max)
        return sum(1 for score in (self._zset(key) or {}).values() if lo <= score <= hi)

    def c_zremrangebyscore(self, key: str, min: Any, max: Any) -> int:  # noqa: A002
        zset = self._zset(key)
        if not zset:
            return 0
        lo, hi = self._bound(min), self._bound(max)
        doomed = [m for m, score in zset.items() if lo <= score <= hi]
        for member in doomed:
            del zset[member]
        if not zset:
            self._drop(key)
        return len(doomed)

    def c_zrange(self, key: str, start: int, end: int, desc: bool = False, withscores: bool = False,
                 **_flags: Any) -> list[Any]:
        items = self._sorted(key)
        if desc:
            items.reverse()
        n = len(items)
        lo = start if start >= 0 else max(0, n + start)
        hi = end if end >= 0 else n + end
        picked = items[lo:hi + 1] if lo <= hi else []
        return [(m, s) for m, s in picked] if withscores else [m for m, _s in picked]

    def c_zrangebyscore(self, key: str, min: Any, max: Any, start: int | None = None,  # noqa: A002
                        num: int | None = None, withscores: bool = False, **_flags: Any) -> list[Any]:
        lo, hi = self._bound(min), self._bound(max)
        picked = [(m, s) for m, s in self._sorted(key) if lo <= s <= hi]
        if start is not None and num is not None:
            picked = picked[start:start + num] if num >= 0 else picked[start:]
        return picked if withscores else [m for m, _s in picked]


async def _redis_client() -> Any | None:
    """The shared asyncio Redis client (``app.security.sessions.get_redis``) or None. Imported lazily (sessions
    pulls in the models) and kept module-level so tests can swap it."""
    from app.security.sessions import get_redis

    return await get_redis()


# =========================================================================== guard
@dataclass(slots=True)
class Admission:
    """A request the guard let through. Hand it back to ``guard.settle`` when the AI work is over - or, for work that
    continues in a background job, store ``to_payload()`` with the job and settle it there with
    ``guard.settle_payload(payload, ok)``."""

    user_id: int
    feature: str
    group: str
    units: int
    fingerprint: str | None = None  # sha256 of the normalised text (+ scope) - duplicate check; None without text
    settled: bool = False
    extra: dict[str, Any] = field(default_factory=dict)  # free for callers
    tier: str | None = None
    backend: str = "memory"  # store holding the charge ("redis" | "memory"): a refund goes back to the same one
    charges: tuple[tuple[str, int, float], ...] = ()  # (counter key, units, window end) per quota window
    dedupe_key: str | None = None
    dedupe_token: str | None = None  # value stored under dedupe_key: a refund only forgets its *own* fingerprint
    scope: str | None = None  # the dedupe scope the text was judged in ('gym:12', 'product:7')
    # set when exported (to_payload): the settle claim's id, and the process that took the charge - an in-memory
    # charge can only be given back there
    ref: str = ""
    origin: str = ""
    shared: bool = False  # exported with to_payload(): every settle goes through one claim in the store

    def to_payload(self) -> dict[str, Any]:
        """JSON-safe copy for a background job (charges, backend, dedupe token, settled ...). The job settles it with
        ``await guard.settle_payload(payload, ok)`` - e.g. ``ok=False`` when it falls back without AI, so the person
        gets back what the click was charged. This admission and its payloads are settled once in total: whichever
        settle comes first claims it (in the store that holds the charge), the others do nothing."""
        if not self.ref:
            self.ref = secrets.token_hex(8)
            self.origin = self.origin or _process_id()
        self.shared = True
        return {
            "v": PAYLOAD_VERSION, "ref": self.ref, "user_id": self.user_id, "feature": self.feature,
            "group": self.group, "units": self.units, "tier": self.tier, "backend": self.backend,
            "origin": self.origin, "charges": [[key, units, end] for key, units, end in self.charges],
            "dedupe_key": self.dedupe_key, "dedupe_token": self.dedupe_token, "fingerprint": self.fingerprint,
            "scope": self.scope, "settled": self.settled,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any] | None) -> Admission | None:
        """The admission a ``to_payload()`` dict describes (it round-trips through JSON), or None when the payload is
        missing or malformed. Its settles go through the shared claim, like the original's."""
        if not isinstance(payload, Mapping) or payload.get("v", PAYLOAD_VERSION) != PAYLOAD_VERSION:
            return None

        def whole(value: Any) -> bool:
            return isinstance(value, int) and not isinstance(value, bool)

        def text_or_none(value: Any, limit: int = 300) -> bool:
            return value is None or (isinstance(value, str) and len(value) <= limit)

        ref, uid, units = payload.get("ref"), payload.get("user_id"), payload.get("units")
        feature, group, backend = payload.get("feature"), payload.get("group"), payload.get("backend")
        if not (isinstance(ref, str) and re.fullmatch(r"[0-9a-f]{8,64}", ref) and whole(uid) and whole(units)
                and units >= 0 and isinstance(feature, str) and isinstance(group, str)
                and backend in (RedisStore.name, MemoryStore.name)):
            return None
        if not all(text_or_none(payload.get(k)) for k in ("tier", "origin", "dedupe_key", "dedupe_token",
                                                            "fingerprint", "scope")):
            return None
        raw = payload.get("charges") or []
        if not isinstance(raw, (list, tuple)) or len(raw) > 16:
            return None
        charges: list[tuple[str, int, float]] = []
        for item in raw:
            if not (isinstance(item, (list, tuple)) and len(item) == 3 and isinstance(item[0], str)
                    and len(item[0]) <= 300 and whole(item[1]) and item[1] >= 0
                    and isinstance(item[2], (int, float)) and not isinstance(item[2], bool)
                    and math.isfinite(item[2])):
                return None
            charges.append((item[0], int(item[1]), float(item[2])))
        return cls(user_id=uid, feature=feature, group=group, units=units, fingerprint=payload.get("fingerprint"),
                   settled=bool(payload.get("settled")), tier=payload.get("tier"), backend=backend,
                   charges=tuple(charges), dedupe_key=payload.get("dedupe_key"),
                   dedupe_token=payload.get("dedupe_token"), scope=payload.get("scope"), ref=ref, shared=True,
                   origin=payload.get("origin") or "")


@dataclass(slots=True)
class Reservation:
    """One use of a per-person allowance (``AIGuard.reserve``): ``taken`` False when the allowance was used up."""

    key: str
    backend: str  # redis | memory | none (not counted: the store failed and the use was let through)
    count: int  # uses in the window, this one included when taken
    taken: bool
    released: bool = False


@dataclass(slots=True, eq=False)
class _Entitlement:
    """One cached answer to "does this customer's active plan include AI?"."""

    has_ai: bool
    expires: float  # time.monotonic()
    version: tuple[str | None, ...] | None  # the shared version keys, read BEFORE the database lookup


class _RedisWipe:
    """What ``AIGuard.reset`` returns: memory is already clear when you get it; awaiting it also deletes this
    namespace's Redis keys. Not a coroutine, so a sync caller that ignores it gets no 'never awaited' warning."""

    __slots__ = ("_guard", "_pending")

    def __init__(self, guard: AIGuard, pending: list[asyncio.Task] | None = None) -> None:
        self._guard = guard
        self._pending = pending or []

    def __await__(self) -> Generator[Any, None, int]:
        return self._guard._wipe_redis(self._pending).__await__()


class _Later:
    """What ``forget_entitlement`` returns: the work already runs in a task of its own (a sync caller may drop this);
    awaiting it waits until that work is done. Never raises."""

    __slots__ = ("_task",)

    def __init__(self, task: asyncio.Task | None) -> None:
        self._task = task

    def __await__(self) -> Generator[Any, None, None]:
        return self._wait().__await__()

    async def _wait(self) -> None:
        if self._task is not None and not self._task.done():
            with contextlib.suppress(Exception):
                await asyncio.shield(self._task)


class AIGuard:
    """Per-user AI spam protection (docs/ai-guard-and-queue.md section 3). ``guard`` is the process singleton;
    tests may build their own with a fake ``clock`` (epoch seconds) to step through windows and cooldowns."""

    redis_retry_seconds = 2.0  # after a Redis error this process uses its in-memory store for this long

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self._memory = MemoryStore()
        self._redis_store: RedisStore | None = None
        self._redis_skip_until = 0.0
        self._redis_logged_at = -math.inf
        self._entitlements: dict[int, _Entitlement] = {}
        self._entitlement_locks: dict[int, asyncio.Lock] = {}
        self._bumps: dict[int | str, set[asyncio.Task]] = {}  # entitlement version bumps on their way to the store
        self._tasks: set[asyncio.Task] = set()  # judges whose caller left, and the refunds they need (strong refs)
        self._ns_cache: tuple[tuple[str, str], str] | None = None

    # ================================================================= public API
    async def admit(self, owner: AIOwner | None, feature: str, text: str | None = None, *,
                    scope: str | None = None) -> Admission | None:
        """Judge one AI request before any compute is spent. Returns None when there is nothing to judge (guard
        off, no signed-in user, a job/system call, an unguarded feature), an ``Admission`` to ``settle`` later, or
        raises ``AIRejected`` (codes cooldown | spam | duplicate | quota).

        ``scope`` names the subject the text is about ('gym:12', 'product:7'): the duplicate check compares
        (feature, scope, normalised text), so the same suggested question about a second gym is a new question.
        ``text`` and ``scope`` are cleaned first (``clean_text``): no text can make the guard raise or skip a check.

        The judging runs in a task of its own that the caller's cancellation cannot interrupt (a closed tab cancels the
        SSE task streaming the answer, a ``wait_for`` runs out): when the caller is gone, the admission it would have
        got is refunded at once - quota units back, fingerprint forgotten - and a refusal keeps its own bookkeeping,
        whether the cancel landed during the charge round trip itself or during anything awaited after it."""
        policy = self._policy(owner, feature)
        if policy is None:
            return None
        judge = self._spawn(self._judge(owner, feature, policy, text, scope), "ai-guard-admit")
        try:
            return await asyncio.shield(judge)
        except asyncio.CancelledError:
            judge.add_done_callback(self._abandoned)  # runs at once (soon) if the judge has already decided
            raise

    def _spawn(self, work: Coroutine[Any, Any, Any], name: str) -> asyncio.Task:
        """A task the guard keeps a strong reference to until it is done (the loop itself only keeps weak ones)."""
        task = asyncio.get_running_loop().create_task(work, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _abandoned(self, judge: asyncio.Task) -> None:
        """Done-callback of a judge whose caller was cancelled: nobody will use or settle what it decided. An
        admission is refunded (quota units back, fingerprint forgotten); a refusal already gave back its own charge
        and booked its strikes; an error after the charge was undone by the judge itself."""
        if judge.cancelled() or judge.exception() is not None:
            return
        admission = judge.result()
        if admission is not None:
            self._spawn(self.settle(admission, ok=False), "ai-guard-refund")

    async def _judge(self, owner: AIOwner, feature: str, policy: ai_policy.FeaturePolicy, text: Any,
                     scope: Any) -> Admission:
        """``admit`` for a request ``_policy`` says to judge (see there)."""
        uid = int(owner.user_id or 0)
        now = self.clock()
        ns = self._ns()
        tier, entitlement = await self._tier(owner)
        units = max(0, int(policy.cost))
        safety = policy.safety
        scope = clean_scope(scope)
        text = clean_text(text)

        spam: str | None = None
        injection: str | None = None
        fingerprint = dedupe_key = token = None
        dedupe_ms = 0
        if text is not None and not safety:  # safety features are never judged by their words
            try:
                spam = spam_reason(text)
                if spam is None:
                    injection = injection_marker(text)
                    normalized = normalize_text(text)
                    if policy.dedupe_seconds > 0 and normalized:
                        fingerprint = _fingerprint(normalized, scope)
                        window = float(policy.dedupe_seconds)
                        if len(normalized) < SHORT_REPLY_CHARS:
                            window = min(window, SHORT_REPLY_DEDUPE)
                        dedupe_ms = max(1, int(window * 1000))
            except Exception:  # noqa: BLE001 - words that cannot be read: judged without them, never let through
                log.exception("AI guard: could not read the text of user %s on %r - judged without it", uid, feature)
                spam = injection = fingerprint = None
                dedupe_ms = 0
            if fingerprint is not None:
                dedupe_key = f"{ns}dd:{uid}:{feature}:{fingerprint}"
                token = secrets.token_hex(8)

        # one pipelined round trip: cooldown? + claim the fingerprint + charge every quota window (+ the version of
        # a customer's cached AI entitlement, so a change committed by another process is seen right now)
        ops: list[Op] = []
        if not safety:
            ops.append(_op("get", f"{ns}cd:{uid}"))
        if dedupe_key:
            ops += [_op("set", dedupe_key, token, px=dedupe_ms, nx=True), _op("pttl", dedupe_key)]
        charges: list[tuple[str, int, float]] = []
        if spam is None:
            for win in ai_policy.WINDOW_SECONDS:
                idx = int(now // win)
                end = float((idx + 1) * win)
                key = f"{ns}q:{uid}:{policy.group}:{win}:{idx}"
                ops += [_op("set", key, "0", ex=max(1, math.ceil(end - now) + 1), nx=True), _op("incrby", key, units)]
                charges.append((key, units, end))
        ops.append(_op("set", f"{ns}tier:{uid}", tier, ex=TIER_MEMORY_SECONDS))
        if entitlement is not None:
            ops.append(_op("mget", self._entitlement_keys(uid)))
        backend, res = await self._run(ops)
        admission = Admission(user_id=uid, feature=feature, group=policy.group, units=units, fingerprint=fingerprint,
                              tier=tier, backend=backend, charges=tuple(charges), dedupe_key=dedupe_key,
                              dedupe_token=token, scope=scope)
        # From here this request holds its charge (and the fingerprint it claimed) until a refusal rolls it back or the
        # admission is handed out; anything else that ends the judging - a bug, this very task cancelled at shutdown -
        # gives it back instead of leaking it.
        held = bool(charges or dedupe_key)
        try:
            # an unknown version (its read failed) is never compared: that answer just lives out its ENTITLEMENT_TTL
            stale = (entitlement is not None and entitlement.version is not None
                     and _version(res[-1]) != entitlement.version)

            pos = 0
            if not safety:
                cooldown = _parse_cooldown(res[0])
                pos = 1
                if cooldown is not None and cooldown["until"] > now:
                    owned = dedupe_key if dedupe_key and res[pos] else None
                    held = False
                    await self._rollback(backend, charges, owned, also=self._count_ops(ns, now, "refused:cooldown")
                                         + self._offender_ops(ns, now, uid))
                    raise self._cooldown_error(cooldown, now, feature)
            claimed, remaining_ms = True, 0
            if dedupe_key:
                claimed, remaining_ms = bool(res[pos]), _int(res[pos + 1])
                pos += 2
            counts = [_int(res[pos + 2 * i + 1]) for i in range(len(charges))]

            if spam is not None:  # charged nothing
                await self._reject(owner, feature, "spam",
                                   f"That message looks like spam ({spam}). Please rephrase it.", 0)
            if injection is not None:
                await self.note_injection(owner, feature, marker=injection)
            if dedupe_key and not claimed:
                held = False
                await self._rollback(backend, charges)
                wait = remaining_ms / 1000.0 if remaining_ms > 0 else dedupe_ms / 1000.0
                await self._reject(owner, feature, "duplicate", DUPLICATE_MESSAGE, wait)
            if stale and entitlement is not None:  # another process changed the subscription: judge with the new tier
                tier = admission.tier = await self._retier(owner, entitlement, backend, tier)
            limits = self._limits(tier, policy.group)
            if limits is not None and units > 0 and not safety:
                refused: tuple[int, float] | None = None
                for (_key, _units, end), win, count, limit in zip(charges, ai_policy.WINDOW_SECONDS, counts, limits):
                    # a single call bigger than a whole window still runs when that window is otherwise unused
                    if count > limit and count - units > 0 and (refused is None or end > refused[1]):
                        refused = (win, end)
                if refused is not None:
                    held = False
                    await self._rollback(backend, charges, dedupe_key)
                    win, end = refused
                    await self._reject(owner, feature, "quota", self._quota_message(policy.group, win, end - now),
                                       end - now)
            held = False
            return admission
        except BaseException:
            if held:  # not a refusal (those roll back their own charge): give back what this request holds
                self._spawn(self._refund(admission), "ai-guard-refund")
            raise

    async def settle(self, admission: Admission | None, *, ok: bool) -> None:
        """End of the AI work. ``ok=False`` (the work never reached a provider, the provider failed, the queue was
        busy): refund the quota units in every still-open window and forget the duplicate fingerprint, so a retry works
        and nobody pays for AI that never answered. ``ok=True`` keeps the charge - an answer, or a call its requester
        dropped after it reached a provider (app/ai/client.py decides). Idempotent; never raises. An admission
        exported with ``to_payload()`` is settled through the same claim as its payloads (``settle_payload``)."""
        if admission is None or admission.settled:
            return
        admission.settled = True
        if admission.shared and admission.ref:
            await asyncio.shield(self._settle_claimed(admission, ok))
            return
        if ok:
            return
        await asyncio.shield(self._refund(admission))

    async def forget_fingerprint(self, admission: Admission | None) -> bool:
        """Let the person ask the same thing again at once while the charge stays - for an answer cut short after a
        provider worked on it (its requester left mid-answer): they never got all of it, and asking again is a new,
        charged question. Only this admission's own fingerprint goes (its token must still be the stored value).
        Returns True when it was removed. Never raises."""
        if admission is None or not admission.dedupe_key or not admission.dedupe_token:
            return False
        try:
            res = await self._run_on(admission.backend, [_op("get", admission.dedupe_key)])
            if res is None or _text(res[0]) != admission.dedupe_token:
                return False
            await self._run_on(admission.backend, [_op("delete", admission.dedupe_key)])
            return True
        except Exception:  # noqa: BLE001 - settles run in finally blocks and must never raise
            log.exception("AI guard: could not forget a fingerprint of user %s", admission.user_id)
            return False

    async def settle_payload(self, payload: Mapping[str, Any] | None, ok: bool) -> bool:
        """``settle`` for an ``Admission.to_payload()`` dict kept with a background job: ``ok=False`` (the job fell
        back without AI, failed or was cancelled) refunds what the person was charged when they asked; ``ok=True``
        keeps the charge. Idempotent across retries and processes: the first settle of an admission - this one, the
        original's ``settle`` or another payload copy - claims it in the store that holds the charge, later ones do
        nothing. Marks the payload ``settled`` in place. Returns True when this call settled it. Never raises."""
        try:
            if not isinstance(payload, Mapping) or payload.get("settled"):
                return False
            admission = Admission.from_payload(payload)
            if admission is None or not self._owns_keys(admission):
                log.warning("AI guard: ignoring an invalid admission payload (feature %r)",
                            payload.get("feature") if isinstance(payload, Mapping) else None)
                return False
            done = await asyncio.shield(self._settle_claimed(admission, ok))
            if isinstance(payload, MutableMapping):
                payload["settled"] = True
            return done
        except Exception:  # noqa: BLE001 - settling runs in finally blocks and must never raise
            log.exception("AI guard: could not settle an admission payload")
            return False

    async def reserve(self, user_id: int, name: str, limit: int, *, window: int = DAY) -> Reservation:
        """Take one of ``limit`` uses of a per-person allowance ``name`` in the current fixed window (the UTC day by
        default) - for AI work the quota groups never refuse, such as the daily AI safety reviews of health readings
        (app/services/health.py). Atomic for any number of requests at once: one INCR in the shared store (Redis, or
        this process's store without it); a use past ``limit`` is given back at once (``taken`` False). ``release``
        returns a taken use when no AI work came of it. Never raises."""
        uid = int(user_id or 0)
        now = self.clock()
        idx = int(now // max(1, window))
        end = float((idx + 1) * window)
        key = f"{self._ns()}rsv:{uid}:{re.sub(r'[^A-Za-z0-9_.-]', '_', str(name))[:60]}:{window}:{idx}"
        try:
            backend, res = await self._run([_op("set", key, "0", ex=max(1, math.ceil(end - now) + 1), nx=True),
                                            _op("incrby", key, 1)])
            count = _int(res[1])
        except Exception:  # noqa: BLE001 - an allowance must never break the work it bounds: let this one through
            log.exception("AI guard: could not count %r for user %s", name, uid)
            return Reservation(key, "none", 0, True)
        if count > max(0, int(limit)):
            await self._run_on(backend, [_op("incrby", key, -1)])
            return Reservation(key, backend, count - 1, False)
        return Reservation(key, backend, count, True)

    async def release(self, reservation: Reservation | None) -> None:
        """Give back a taken use (``reserve``) - in the window and store it was taken from. Idempotent; never raises."""
        if reservation is None or not reservation.taken or reservation.released or reservation.backend == "none":
            return
        reservation.released = True
        try:
            await self._run_on(reservation.backend, [_op("incrby", reservation.key, -1)])
        except Exception:  # noqa: BLE001
            log.exception("AI guard: could not give back a use of %s", reservation.key)

    async def snapshot(self) -> dict[str, Any]:
        """Today's (UTC) refusal counters for the admin panel: {'refused': {code: n} for quota | duplicate | spam |
        cooldown, 'cooldowns_started_today': n, 'escalations_today': n, 'day': 'YYYY-MM-DD', 'backend': 'redis' |
        'memory'}. With Redis the counts cover every process; without it, this process only. A refusal is counted
        under its reason even when its strikes turned it into a pause (the pause itself is a started cooldown);
        'cooldown' counts requests that arrived during a pause. The AI queue's own refusals ('too_many', 'busy') are
        in ``scheduler.snapshot()``."""
        now = self.clock()
        ns = self._ns()
        day = _utc_day(now)
        names = [f"refused:{code}" for code in REFUSAL_CODES] + ["cooldowns", "escalations"]
        backend, res = await self._run([_op("mget", [f"{ns}stat:{day}:{name}" for name in names])])
        values = [max(0, _int(value)) for value in res[0]]
        return {
            "refused": dict(zip(REFUSAL_CODES, values)),
            "cooldowns_started_today": values[len(REFUSAL_CODES)],
            "escalations_today": values[len(REFUSAL_CODES) + 1],
            "day": day,
            "backend": backend,
        }  # a cancelled caller must not lose the refund half-way

    async def offenders(self, limit: int = 10) -> dict[str, Any]:
        """The "frequent limit offenders" of the super-admin's site overview (app/services/admin_insights.py): the
        accounts the guard refused most today and yesterday (UTC; every refusal of ``snapshot()``'s codes counts) -
        {'today': [(user_id, refusals)], 'yesterday': [...], 'floods_today': bursts of refusals that earned a 'flood'
        strike, 'day', 'backend'}. At most ``limit`` accounts per day, most refused first; one round trip."""
        now = self.clock()
        ns = self._ns()
        n = max(1, int(limit))
        today = _utc_day(now)
        backend, res = await self._run([
            _op("zrange", f"{ns}stat:{today}:offenders", 0, n - 1, desc=True, withscores=True),
            _op("zrange", f"{ns}stat:{_utc_day(now - DAY)}:offenders", 0, n - 1, desc=True, withscores=True),
            _op("get", f"{ns}stat:{today}:floods"),
        ])

        def ranked(rows: Any) -> list[tuple[int, int]]:
            return [(uid, int(score)) for member, score in rows or () if (uid := _int(member)) > 0 and score >= 1]

        return {"today": ranked(res[0]), "yesterday": ranked(res[1]), "floods_today": max(0, _int(res[2])),
                "day": today, "backend": backend}

    async def status(self, user_id: int) -> dict[str, Any]:
        """Admin view of one account: {'cooldown_until': iso | None, 'cooldown_seconds', 'strikes', 'tier',
        'usage': {group: [minute, hour, day]}} (+ 'cooldown' details and 'limits' for the last-seen tier)."""
        uid = int(user_id)
        now = self.clock()
        ns = self._ns()
        groups = _usage_groups()
        windows = ai_policy.WINDOW_SECONDS
        keys = [f"{ns}q:{uid}:{group}:{win}:{int(now // win)}" for group in groups for win in windows]
        _backend, res = await self._run([
            _op("get", f"{ns}cd:{uid}"),
            _op("zrangebyscore", f"{ns}st:{uid}", now - ai_policy.STRIKE_WINDOW, "+inf", withscores=True),
            _op("get", f"{ns}tier:{uid}"),
            _op("mget", keys),
        ])
        info = _parse_cooldown(res[0])
        active = info is not None and info["until"] > now
        values = [max(0, _int(v)) for v in res[3]]
        n = len(windows)
        tier = _text(res[2]) or None
        out: dict[str, Any] = {
            "cooldown_until": _iso(info["until"]) if active and info else None,
            "cooldown_seconds": math.ceil(info["until"] - now) if active and info else 0,
            "strikes": round(_strike_total(res[1]), 2),
            "tier": tier,
            "usage": {group: values[i * n:(i + 1) * n] for i, group in enumerate(groups)},
        }
        if active and info:
            out["cooldown"] = {key: info.get(key) for key in ("level", "reason", "code", "repeat", "seconds", "feature")}
            out["cooldown"]["started"] = _iso(info["started"])
        if tier in ai_policy.QUOTAS:
            out["limits"] = {g: list(lim) for g in groups if (lim := self._limits(tier, g)) is not None}
        return out

    async def active_cooldowns(self, limit: int = 50) -> list[dict[str, Any]]:
        """Accounts whose AI is paused right now, newest pause first: [{'user_id', 'until', 'seconds_left',
        'level', 'reason', ...}] ('until'/'started' are ISO strings, 'until_dt' an aware datetime)."""
        if limit <= 0:
            return []
        now = self.clock()
        ns = self._ns()
        index_key = f"{ns}cdidx"
        backend, res = await self._run([_op("zremrangebyscore", index_key, "-inf", now),
                                        _op("zrangebyscore", index_key, now, "+inf", withscores=True)])
        uids = [_text(member) or "" for member, _score in res[1]]
        if not uids:
            return []
        raw = await self._run_on(backend, [_op("mget", [f"{ns}cd:{uid}" for uid in uids])])
        if raw is None:
            return []
        rows: list[tuple[float, dict[str, Any]]] = []
        for uid, value in zip(uids, raw[0]):
            info = _parse_cooldown(value)
            if info is None or info["until"] <= now:
                continue
            rows.append((info["started"], {
                "user_id": _int(uid), "until": _iso(info["until"]),
                "until_dt": datetime.fromtimestamp(info["until"], UTC), "seconds_left": math.ceil(info["until"] - now),
                "level": info.get("level"), "reason": info.get("reason"), "code": info.get("code"),
                "repeat": bool(info.get("repeat")), "seconds": info.get("seconds"), "feature": info.get("feature"),
                "strikes": info.get("strikes"), "started": _iso(info["started"]),
            }))
        rows.sort(key=lambda row: (row[0], row[1]["user_id"]), reverse=True)
        return [row for _started, row in rows[:limit]]

    async def lift(self, user_id: int) -> bool:
        """Admin: clear the account's cooldown, strikes, duplicate and dropped-call counters and fingerprints (quota
        usage stays). Returns True when something was actually cleared."""
        uid = int(user_id)
        now = self.clock()
        ns = self._ns()
        flood_window = float(ai_policy.FLOOD_BURST[1])
        keys = [f"{ns}cd:{uid}", f"{ns}st:{uid}", f"{ns}dc:{uid}", f"{ns}cdh:{uid}",
                f"{ns}fl:{uid}:{int(now // flood_window)}", f"{ns}ab:{uid}"]
        ops = [
            _op("get", keys[0]),
            _op("zcount", keys[1], now - ai_policy.STRIKE_WINDOW, "+inf"),
            _op("zcount", keys[2], now - ai_policy.STRIKE_WINDOW, "+inf"),
            _op("delete", *keys),
            _op("zrem", f"{ns}cdidx", str(uid)),
        ]
        stores: list[RedisStore | MemoryStore] = [self._memory]
        redis = await self._redis()
        if redis is not None:
            stores.insert(0, redis)
        cleared = False
        for store in stores:
            try:
                res = await store.run(ops)
                await store.delete_prefix(f"{ns}dd:{uid}:")
            except Exception as exc:  # noqa: BLE001 - Redis trouble: memory is still cleared below
                if store is not redis:
                    raise
                self._redis_failed(exc)
                continue
            info = _parse_cooldown(res[0])
            cleared = cleared or (info is not None and info["until"] > now) or _int(res[1]) > 0 or _int(res[2]) > 0
        log.info("AI guard: restrictions of user %s lifted (anything active: %s)", uid, cleared)
        return cleared

    async def note_injection(self, owner: AIOwner | None, feature: str, *, marker: str | None = None) -> None:
        """A prompt-injection attempt ('ignore all previous instructions' ...): one strike and a log line, never a
        refusal - the model is told to treat user text as data anyway. Never raises."""
        policy = self._policy(owner, feature)
        if policy is None or policy.safety:
            return
        assert owner is not None
        log.warning("AI guard: possible prompt injection by user %s on %r%s", owner.user_id, feature,
                    f" ({marker!r})" if marker else "")
        try:
            await self._strike(owner, feature, "injection", refusal=False)
        except Exception:  # noqa: BLE001 - noting must never break the request it is attached to
            log.exception("AI guard: could not note a prompt injection by user %s", owner.user_id)

    async def note_rejection(self, owner: AIOwner | None, feature: str, code: str) -> dict[str, Any] | None:
        """A refusal decided elsewhere - the AI queue's 'too_many' - earns its strike points and counts towards the
        flood burst like the guard's own refusals. Codes without points ('busy': the system's fault, 'cooldown')
        and safety features are ignored. Returns the account's cooldown if one is now active. Never raises."""
        policy = self._policy(owner, feature)
        if policy is None or policy.safety or ai_policy.STRIKE_POINTS.get(code, 0) <= 0:
            return None
        assert owner is not None
        try:
            return await self._strike(owner, feature, code, refusal=True)
        except Exception:  # noqa: BLE001
            log.exception("AI guard: could not record a %r strike for user %s", code, owner.user_id)
            return None

    async def note_abort(self, owner: AIOwner | None, feature: str) -> dict[str, Any] | None:
        """The requester dropped an AI call after it reached a provider - closed the tab or the sheet, or lost the
        connection, mid-answer. The router keeps that call's charge; dropping answers again and again is an abuse
        signal: beyond ``policy.ABORT_FREE`` drops within STRIKE_WINDOW each one earns STRIKE_POINTS['abort'] (never
        a refusal, never part of a flood burst). Jobs, system work, unguarded and safety features are ignored. Returns
        the account's active cooldown, if any. Never raises."""
        policy = self._policy(owner, feature)
        if policy is None or policy.safety or ai_policy.STRIKE_POINTS.get("abort", 0) <= 0:
            return None
        assert owner is not None
        try:
            return await self._strike(owner, feature, "abort", refusal=False)
        except Exception:  # noqa: BLE001 - bookkeeping of a call that is over anyway
            log.exception("AI guard: could not record an AI call dropped by user %s", owner.user_id)
            return None

    async def refusal(self, owner: AIOwner | None, feature: str, exc: AIRejected) -> AIRejected:
        """For the AI router: a refusal decided after ``admit`` (the AI queue's 'too_many') earns its strikes like
        the guard's own (``note_rejection``). Returns the error to raise - ``exc`` itself, or a 'cooldown' refusal
        when the account is paused by now, so the person sees the real wait. Never raises."""
        cooldown = await self.note_rejection(owner, feature, exc.code)
        now = self.clock()
        if cooldown is not None and cooldown.get("until", 0) > now:
            return self._cooldown_error(cooldown, now, feature)
        return exc

    def forget_entitlement(self, user_id: int | None = None) -> Awaitable[None]:
        """Drop the cached 'plan includes AI' answer (call after a subscription is activated, cancelled or ends)
        so the next request is judged with the new tier. None forgets everyone.

        This process forgets at once. Every other process learns it at its next request of that user: this bumps the
        shared version key ``ip:<env>:<db>:ai:entv:<uid>`` (``entv:all`` for everyone), which ``admit`` reads in the
        round trip it makes anyway. The bump runs in a task of its own, so synchronous callers (``call_after_commit``)
        just call this; awaiting the result waits for the bump. Without Redis the bump stays in this process."""
        key: int | str = "all" if user_id is None else int(user_id)
        if user_id is None:
            self._entitlements.clear()
        else:
            self._entitlements.pop(int(user_id), None)
        try:
            task = asyncio.get_running_loop().create_task(self._bump_entitlement(key), name="ai-guard-entv")
        except RuntimeError:  # no running loop (a script): nobody else to tell
            return _Later(None)
        self._bumps.setdefault(key, set()).add(task)  # also the strong reference that keeps the task alive

        def landed(done: asyncio.Task, key: int | str = key) -> None:
            pending = self._bumps.get(key)
            if pending is not None:
                pending.discard(done)
                if not pending:
                    del self._bumps[key]

        task.add_done_callback(landed)
        return _Later(task)

    def reset(self) -> Awaitable[int]:
        """Tests: clear this process's memory at once; awaiting the result also deletes every Redis key of this
        namespace (``ip:<env>:<db>:ai:``) with SCAN + UNLINK - after any entitlement bump still under way has
        landed, so none of them reappears. Registered with ``app.core.state``."""
        pending = [task for tasks in self._bumps.values() for task in tasks if not task.done()]
        pending += [task for task in self._tasks if not task.done()]  # judges whose caller left, their refunds
        self._bumps.clear()
        self._memory.clear()
        self._entitlements.clear()
        self._entitlement_locks.clear()
        self._redis_store = None
        self._redis_skip_until = 0.0
        return _RedisWipe(self, pending)

    # ================================================================= judging helpers
    def _policy(self, owner: AIOwner | None, feature: str) -> ai_policy.FeaturePolicy | None:
        if owner is None or owner.kind != OWNER_USER or owner.user_id is None:
            return None
        if not get_settings().ai_guard_enabled:
            return None
        policy = ai_policy.policy_for(feature)
        return policy if policy.guarded else None

    @staticmethod
    def _limits(tier: str, group: str) -> tuple[int, ...] | None:
        """Scaled (minute, hour, day) limits, or None when the group is unlimited for the tier ('system')."""
        window = ai_policy.QUOTAS.get(tier, {}).get(group)
        if window is None:
            return None
        scale = float(get_settings().ai_quota_scale)
        return tuple(max(1, round(limit * scale)) for limit in window)

    @staticmethod
    def _quota_message(group: str, window: int, wait: float) -> str:
        noun, verb = _GROUP_WORDING.get(group, ("AI requests", "try again"))
        period = _PERIOD.get(window, f"the last {human_wait(window)}")
        return f"You've used your {noun} for {period}. You can {verb} in {human_wait(wait)}."

    @staticmethod
    def _cooldown_error(info: dict[str, Any], now: float, feature: str) -> AIRejected:
        left = max(1.0, info["until"] - now)
        reason = info.get("reason") or _DEFAULT_COOLDOWN_REASON
        return AIRejected("cooldown", f"AI is paused on your account for {human_wait(left)} because of {reason}.",
                          retry_after=math.ceil(left), feature=feature)

    async def _reject(self, owner: AIOwner, feature: str, code: str, message: str, retry_after: float) -> NoReturn:
        """Book the refusal's strikes, then raise it - as 'cooldown' when the account is paused by now (this refusal
        crossed a ladder level, or a concurrent one did), so the person sees the real wait."""
        log.info("AI guard: refused user %s on %r (%s)", owner.user_id, feature, code)
        cooldown = None
        try:
            cooldown = await self._strike(owner, feature, code, refusal=True, count=True)
        except Exception:  # noqa: BLE001 - bookkeeping trouble must not turn a refusal into a 500
            log.exception("AI guard: could not record a %r strike for user %s", code, owner.user_id)
        now = self.clock()
        if cooldown is not None and cooldown["until"] > now:
            raise self._cooldown_error(cooldown, now, feature)
        raise AIRejected(code, message, retry_after=math.ceil(max(0.0, retry_after)), feature=feature)

    async def _strike(self, owner: AIOwner, feature: str, code: str, *, refusal: bool,
                      count: bool = False) -> dict[str, Any] | None:
        """Add the strike points of ``code`` (a refusal also counts towards the flood burst; duplicates and dropped
        calls only cost points beyond DUPLICATE_FREE / ABORT_FREE) and start a cooldown when the rolling total crosses
        a ladder level. ``count``: the guard refused this request itself - it goes into today's refusal counters
        (``snapshot()``). Returns the account's active cooldown afterwards, or None."""
        uid = int(owner.user_id or 0)
        now = self.clock()
        ns = self._ns()
        flood_count, flood_window = ai_policy.FLOOD_BURST
        ops = [_op("get", f"{ns}cd:{uid}")]
        burst_at = dup_at = -1
        if refusal and flood_count > 0:
            # fixed FLOOD_BURST windows of the clock; INCR is atomic, so exactly one refusal sees each multiple
            burst_key = f"{ns}fl:{uid}:{int(now // flood_window)}"
            ops += [_op("set", burst_key, "0", ex=math.ceil(flood_window) + 1, nx=True), _op("incrby", burst_key, 1)]
            burst_at = len(ops) - 1
        allowance = _free_strikes(code)
        if allowance is not None:  # the first few of these within STRIKE_WINDOW are free: count them
            dup_key = f"{ns}{allowance[0]}:{uid}"
            ops += [_op("zadd", dup_key, {secrets.token_hex(6): now}),
                    _op("zremrangebyscore", dup_key, "-inf", now - ai_policy.STRIKE_WINDOW),
                    _op("zcard", dup_key), _op("expire", dup_key, ai_policy.STRIKE_WINDOW + 60)]
            dup_at = len(ops) - 2
        if count:
            ops += self._count_ops(ns, now, f"refused:{code}") + self._offender_ops(ns, now, uid)  # same round trip
        _backend, res = await self._run(ops)
        current = _parse_cooldown(res[0])
        active = current if current is not None and current["until"] > now else None
        if refusal and active is not None:
            return active  # a concurrent request paused the account already: piling on earns nothing more

        entries: list[tuple[str, float]] = []
        points = float(ai_policy.STRIKE_POINTS.get(code, 0))
        if allowance is not None and dup_at >= 0 and _int(res[dup_at]) <= allowance[1]:
            points = 0.0  # double clicks, impatience and the odd closed tab are free
        if points > 0:
            entries.append((code, points))
        if burst_at >= 0 and _int(res[burst_at]) % flood_count == 0:
            flood = float(ai_policy.STRIKE_POINTS.get("flood", 0))
            if flood > 0:
                entries.append(("flood", flood))
        if not entries:
            return active

        strike_key = f"{ns}st:{uid}"
        members = {f"{pts:g}|{name}|{secrets.token_hex(6)}": now for name, pts in entries}
        _backend, res = await self._run([
            _op("zremrangebyscore", strike_key, "-inf", now - ai_policy.STRIKE_WINDOW),
            _op("zadd", strike_key, members),
            _op("zrangebyscore", strike_key, now - ai_policy.STRIKE_WINDOW, "+inf", withscores=True),
            _op("expire", strike_key, ai_policy.STRIKE_WINDOW + 60),
            *(self._count_ops(ns, now, "floods") if any(name == "flood" for name, _p in entries) else ()),
        ])
        total = _strike_total(res[2])  # atomic with our ZADD: concurrent strikes each see a distinct total
        level = _crossed_level(total - sum(p for _n, p in entries), total)
        if level is None:
            return active
        started = await self._start_cooldown(owner, feature, level=level[0], seconds=level[1], code=entries[0][0],
                                             strikes=total)
        return started or active

    async def _start_cooldown(self, owner: AIOwner, feature: str, *, level: int, seconds: int, code: str,
                              strikes: float) -> dict[str, Any] | None:
        """Pause AI for the account (atomic read-modify-write). Never shortens a longer active pause; a pause that
        starts while none is active is an 'episode', and the REPEAT_COOLDOWN_AFTER-th episode within 24 h lasts
        REPEAT_COOLDOWN_SECONDS. Pauses of ESCALATE_MIN_SECONDS or more are reported once to the security system.
        Returns the new cooldown record, or None when an existing longer pause was kept."""
        uid = int(owner.user_id or 0)
        now = self.clock()
        ns = self._ns()
        cd_key, hist_key, index_key = f"{ns}cd:{uid}", f"{ns}cdh:{uid}", f"{ns}cdidx"
        reads = [_op("get", cd_key), _op("zcount", hist_key, now - DAY, "+inf")]
        reportable = _reportable_ip(owner) is not None
        stat_prefix = f"{ns}stat:"

        def decide(results: list[Any]) -> tuple[list[Op], Any]:
            current = _parse_cooldown(results[0])
            active = current is not None and current["until"] > now
            length = float(seconds)
            repeat = bool(active and current and current.get("repeat"))
            if not active and _int(results[1]) + 1 >= ai_policy.REPEAT_COOLDOWN_AFTER:
                length, repeat = max(length, float(ai_policy.REPEAT_COOLDOWN_SECONDS)), True
            until = now + length
            if active and current and current["until"] >= until:
                return [], None
            severity = _escalation_severity(length)
            reported = _int(current.get("reported")) if active and current else 0
            info = {
                "user_id": uid, "until": until, "started": current["started"] if active and current else now,
                "seconds": int(length), "level": level, "repeat": repeat, "code": code,
                "reason": _COOLDOWN_REASONS.get(code, _DEFAULT_COOLDOWN_REASON), "feature": feature,
                "strikes": round(strikes, 2), "reported": max(reported, severity),
            }
            ops = [
                _op("set", cd_key, json.dumps(info, separators=(",", ":")), px=int((length + COOLDOWN_GRACE) * 1000)),
                _op("zadd", index_key, {str(uid): until}),
                _op("zremrangebyscore", index_key, "-inf", now),
            ]
            if not active:
                ops += [_op("zadd", hist_key, {secrets.token_hex(6): now}),
                        _op("zremrangebyscore", hist_key, "-inf", now - DAY),
                        _op("expire", hist_key, DAY + 60)]
                ops += self._count_ops(ns, now, "cooldowns")  # a new pause (an extension is not one)
            if severity > reported and reportable:
                ops += self._count_ops(ns, now, "escalations")
            return ops, (info, severity > reported)

        backend, ops, value = await self._transact([cd_key, hist_key], reads, decide)
        if value is None:
            return None
        info, report = value
        if backend == "redis":  # mirror, so this process still knows about the pause if Redis goes away
            with contextlib.suppress(Exception):
                self._memory.run_now([op for op in ops if not str(op[1][0]).startswith(stat_prefix)])
        log.warning("AI guard: AI paused for user %s for %s (level %s%s, %s strike points): %s", uid,
                    human_wait(info["seconds"]), level, ", repeat" if info["repeat"] else "", info["strikes"],
                    info["reason"])
        if report:
            self._escalate(owner, feature, info)
        return info

    def _escalate(self, owner: AIOwner, feature: str, info: dict[str, Any]) -> None:
        """Report a long pause to the IP threat system (docs section 3, 'Escalation'): an AI_ABUSE event, threat
        points for the IP and - only if the score reaches threat_ban_score - a ban request. Gym Wi-Fi NAT means only
        sustained abuse may reach the IP level; whitelisted IPs are skipped entirely."""
        ip = _reportable_ip(owner)
        if ip is None:
            return
        severity = max(1, _int(info.get("reported")))
        try:
            from app.models.enums import ThreatCategory
            from app.security.bans import security_events, threat_tracker

            detail = (f"AI paused {human_wait(info['seconds'])} for user #{info['user_id']} after "
                      f"{info['strikes']:g} strike points in {ai_policy.STRIKE_WINDOW // 60} min ({info['reason']})")
            security_events.record(ip, ThreatCategory.AI_ABUSE, severity, detail, method="AI", path=f"/ai/{feature}",
                                   user_agent=None, action="blocked", session_hash=owner.session_hash)
            # the background update bans on the SHARED total through the claim (one ip_bans row whichever worker
            # reports; never from a copy another worker's Lift left stale) - app/security/bans.py ThreatTracker.add
            score = threat_tracker.add(ip, severity, reason=f"{ThreatCategory.AI_ABUSE.value}: {detail}",
                                       session_hash=owner.session_hash)
            log.warning("AI guard: AI abuse of user %s reported for IP %s (severity %s, threat score %.1f)",
                        info["user_id"], ip, severity, score)
        except Exception:  # noqa: BLE001 - reporting must never break the refusal itself
            log.exception("AI guard: could not report AI abuse of user %s", info.get("user_id"))

    @staticmethod
    def _uncharge_ops(charges: Iterable[tuple[str, int, float]], now: float) -> tuple[list[tuple[str, int]], list[Op]]:
        """Commands giving ``units`` back to every still-open window counter. Each starts with SET NX EX: a counter
        that was cleared meanwhile (reset, expiry) comes back with its TTL instead of as a negative value that
        never expires. Their INCRBY results sit at the odd positions."""
        live: list[tuple[str, int]] = []
        ops: list[Op] = []
        for key, units, end in charges:
            if units > 0 and end > now:
                live.append((key, units))
                ops += [_op("set", key, "0", ex=max(1, math.ceil(end - now) + 1), nx=True), _op("incrby", key, -units)]
        return live, ops

    async def _fix_negative(self, backend: str, live: list[tuple[str, int]], results: list[Any]) -> None:
        """A refund that met a cleared counter must not leave free quota behind: lift negatives back to zero (an
        INCRBY, so a concurrent charge in between is kept)."""
        values = results[1:2 * len(live):2]
        fixes = [_op("incrby", key, -_int(value)) for (key, _units), value in zip(live, values) if _int(value) < 0]
        if fixes:
            await self._run_on(backend, fixes)

    @staticmethod
    def _count_ops(ns: str, now: float, name: str) -> list[Op]:
        """+1 on one of today's (UTC) counters of ``snapshot()`` - created with its TTL, so it never lives forever."""
        key = f"{ns}stat:{_utc_day(now)}:{name}"
        return [_op("set", key, "0", ex=STATS_TTL, nx=True), _op("incrby", key, 1)]

    @staticmethod
    def _offender_ops(ns: str, now: float, uid: int) -> list[Op]:
        """+1 refusal of ``uid`` in today's (UTC) ranking of the most refused accounts (``offenders()``)."""
        key = f"{ns}stat:{_utc_day(now)}:offenders"
        return [_op("zincrby", key, 1, str(uid)), _op("expire", key, STATS_TTL)]

    async def _rollback(self, backend: str, charges: list[tuple[str, int, float]], dedupe_key: str | None = None,
                        *, also: list[Op] | None = None) -> None:
        """Undo the charges of a refused request (and a fingerprint it claimed itself) on the store that took them,
        plus ``also`` (bookkeeping of the refusal) in the same round trip. If that store is unreachable the charge
        stays - failing closed, never open."""
        live, ops = self._uncharge_ops(charges, self.clock())
        if dedupe_key:
            ops.append(_op("delete", dedupe_key))
        ops += also or []
        if not ops:
            return
        try:
            res = await self._run_on(backend, ops)
            if res is not None:
                await self._fix_negative(backend, live, res)
        except Exception:  # noqa: BLE001
            log.exception("AI guard: rollback failed on %s", backend)

    async def _refund(self, admission: Admission) -> None:
        try:
            live, ops = self._uncharge_ops(admission.charges, self.clock())
            check = bool(admission.dedupe_key and admission.dedupe_token)
            if check:
                ops.append(_op("get", admission.dedupe_key))
            if not ops:
                return
            res = await self._run_on(admission.backend, ops)
            if res is None:
                log.info("AI guard: refund for user %s skipped (%s unavailable)", admission.user_id, admission.backend)
                return
            await self._fix_negative(admission.backend, live, res)
            if check and _text(res[-1]) == admission.dedupe_token:
                await self._run_on(admission.backend, [_op("delete", admission.dedupe_key)])
        except Exception:  # noqa: BLE001 - settle runs in finally blocks and must never raise
            log.exception("AI guard: refund failed for user %s", admission.user_id)

    async def _settle_claimed(self, admission: Admission, ok: bool) -> bool:
        """Settle an exported admission exactly once over all its copies: SET NX of ``stl:<ref>`` in the store that
        holds the charge decides who settles; only that one refunds (``ok=False``). Returns True when this call
        settled it. An in-memory charge taken by another process cannot be reached from here: nothing to do."""
        try:
            if admission.backend == MemoryStore.name and admission.origin != _process_id():
                log.info("AI guard: the %s charge of user %s was taken in another process's memory - not refundable "
                         "here", admission.feature, admission.user_id)
                return False
            now = self.clock()
            last_end = max((end for _key, _units, end in admission.charges), default=now)
            ttl = max(60, min(DAY + 60, math.ceil(last_end - now) + 60))
            res = await self._run_on(admission.backend,
                                     [_op("set", f"{self._ns()}stl:{admission.ref}", "1" if ok else "0", ex=ttl,
                                          nx=True)])
            if res is None:  # the store holding the charge is unreachable: nothing can be given back now
                log.info("AI guard: settle of user %s skipped (%s unavailable)", admission.user_id, admission.backend)
                return False
            if not res[0]:
                return False  # settled already (another copy, a retry of the job, the original admission)
            if not ok:
                await self._refund(admission)
            return True
        except Exception:  # noqa: BLE001
            log.exception("AI guard: settle failed for user %s", admission.user_id)
            return False

    def _owns_keys(self, admission: Admission) -> bool:
        """A payload may only touch this namespace's counters and fingerprints of its own user."""
        ns = self._ns()
        quota = f"{ns}q:{admission.user_id}:"
        if any(not key.startswith(quota) for key, _units, _end in admission.charges):
            return False
        return admission.dedupe_key is None or admission.dedupe_key.startswith(f"{ns}dd:{admission.user_id}:")

    # ================================================================= tiers
    async def _tier(self, owner: AIOwner) -> tuple[str, _Entitlement | None]:
        """(tier, the cached entitlement it came from - None for role tiers, which need no lookup)."""
        role = str(owner.role or "").lower()
        if role in ("staff", "owner", "superadmin"):
            return role, None
        if role == "customer" and owner.user_id is not None:
            entitlement = await self._entitlement(int(owner.user_id))
            return ("member_ai" if entitlement.has_ai else "member"), entitlement
        return "member", None

    async def _retier(self, owner: AIOwner, stale: _Entitlement, backend: str, tier: str) -> str:
        """``admit`` read a newer entitlement version than the cached answer was looked up at (a subscription change
        committed by another process): look it up again and record the tier the request is now judged with."""
        uid = int(owner.user_id or 0)
        if self._entitlements.get(uid) is stale:
            del self._entitlements[uid]
        fresh, _entry = await self._tier(owner)
        if fresh != tier:
            log.info("AI guard: AI tier of user %s changed elsewhere: %s -> %s", uid, tier, fresh)
            with contextlib.suppress(Exception):
                await self._run_on(backend, [_op("set", f"{self._ns()}tier:{uid}", fresh, ex=TIER_MEMORY_SECONDS)])
        return fresh

    async def _has_ai(self, user_id: int) -> bool:
        """Does the customer have an ACTIVE subscription whose plan includes AI? (cached, see ``_entitlement``)"""
        return (await self._entitlement(user_id)).has_ai

    def _entitlement_keys(self, user_id: int) -> list[str]:
        ns = self._ns()
        return [f"{ns}entv:{user_id}", f"{ns}entv:all"]

    async def _entitlement(self, user_id: int) -> _Entitlement:
        """The cached answer for one customer: one cheap query in its own short session, cached per user for
        ENTITLEMENT_TTL; concurrent misses for one user share a single lookup. The shared version is read before the
        query (after this process's own pending bumps have landed), so a change committed while the query runs shows
        up as a newer version at the next ``admit`` - never as a stale answer filed under the new version."""
        hit = self._entitlements.get(user_id)
        if hit is not None and hit.expires > time.monotonic():
            return hit
        lock = self._entitlement_locks.get(user_id)
        if lock is None:
            if len(self._entitlement_locks) > 10_000:
                for key in [k for k, lk in self._entitlement_locks.items() if not lk.locked()]:
                    del self._entitlement_locks[key]
            lock = self._entitlement_locks[user_id] = asyncio.Lock()
        async with lock:
            hit = self._entitlements.get(user_id)
            if hit is not None and hit.expires > time.monotonic():
                return hit
            version = await self._read_entitlement_version(user_id)
            try:
                value = await asyncio.wait_for(self._lookup_entitlement(user_id), timeout=ENTITLEMENT_TIMEOUT)
                ttl = ENTITLEMENT_TTL
            except Exception as exc:  # noqa: BLE001 - the stricter tier for a while, never an error page
                log.warning("AI guard: AI entitlement lookup for user %s failed (%s) - using the member tier",
                            user_id, exc.__class__.__name__)
                value, ttl = False, ENTITLEMENT_RETRY
            if len(self._entitlements) > 50_000:
                now = time.monotonic()
                self._entitlements = {k: v for k, v in self._entitlements.items() if v.expires > now}
            entry = self._entitlements[user_id] = _Entitlement(value, time.monotonic() + ttl, version)
            return entry

    async def _read_entitlement_version(self, user_id: int) -> tuple[str | None, ...] | None:
        """The shared entitlement version right now (None if even the memory store failed: never matches)."""
        pending = [task for key in (user_id, "all") for task in self._bumps.get(key, ()) if not task.done()]
        if pending:  # this process's own forget_entitlement: let its bumps land first
            with contextlib.suppress(Exception):
                await asyncio.wait(pending, timeout=ENTITLEMENT_TIMEOUT)
        try:
            _backend, res = await self._run([_op("mget", self._entitlement_keys(user_id))])
        except Exception:  # noqa: BLE001
            return None
        return _version(res[0])

    async def _bump_entitlement(self, key: int | str) -> None:
        """INCR the shared entitlement version of one user (or 'all') - a key that starts from a random base and
        expires after ENTITLEMENT_VERSION_TTL. Never raises."""
        name = f"{self._ns()}entv:{key}"
        try:
            await self._run([
                _op("set", name, str(secrets.randbelow(1 << 48) + 1), ex=ENTITLEMENT_VERSION_TTL, nx=True),
                _op("incrby", name, 1),
                _op("expire", name, ENTITLEMENT_VERSION_TTL),
            ])
        except Exception:  # noqa: BLE001 - other processes then see the change within ENTITLEMENT_TTL
            log.exception("AI guard: could not publish the entitlement change of %s", key)

    async def _lookup_entitlement(self, user_id: int) -> bool:
        from sqlalchemy import exists, or_, select

        from app.db import get_sessionmaker
        from app.models import Plan, Subscription
        from app.models.enums import SubscriptionStatus

        today = date.today()  # same rule as app.services.subscriptions.active_subscriptions
        stmt = select(exists().where(
            Subscription.customer_id == user_id,
            Subscription.status == SubscriptionStatus.ACTIVE,
            or_(Subscription.ends_on.is_(None), Subscription.ends_on >= today),
            Plan.id == Subscription.plan_id,
            Plan.includes_ai.is_(True),
        ))
        async with get_sessionmaker()() as session:
            return bool(await session.scalar(stmt))

    # ================================================================= storage plumbing
    def _ns(self) -> str:
        """Key prefix ``ip:<env>:<db-name>:ai:`` - test databases and both app copies never share counters."""
        settings = get_settings()
        marker = (settings.environment, settings.database_url)
        if self._ns_cache is None or self._ns_cache[0] != marker:
            db_name = settings.database_url.rsplit("/", 1)[-1].split("?", 1)[0]
            env = re.sub(r"[^A-Za-z0-9_.-]", "_", settings.environment) or "default"
            db = re.sub(r"[^A-Za-z0-9_.-]", "_", db_name) or "default"
            self._ns_cache = (marker, f"ip:{env}:{db}:ai:")
        return self._ns_cache[1]

    async def _redis(self) -> RedisStore | None:
        if time.monotonic() < self._redis_skip_until:
            return None
        try:
            client = await _redis_client()  # inside the try: a bad REDIS_URL or a missing package means "no Redis"
        except Exception as exc:  # noqa: BLE001
            self._redis_failed(exc)
            return None
        if client is None:
            return None
        store = self._redis_store
        if store is None or store.client is not client:
            store = self._redis_store = RedisStore(client)
        return store

    def _redis_failed(self, exc: BaseException) -> None:
        """Use memory for a moment and say so at most once a minute. Deliberately does NOT mark the session store's
        Redis as down (app.security.sessions._mark_down): a guard hiccup must not move logins to PostgreSQL."""
        now = time.monotonic()
        self._redis_skip_until = now + self.redis_retry_seconds
        if now - self._redis_logged_at >= 60:
            self._redis_logged_at = now
            log.warning("AI guard: Redis unavailable (%s) - enforcing AI limits in this process's memory",
                        exc.__class__.__name__)

    async def _run(self, ops: list[Op]) -> tuple[str, list[Any]]:
        store = await self._redis()
        if store is not None:
            try:
                return store.name, await store.run(ops)
            except Exception as exc:  # noqa: BLE001 - fall back to memory for this operation
                self._redis_failed(exc)
        return self._memory.name, await self._memory.run(ops)

    async def _run_on(self, backend: str, ops: list[Op]) -> list[Any] | None:
        """Run on one specific store (undoing a charge where it was taken); None when it is unreachable."""
        if backend != RedisStore.name:
            return await self._memory.run(ops)
        store = await self._redis()
        if store is None:
            return None
        try:
            return await store.run(ops)
        except Exception as exc:  # noqa: BLE001
            self._redis_failed(exc)
            return None

    async def _transact(self, watch: list[str], reads: list[Op],
                        decide: Callable[[list[Any]], tuple[list[Op], Any]]) -> tuple[str, list[Op], Any]:
        store = await self._redis()
        if store is not None:
            try:
                ops, value = await store.transact(watch, reads, decide)
                return store.name, ops, value
            except Exception as exc:  # noqa: BLE001
                self._redis_failed(exc)
        ops, value = await self._memory.transact(watch, reads, decide)
        return self._memory.name, ops, value

    async def _wipe_redis(self, pending: list[asyncio.Task] | None = None) -> int:
        if pending:  # bumps, judges and refunds still under way would recreate their keys after the wipe
            with contextlib.suppress(Exception):
                await asyncio.wait(pending, timeout=ENTITLEMENT_TIMEOUT)
            self._memory.clear()  # ... or their counters in memory
        try:
            client = await _redis_client()
            if client is None:
                return 0
            return await RedisStore(client).delete_prefix(self._ns())
        except Exception as exc:  # noqa: BLE001 - a reset must not fail because Redis is away
            log.warning("AI guard: could not clear the Redis namespace (%s)", exc.__class__.__name__)
            return 0


guard = AIGuard()
register_reset(guard.reset, name="app.ai.guard")
