"""WP-K text-to-speech: Kokoro via the OpenAI-compatible speech endpoint (injected fake client - never the network),
LRU cache, circuit breaker / browser fallback, validation and login requirement.

Plus the AI guard and the AI queue (docs/ai-guard-and-queue.md): a cache miss is judged by the per-user guard (voice
quota, AI pause -> 429 + Retry-After) while cache hits stay free; every synthesis holds a ticket in the queue's 'tts'
lane; concurrent requests of one member for one phrase share ONE synthesis that survives any single requester leaving
(its request cancelled, its page closed) and stops only when every waiter has gone. Members never share a cached line
or a synthesis (tests/test_ai_hardening_features.py)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

import pytest

from app.ai import policy
from app.ai.errors import AIRejected
from app.ai.guard import guard
from app.ai.owner import AIOwner, current_owner, owner_scope
from app.ai.scheduler import scheduler
from app.config import get_settings
from app.services import tts as tts_mod
from app.services.tts import tts
from tests.conftest import api, login_as
from tests.factories import make_customer, make_owner

MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x64" * 64
PAGE = "tts-page-0001-abcdef"


class FakeSpeech:
    """Mimics AsyncOpenAI(...).audio.speech.create(...) returning a binary response.

    ``hold(text)`` makes the synthesis of ``text`` wait until ``release(text)``; ``cancelled`` lists the phrases whose
    synthesis was cancelled mid-way; ``owners`` / ``lanes`` record the AI owner and the 'tts' lane each synthesis saw."""

    def __init__(self, fail: bool = False, payload: bytes = MP3) -> None:
        self.calls: list[dict] = []
        self.cancelled: list[str] = []
        self.owners: list[AIOwner | None] = []
        self.lanes: list[dict[str, Any]] = []
        self.fail = fail
        self.payload = payload
        self.gates: dict[str, asyncio.Event] = {}
        outer = self

        class _Speech:
            async def create(self, **kwargs):
                outer.calls.append(kwargs)
                outer.owners.append(current_owner())
                outer.lanes.append(dict(scheduler.snapshot()["lanes"]["tts"]))
                gate = outer.gates.get(kwargs["input"])
                try:
                    if gate is not None:
                        await gate.wait()
                except asyncio.CancelledError:
                    outer.cancelled.append(kwargs["input"])
                    raise
                if outer.fail:
                    raise ConnectionError("kokoro container is not running")
                return _Response(outer.payload)

        class _Audio:
            speech = _Speech()

        self.audio = _Audio()

    def hold(self, *texts: str) -> None:
        for text in texts:
            self.gates[text] = asyncio.Event()

    def release(self, *texts: str) -> None:
        for text in texts or tuple(self.gates):
            self.gates[text].set()

    def started(self, text: str) -> int:
        return sum(1 for c in self.calls if c["input"] == text)


class _Response:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def aread(self) -> bytes:
        return self._data


@pytest.fixture
def speech():
    installed: list[FakeSpeech] = []

    def install(**kwargs) -> FakeSpeech:
        fake = FakeSpeech(**kwargs)
        tts.use_client(fake)
        installed.append(fake)
        return fake

    yield install
    for fake in installed:
        fake.release()  # nothing may stay blocked into the next test
    tts.use_client(None)


@pytest.fixture(autouse=True)
def _guard_on(monkeypatch):
    """The AI guard as configured by default, whatever the local .env says."""
    monkeypatch.setattr(get_settings(), "ai_guard_enabled", True)
    monkeypatch.setattr(get_settings(), "ai_quota_scale", 1.0)


async def wait_until(predicate: Callable[[], Any], *, timeout: float = 5.0, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.01)


def tts_lane() -> dict[str, Any]:
    return scheduler.snapshot()["lanes"]["tts"]


def page_owner(n: int, user_id: int = 900) -> AIOwner:
    """A signed-in member's browser tab (user ids >= 900 never exist: the guard judges them as plain members)."""
    return AIOwner(user_id=user_id + n, role="customer", session_hash=f"session-{n}", page_id=f"tts-page-{n:04d}-xyz")


