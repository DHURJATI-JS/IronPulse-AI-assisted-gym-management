"""Registry of process-local state that tests must reset between cases (AI guard counters, the AI queue,
presence timers, running job tasks ...). Modules call ``register_reset(fn)`` at import time; tests/conftest.py
runs ``await reset_all()`` before every test. ``fn`` may be sync or async."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from typing import Any

log = logging.getLogger("ironpulse.state")
_resets: dict[str, Callable[[], Any]] = {}


def register_reset(fn: Callable[[], Any], *, name: str | None = None) -> Callable[[], Any]:
    _resets[name or f"{fn.__module__}.{fn.__qualname__}"] = fn
    return fn


async def reset_all() -> None:
    for name, fn in list(_resets.items()):
        try:
            result = fn()
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001 - a broken reset must not hide the real test failure
            log.exception("state reset %s failed", name)
