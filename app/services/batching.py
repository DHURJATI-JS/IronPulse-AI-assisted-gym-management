"""Chunked background work: bounded batches, one short transaction each, resumable and idempotent.

A ``@periodic`` task is called with ``run_periodic``'s session (the task's advisory lock is held on a connection of
its own for the whole run). Batch work never happens in that session's transaction. Each chunk opens its own ``session_scope()``, claims at
most ``batch`` rows (keyset over id and/or ``... LIMIT n FOR UPDATE SKIP LOCKED``), does the work and commits, so

* a transaction stays short: its row locks, WAL and a failure are one chunk's worth (a failed chunk rolls back alone);
* no unbounded id list is ever loaded into Python or bound as parameters (asyncpg caps a statement at 32,767);
* a run is resumable and idempotent: every chunk records what it did in the same transaction (``reminded_at``, a
  status, a "sent today" date ...), so a crash or a spent time budget leaves the rest to the next tick and nothing is
  ever done twice.

``Chunks`` keeps the books of one run::

    run = Chunks(holder=db, budget_s=BUDGET_S)       # db = the periodic's lock-holding session (or None in a job)
    last_id = 0
    while await run.next():                          # False once a chunk came back short or the budget is spent
        async with session_scope() as s:
            rows = ...                               # WHERE ... AND id > :last_id ORDER BY id LIMIT :batch
            ...                                      # work + the "done" marks, committed together
        last_id = rows[-1].id if rows else last_id
        run.done(len(rows), batch)

Before the second chunk ``next()`` lifts the idle-in-transaction limit of ``holder`` (its transaction waits, idle,
while the chunks commit - see ``long_statement``), so a long run never loses its advisory lock half-way. A run that
stops on its budget can leave a keyset cursor with ``remember(name, last_id)``; the next run in this process starts
there (``resume_after(name)``) instead of at the beginning, so a backlog larger than one run's budget is still worked
through in order.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar

from app.core.state import register_reset
from app.db import long_statement

T = TypeVar("T")

BATCH_SIZE = 500  # default rows per chunk (one short transaction)
DEFAULT_BUDGET_S = 50.0  # default time budget of one run (seconds); the next tick continues


@dataclass
class Chunks:
    """Bookkeeping of one chunked run: chunk count, rows, time budget, optional chunk cap."""

    holder: Any = None  # the periodic's advisory-lock session (kept alive across chunks); None in jobs / tests
    budget_s: float | None = DEFAULT_BUDGET_S
    max_chunks: int | None = None
    chunks: int = 0
    rows: int = 0
    finished: bool = False  # a chunk came back short: nothing is left
    started: float = field(default_factory=time.monotonic)

    @property
    def out_of_time(self) -> bool:
        return self.budget_s is not None and time.monotonic() - self.started >= self.budget_s

    @property
    def stopped_early(self) -> bool:
        """The run ended on its budget / chunk cap with work possibly left (the next tick carries on)."""
        return not self.finished

    async def next(self) -> bool:
        """True while another chunk may run (the first always does)."""
        if self.finished:
            return False
        if self.chunks and (self.out_of_time or (self.max_chunks is not None and self.chunks >= self.max_chunks)):
            return False
        if self.chunks == 1 and self.holder is not None:
            await keep_lock_alive(self.holder)
        self.chunks += 1
        return True

    def done(self, scanned: int, batch: int) -> None:
        """Record a chunk that looked at ``scanned`` rows of at most ``batch``: fewer means nothing is left."""
        self.rows += scanned
        if scanned < batch:
            self.finished = True

    def finish(self) -> None:
        self.finished = True


async def keep_lock_alive(holder: Any) -> None:
    """The lock-holding transaction of a periodic run sits idle while its chunks commit elsewhere: lift its
    idle-in-transaction limit (it holds no snapshot and writes nothing, so waiting costs the server nothing)."""
    await long_statement(holder, idle_ms=0)


def slices(items: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    """``items`` in consecutive slices of at most ``size`` (bounded IN lists / multi-row INSERTs)."""
    size = max(1, int(size))
    for start in range(0, len(items), size):
        yield items[start:start + size]


# ------------------------------------------------------------------ resume cursors (process-local hints)
_resume: dict[str, int] = {}


def resume_after(name: str) -> int:
    """Keyset start of the next run of ``name`` (0 = from the beginning); consumed by the call."""
    return _resume.pop(name, 0)


def remember(name: str, last_id: int) -> None:
    """A run of ``name`` stopped on its budget after ``last_id``: the next run in this process starts there."""
    _resume[name] = int(last_id)


register_reset(_resume.clear, name="batching.resume")