def member_tab(n: int, user_id: int = 950) -> AIOwner:
    """Tab ``n`` of ONE member (members never share a synthesis or a cached line - their own tabs do)."""
    return AIOwner(user_id=user_id, role="customer", session_hash=f"session-{user_id}", page_id=f"tts-tab-{n:04d}-xyz")


async def say_as(owner: AIOwner | None, text: str) -> bytes | None:
    with owner_scope(owner):
        return await tts.synthesize(text)


def voice_minute_quota(tier: str = "member") -> int:
    return max(1, round(policy.QUOTAS[tier]["voice"][0] * float(get_settings().ai_quota_scale)))


# ============================================================================================ basics (unchanged)
async def test_tts_requires_login(client):
    r = await api(client, "POST", "/api/tts", json={"text": "Hello"})
    assert r.status_code == 401 and r.headers["content-type"].startswith("application/json")
    r = await client.get("/api/tts/status", headers={"Accept": "application/json"})
    assert r.status_code == 401


async def test_tts_success_returns_cacheable_mp3(client, db, speech):
    fake = speech()
    user = await make_customer(db)
    await login_as(client, user)
    r = await api(client, "POST", "/api/tts", json={"text": "  Nice work!\n Rest 45 seconds.  "})
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/mpeg"
    assert r.headers["cache-control"] == "private, max-age=3600"
    assert r.content == MP3
    call = fake.calls[0]
    assert call == {"model": "kokoro", "voice": "af_heart", "input": "Nice work! Rest 45 seconds.", "response_format": "mp3",
                    "speed": 0.95}


async def test_tts_cache_hit_and_voice_key(client, db, speech):
    fake = speech()
    user = await make_owner(db)  # any signed-in user may use the voice
    await login_as(client, user)
    for _ in range(3):
        assert (await api(client, "POST", "/api/tts", json={"text": "Set two of three. Go!"})).status_code == 200
    assert len(fake.calls) == 1 and tts.stats.hits == 2
    assert (await api(client, "POST", "/api/tts", json={"text": "Set two of three. Go!", "voice": "bf_emma"})).status_code == 200
    assert len(fake.calls) == 2 and fake.calls[1]["voice"] == "bf_emma"


async def test_tts_down_returns_503_browser_fallback_and_opens_circuit(client, db, speech):
    fake = speech(fail=True)
    user = await make_customer(db)
    await login_as(client, user)
    r = await api(client, "POST", "/api/tts", json={"text": "Hello there"})
    assert r.status_code == 503 and r.json()["fallback"] == "browser"
    r = await api(client, "POST", "/api/tts", json={"text": "Another phrase"})
    assert r.status_code == 503
    assert len(fake.calls) == 1  # circuit open: no second attempt for 60 s
    status = (await client.get("/api/tts/status")).json()
    assert status["state"] == "down" and status["available"] is False and status["retry_in_s"] > 0
    await wait_until(lambda: not tts_mod._background, what="the refund")
    usage = (await guard.status(user.id))["usage"]["voice"]
    assert usage[0] == 0  # an engine failure is refunded; the circuit-open request never reached the guard


async def test_tts_validation(client, db, speech):
    speech()
    user = await make_customer(db)
    await login_as(client, user)
    assert (await api(client, "POST", "/api/tts", json={"text": "x" * 401})).status_code == 422
    assert (await api(client, "POST", "/api/tts", json={"text": ""})).status_code == 422
    assert (await api(client, "POST", "/api/tts", json={"text": "   "})).status_code == 422
    assert (await api(client, "POST", "/api/tts", json={"text": "hi", "voice": "../../etc"})).status_code == 422
    assert (await api(client, "POST", "/api/tts", json={"text": "x" * 400})).status_code == 200


