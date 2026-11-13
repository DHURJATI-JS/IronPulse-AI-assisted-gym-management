"""Per-process request log: one CSV row per request event (HTTP hops and WebSocket events) plus NOTE rows.

analyze.py computes exact per-step percentiles / error rates / per-class numbers from these rows (Locust's own
stats_history only has rolling-window percentiles). Each worker process writes its own file
``<run dir>/requests_<pid>.csv``; buffered, flushed every 2 s and at exit.

Columns: t (epoch s at the END of the request), cls (user class), type (GET/POST/WS/TASK/NOTE), name, ms, bytes,
ok (1/0), status, err, note (an outcome the task attached: AI answer / fallback / busy / blocked, checkout result ...;
NOTE rows: an event without a request, e.g. ``new visitor``).
"""

from __future__ import annotations

import csv
import os
import time
from pathlib import Path
from typing import Any

FIELDS = ["t", "cls", "type", "name", "ms", "bytes", "ok", "status", "err", "note"]


class RequestLog:
    def __init__(self) -> None:
        self.fh: Any = None
        self.writer: Any = None
        self.last_flush = 0.0
        self.path: Path | None = None

    def open(self, run_dir: Path) -> None:
        run_dir.mkdir(parents=True, exist_ok=True)
        self.path = run_dir / f"requests_{os.getpid()}.csv"
        self.fh = open(self.path, "a", newline="", encoding="utf-8", buffering=1 << 16)  # noqa: SIM115
        self.writer = csv.writer(self.fh)
        if self.fh.tell() == 0:
            self.writer.writerow(FIELDS)

    def write(self, request_type: str, name: str, response_time: float, response_length: int, exception: Any,
              context: Any, response: Any) -> None:
        if self.writer is None:
            return
        status = getattr(response, "status_code", "") if response is not None else ""
        ctx = context if isinstance(context, dict) else {}
        err = ""
        if exception is not None:
            err = str(exception).replace("\n", " ")[:160]
        self.writer.writerow([f"{time.time():.3f}", ctx.get("cls", ""), request_type, name, f"{response_time:.1f}",
                              response_length or 0, 0 if exception is not None else 1, status, err,
                              str(ctx.get("note", ""))[:60]])
        self._maybe_flush()

    def note(self, cls: str, name: str, note: str = "") -> None:
        """An event that is not a request (not shown in Locust's stats): e.g. a new anonymous visitor."""
        if self.writer is None:
            return
        self.writer.writerow([f"{time.time():.3f}", cls, "NOTE", name, "0", 0, 1, "", "", note[:60]])
        self._maybe_flush()

    def _maybe_flush(self) -> None:
        now = time.monotonic()
        if now - self.last_flush > 2.0:
            self.last_flush = now
            self.fh.flush()

    def close(self) -> None:
        if self.fh is not None:
            self.fh.flush()
            self.fh.close()
            self.fh = self.writer = None


reqlog = RequestLog()
