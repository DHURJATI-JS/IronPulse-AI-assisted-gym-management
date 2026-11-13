"""Content sync (WP-I): exercise library + pre-recorded video catalogue -> database.

* ``sync_exercises(db)`` upserts ``app.data.exercises.EXERCISES`` by slug, with a 384-dim embedding of
  name + description + cues + goals (+ muscles, category) for semantic search (``search_exercises``).
* ``sync_videos(db)`` upserts ``app.data.videos.VIDEOS``; ``file_name`` / ``poster_name`` / ``duration_seconds``
  come from the generator's ``app/static/videos/manifest.json`` when it exists (see scripts/generate_videos.py).
* ``ensure_content(db)`` runs both. Idempotent and cheap: one SELECT per table, a single race-safe
  ``INSERT ... ON CONFLICT DO NOTHING`` for new rows and UPDATEs only for rows that really changed.

Rows that are not in the catalogue (e.g. videos added by a gym) are never touched, and an admin's
``Video.is_active = False`` is kept. The caller commits (``session_scope`` / request handler / seed).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
from pgvector.sqlalchemy import Vector
from sqlalchemy import String, cast, literal, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer
from sqlalchemy.sql.elements import ColumnElement

from app.ai.embeddings import embed
from app.config import APP_DIR
from app.data.exercises import EXERCISES, ExerciseSpec
from app.data.videos import VIDEOS, VideoSpec
from app.models import Exercise, Video
from app.models.base import EMBEDDING_DIM
from app.models.enums import FitnessLevel

log = logging.getLogger("ironpulse.content")

VIDEO_DIR = APP_DIR / "static" / "videos"
MANIFEST_NAME = "manifest.json"
_SAFE_MEDIA = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,150}\.(mp4|webm|m4v|jpg|jpeg|png|webp)$")
_EXERCISE_FIELDS = ("name", "category", "goals", "muscles", "equipment", "difficulty", "intensity", "impact",
                    "description", "cues", "contraindications", "video_slug")
_VIDEO_FIELDS = ("title", "goal", "level", "description", "duration_seconds", "file_name", "poster_name",
                 "exercise_slugs", "sort_order")


@dataclass(slots=True)
class SyncStats:
    created: int = 0
    updated: int = 0
    unchanged: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.created or self.updated)

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


# --------------------------------------------------------------------------- manifest
def load_manifest(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """``{slug: {"file", "poster", "duration"}}`` from the generator's manifest.json (``{}`` when missing/invalid).

    Accepts ``{"videos": [...]}`` or a bare list. Unsafe file names are dropped (never trust paths blindly)."""
    path = path or (VIDEO_DIR / MANIFEST_NAME)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("ignoring unreadable video manifest %s: %s", path, exc)
        return {}
    items = raw.get("videos") if isinstance(raw, dict) else raw
    out: dict[str, dict[str, Any]] = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("slug"), str):
            continue
        entry: dict[str, Any] = {}
        for key in ("file", "poster"):
            name = item.get(key)
            if isinstance(name, str) and _SAFE_MEDIA.match(name):
                entry[key] = name
        try:
            duration = float(item.get("duration"))
        except (TypeError, ValueError):
            duration = math.nan
        if math.isfinite(duration) and duration > 0:
            entry["duration"] = max(1, min(3600, int(round(duration))))
        out[item["slug"]] = entry
    return out


def media_path(name: str | None) -> Path | None:
    """Path of a generated video/poster in static/videos (None for unsafe names)."""
    if not name or not _SAFE_MEDIA.match(name):
        return None
    return VIDEO_DIR / name


# --------------------------------------------------------------------------- helpers
def vector_sql(vec: list[float]) -> ColumnElement[Any]:
    """Bind an embedding as ``CAST('[...]' AS vector)``: works with and without the pgvector asyncpg binary
    codec that ``app.db`` registers (the SQLAlchemy type would bind text, the codec expects a sequence)."""
    text = "[" + ",".join(format(float(x), ".7g") for x in vec) + "]"
    return cast(literal(text, String), Vector(EMBEDDING_DIM))


def _same(current: Any, wanted: Any) -> bool:
    if isinstance(current, (list, tuple)) and isinstance(wanted, (list, tuple)):
        return list(current) == list(wanted)
    return current == wanted


def _same_vector(current: Any, wanted: list[float]) -> bool:
    if current is None:
        return False
    try:
        arr = np.asarray(current, dtype=np.float64)
    except (TypeError, ValueError):
        return False
    return arr.shape == (EMBEDDING_DIM,) and bool(np.allclose(arr, wanted, atol=1e-5))


def exercise_values(spec: ExerciseSpec) -> dict[str, Any]:
    values = spec.as_row()
    values["difficulty"] = FitnessLevel(spec.difficulty)
    return values


def video_values(spec: VideoSpec, manifest_entry: dict[str, Any] | None = None) -> dict[str, Any]:
    values = spec.as_row()
    entry = manifest_entry or {}
    if entry.get("file"):
        values["file_name"] = entry["file"]
    if entry.get("poster"):
        values["poster_name"] = entry["poster"]
    if entry.get("duration"):
        values["duration_seconds"] = int(entry["duration"])
    return values


# --------------------------------------------------------------------------- sync
async def sync_exercises(db: AsyncSession) -> SyncStats:
    """Upsert the exercise library by slug (+ embeddings). Flushes; the caller commits."""
    await db.flush()  # persist the caller's pending work first (populate_existing below reloads these rows)
    stats = SyncStats()
    slugs = [spec.slug for spec in EXERCISES]
    stmt = select(Exercise).where(Exercise.slug.in_(slugs)).execution_options(populate_existing=True)
    existing = {row.slug: row for row in (await db.execute(stmt)).scalars()}
    new_rows: list[dict[str, Any]] = []
    refresh: list[Exercise] = []
    for spec in EXERCISES:
        values = exercise_values(spec)
        vector = embed(spec.embedding_text)
        row = existing.get(spec.slug)
        if row is None:
            new_rows.append({**values, "embedding": vector_sql(vector)})
            continue
        changed = False
        for field in _EXERCISE_FIELDS:
            if not _same(getattr(row, field), values[field]):
                setattr(row, field, values[field])
                changed = True
        if not _same_vector(row.embedding, vector):
            row.embedding = vector_sql(vector)
            refresh.append(row)
            changed = True
        if changed:
            stats.updated += 1
        else:
            stats.unchanged += 1
    if new_rows:
        insert = pg_insert(Exercise).values(new_rows).on_conflict_do_nothing(index_elements=[Exercise.slug])
        stats.created = len((await db.execute(insert.returning(Exercise.id))).all())
    await db.flush()
    for row in refresh:  # a SQL-expression assignment is expired by the flush; reload it (no lazy IO later)
        await db.refresh(row, attribute_names=["embedding"])
    return stats


async def sync_videos(db: AsyncSession, *, manifest: dict[str, dict[str, Any]] | None = None) -> SyncStats:
    """Upsert the video catalogue by slug, applying manifest.json file names/durations. Flushes; the caller commits."""
    if manifest is None:
        manifest = await asyncio.to_thread(load_manifest)
    await db.flush()
    stats = SyncStats()
    slugs = [spec.slug for spec in VIDEOS]
    stmt = select(Video).where(Video.slug.in_(slugs)).execution_options(populate_existing=True)
    existing = {row.slug: row for row in (await db.execute(stmt)).scalars()}
    new_rows: list[dict[str, Any]] = []
    for spec in VIDEOS:
        values = video_values(spec, manifest.get(spec.slug))
        row = existing.get(spec.slug)
        if row is None:
            new_rows.append({**values, "is_active": True})
            continue
        changed = False
        for field in _VIDEO_FIELDS:
            if not _same(getattr(row, field), values[field]):
                setattr(row, field, values[field])
                changed = True
        if changed:
            stats.updated += 1
        else:
            stats.unchanged += 1
    if new_rows:
        insert = pg_insert(Video).values(new_rows).on_conflict_do_nothing(index_elements=[Video.slug])
        stats.created = len((await db.execute(insert.returning(Video.id))).all())
    await db.flush()
    return stats


async def ensure_content(db: AsyncSession) -> dict[str, dict[str, int]]:
    """Exercise library + video catalogue in the database (idempotent). Flushes; the caller commits."""
    exercises = await sync_exercises(db)
    videos = await sync_videos(db)
    if exercises.changed or videos.changed:
        log.info("content synced: exercises %s, videos %s", exercises.as_dict(), videos.as_dict())
    return {"exercises": exercises.as_dict(), "videos": videos.as_dict()}


# --------------------------------------------------------------------------- search
async def search_exercises(db: AsyncSession, query: str, *, limit: int = 8, category: str | None = None) -> list[Exercise]:
    """Exercises closest to ``query`` (cosine distance on the local embeddings). Embedding column deferred."""
    text = (query or "").strip()[:300]
    if not text:
        return []
    vector = embed(text)
    if not any(vector):
        return []
    stmt = (
        select(Exercise)
        .options(defer(Exercise.embedding))
        .where(Exercise.embedding.is_not(None))
        .order_by(Exercise.embedding.cosine_distance(vector_sql(vector)), Exercise.slug)
        .limit(max(1, min(int(limit), 50)))
    )
    if category:
        stmt = stmt.where(Exercise.category == category)
    return list((await db.execute(stmt)).scalars().all())


__all__ = ["MANIFEST_NAME", "SyncStats", "VIDEO_DIR", "ensure_content", "exercise_values", "load_manifest", "media_path",
           "search_exercises", "sync_exercises", "sync_videos", "vector_sql", "video_values"]