async def test_tts_never_touches_the_network_in_tests(client, db):
    user = await make_customer(db)
    await login_as(client, user)
    assert tts.configured is False
    r = await api(client, "POST", "/api/tts", json={"text": "Hello"})
    assert r.status_code == 503 and r.json()["fallback"] == "browser"
    assert (await client.get("/api/tts/status")).json()["state"] == "not_configured"


async def test_tts_status_probe_marks_engine_up(client, db, speech):
    speech()
    user = await make_customer(db)
    await login_as(client, user)
    assert (await client.get("/api/tts/status")).json()["state"] == "unknown"
    s = (await client.get("/api/tts/status?probe=1")).json()
    assert s["state"] == "up" and s["engine"] == "kokoro" and s["voice"] == "af_heart"


async def test_tts_service_lru_eviction_and_bad_responses(speech, monkeypatch):
    fake = speech()
    monkeypatch.setattr(tts_mod, "CACHE_SIZE", 2)
    for text in ("one", "two", "three"):
        assert await tts.synthesize(text) == MP3
    assert await tts.synthesize("three") == MP3 and len(fake.calls) == 3  # cache hit
    assert await tts.synthesize("one") == MP3 and len(fake.calls) == 4  # evicted -> synthesised again
    assert tts.status(admin=True)["cached"] == 2 and "cached" not in tts.status()  # members never see the cache
    empty = speech(payload=b"")
    assert await tts.synthesize("empty audio") is None  # never raises; trips the breaker instead
    assert tts.is_down and len(empty.calls) == 1
    assert await tts.synthesize("") is None


async def test_concurrent_identical_phrases_share_one_synthesis(speech):
    fake = speech()
    results = await asyncio.gather(*(tts.synthesize("Rest 30 seconds") for _ in range(5)))
    assert all(r == MP3 for r in results) and len(fake.calls) == 1


# ============================================================================================ shared syntheses
async def test_cancelling_one_of_five_waiters_keeps_the_audio_for_the_other_four(speech):
    """The old in-flight dedupe handed None to every waiter when the request that started the synthesis was cancelled
    (and cached nothing). Now the synthesis is a task of its own with a waiter count."""
    fake = speech()
    fake.hold("Rest 30 seconds")
    tasks = [asyncio.create_task(tts.synthesize("Rest 30 seconds")) for _ in range(5)]
    await wait_until(lambda: fake.started("Rest 30 seconds") == 1, what="the synthesis to start")
    tasks[0].cancel()  # the request that started it goes away
    await asyncio.sleep(0.05)
    assert tasks[0].cancelled() and not fake.cancelled  # ... but the synthesis carries on for the other four
    fake.release("Rest 30 seconds")
    assert await asyncio.gather(*tasks[1:]) == [MP3] * 4
    assert len(fake.calls) == 1 and tts.stats.joined == 4
    assert await tts.synthesize("Rest 30 seconds") == MP3 and len(fake.calls) == 1  # and it was cached
    assert tts_lane()["running"] == 0 and not tts._inflight


async def test_the_synthesis_stops_once_every_waiter_has_gone(speech):
    fake = speech()
    fake.hold("Ten seconds left!")
    tasks = [asyncio.create_task(tts.synthesize("Ten seconds left!")) for _ in range(3)]
    await wait_until(lambda: fake.started("Ten seconds left!") == 1, what="the synthesis to start")
    tasks[2].cancel()
    tasks[0].cancel()
    await asyncio.sleep(0.05)
    assert not fake.cancelled  # one waiter is still there
    tasks[1].cancel()
    await wait_until(lambda: fake.cancelled == ["Ten seconds left!"], what="Kokoro call cancelled")
    assert all(t.cancelled() for t in tasks)
    await wait_until(lambda: not tts._inflight and tts_lane()["running"] == 0, what="ticket released")
    assert tts.stats.failures == 0 and not tts.is_down  # stopping is not an engine failure
    counters = scheduler.snapshot()["counters"]
    assert counters["completed"] == 0 and sum(counters["cancelled"].values()) == 1
    fake.release("Ten seconds left!")
    assert await tts.synthesize("Ten seconds left!") == MP3 and len(fake.calls) == 2  # nothing stale was cached


