"""Pre-recorded demonstration videos (WP-I) - the catalogue behind ``/me/videos``.

The MP4s are rendered programmatically by ``scripts/generate_videos.py`` (a stylised mannequin driven by
keyframed joint angles - no external assets, no AI provider needed), so the library keeps working when
every AI provider is down. ``app.services.content.sync_videos`` upserts these entries into the ``videos``
table; ``file_name`` / ``poster_name`` / ``duration_seconds`` are taken from the generator's
``app/static/videos/manifest.json`` when it exists.

Every video belongs to one goal group (``calisthenics``, ``bulking``, ``weight_loss``,
``general_fitness``, ``recovery``) and lists the library exercises it demonstrates (``exercise_slugs``).
``cues`` are the coaching lines shown on screen, in order.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

VIDEO_GOALS: tuple[str, ...] = ("calisthenics", "bulking", "weight_loss", "general_fitness", "recovery")
VIDEO_LEVELS: tuple[str, ...] = ("beginner", "intermediate", "advanced")
CUE_MAX_CHARS = 64  # one line of the on-screen cue pill


@dataclass(frozen=True, slots=True)
class VideoSpec:
    slug: str
    title: str
    goal: str
    level: str
    description: str
    duration_seconds: int  # planned length; the generator's manifest.json is authoritative when present
    exercise_slugs: tuple[str, ...]
    sets_reps: str
    cues: tuple[str, ...]
    sort_order: int = 0

    @property
    def file_name(self) -> str:
        return f"{self.slug}.mp4"

    @property
    def poster_name(self) -> str:
        return f"{self.slug}.jpg"

    def as_row(self) -> dict[str, Any]:
        """Default column values for ``app.models.Video`` (before manifest overrides)."""
        return {
            "slug": self.slug,
            "title": self.title,
            "goal": self.goal,
            "level": self.level,
            "description": self.description,
            "duration_seconds": self.duration_seconds,
            "file_name": self.file_name,
            "poster_name": self.poster_name,
            "exercise_slugs": list(self.exercise_slugs),
            "sort_order": self.sort_order,
        }


VIDEOS: tuple[VideoSpec, ...] = (
    # ------------------------------------------------------------------ calisthenics
    VideoSpec(
        slug="push-up", title="Push-up fundamentals", goal="calisthenics", level="beginner", sort_order=10,
        description="Build pressing strength with a strict push-up: a rigid plank from head to heels, elbows at about "
                    "45 degrees and the chest lowered under control. Use a bench to make it easier.",
        duration_seconds=28, exercise_slugs=("push-up",), sets_reps="3 sets × 6-12 reps · rest 60-90 s",
        cues=("Hands under your shoulders, body in one straight line",
              "Brace your stomach and squeeze your glutes",
              "Lower with control, elbows at about 45°",
              "Chest just above the floor, then press it away",
              "Breathe in on the way down, out as you push",
              "Too hard? Put your hands on a bench")),
    VideoSpec(
        slug="bodyweight-squat", title="Bodyweight squat", goal="calisthenics", level="beginner", sort_order=20,
        description="The foundation of lower-body strength: sit back and down between your hips, keep the whole foot "
                    "planted and stand tall with control. Reaching the arms forward helps you balance.",
        duration_seconds=27, exercise_slugs=("bodyweight-squat",), sets_reps="3 sets × 10-15 reps · rest 60 s",
        cues=("Feet shoulder-width apart, toes slightly out",
              "Reach your arms forward as you sit back and down",
              "Knees track in line with your toes",
              "Heels down, chest proud",
              "Only go as deep as you can control",
              "Push through your whole foot to stand tall")),
    VideoSpec(
        slug="forearm-plank", title="Forearm plank hold", goal="calisthenics", level="beginner", sort_order=30,
        description="A calm, controlled plank on the forearms: one long line from head to heels, glutes and stomach "
                    "engaged and steady breathing the whole time. Stop the hold when your hips start to sag.",
        duration_seconds=28, exercise_slugs=("forearm-plank",), sets_reps="3 holds × 20-40 s · rest 45 s",
        cues=("Elbows under your shoulders, forearms parallel",
              "One long line from head to heels",
              "Squeeze your glutes and brace your stomach",
              "Keep breathing - never hold your breath",
              "Stop when your hips start to sag")),
    VideoSpec(
        slug="pull-up-progression", title="Pulling progression: row to pull-up", goal="calisthenics",
        level="intermediate", sort_order=40,
        description="Start with inverted rows under a waist-height bar, then progress to strict pull-ups. Both teach you "
                    "to pull with your back, squeeze the shoulder blades and control every rep.",
        duration_seconds=27, exercise_slugs=("inverted-row", "pull-up"),
        sets_reps="Rows 3 × 8-12 · Pull-ups 3 × 3-6 · rest 90 s",
        cues=("Rows: heels down, body straight like a plank",
              "Pull your chest to the bar, squeeze your shoulder blades",
              "Lower slowly until your arms are straight",
              "Pull-ups: start from a still, active hang",
              "Drive your elbows down until your chin clears the bar",
              "No swinging - control every rep")),
    # ------------------------------------------------------------------ bulking
    VideoSpec(
        slug="barbell-back-squat", title="Barbell back squat", goal="bulking", level="intermediate", sort_order=10,
        description="The cornerstone lift for leg size and strength. Brace, sit down between your hips with the bar over "
                    "mid-foot and drive up with hips and chest rising together. Always squat inside a rack with "
                    "safety bars.",
        duration_seconds=26, exercise_slugs=("barbell-back-squat",), sets_reps="4 sets × 6-8 reps · rest 2-3 min",
        cues=("Bar on your upper back, hands just outside your shoulders",
              "Brace your stomach before every rep",
              "Sit down between your hips, knees over your toes",
              "Keep the bar over the middle of your foot",
              "Hips and chest rise together",
              "Use the safety bars and leave 2 reps in reserve")),
    VideoSpec(
        slug="barbell-bench-press", title="Barbell bench press", goal="bulking", level="intermediate", sort_order=20,
        description="Press with a stable base: shoulder blades squeezed into the bench, feet planted, the bar lowered to "
                    "the mid-chest and pressed back over the shoulders. Always use a spotter or safety arms.",
        duration_seconds=25, exercise_slugs=("barbell-bench-press",), sets_reps="4 sets × 6-10 reps · rest 2 min",
        cues=("Eyes under the bar, feet flat on the floor",
              "Squeeze your shoulder blades into the bench",
              "Lower the bar to your mid-chest with control",
              "Forearms vertical at the bottom",
              "Press up and slightly back over your shoulders",
              "Always use a spotter or safety arms")),
    VideoSpec(
        slug="barbell-deadlift", title="Deadlift: master the hip hinge", goal="bulking", level="intermediate",
        sort_order=30,
        description="Learn the hip hinge with the conventional deadlift: bar over mid-foot, flat back, push the floor "
                    "away and keep the bar close to your legs. Stand tall at the top without leaning back.",
        duration_seconds=26, exercise_slugs=("barbell-deadlift",), sets_reps="3 sets × 4-6 reps · rest 2-3 min",
        cues=("Bar over mid-foot, shins close to the bar",
              "Hinge at the hips, grip just outside your knees",
              "Flat back and chest up - take the slack out",
              "Push the floor away, keep the bar close",
              "Stand tall - squeeze your glutes, don't lean back",
              "Lower by pushing your hips back first")),
    VideoSpec(
        slug="barbell-overhead-press", title="Standing overhead press", goal="bulking", level="intermediate",
        sort_order=40,
        description="Strong shoulders from a standing press: glutes squeezed, ribs down, the bar travelling in a "
                    "straight line while your head moves out of the way and back through at the top.",
        duration_seconds=27, exercise_slugs=("barbell-overhead-press",), sets_reps="4 sets × 5-8 reps · rest 2 min",
        cues=("Bar on your front shoulders, grip just outside",
              "Squeeze your glutes and brace - ribs down",
              "Press straight up, head back slightly",
              "Push your head through once the bar passes",
              "Lock out over mid-foot, biceps by your ears",
              "Lower under control to your shoulders")),
    # ------------------------------------------------------------------ weight loss
    VideoSpec(
        slug="jumping-jacks", title="Jumping jacks", goal="weight_loss", level="beginner", sort_order=10,
        description="A classic full-body cardio drill to raise your heart rate. Land softly with bent knees and keep a "
                    "steady rhythm - step one foot out at a time for a low-impact version.",
        duration_seconds=26, exercise_slugs=("jumping-jacks",), sets_reps="3 rounds × 30-45 s · rest 30 s",
        cues=("Stand tall, feet together, arms by your sides",
              "Jump your feet out as your arms sweep overhead",
              "Land softly with a slight bend in your knees",
              "Keep a steady rhythm and keep breathing",
              "Low impact: step one foot out at a time")),
    VideoSpec(
        slug="mountain-climbers", title="Mountain climbers", goal="weight_loss", level="intermediate", sort_order=20,
        description="Conditioning from a strong high plank: drive the knees towards your chest one at a time while the "
                    "hips stay level. Slow the pace down whenever your form starts to slip.",
        duration_seconds=26, exercise_slugs=("mountain-climbers",), sets_reps="3 rounds × 20-30 s · rest 40 s",
        cues=("Strong plank, hands under your shoulders",
              "Drive one knee towards your chest",
              "Switch legs with a smooth rhythm",
              "Hips level - don't let them bounce",
              "Slow it down to keep good form")),
    VideoSpec(
        slug="high-knees", title="High knees", goal="weight_loss", level="intermediate", sort_order=30,
        description="Run on the spot with the knees driving towards hip height and the arms pumping in opposition. "
                    "Light, quick contacts keep it springy; march for a low-impact version.",
        duration_seconds=26, exercise_slugs=("high-knees",), sets_reps="3 rounds × 20-30 s · rest 40 s",
        cues=("Stand tall and run on the spot",
              "Lift your knees towards hip height",
              "Pump your arms in opposition",
              "Land lightly on the balls of your feet",
              "Low impact: march with high knees")),
    VideoSpec(
        slug="reverse-lunges", title="Reverse lunges", goal="weight_loss", level="beginner", sort_order=40,
        description="Alternating reverse lunges build the legs and lift your heart rate while staying kind to the "
                    "knees. Step back, lower under control and drive through the front heel.",
        duration_seconds=27, exercise_slugs=("reverse-lunge",), sets_reps="3 sets × 8-10 reps per leg · rest 60 s",
        cues=("Stand tall, feet hip-width apart",
              "Step back and lower your back knee",
              "Front knee stays over your ankle",
              "Push through the front heel to return",
              "Alternate legs, torso upright",
              "Hold a wall or chair for balance if needed")),
    # ------------------------------------------------------------------ general fitness
    VideoSpec(
        slug="full-body-starter", title="Full-body starter flow", goal="general_fitness", level="beginner",
        sort_order=10,
        description="A short, joint-friendly circuit for every level: bodyweight squats, glute bridges and bird dogs "
                    "train the legs, hips and core with control. Repeat the circuit two or three times.",
        duration_seconds=34, exercise_slugs=("bodyweight-squat", "glute-bridge", "bird-dog"),
        sets_reps="2-3 rounds × 8-10 reps each · rest 60 s",
        cues=("Squat: sit back and down, then stand tall",
              "Keep your chest proud and heels down",
              "Bridge: drive through your heels",
              "Squeeze your glutes at the top",
              "Bird dog: reach opposite arm and leg",
              "Keep your hips level and move slowly")),
    # ------------------------------------------------------------------ recovery
    VideoSpec(
        slug="box-breathing", title="Box breathing: 4-4-4-4", goal="recovery", level="beginner", sort_order=10,
        description="A calming breathing pattern for stressful moments and post-workout recovery: breathe in, hold, "
                    "breathe out and hold for four seconds each. Two guided rounds - keep going for a few minutes.",
        duration_seconds=35, exercise_slugs=("box-breathing",), sets_reps="2-4 minutes · seated or lying down",
        cues=("Sit tall and relax your shoulders",
              "Breathe in through your nose",
              "Hold gently",
              "Breathe out slowly",
              "Hold - and begin again",
              "Skip the holds if you feel light-headed")),
    VideoSpec(
        slug="spine-hamstring-reset", title="Spine & hamstring reset", goal="recovery", level="beginner", sort_order=20,
        description="Ease stiffness after sitting or training: slow cat-cow to mobilise the whole spine, then a "
                    "supported hamstring stretch held with calm breathing. Move only in a comfortable range.",
        duration_seconds=31, exercise_slugs=("cat-cow", "hamstring-stretch"),
        sets_reps="Cat-cow 6-8 slow reps · stretch 2 × 30 s per leg",
        cues=("Hands under shoulders, knees under hips",
              "Breathe out - round your back to the ceiling",
              "Breathe in - let your chest sink, look forward",
              "Stretch: heel on a low step, toes up",
              "Hinge from your hips with a long spine",
              "Hold a gentle stretch - never bounce")),
)

VIDEO_BY_SLUG: dict[str, VideoSpec] = {v.slug: v for v in VIDEOS}


def get_video(slug: str) -> VideoSpec | None:
    return VIDEO_BY_SLUG.get(slug)


__all__ = ["CUE_MAX_CHARS", "VIDEOS", "VIDEO_BY_SLUG", "VIDEO_GOALS", "VIDEO_LEVELS", "VideoSpec", "get_video"]
