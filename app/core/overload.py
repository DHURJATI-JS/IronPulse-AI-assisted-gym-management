"""Degrading quietly while the database is overloaded or out of reach (ARCHITECTURE.md "Database sizing", Overload).

A request answers such a moment with the friendly 503 (app/core/errors.py). Everything else that needs the database
then - presence's resume / exit steps (app/services/presence.py), the job runner (app/services/jobs.py), the worker
loops (app/services/worker.py) - and the /ws handshake (1013, app/routers/core.py) must neither crash nor fill the log
with tracebacks: each logs ONE warning per call site per ``LOG_EVERY_SECONDS`` (the next one says how many more there
were) and retries later with back-off. ``app.db.db_unavailable`` says which errors those are; anything else is still
logged with its traceback - it is a bug, not the weather.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any

from app.core.state import register_reset

LOG_EVERY_SECONDS = 60.0  # one warning per call site this often while the database stays unavailable
_SITES_MAX = 512  # call sites are a fixed set of names; this only bounds a programming error

KIND_LABELS = {
    "pool": "pool timeout",  # no pooled connection free within DB_POOL_TIMEOUT_S
    "statement": "statement timeout",  # cancelled by statement_timeout
    "connections": "connection refused",  # too many clients / starting up / shutting down (CONNECTION_REFUSED)
    "connection": "connection error",  # could not connect, or the connection was lost
}


class ThrottledWarnings:
    """At most one WARNING per call site per ``every`` seconds; the warnings in between are counted and the next line
    says how many there were (``... (12 more like this in the last 60 s)``)."""

    def __init__(self, every: float = LOG_EVERY_SECONDS) -> None:
        self.every = every
        self._sites: dict[str, tuple[float, int]] = {}  # site -> (last logged at, monotonic; suppressed since)

    def warn(self, logger: logging.Logger, site: str, msg: str, *args: Any) -> bool:
        """Log ``msg % args`` unless ``site`` logged within the last ``every`` seconds. True when it was logged."""
        now = time.monotonic()
        last = self._sites.get(site)
        if last is not None and now - last[0] < self.every:
            self._sites[site] = (last[0], last[1] + 1)
            return False
        if last is None and len(self._sites) >= _SITES_MAX:
            self._sites.clear()
        suppressed = last[1] if last is not None else 0
        self._sites[site] = (now, 0)
        if suppressed:
            msg += " (%d more like this in the last %.0f s)"
            args = (*args, suppressed, now - last[0])  # type: ignore[index]
        logger.warning(msg, *args)
        return True

    def suppressed(self, site: str) -> int:
        """How many warnings of ``site`` were held back since its last line (tests, diagnostics)."""
        last = self._sites.get(site)
        return last[1] if last is not None else 0

    def reset(self) -> None:
        self._sites.clear()


throttled = ThrottledWarnings()
register_reset(throttled.reset, name="overload.throttled")


def first_line(exc: BaseException, limit: int = 200) -> str:
    return (str(exc).splitlines() or [""])[0][:limit] or exc.__class__.__name__


def warn_db_unavailable(logger: logging.Logger, site: str, kind: str, exc: BaseException, what: str) -> bool:
    """The one-a-minute line of a call site that met an unavailable database: ``database pool timeout in <site>: <what>
    - QueuePool limit of size 10 overflow 5 reached ...``. A request's own 503 line (app/core/errors.py) starts the same
    way, so the load test counts both as DB timeouts of their step."""
    return throttled.warn(logger, site, "database %s in %s: %s - %s", KIND_LABELS.get(kind, kind), site, what,
                          first_line(exc))


def backoff_delay(failures: int, base: float, cap: float, jitter: float = 0.2) -> float:
    """``base`` doubled for every failure after the first, at most ``cap``, spread by +/- ``jitter`` so that the
    workers (and the users' pages) that met the same overload do not all come back in the same second."""
    n = max(1, int(failures))
    delay = min(float(cap), float(base) * (2 ** min(n - 1, 30)))
    return max(0.0, delay * random.uniform(1.0 - jitter, 1.0 + jitter))