async def test_a_closed_page_drops_only_its_own_share_of_a_running_synthesis(speech):
    fake = speech()
    fake.hold("Halfway there - keep it smooth.")
    owners = [member_tab(n) for n in range(5)]  # five tabs of one member (members never share a synthesis)
    tasks = [asyncio.create_task(say_as(owners[0], "Halfway there - keep it smooth."))]
    await wait_until(lambda: fake.started("Halfway there - keep it smooth.") == 1, what="the synthesis to start")
    tasks += [asyncio.create_task(say_as(o, "Halfway there - keep it smooth.")) for o in owners[1:]]
    await wait_until(lambda: tts.stats.joined == 4, what="four more tabs to join")
    assert fake.owners[0] is owners[0] and tts_lane()["running"] == 1  # the ticket of the request that started it
    # the first tab closes (presence: close code 4001) - the queue stops its ticket ...
    assert scheduler.cancel(page_id=owners[0].page_id, user_id=owners[0].user_id, kinds={"user"},
                            reason="page_closed") == 1
    await asyncio.sleep(0.05)
    assert not fake.cancelled  # ... but four other tabs still want the line
    fake.release("Halfway there - keep it smooth.")
    assert await asyncio.gather(*tasks) == [MP3] * 5  # the closed tab's request gets it too - nobody reads it
    assert len(fake.calls) == 1 and tts_lane()["running"] == 0


async def test_a_closed_page_while_queued_hands_the_ticket_to_the_next_waiter(speech, monkeypatch):
    monkeypatch.setattr(get_settings(), "ai_tts_concurrency", 1)
    fake = speech()
    fake.hold("Busy phrase")
    busy = asyncio.create_task(say_as(page_owner(9), "Busy phrase"))
    await wait_until(lambda: fake.started("Busy phrase") == 1, what="the lane to fill")
    owners = [member_tab(n) for n in range(3)]  # three tabs of one member
    tasks = [asyncio.create_task(say_as(owners[0], "Get ready for the plank."))]
    await wait_until(lambda: tts_lane()["queued"] == 1, what="the first tab's ticket to wait for a slot")
    tasks += [asyncio.create_task(say_as(o, "Get ready for the plank.")) for o in owners[1:]]
    await wait_until(lambda: tts.stats.joined == 2, what="two more tabs to join")
    # the tab that started the synthesis closes while the phrase still waits for a slot
    assert scheduler.cancel(page_id=owners[0].page_id, user_id=owners[0].user_id, kinds={"user"},
                            reason="page_closed") == 1
    await wait_until(lambda: tts_lane()["queued"] == 1 and scheduler.snapshot()["counters"]["submitted"] == 3,
                     what="the next waiter's ticket")
    fake.release("Busy phrase")
    assert await busy == MP3
    assert await asyncio.gather(*tasks) == [MP3] * 3
    assert fake.started("Get ready for the plank.") == 1
    assert fake.owners[-1] is owners[1]  # synthesised on the second tab's ticket


async def test_every_page_closed_while_queued_cancels_the_synthesis(speech, monkeypatch):
    monkeypatch.setattr(get_settings(), "ai_tts_concurrency", 1)
    fake = speech()
    fake.hold("Busy phrase")
    busy = asyncio.create_task(say_as(page_owner(9), "Busy phrase"))
    await wait_until(lambda: fake.started("Busy phrase") == 1, what="the lane to fill")
    owner = page_owner(1)
    tasks = [asyncio.create_task(say_as(owner, "Last set!"))]
    await wait_until(lambda: tts_lane()["queued"] == 1, what="queued ticket")
    tasks.append(asyncio.create_task(say_as(owner, "Last set!")))  # the same tab asks again (a second utterance)
    await wait_until(lambda: tts.stats.joined == 1, what="the repeat to join")
    scheduler.cancel(page_id=owner.page_id, user_id=owner.user_id, kinds={"user"}, reason="page_closed")
    assert await asyncio.gather(*tasks) == [None, None]  # browser voice; nothing was synthesised
    fake.release("Busy phrase")
    assert await busy == MP3
    assert fake.started("Last set!") == 0
    await wait_until(lambda: not tts_mod._background, what="the refunds")
    assert (await guard.status(owner.user_id))["usage"]["voice"][0] == 0  # nothing synthesised: nothing charged


