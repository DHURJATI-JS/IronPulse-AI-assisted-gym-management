"""Bug-hunt aids of the simulated AI provider (app/ai/simulated.py, ``SimulatedProvider`` in app/ai/client.py) and two
review fixes of that provider.

* AI_SIM_ECHO_NONCE=true: an answer starts with the nonce tokens (nq + 8 of a-z0-9) of the requester's own words - the
  words the feature passed as ``user_text``, else the prompt - so a hunter can prove an answer belongs to its question
  and user; streamed, completed and structured (JSON) answers alike; off by default;
* AI_SIM_CAPTURE_PROMPTS=<path>: every prompt (system + prompt, user id, feature, the requester's words) lands in that
  JSONL file within a moment, mode 0600, bounded in size; several prompts, several users, one line each;
* both are ignored under ENVIRONMENT=production; neither touches a real provider;
* review #12: the simulated health check fails as often as its calls (AI_SIM_FAILURE_RATE=1 keeps the breaker open);
* review #13: production never runs the simulated provider next to real ones (only alone, for a load test).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import stat
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, Field

from app.ai import guard as guard_module
from app.ai import simulated
from app.ai.client import SimulatedProvider, _simulated_provider, ai
from app.ai.owner import AIOwner
from app.config import get_settings

NONCE = "nqab12cd34"
OTHER = "nqzz99yy88"  # another member's nonce, planted in the prompt's context


class Admit:
    async def admit(self, owner: Any, feature: str, text: str | None = None, *, scope: str | None = None) -> Any:
        return SimpleNamespace()

    async def settle(self, admission: Any, *, ok: bool) -> None:
        return None


@pytest.fixture(autouse=True)
def quiet_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guard_module, "guard", Admit())


def fast(**overrides: Any) -> Any:
    values: dict[str, Any] = {"ai_provider_order": ["simulated"], "ai_sim_first_token_ms": (5.0, 10.0),
                              "ai_sim_tokens_per_s": (2000.0, 2000.0), "ai_sim_failure_rate": 0.0,
                              "ai_sim_max_concurrency": 4}
    values.update(overrides)
    return get_settings().model_copy(update=values)


def sim(**kwargs: Any) -> SimulatedProvider:
    settings = kwargs.pop("settings", None) or fast()
    p = SimulatedProvider(settings, rng=random.Random(3), **kwargs)
    ai.use_providers([p])
    return p


async def stream(prompt: str, *, user_text: str | None = None, uid: int = 5, feature: str = "plans") -> str:
    parts = [c async for c in ai.stream(feature=feature, system="sys", prompt=prompt, user_text=user_text,
                                        owner=AIOwner(user_id=uid))]
    return "".join(parts)


class Reply(BaseModel):
    answer: str
    safe: bool = False


class Tight(BaseModel):
    answer: str = Field(max_length=12)


# ------------------------------------------------------------------ AI_SIM_ECHO_NONCE
async def test_echo_puts_the_requesters_nonces_first_in_a_stream():
    sim(echo_nonces=True)
    first = [c async for c in ai.stream(feature="plans", system="s", prompt=f"question {NONCE}",
                                        user_text=f"warm-up ideas {NONCE} please", owner=AIOwner(user_id=5))]
    assert first[0] == f"[{NONCE}] " and len(first) > 2  # alone in the first chunk, then the usual words
    text = await stream(f"q {NONCE} and {NONCE.upper()} again nqab12cd345", user_text=None)
    assert text.startswith(f"[{NONCE}] ")  # each nonce once; a longer token is not a nonce


async def test_echo_reads_only_the_requesters_own_words_when_the_feature_names_them():
    sim(echo_nonces=True)
    prompt = f"Member context: injured knee. Note from another member: {OTHER}.\nQuestion: squats? {NONCE}"
    text = await stream(prompt, user_text=f"squats? {NONCE}")
    assert text.startswith(f"[{NONCE}] ") and OTHER not in text
    text = await stream(f"context {OTHER}", user_text="no nonce in my words")
    assert not text.startswith("[") and OTHER not in text


async def test_echo_in_completions_and_structured_answers():
    sim(echo_nonces=True)
    res = await ai.complete(feature="plans", system="s", prompt="p", user_text=f"hi {NONCE}", owner=AIOwner(user_id=5))
    assert res.text.startswith(f"[{NONCE}] ") and res.provider == "simulated"
    res = await ai.complete(feature="shop", system="s", prompt="p", schema=Reply, user_text=f"is it vegan? {NONCE}",
                            owner=AIOwner(user_id=5))
    assert isinstance(res.data, Reply) and res.data.answer.startswith(f"[{NONCE}] ") and res.data.safe is False
    res = await ai.complete(feature="shop", system="s", prompt="p", schema=Tight, user_text=f"x {NONCE}",
                            owner=AIOwner(user_id=5))
    assert isinstance(res.data, Tight)  # a field too short for the nonces: answered unchanged, still valid


async def test_echo_is_off_by_default():
    sim()
    assert not (await stream(f"q {NONCE}", user_text=f"q {NONCE}")).startswith("[")


def test_nonce_rules():
    assert simulated.find_nonces(f"a {NONCE}, b {OTHER}; {NONCE} xnq12345678 nq1234567 nq1234567890") == [NONCE, OTHER]
    assert simulated.find_nonces(" ".join(f"nq{i:08d}" for i in range(50))) == [f"nq{i:08d}" for i in range(20)]
    assert simulated.echo_prefix([]) == "" and simulated.echo_prefix([NONCE, OTHER]) == f"[{NONCE} {OTHER}] "


# ------------------------------------------------------------------ AI_SIM_CAPTURE_PROMPTS
async def test_capture_writes_every_prompt_with_its_user_and_feature(tmp_path):
    path = tmp_path / "hunt" / "prompts.jsonl"
    p = sim(capture_path=str(path))
    await stream("prompt for five", user_text=f"words of five {NONCE}", uid=5, feature="plans")
    await stream("prompt for six", uid=6, feature="shop_qa")
    await ai.complete(feature="coach", system="coach system", prompt="plan please", schema=Reply,
                      owner=AIOwner(user_id=5))
    await asyncio.sleep(simulated.FLUSH_SECONDS + 0.15)  # buffered: on disk within FLUSH_SECONDS
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [(r["user_id"], r["feature"], r["kind"], r["prompt"]) for r in rows] == [
        (5, "plans", "stream", "prompt for five"), (6, "shop_qa", "stream", "prompt for six"),
        (5, "coach", "complete", "plan please")]
    assert rows[0]["user_text"] == f"words of five {NONCE}" and rows[0]["system"] == "sys"
    assert rows[2]["schema"] == "Reply" and rows[2]["system"].startswith("coach system")
    assert all(r["owner_kind"] == "user" and r["provider"] == "simulated" and r["pid"] == os.getpid() for r in rows)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600  # prompts hold member data
    assert p.capture is not None and p.capture.written == 3


def test_capture_stops_at_its_size_limit(tmp_path, caplog):
    path = tmp_path / "small.jsonl"
    capture = simulated.PromptCapture(path, max_bytes=300, flush_seconds=0)
    with caplog.at_level(logging.WARNING, logger="ironpulse.ai.simulated"):
        capture.add({"prompt": "x" * 100})
        capture.add({"prompt": "y" * 400})  # would pass the limit: dropped, and capturing stops
        capture.add({"prompt": "z"})
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["prompt"] == "x" * 100
    assert "size limit" in caplog.text


async def test_both_aids_come_from_the_environment(monkeypatch, tmp_path):
    path = tmp_path / "env.jsonl"
    monkeypatch.setenv("AI_SIM_ECHO_NONCE", "true")
    monkeypatch.setenv("AI_SIM_CAPTURE_PROMPTS", str(path))
    p = sim()
    assert p.echo_nonces and p.capture is not None and p.describe()["detail"]["capture_prompts"] is True
    assert (await stream(f"q {NONCE}", user_text=f"q {NONCE}")).startswith(f"[{NONCE}] ")
    p.capture.flush()
    assert json.loads(path.read_text(encoding="utf-8").splitlines()[0])["user_text"] == f"q {NONCE}"


async def test_production_ignores_both_aids(monkeypatch, tmp_path, caplog):
    path = tmp_path / "prod.jsonl"
    monkeypatch.setenv("AI_SIM_ECHO_NONCE", "true")
    monkeypatch.setenv("AI_SIM_CAPTURE_PROMPTS", str(path))
    with caplog.at_level(logging.WARNING, logger="ironpulse.ai"):
        p = sim(settings=fast(environment="production", ai_simulated_allowed=True))
    assert not p.echo_nonces and p.capture is None and "ignored under ENVIRONMENT=production" in caplog.text
    assert not (await stream(f"q {NONCE}", user_text=f"q {NONCE}")).startswith("[")
    assert not path.exists()


# ------------------------------------------------------------------ review fixes
async def test_a_fully_failing_simulation_stays_down_after_the_health_check():
    """Review #12: the health job used to close the breaker every minute (ping always passed) and announce 'AI online'
    to everyone, only for the next calls to fail again."""
    p = sim(settings=fast(ai_sim_failure_rate=1.0))
    for _ in range(2):
        with pytest.raises(Exception):
            await ai.complete(feature="plans", system="s", prompt="p", owner=AIOwner(user_id=5))
    assert p.circuit_open()
    status = await ai.refresh_health()
    assert p.circuit_open() and status["state"] == "offline"
    healthy = sim(settings=fast(ai_sim_failure_rate=0.0))
    assert await healthy.ping() is True


def test_production_runs_the_simulated_provider_only_alone(caplog):
    """Review #13: AI_PROVIDER_ORDER=gateway,simulated with AI_SIMULATED_ALLOWED=true would answer members with
    generated text whenever the real model fails - the router leaves it out (the settings should refuse it too)."""
    mixed = fast(environment="production", ai_simulated_allowed=True, ai_provider_order=["gateway", "simulated"])
    with caplog.at_level(logging.ERROR, logger="ironpulse.ai"):
        assert _simulated_provider(mixed) is None
    assert "only alone" in caplog.text
    alone = fast(environment="production", ai_simulated_allowed=True, ai_provider_order=["simulated"])
    assert isinstance(_simulated_provider(alone), SimulatedProvider)
    assert isinstance(_simulated_provider(fast(ai_provider_order=["gateway", "simulated"])), SimulatedProvider)  # dev/test
