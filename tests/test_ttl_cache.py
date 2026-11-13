"""app.core.ttl_cache - the in-process cache of read-mostly page blocks (pagination audit: home page, /gyms cities,
shop facets): single flight, stale-while-revalidate, failures never cached, LRU bound, invalidation on commit."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.core.ttl_cache import TTLCache, clear_on_commit
from app.models import Gym, User
from tests.factories import make_gym, make_owner


async def test_concurrent_misses_compute_once_and_fresh_hits_never_compute():
    cache = TTLCache("test.single_flight", ttl=60)
    gate = asyncio.Event()
    calls = 0

    async def compute() -> int:
        nonlocal calls
        calls += 1
        await gate.wait()
        return calls

    waiting = [asyncio.create_task(cache.get_or_compute("k", compute)) for _ in range(10)]
    await asyncio.sleep(0)
    gate.set()
    assert await asyncio.gather(*waiting) == [1] * 10
    assert calls == 1 == cache.computed  # ten requests at once: one computation (no stampede)
    assert await cache.get_or_compute("k", compute) == 1 and calls == 1  # fresh: straight from memory
    assert "k" in cache and len(cache) == 1


async def test_an_expired_value_is_served_stale_while_one_request_refreshes_it():
    cache = TTLCache("test.stale", ttl=0)  # every value is stale as soon as it is stored
    assert await cache.get_or_compute("k", _value("v1")) == "v1"
    gate = asyncio.Event()

    async def slow() -> str:
        await gate.wait()
        return "v2"

    refresh = asyncio.create_task(cache.get_or_compute("k", slow))
    await asyncio.sleep(0)
    never = _fail("a second refresh must not start")
    assert await cache.get_or_compute("k", never) == "v1"  # the stale value, no second computation
    gate.set()
    assert await refresh == "v2" and cache.peek("k") == "v2"


async def test_failures_are_not_cached_and_waiters_compute_themselves():
    cache = TTLCache("test.failure", ttl=60)
    gate = asyncio.Event()

    async def broken() -> str:
        await gate.wait()
        raise RuntimeError("database down")

    leader = asyncio.create_task(cache.get_or_compute("k", broken))
    await asyncio.sleep(0)
    follower = asyncio.create_task(cache.get_or_compute("k", _value("recovered")))
    await asyncio.sleep(0)
    gate.set()
    with pytest.raises(RuntimeError):
        await leader
    assert await follower == "recovered"  # the follower did not inherit the leader's error
    assert cache.peek("k") == "recovered"

    cancelled = TTLCache("test.cancelled", ttl=60)
    stuck = asyncio.create_task(cancelled.get_or_compute("k", _forever))
    await asyncio.sleep(0)
    waiter = asyncio.create_task(cancelled.get_or_compute("k", _value("mine")))
    await asyncio.sleep(0)
    stuck.cancel()  # e.g. the leading request's client went away
    assert await waiter == "mine"


async def test_a_value_computed_across_an_invalidation_is_not_stored():
    cache = TTLCache("test.generation", ttl=60)
    gate = asyncio.Event()

    async def compute() -> str:
        await gate.wait()
        return "old"

    task = asyncio.create_task(cache.get_or_compute("k", compute))
    await asyncio.sleep(0)
    cache.clear()  # the data changed while it was being read
    gate.set()
    assert await task == "old"  # the caller still gets its answer ...
    assert "k" not in cache  # ... but it is not kept: the next request reads the new data
    cache.invalidate("missing")  # unknown keys are fine


async def test_lru_bound():
    cache = TTLCache("test.lru", ttl=60, maxsize=3)
    for i in range(5):
        await cache.get_or_compute(i, _value(i))
    assert len(cache) == 3 and 0 not in cache and 1 not in cache and 4 in cache


async def test_clear_on_commit_follows_the_watched_models_and_columns(db):
    cache = TTLCache("test.on_commit", ttl=600)
    clear_on_commit([cache], [Gym], changed=lambda gym: _changed(gym, "name"))
    owner = await make_owner(db)
    await cache.get_or_compute("k", _value(1))
    gym = await make_gym(db, owner, name="First Name")  # INSERT + commit
    assert "k" not in cache

    await cache.get_or_compute("k", _value(2))
    gym.tagline = "Not watched"
    await db.commit()
    assert cache.peek("k") == 2  # an UPDATE of an unwatched column keeps the block
    owner.full_name = "Someone Else"  # another model
    await db.commit()
    assert cache.peek("k") == 2

    gym.name = "Second Name"
    await db.flush()
    assert cache.peek("k") == 2  # flushed but not committed: nothing is cleared yet
    await db.commit()
    assert "k" not in cache

    await cache.get_or_compute("k", _value(3))
    from sqlalchemy import update

    await db.execute(update(Gym).where(Gym.id == gym.id).values(online_open=False))  # ORM bulk UPDATE
    await db.commit()
    assert "k" not in cache
    assert (await db.execute(select(User.id).where(User.id == owner.id))).scalar_one() == owner.id


# ------------------------------------------------------------------ helpers
def _value(v):
    async def compute():
        return v

    return compute


def _fail(message: str):
    async def compute():
        raise AssertionError(message)

    return compute


async def _forever() -> str:
    await asyncio.Event().wait()
    return "never"


def _changed(obj, column: str) -> bool:
    from sqlalchemy import inspect

    return inspect(obj).attrs[column].history.has_changes()