async def refused_or_audio(owner: AIOwner, text: str) -> Any:
    with owner_scope(owner):
        try:
            return await tts.synthesize_or_refuse(text)
        except AIRejected as exc:
            return exc.code


async def test_a_member_never_inherits_another_members_refusal(speech, monkeypatch):
    """The queue refuses a member's line because *that member* has too many voice requests in flight: they get the
    refusal. Another member asking for the same line meanwhile has a synthesis of their own (members never share one)
    and gets the audio - never someone else's 429."""
    monkeypatch.setattr(get_settings(), "ai_queue_max_per_user", 1)
    fake = speech()
    fake.hold("Breathe out on the way up.")
    flooder, other = page_owner(1), page_owner(2)
    first = asyncio.create_task(say_as(flooder, "Breathe out on the way up."))
    await wait_until(lambda: fake.started("Breathe out on the way up.") == 1, what="the flooder's first line")

    real_acquire = scheduler.acquire
    door = asyncio.Event()

    async def acquire(owner: AIOwner | None, feature: str, **kwargs: Any) -> Any:
        if owner is flooder:  # the flooder's second line waits at the queue's door until the other member has asked
            await door.wait()
        return await real_acquire(owner, feature, **kwargs)

    monkeypatch.setattr(scheduler, "acquire", acquire)
    started = asyncio.create_task(refused_or_audio(flooder, "Keep your core tight."))
    await wait_until(lambda: len(tts._inflight) == 2, what="the flooder's second line to be under way")
    other_line = asyncio.create_task(refused_or_audio(other, "Keep your core tight."))
    await wait_until(lambda: other_line.done() or tts.stats.joined == 1, what="the other member's request")
    door.set()
    assert await started == "too_many"
    assert await other_line == MP3  # their own synthesis (it never waited for the flooder's)
    assert fake.started("Keep your core tight.") == 1 and tts.stats.joined == 0
    assert (await guard.status(flooder.user_id))["strikes"] == policy.STRIKE_POINTS["too_many"]
    assert (await guard.status(other.user_id))["strikes"] == 0
    fake.release("Breathe out on the way up.")
    assert await first == MP3


async def test_a_refusal_reaches_every_tab_of_the_member_it_belongs_to(speech, monkeypatch):
    """Two tabs of one member share a line; the queue refuses it because that member already has too many voice
    requests in flight - both requests are theirs, so both learn to back off (none is silently given the browser
    voice for 60 s, and nobody else ever sees the refusal)."""
    monkeypatch.setattr(get_settings(), "ai_queue_max_per_user", 1)
    fake = speech()
    fake.hold("Breathe out on the way up.")
    tab_a, tab_b = member_tab(1), member_tab(2)
    first = asyncio.create_task(say_as(tab_a, "Breathe out on the way up."))
    await wait_until(lambda: fake.started("Breathe out on the way up.") == 1, what="the member's first line")

    real_acquire = scheduler.acquire
    asked: list[AIOwner | None] = []

    async def acquire(owner: AIOwner | None, feature: str, **kwargs: Any) -> Any:
        asked.append(owner)
        await wait_until(lambda: tts.stats.joined == 1, what="the second tab to join")
        return await real_acquire(owner, feature, **kwargs)

    monkeypatch.setattr(scheduler, "acquire", acquire)
    started = asyncio.create_task(refused_or_audio(tab_a, "Keep your core tight."))
    await wait_until(lambda: len(asked) == 1, what="the line to reach the queue")
    joined = asyncio.create_task(refused_or_audio(tab_b, "Keep your core tight."))
    assert await asyncio.gather(started, joined) == ["too_many", "too_many"]
    assert asked == [tab_a] and fake.started("Keep your core tight.") == 0
    assert (await guard.status(tab_a.user_id))["strikes"] == policy.STRIKE_POINTS["too_many"]  # one refusal, one strike
    fake.release("Breathe out on the way up.")
    assert await first == MP3


