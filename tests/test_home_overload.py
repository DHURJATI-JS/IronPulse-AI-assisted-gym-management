"""The home page under database overload: it still renders, and logs one throttled warning instead of a traceback.

Found by the owner's 10-minute stress test (loadtest/results/stress-20261113-132826): during the 1,600-user spike the
home page's data loader met a pool timeout five times and logged each with a full traceback ("home page data failed to
load"), the only tracebacks left in that run. A real bug in the loader must still be logged with its traceback.
"""

from __future__ import annotations

import logging

import httpx
import pytest
import sqlalchemy.exc

from app.core import overload
from app.routers import public


def _tracebacks(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [f"{r.name}: {r.getMessage()}" for r in caplog.records if r.exc_info or r.levelno >= logging.ERROR]


@pytest.fixture(autouse=True)
def _fresh_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    # the one-a-minute throttle is process-wide: start each test with a clean slate
    monkeypatch.setattr(overload, "throttled", overload.ThrottledWarnings())


async def test_a_pool_timeout_on_the_home_page_is_one_warning_not_a_traceback(
        client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    async def busy(*_a, **_k):
        raise sqlalchemy.exc.TimeoutError("QueuePool limit of size 10 overflow 5 reached, connection timed out, timeout 10.00")

    monkeypatch.setattr(public, "home_featured", busy)
    caplog.set_level(logging.DEBUG)
    for _ in range(3):
        r = await client.get("/")
        assert r.status_code == 200  # the landing page always renders
    assert _tracebacks(caplog) == []
    lines = [r.getMessage() for r in caplog.records if r.name == "ironpulse.public" and r.levelno == logging.WARNING]
    assert len(lines) == 1, lines  # throttled: one line per call site per minute
    assert lines[0].startswith("database pool timeout in GET / (home page data)")


async def test_a_real_bug_in_the_home_page_data_still_logs_its_traceback(
        client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    async def broken(*_a, **_k):
        raise ValueError("a real bug")

    monkeypatch.setattr(public, "home_featured", broken)
    caplog.set_level(logging.DEBUG)
    r = await client.get("/")
    assert r.status_code == 200
    assert any("home page data failed to load" in t for t in _tracebacks(caplog))
