"""Rate-limit buckets (app/security/ratelimit.py) and their defaults.

Live-workout traffic (progress events, commands, chat messages, coach voice) has its own per-address "live" bucket, so
it never exhausts the "ai" one. These limits are per address, and a whole gym behind one NAT shares an address: each
member's own AI limits are the AI guard's (app/ai/policy.py), which is why the per-address AI limit is 60 a minute.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from dotenv import dotenv_values

from app.config import Settings, get_settings
from app.security import ratelimit
from app.security.gate import SecurityGateMiddleware
from app.security.ratelimit import RateLimiter, bucket_for

ROOT = Path(__file__).resolve().parent.parent
RATE_FIELDS = ("rate_limit_per_minute", "rate_limit_auth_per_minute", "rate_limit_ai_per_minute",
               "rate_limit_live_per_minute", "rate_limit_upload_per_minute", "rate_limit_static_per_minute")
JSON = "application/json"


def _default(field: str) -> Any:
    return Settings.model_fields[field].default


@pytest.fixture
def shipped_limiter(monkeypatch: pytest.MonkeyPatch) -> RateLimiter:
    """A limiter with the shipped defaults, whatever a local .env pins (setup copies .env.example into .env)."""
    shipped = get_settings().model_copy(update={field: _default(field) for field in RATE_FIELDS})
    monkeypatch.setattr(ratelimit, "get_settings", lambda: shipped)
    return RateLimiter()


def test_live_coach_traffic_has_its_own_bucket():
    assert bucket_for("POST", "/api/live/sessions", JSON) == "live"
    assert bucket_for("POST", "/api/live/sessions/5/event", JSON) == "live"
    assert bucket_for("POST", "/api/live/sessions/5/discomfort", JSON) == "live"
    assert bucket_for("POST", "/api/live/sessions/5/end", JSON) == "live"
    assert bucket_for("POST", "/api/tts", JSON) == "live"
    # chat messages too: mostly commands, and the AI behind them is held to each member's quota by the AI guard
    assert bucket_for("POST", "/api/live/sessions/5/message", JSON) == "live"
    # the other AI endpoints stay in the AI bucket
    for path in ("/api/assistant/chat", "/api/plans/ask", "/api/shop/products/7/ask", "/api/shop/products/7/ask/stream",
                 "/api/ai/anything", "/me/coach/generate", "/me/wellness", "/owner/customers/intake"):
        assert bucket_for("POST", path, JSON) == "ai", path
    # reads only spend the global budget
    assert bucket_for("GET", "/api/live/sessions/5", JSON) is None
    assert bucket_for("GET", "/api/tts/status", JSON) is None


def test_a_busy_workout_fits_the_live_bucket(shipped_limiter: RateLimiter):
    limits = shipped_limiter.limits()
    # a cue, an event and a voice line every couple of seconds is ~90/minute; bursts happen between sets
    assert limits["live"].per_second * 60 >= 120 and limits["live"].capacity >= 40
    assert all(shipped_limiter.hit("203.0.113.1", "live")[0] for _ in range(40))


def test_the_per_address_ai_limit_is_60_a_minute(shipped_limiter: RateLimiter):
    assert _default("rate_limit_ai_per_minute") == 60
    ai = shipped_limiter.limits()["ai"]
    assert ai.per_second * 60 == pytest.approx(60) and ai.capacity == 30
    assert all(shipped_limiter.hit("203.0.113.2", "ai")[0] for _ in range(30))  # one gym's burst of questions
    allowed, retry = shipped_limiter.hit("203.0.113.2", "ai")
    assert not allowed and retry >= 1


def test_env_example_ships_the_defaults_and_says_where_ai_quotas_live():
    path = ROOT / ".env.example"
    example = dotenv_values(path)
    for field in RATE_FIELDS:
        key = field.upper()
        if key in example:
            assert int(example[key] or "") == _default(field), key
    assert example["RATE_LIMIT_AI_PER_MINUTE"] == "60"
    line = next(row for row in path.read_text(encoding="utf-8").splitlines()
                if row.startswith("RATE_LIMIT_AI_PER_MINUTE="))
    assert "app/ai/policy.py" in line  # per-user AI quotas are set there, not by this per-address limit


# ------------------------------------------------------------------------------------------- through the security gate
class _Downstream:
    """The app behind the gate: answers 200 and counts what got through."""

    def __init__(self) -> None:
        self.paths: list[str] = []

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        self.paths.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b"{}"})


async def _post(gate: SecurityGateMiddleware, ip: str, path: str,
                body: bytes = b'{"text": "next"}') -> tuple[int, dict]:
    scope = {"type": "http", "method": "POST", "path": path, "raw_path": path.encode(), "query_string": b"",
             "headers": [(b"content-type", JSON.encode()), (b"content-length", str(len(body)).encode()),
                         (b"user-agent", b"Mozilla/5.0 (iPhone)")],
             "client": (ip, 51000), "server": ("testserver", 80), "scheme": "http", "state": {}}
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    await gate(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    return start["status"], {k.decode(): v.decode() for k, v in start.get("headers", [])}


async def test_live_chat_spends_the_live_budget_never_the_ai_one(monkeypatch: pytest.MonkeyPatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_whitelist", [])
    monkeypatch.setattr(settings, "rate_limit_per_minute", 240)  # a burst of 60: never the limit here
    monkeypatch.setattr(settings, "rate_limit_live_per_minute", 60)  # a burst of 20
    monkeypatch.setattr(settings, "rate_limit_ai_per_minute", 2)  # a burst of 5: any message charged to it shows
    downstream = _Downstream()
    gate = SecurityGateMiddleware(downstream)
    ip = "203.0.113.77"  # one gym's NAT address
    statuses = [(await _post(gate, ip, "/api/live/sessions/5/message"))[0] for _ in range(20)]
    assert statuses == [200] * 20  # the gym's members' messages (commands, chat) get the live burst ...
    status, headers = await _post(gate, ip, "/api/live/sessions/5/message")
    assert status == 429 and int(headers["retry-after"]) >= 1  # ... and the live limit is what holds a flood
    assert (await _post(gate, ip, "/api/live/sessions/5/event", b'{"type": "paused"}'))[0] == 429  # one live budget
    # the AI bucket is untouched: the other AI endpoints still have their whole burst, and only then their limit
    for _ in range(5):
        assert (await _post(gate, ip, "/api/assistant/chat", b'{"message": "hi"}'))[0] == 200
    status, headers = await _post(gate, ip, "/api/assistant/chat", b'{"message": "hi"}')
    assert status == 429 and int(headers["retry-after"]) >= 1
    assert downstream.paths.count("/api/live/sessions/5/message") == 20