async def test_a_refused_request_still_gets_its_own_line_that_became_free_meanwhile(speech, monkeypatch):
    """The guard's round trip takes a moment. If the member's other tab got the phrase cached meanwhile - or started
    synthesising it - the refused request is served from that too: their own cache hits and shared syntheses cost
    nothing. Another member's line never serves it (that would tell them what the other member heard)."""
    fake = speech()
    real_admit = guard.admit
    refused_owner, other_tab = member_tab(1), member_tab(2)
    release = asyncio.Event()

    async def admit(owner, feature, text=None):
        if owner is refused_owner:
            await release.wait()
            raise AIRejected("quota", "You've used your AI voice replies for this minute.", retry_after=30,
                             feature=feature)
        return await real_admit(owner, feature, text)

    monkeypatch.setattr(guard, "admit", admit)
    # 1. cached meanwhile (by the member's other tab)
    refused = asyncio.create_task(say_as(refused_owner, "Last round!"))
    await asyncio.sleep(0.02)
    assert await say_as(other_tab, "Last round!") == MP3
    release.set()
    assert await refused == MP3 and len(fake.calls) == 1
    # 2. underway for the member's other tab meanwhile
    release.clear()
    fake.hold("Final push!")
    refused = asyncio.create_task(say_as(refused_owner, "Final push!"))
    await asyncio.sleep(0.02)
    other = asyncio.create_task(say_as(other_tab, "Final push!"))
    await wait_until(lambda: fake.started("Final push!") == 1, what="the other tab's synthesis")
    release.set()
    await wait_until(lambda: tts.stats.joined == 1, what="the refused request to share it")
    fake.release("Final push!")
    assert await asyncio.gather(refused, other) == [MP3, MP3]
    assert len(fake.calls) == 2 and tts.stats.refused == 0
    # 3. nothing of their own to share: the refusal stands ...
    release.set()
    with owner_scope(refused_owner):
        with pytest.raises(AIRejected) as exc:
            await tts.synthesize_or_refuse("Cool down now.")
    assert exc.value.code == "quota" and tts.stats.refused == 1 and len(fake.calls) == 2
    # ... also when another member has just heard that very line
    assert await say_as(page_owner(3), "Cool down now.") == MP3 and len(fake.calls) == 3
    with owner_scope(refused_owner):
        with pytest.raises(AIRejected) as exc:
            await tts.synthesize_or_refuse("Cool down now.")
    assert exc.value.code == "quota" and tts.stats.refused == 2


# ============================================================================================ guard + queue over HTTP
async def test_tts_takes_a_ticket_in_the_tts_lane_and_gives_it_back(client, db, speech):
    fake = speech()
    user = await make_customer(db)
    await login_as(client, user)
    before = scheduler.snapshot()["counters"]
    r = await api(client, "POST", "/api/tts", json={"text": "Squeeze at the top."}, headers={"X-Page-Id": PAGE})
    assert r.status_code == 200 and r.content == MP3
    assert fake.lanes[0]["running"] == 1 and fake.lanes[0]["capacity"] == get_settings().ai_tts_concurrency
    seen = fake.owners[0]
    assert seen is not None and seen.user_id == user.id and seen.page_id == PAGE  # the requester's ticket
    after = scheduler.snapshot()
    assert after["lanes"]["tts"]["running"] == 0 and after["lanes"]["llm"]["running"] == 0
    assert after["counters"]["started"] == before["started"] + 1
    assert after["counters"]["completed"] == before["completed"] + 1
    # a cache hit never touches the queue
    assert (await api(client, "POST", "/api/tts", json={"text": "Squeeze at the top."})).status_code == 200
    assert scheduler.snapshot()["counters"]["submitted"] == after["counters"]["submitted"]


