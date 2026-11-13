"""Test data for the Locust users: loadtest/accounts.json + loadtest/catalogue.json (both written by the seeder, so
every id, slug and account exists in ironpulse_loadtest), and a per-process account allocator."""

from __future__ import annotations

import itertools
import json
import os
import random
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any

LT_DIR = Path(__file__).resolve().parent.parent


@lru_cache(maxsize=1)
def accounts() -> dict[str, Any]:
    path = LT_DIR / "accounts.json"
    if not path.is_file():
        raise SystemExit(f"{path} is missing - run the seeder first: .venv/bin/python loadtest/seed_loadtest.py")
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def catalogue() -> dict[str, Any]:
    path = LT_DIR / "catalogue.json"
    if not path.is_file():
        raise SystemExit(f"{path} is missing - run the seeder first")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["gyms_with_branches"] = [g for g in data["gyms"] if g["branches"]]
    data["gym_by_id"] = {g["id"]: g for g in data["gyms"]}
    data["products_by_gym"] = {}
    for pid, slug, gid in data["products"]:
        data["products_by_gym"].setdefault(gid, []).append((pid, slug))
    data["store_ids"] = [g["id"] for g in data["gyms"] if g["id"] in data["products_by_gym"]]
    return data


class Allocator:
    """Hands out accounts so that no two simulated users (in any worker process) share one: worker ``i`` of ``n``
    takes indexes i, i + n, i + 2n ... of each pool. Wraps around when the pool is exhausted."""

    def __init__(self) -> None:
        self.environment: Any = None
        self.worker_count = 1
        self._counters: dict[str, itertools.count] = {}
        self._lock = threading.Lock()

    def configure(self, environment: Any) -> None:
        self.environment = environment
        procs = getattr(environment.parsed_options, "processes", None) if environment.parsed_options else None
        if procs in (None, 0, 1):
            self.worker_count = max(1, int(os.environ.get("LT_WORKERS", "1")))
        elif procs == -1:
            self.worker_count = os.cpu_count() or 1
        else:
            self.worker_count = int(procs)

    @property
    def worker_index(self) -> int:
        """A worker learns its index from the master's ack, i.e. after init - so it is read when users spawn."""
        runner = getattr(self.environment, "runner", None)
        return max(0, int(getattr(runner, "worker_index", 0) or 0))

    def take(self, pool_name: str, pool: list[Any]) -> tuple[int, Any]:
        with self._lock:
            counter = self._counters.setdefault(pool_name, itertools.count())
            k = next(counter)
        idx = (self.worker_index + k * self.worker_count) % len(pool)
        return idx, pool[idx]


allocator = Allocator()


def near_point(rng: random.Random) -> tuple[float, float]:
    """A visitor's "Use my location": mostly around the big cities (Bengaluru first), sometimes at a real branch."""
    cat = catalogue()
    if rng.random() < 0.4 and cat["branch_points"]:
        _, lat, lon = rng.choice(cat["branch_points"])
        return round(lat + rng.uniform(-0.03, 0.03), 5), round(lon + rng.uniform(-0.03, 0.03), 5)
    centres = cat["city_centres"]
    weights = [6 if c["name"] == "Bengaluru" else 3 if i < 6 else 1 for i, c in enumerate(centres)]
    c = rng.choices(centres, weights=weights, k=1)[0]
    return round(c["lat"] + rng.uniform(-0.08, 0.08), 5), round(c["lon"] + rng.uniform(-0.08, 0.08), 5)
