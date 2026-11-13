"""In-process TTL cache for read-mostly page blocks (home page figures, the directory's city list, shop facets ...).

    featured = TTLCache("home.featured", ttl=120)
    cards = await featured.get_or_compute("all", lambda: load_featured(db))

* A fresh value comes straight from memory. A missing value is computed ONCE however many requests ask for it at the
  same moment (single flight: the others wait for that computation). An expired value is served, stale, to everyone
  except the one request that refreshes it (stale-while-revalidate) - a hot page never stampedes the database.
* A failed or cancelled computation is not cached: requests that were waiting for it compute the value themselves.
* Values must be plain data (frozen dataclasses, tuples, lists/dicts of scalars) - never ORM objects, which belong to
  the session that loaded them (a later rollback there would expire them under every other request).
* Per process: each worker has its own copy, so a change is visible everywhere within ``ttl`` seconds - and at once in
  the process that made it when ``clear_on_commit`` watches the models the block is built from.
* Every cache is cleared between tests (``app.core.state.register_reset``).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Hashable, Iterable
from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.state import register_reset

log = logging.getLogger("ironpulse.cache")

_MISSING = object()
_caches: dict[str, "TTLCache"] = {}


class TTLCache:
    """``get_or_compute(key, compute)``: see the module docstring. ``maxsize`` keys at most (least recently used go)."""

    def __init__(self, name: str, ttl: float, *, maxsize: int = 256) -> None:
        self.name = name
        self.ttl = float(ttl)
        self.maxsize = max(1, int(maxsize))
        self._data: OrderedDict[Hashable, tuple[float, Any]] = OrderedDict()  # key -> (expires at, value)
        self._inflight: dict[Hashable, asyncio.Future[Any]] = {}
        self._generation = 0  # bumped by clear()/invalidate(): a computation started before is not stored
        self.computed = 0  # computations finished (tests and logs)
        _caches[name] = self
        register_reset(self.reset, name=f"ttl_cache:{name}")

    # ------------------------------------------------------------------ reading
    def peek(self, key: Hashable) -> Any:
        """The cached value (fresh or stale) or None - never computes."""
        hit = self._data.get(key)
        return None if hit is None else hit[1]

    async def get_or_compute(self, key: Hashable, compute: Callable[[], Awaitable[Any]]) -> Any:
        hit = self._data.get(key)
        if hit is not None:
            expires, value = hit
            if expires > time.monotonic():
                self._data.move_to_end(key)
                return value
            if key in self._inflight:  # being refreshed by another request: the stale value meanwhile
                return value
            return await self._compute(key, compute)
        pending = self._inflight.get(key)
        if pending is not None:
            await asyncio.wait({pending})  # never raises the other request's error (or its cancellation)
            if not pending.cancelled() and pending.exception() is None:
                return pending.result()
        return await self._compute(key, compute)

    async def _compute(self, key: Hashable, compute: Callable[[], Awaitable[Any]]) -> Any:
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        future.add_done_callback(_consume)
        self._inflight[key] = future
        generation = self._generation
        try:
            value = await compute()
        except BaseException as exc:  # noqa: BLE001 - re-raised; waiters fall back to computing themselves
            if not future.done():
                if isinstance(exc, asyncio.CancelledError):
                    future.cancel()
                else:
                    future.set_exception(exc)
            raise
        finally:
            if self._inflight.get(key) is future:
                del self._inflight[key]
        self.computed += 1
        if generation == self._generation:  # nothing was invalidated while this was being computed
            self._data[key] = (time.monotonic() + self.ttl, value)
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)
        if not future.done():
            future.set_result(value)
        return value

    # ------------------------------------------------------------------ invalidation
    def invalidate(self, key: Hashable) -> None:
        self._generation += 1
        self._data.pop(key, None)

    def clear(self) -> None:
        self._generation += 1
        self._data.clear()

    def reset(self) -> None:
        """clear() plus forgetting computations in flight (tests: each test has its own event loop)."""
        self.clear()
        self._inflight.clear()
        self.computed = 0

    def __len__(self) -> int:
        return len(self._data)

    def __contains__(self, key: object) -> bool:
        return key in self._data


def _consume(future: asyncio.Future[Any]) -> None:
    """Mark a failed computation's error as retrieved (nobody may be waiting for it)."""
    if not future.cancelled():
        future.exception()


def get_cache(name: str) -> TTLCache | None:
    return _caches.get(name)


# ====================================================================================================== invalidation
_PENDING = "ttl_cache_pending"  # Session.info key: caches to clear when the transaction commits


def clear_on_commit(caches: Iterable[TTLCache], models: Iterable[type], *,
                    changed: Callable[[Any], bool] | None = None) -> None:
    """Clear ``caches`` (in this process) after any ORM transaction that inserted, deleted or updated an instance of
    ``models`` - from any module, request or background job. ``changed(obj)`` narrows UPDATEs down (e.g. to the
    columns the cached block shows); inserts and deletes always count. ORM-enabled bulk ``update(Model)`` /
    ``delete(Model)`` statements count too. Raw SQL does not: the TTL covers it (and the other worker processes)."""
    targets = tuple(caches)
    watched = tuple(models)

    def relevant(obj: Any, dirty: bool) -> bool:
        if not isinstance(obj, watched):
            return False
        return not dirty or changed is None or changed(obj)

    def mark(session: Session) -> None:
        session.info.setdefault(_PENDING, set()).update(targets)

    @event.listens_for(Session, "after_flush")
    def _after_flush(session: Session, _ctx: Any) -> None:
        try:
            if any(relevant(o, False) for o in session.new) or any(relevant(o, False) for o in session.deleted) or any(
                relevant(o, True) for o in session.dirty
            ):
                mark(session)
        except Exception:  # noqa: BLE001 - a cache hook must never break a flush
            log.exception("cache invalidation hook failed")

    @event.listens_for(Session, "do_orm_execute")
    def _bulk(state: Any) -> None:
        try:
            if (state.is_update or state.is_delete or state.is_insert) and state.bind_mapper is not None and issubclass(
                state.bind_mapper.class_, watched
            ):
                mark(state.session)
        except Exception:  # noqa: BLE001
            log.exception("cache invalidation hook failed")


@event.listens_for(Session, "after_commit")
def _clear_pending(session: Session) -> None:
    # Marks are only consumed by a commit. A rollback keeps them (the next commit of this session clears once more):
    # clearing too often costs one recomputation, missing a change would serve stale data for a whole TTL.
    pending = session.info.pop(_PENDING, None)
    for cache in pending or ():
        try:
            cache.clear()
        except Exception:  # noqa: BLE001
            log.exception("cache clear failed")