async def test_tts_cache_hits_are_free_and_served_even_over_the_quota(client, db, speech, monkeypatch):
    # a plain member gets 1 new voice line per minute AND per hour (the refusal never depends on a minute boundary)
    monkeypatch.setattr(get_settings(), "ai_quota_scale", 1 / 600)
    assert voice_minute_quota() == 1
    fake = speech()
    user = await make_customer(db)
    await login_as(client, user)
    for _ in range(4):
        r = await api(client, "POST", "/api/tts", json={"text": "Nice work!"})
        assert r.status_code == 200 and r.content == MP3
    assert (await guard.status(user.id))["usage"]["voice"][0] == 1  # one synthesis charged, three cache hits free
    r = await api(client, "POST", "/api/tts", json={"text": "Rest 45 seconds."})
    assert r.status_code == 429, r.text
    body = r.json()
    assert body["code"] == "quota" and body["fallback"] == "browser" and body["ai_available"] is True
    assert 0 < body["retry_after"] <= 3600 and r.headers["Retry-After"] == str(body["retry_after"])
    assert r.headers["cache-control"] == "no-store" and "voice" in body["detail"]
    assert len(fake.calls) == 1  # the refused line never reached Kokoro
    again = await api(client, "POST", "/api/tts", json={"text": "Nice work!"})  # cached: still plays
    assert again.status_code == 200 and again.content == MP3
    assert tts.stats.refused == 1 and tts.stats.hits == 4


async def test_tts_during_an_ai_pause_answers_429_but_cached_lines_still_play(client, db, speech):
    fake = speech()
    user = await make_customer(db)
    await login_as(client, user)
    assert (await api(client, "POST", "/api/tts", json={"text": "Let's go!"})).status_code == 200
    owner = AIOwner(user_id=user.id, role="customer")
    codes = []
    for _ in range(2):  # two spam refusals elsewhere pause AI on the account (cooldown ladder level 1)
        with pytest.raises(AIRejected) as refused:
            await guard.admit(owner, "assistant", "!" * 60)
        codes.append(refused.value.code)
    assert codes == ["spam", "cooldown"]
    r = await api(client, "POST", "/api/tts", json={"text": "Keep breathing steadily."})
    assert r.status_code == 429 and r.json()["code"] == "cooldown" and r.json()["fallback"] == "browser"
    assert 60 < int(r.headers["Retry-After"]) <= policy.COOLDOWN_LADDER[0][1]
    cached = await api(client, "POST", "/api/tts", json={"text": "Let's go!"})
    assert cached.status_code == 200 and cached.content == MP3 and len(fake.calls) == 1


async def test_tts_queue_refusal_answers_503_with_a_retry_after(client, db, speech, monkeypatch):
    monkeypatch.setattr(get_settings(), "ai_queue_max_total", 0)  # the voice lane takes no more waiting work
    fake = speech()
    user = await make_customer(db)
    await login_as(client, user)
    r = await api(client, "POST", "/api/tts", json={"text": "Shoulders down and back."})
    assert r.status_code == 503, r.text
    body = r.json()
    assert body["fallback"] == "browser" and body["code"] == "busy" and body["retry_after"] > 0
    assert r.headers["Retry-After"] == str(body["retry_after"]) and r.headers["cache-control"] == "no-store"
    assert not fake.calls
    await wait_until(lambda: not tts_mod._background, what="the refund")
    assert (await guard.status(user.id))["usage"]["voice"][0] == 0  # nothing synthesised: refunded


async def test_tts_without_a_signed_in_owner_is_never_judged(speech, monkeypatch):
    """System callers (no AI owner) and the startup probe are not per-user work: no guard, no per-user limits."""
    monkeypatch.setattr(get_settings(), "ai_quota_scale", 0.001)
    speech()
    for n in range(5):
        assert await tts.synthesize(f"System line {n}") == MP3
    assert tts.stats.refused == 0
