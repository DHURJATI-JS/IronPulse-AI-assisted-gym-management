"""WP-I: exercise library, video catalogue, content sync (+ embeddings) and the video generator."""

from __future__ import annotations

import html
import importlib.util
import json
import math
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.data.exercises import (
    CATEGORIES,
    CUE_MAX_CHARS,
    DIFFICULTIES,
    EXERCISE_BY_SLUG,
    EXERCISES,
    GOALS,
    IMPACTS,
    INTENSITIES,
)
from app.data.videos import CUE_MAX_CHARS as VIDEO_CUE_MAX, VIDEO_BY_SLUG, VIDEO_GOALS, VIDEO_LEVELS, VIDEOS
from app.db import QueryCounter
from app.models import Exercise, Video
from app.models.base import EMBEDDING_DIM
from app.models.enums import FitnessLevel
from app.services import content
from app.services.conditions import CONDITION_TAGS
from tests.conftest import login_as
from tests.factories import make_customer, make_gym, make_owner, make_plans, make_subscription

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "generate_videos.py"
SAFETY_TAGS = set(CONDITION_TAGS) | {"minor"}


@pytest.fixture(scope="module")
def gen() -> ModuleType:
    """The generator script imported as a module (it is a CLI, not a package)."""
    spec = importlib.util.spec_from_file_location("ironpulse_generate_videos", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def no_manifest(tmp_path, monkeypatch) -> Path:
    """Point the content service at an empty folder (catalogue defaults, never the real static/videos)."""
    monkeypatch.setattr(content, "VIDEO_DIR", tmp_path)
    return tmp_path


def _lib_view(spec) -> SimpleNamespace:
    return SimpleNamespace(**spec.as_row())


# =========================================================================== catalogue integrity
def test_exercise_catalogue_integrity():
    assert len(EXERCISES) >= 45
    slugs = [e.slug for e in EXERCISES]
    assert len(slugs) == len(set(slugs)), "duplicate exercise slugs"
    names = [e.name.lower() for e in EXERCISES]
    assert len(names) == len(set(names)), "duplicate exercise names (the safety filter matches by name)"
    assert {e.category for e in EXERCISES} == set(CATEGORIES)
    for e in EXERCISES:
        assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", e.slug) and len(e.slug) <= 80, e.slug
        assert e.category in CATEGORIES and e.difficulty in DIFFICULTIES, e.slug
        assert e.intensity in INTENSITIES and e.impact in IMPACTS, e.slug
        assert e.goals and set(e.goals) <= set(GOALS), e.slug
        assert e.muscles and 0 < len(e.equipment) <= 60 and len(e.name) <= 120, e.slug
        assert len(e.description) >= 40, e.slug
        assert set(e.contraindications) <= SAFETY_TAGS, (e.slug, set(e.contraindications) - SAFETY_TAGS)
        assert len(set(e.contraindications)) == len(e.contraindications), e.slug
        FitnessLevel(e.difficulty)  # maps onto the DB enum


def test_cues_are_short_speakable_sentences():
    """The live voice coach (WP-K) reads cues aloud: complete sentences, <= 90 chars, no symbols or digits."""
    for e in EXERCISES:
        assert 3 <= len(e.cues) <= 5, e.slug
        for cue in e.cues:
            assert len(cue) <= CUE_MAX_CHARS, (e.slug, cue)
            assert cue[0].isupper() and cue.endswith((".", "!", "?")), (e.slug, cue)
            assert not re.search(r"[0-9°×%/&<>#*@=]", cue), (e.slug, cue)


def test_video_catalogue_integrity():
    assert 14 <= len(VIDEOS) <= 16
    slugs = [v.slug for v in VIDEOS]
    assert len(slugs) == len(set(slugs))
    assert "box-breathing" in VIDEO_BY_SLUG  # WP-E1 links stressed members to /me/videos/box-breathing
    groups: dict[str, set[str]] = {}
    for v in VIDEOS:
        assert re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", v.slug), v.slug
        assert v.goal in VIDEO_GOALS and v.level in VIDEO_LEVELS, v.slug
        assert 20 <= v.duration_seconds <= 35, v.slug
        assert v.exercise_slugs and all(s in EXERCISE_BY_SLUG for s in v.exercise_slugs), v.slug
        assert v.cues and all(0 < len(c) <= VIDEO_CUE_MAX for c in v.cues), v.slug
        assert v.title and len(v.title) <= 160 and len(v.description) >= 60 and v.sets_reps, v.slug
        assert v.file_name == f"{v.slug}.mp4" and v.poster_name == f"{v.slug}.jpg"
        groups.setdefault(v.goal, set()).add(v.slug)
    assert groups["calisthenics"] >= {"push-up", "bodyweight-squat", "forearm-plank", "pull-up-progression"}
    assert groups["bulking"] >= {"barbell-back-squat", "barbell-bench-press", "barbell-deadlift", "barbell-overhead-press"}
    assert groups["weight_loss"] >= {"jumping-jacks", "mountain-climbers", "high-knees", "reverse-lunges"}
    assert groups["recovery"] >= {"box-breathing", "spine-hamstring-reset"}
    assert set(groups) == set(VIDEO_GOALS), "every goal tab of the library has at least one session"


def test_exercise_and_video_links_are_consistent():
    for e in EXERCISES:
        if e.video_slug:
            assert e.video_slug in VIDEO_BY_SLUG, e.slug
            assert e.slug in VIDEO_BY_SLUG[e.video_slug].exercise_slugs, (e.slug, e.video_slug)
    for v in VIDEOS:
        for slug in v.exercise_slugs:
            assert EXERCISE_BY_SLUG[slug].video_slug, f"{slug} is demonstrated in {v.slug} but links no video"


def test_contraindications_are_medically_conservative():
    high_impact = [e for e in EXERCISES if e.impact == "high"]
    assert high_impact
    for e in high_impact:
        assert {"knee_injury", "ankle_injury", "pregnancy", "osteoporosis", "arthritis", "obesity"} <= set(e.contraindications), e.slug
    for e in EXERCISES:
        if e.intensity == "high":
            assert {"cardiac", "hypertension"} <= set(e.contraindications), e.slug
        if re.search(r"bench press|bridge|dead bug|hip thrust|supine|lying|superman|hollow", e.name, re.I):
            assert "pregnancy" in e.contraindications, e.slug  # supine / prone work
    for slug in ("barbell-back-squat", "barbell-deadlift", "barbell-bench-press", "barbell-overhead-press"):
        assert {"hypertension", "cardiac", "hernia", "epilepsy", "pregnancy"} <= set(EXERCISE_BY_SLUG[slug].contraindications)
    for slug in ("barbell-back-squat", "barbell-deadlift", "barbell-bench-press"):
        assert "minor" in EXERCISE_BY_SLUG[slug].contraindications  # needs a coach and a spotter
    assert {"copd", "cardiac", "pregnancy"} <= set(EXERCISE_BY_SLUG["box-breathing"].contraindications)  # breath holds
    gentle = [e for e in EXERCISES if e.category in ("mobility", "breathing") and not e.contraindications]
    assert len(gentle) >= 6, "plenty of options remain for members after surgery"


def test_safety_guardrail_and_template_plans_work_with_the_library():
    """The deterministic guardrail (WP-E2) builds complete, safe plans from this library for every goal."""
    safety = pytest.importorskip("app.ai.safety")
    coach = pytest.importorskip("app.services.coach")
    lib = [safety.LibExercise.from_model(_lib_view(e)) for e in EXERCISES]
    for ex in lib:
        for tag in ex.contraindications:
            assert not safety.is_safe(ex, [tag]), (ex.slug, tag)
    cases = [
        ("general_fitness", "beginner", []),
        ("bulking", "intermediate", []),
        ("calisthenics", "advanced", ["wrist_injury"]),
        ("weight_loss", "beginner", ["knee_injury", "hypertension"]),
        ("weight_loss", "intermediate", ["asthma", "obesity"]),
        ("bulking", "beginner", ["pregnancy"]),
        ("calisthenics", "beginner", ["minor"]),
        ("general_fitness", "beginner", ["recent_surgery"]),
    ]
    linked = False
    for goal, level, tags in cases:
        plan, _removed, _notes = coach.template_plan(goal, level, tags, library=lib)
        assert plan["days"], (goal, tags)
        assert all(day["items"] for day in plan["days"]), (goal, tags)
        assert safety.plan_is_safe(plan, tags, lib), (goal, tags)
        if "recent_surgery" not in tags:
            assert sum(len(d["items"]) for d in plan["days"]) >= 4 * len(plan["days"]), (goal, tags)
        linked = linked or any(item.get("video_slug") for d in plan["days"] for item in d["items"])
    assert linked, "template plans link the demonstration videos"


# =========================================================================== content sync
async def test_ensure_content_creates_everything_and_is_idempotent(db, no_manifest):
    first = await content.ensure_content(db)
    await db.commit()
    assert first["exercises"] == {"created": len(EXERCISES), "updated": 0, "unchanged": 0}
    assert first["videos"] == {"created": len(VIDEOS), "updated": 0, "unchanged": 0}
    assert await db.scalar(select(func.count(Exercise.id))) == len(EXERCISES)
    assert await db.scalar(select(func.count(Video.id))) == len(VIDEOS)

    second = await content.ensure_content(db)
    await db.commit()
    assert second == {"exercises": {"created": 0, "updated": 0, "unchanged": len(EXERCISES)},
                      "videos": {"created": 0, "updated": 0, "unchanged": len(VIDEOS)}}

    rows = (await db.execute(select(Exercise).execution_options(populate_existing=True))).scalars().all()
    for row in rows:
        spec = EXERCISE_BY_SLUG[row.slug]
        assert row.cues == list(spec.cues) and row.contraindications == list(spec.contraindications)
        assert row.difficulty == FitnessLevel(spec.difficulty) and row.video_slug == spec.video_slug
        vec = [float(x) for x in row.embedding]
        assert len(vec) == EMBEDDING_DIM == 384
        assert abs(math.sqrt(sum(v * v for v in vec)) - 1.0) < 1e-3  # L2-normalised
    video = (await db.execute(select(Video).where(Video.slug == "box-breathing"))).scalar_one()
    spec = VIDEO_BY_SLUG["box-breathing"]
    assert (video.file_name, video.poster_name, video.duration_seconds) == ("box-breathing.mp4", "box-breathing.jpg",
                                                                          spec.duration_seconds)
    assert video.goal == "recovery" and video.is_active and video.exercise_slugs == ["box-breathing"]


async def test_sync_query_count_is_constant(db, no_manifest):
    """N+1 guard: one SELECT per table + one bulk INSERT - independent of the number of rows."""
    with QueryCounter() as first:
        await content.ensure_content(db)
    await db.commit()
    assert first.count <= 6, first.count
    with QueryCounter() as steady:
        await content.ensure_content(db)
    await db.commit()
    for i in range(30):  # gym-made videos outside the catalogue must not make the sync slower
        db.add(Video(slug=f"gym-session-{i}", title=f"Gym session {i}", goal="general_fitness", level="beginner",
                     description="Custom", duration_seconds=60, file_name=f"gym-session-{i}.mp4", exercise_slugs=[]))
    await db.commit()
    with QueryCounter() as more_rows:
        await content.ensure_content(db)
    await db.commit()
    assert steady.count == more_rows.count <= 3, (steady.count, more_rows.count)


async def test_sync_repairs_drift_but_keeps_admin_choices_and_foreign_rows(db, no_manifest):
    await content.ensure_content(db)
    await db.commit()
    ex = (await db.execute(select(Exercise).where(Exercise.slug == "push-up"))).scalar_one()
    ex.cues, ex.contraindications, ex.embedding = ["tampered"], [], None
    vid = (await db.execute(select(Video).where(Video.slug == "push-up"))).scalar_one()
    vid.title, vid.is_active = "Old title", False
    db.add(Video(slug="gym-special", title="Gym special", goal="general_fitness", level="beginner", description="Ours",
                 duration_seconds=61, file_name="gym-special.mp4", exercise_slugs=[], sort_order=99))
    await db.commit()

    stats = await content.ensure_content(db)
    await db.commit()
    assert stats["exercises"]["updated"] == 1 and stats["videos"]["updated"] == 1
    ex = (await db.execute(select(Exercise).where(Exercise.slug == "push-up").execution_options(populate_existing=True))).scalar_one()
    assert ex.cues == list(EXERCISE_BY_SLUG["push-up"].cues)
    assert "wrist_injury" in ex.contraindications and len(list(ex.embedding)) == EMBEDDING_DIM
    vid = (await db.execute(select(Video).where(Video.slug == "push-up").execution_options(populate_existing=True))).scalar_one()
    assert vid.title == VIDEO_BY_SLUG["push-up"].title and vid.is_active is False  # an admin's hide is kept
    special = (await db.execute(select(Video).where(Video.slug == "gym-special"))).scalar_one()
    assert special.title == "Gym special" and special.duration_seconds == 61


async def test_sync_videos_applies_the_generator_manifest(db, tmp_path, monkeypatch):
    manifest = {"videos": [
        {"slug": "push-up", "file": "push-up.mp4", "poster": "push-up.jpg", "duration": 27.6},
        {"slug": "high-knees", "file": "../../etc/passwd.mp4", "poster": "evil.exe", "duration": "abc"},
        {"slug": "not-in-catalogue", "file": "x.mp4", "duration": 30},
        "junk", {"no": "slug"},
    ]}
    (tmp_path / content.MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(content, "VIDEO_DIR", tmp_path)
    await content.sync_videos(db)
    await db.commit()
    rows = {v.slug: v for v in (await db.execute(select(Video))).scalars()}
    assert rows["push-up"].duration_seconds == 28 and rows["push-up"].file_name == "push-up.mp4"
    assert rows["high-knees"].file_name == "high-knees.mp4"  # unsafe names are ignored
    assert rows["high-knees"].poster_name == "high-knees.jpg"
    assert rows["high-knees"].duration_seconds == VIDEO_BY_SLUG["high-knees"].duration_seconds
    assert "not-in-catalogue" not in rows

    # durations follow a re-rendered manifest
    manifest["videos"][0]["duration"] = 31.2
    (tmp_path / content.MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    stats = await content.sync_videos(db)
    await db.commit()
    assert stats.updated == 1
    row = (await db.execute(select(Video).where(Video.slug == "push-up").execution_options(populate_existing=True))).scalar_one()
    assert row.duration_seconds == 31


def test_manifest_loader_is_defensive(tmp_path):
    assert content.load_manifest(tmp_path / "missing.json") == {}
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert content.load_manifest(broken) == {}
    listed = tmp_path / "list.json"
    listed.write_text(json.dumps([{"slug": "push-up", "file": "p.mp4", "poster": "p.jpg", "duration": 3},
                                  {"slug": "x", "duration": -4}, {"slug": "y", "duration": float("inf")}]), encoding="utf-8")
    assert content.load_manifest(listed) == {"push-up": {"file": "p.mp4", "poster": "p.jpg", "duration": 3}, "x": {}, "y": {}}
    assert content.media_path("push-up.mp4") == content.VIDEO_DIR / "push-up.mp4"
    assert content.media_path("../secret.mp4") is None and content.media_path(None) is None


async def test_search_exercises_uses_the_embeddings(db, no_manifest):
    await content.ensure_content(db)
    await db.commit()
    hits = await content.search_exercises(db, "calm box breathing", limit=3)
    assert hits and hits[0].slug == "box-breathing"
    hits = await content.search_exercises(db, "barbell squat", limit=5, category="strength")
    assert "barbell-back-squat" in [h.slug for h in hits] and all(h.category == "strength" for h in hits)
    assert await content.search_exercises(db, "   ") == []


async def test_synced_videos_play_for_members_even_when_ai_is_down(client, db, ai_down, tmp_path, monkeypatch):
    """End to end through WP-E2's library pages: the catalogue is visible with any membership, AI offline."""
    from app.services import coach as coach_service

    monkeypatch.setattr(content, "VIDEO_DIR", tmp_path)
    monkeypatch.setattr(coach_service, "VIDEO_DIR", tmp_path)
    await content.ensure_content(db)
    await db.commit()
    (tmp_path / "box-breathing.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    (tmp_path / "box-breathing.jpg").write_bytes(b"\xff\xd8\xff\xe0")
    owner = await make_owner(db)
    gym = await make_gym(db, owner)
    plans = {p.code: p for p in await make_plans(db, gym)}
    customer = await make_customer(db)
    await make_subscription(db, customer, plans["online"])
    await login_as(client, customer)

    r = await client.get("/me/videos")
    assert r.status_code == 200
    for v in VIDEOS:
        assert html.escape(v.title) in r.text, v.slug
    r = await client.get("/me/videos/box-breathing")
    assert r.status_code == 200
    assert "/static/videos/box-breathing.mp4" in r.text and 'poster="/static/videos/box-breathing.jpg"' in r.text
    assert html.escape(EXERCISE_BY_SLUG["box-breathing"].cues[0]) in r.text
    r = await client.get("/me/videos/barbell-deadlift")
    assert r.status_code == 200 and "has not been generated yet" in r.text  # file missing -> graceful state


# =========================================================================== video generator
def test_every_video_has_an_animation_program_matching_the_catalogue(gen):
    for v in VIDEOS:
        planned = gen.planned_duration(v)
        assert 20.0 <= planned <= 35.0, (v.slug, planned)
        assert abs(planned - v.duration_seconds) <= 1.0, (v.slug, planned, v.duration_seconds)
        prog = gen.build_program(v)
        assert {seg.exercise for seg in prog.segments} <= set(v.exercise_slugs), v.slug
        covered = set()
        for seg in prog.segments:
            covered |= set(list(range(len(v.cues)))[seg.cues[0]:seg.cues[1]])
        assert covered == set(range(len(v.cues))), f"{v.slug}: every cue line is shown"


def test_animations_keep_bones_rigid_and_stay_above_the_floor(gen):
    bones = (("hip", "knee", gen.THIGH), ("knee", "ankle", gen.SHIN), ("sh", "el", gen.UPPER), ("el", "wr", gen.FORE))
    for v in VIDEOS:
        for seg in gen.build_program(v).segments:
            for i in range(41):
                body = seg.frame3d(seg.duration * i / 40).body
                if body is None:  # box breathing has no mannequin
                    continue
                pts = body.pts
                lows = [pts["head"][1] - gen.HEAD_R, pts["pelvis"][1] - 0.11]
                for s in ("n", "f"):
                    lows += [body.foot_floor(s), pts["knee_" + s][1] - gen.R_KNEE, pts["el_" + s][1] - gen.R_ELBOW,
                             pts["hand_" + s][1] - 0.03]
                    for a, b, length in bones:
                        assert abs(math.dist(pts[f"{a}_{s}"], pts[f"{b}_{s}"]) - length) < 2e-3, (v.slug, seg.name, a, b)
                assert min(lows) > -0.012, (v.slug, seg.name, round(seg.duration * i / 40, 2), min(lows))


def test_frames_and_posters_are_deterministic(gen):
    a = gen.Renderer(VIDEO_BY_SLUG["barbell-deadlift"]).frame(8.0)
    b = gen.Renderer(VIDEO_BY_SLUG["barbell-deadlift"]).frame(8.0)
    assert a.size == (gen.W, gen.H) == (960, 540) and a.tobytes() == b.tobytes()
    breathing = gen.Renderer(VIDEO_BY_SLUG["box-breathing"])
    assert breathing.frame(12.0).size == (960, 540) and breathing.poster().size == (960, 540)


def test_cli_lists_and_rejects_unknown_slugs():
    run = subprocess.run([sys.executable, str(SCRIPT), "--list"], capture_output=True, text=True, timeout=120, cwd=ROOT)
    assert run.returncode == 0 and all(v.slug in run.stdout for v in VIDEOS)
    run = subprocess.run([sys.executable, str(SCRIPT), "--only", "no-such-video", "--out", str(ROOT / "var" / "never")],
                         capture_output=True, text=True, timeout=120, cwd=ROOT)
    assert run.returncode == 2 and "unknown video slug" in run.stderr


def test_quick_render_writes_a_readable_mp4_poster_and_manifest(tmp_path):
    imageio = pytest.importorskip("imageio")
    run = subprocess.run([sys.executable, str(SCRIPT), "--only", "push-up", "--quick", "--jobs", "1", "--out", str(tmp_path)],
                         capture_output=True, text=True, timeout=240, cwd=ROOT)
    assert run.returncode == 0, run.stdout + run.stderr
    mp4 = tmp_path / "push-up.mp4"
    assert mp4.stat().st_size > 10_000
    reader = imageio.get_reader(str(mp4), "ffmpeg")
    try:
        first = reader.get_data(0)
        meta = reader.get_meta_data()
    finally:
        reader.close()
    assert first.shape == (540, 960, 3) and first.mean() > 2
    assert meta.get("codec") == "h264" and tuple(meta.get("size")) == (960, 540)
    assert (tmp_path / "push-up.jpg").read_bytes()[:3] == b"\xff\xd8\xff"
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    entry = next(v for v in manifest["videos"] if v["slug"] == "push-up")
    assert entry["file"] == "push-up.mp4" and entry["poster"] == "push-up.jpg" and entry["duration"] > 0 and entry["quick"]
    assert content.load_manifest(tmp_path / "manifest.json")["push-up"]["file"] == "push-up.mp4"
    assert not list(tmp_path.glob(".*.rendering.mp4"))  # temp file renamed atomically
