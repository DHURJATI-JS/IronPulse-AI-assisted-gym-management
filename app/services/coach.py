"""Coaching service (WP-E2): customer context, exercise library, template plans and plan generation.

* ``template_plan(goal, level, tags)`` builds a deterministic weekly plan from the exercise library
  (the seeded ``exercises`` table, or the built-in fallback list below when the table is empty) and
  runs it through the safety filter - this is what customers get without AI or when AI is down.
* ``generate_plan`` tries the AI coach (``app.ai.coach``) when the membership includes AI, then
  always re-validates the result with ``app.ai.safety.filter_plan`` before storing a ``WorkoutPlan``.
* Plans are built in the background (docs/ai-guard-and-queue.md section 6): ``queue_plan`` (POST
  /me/coach/generate) judges the click with the AI guard and queues one ``coach_plan`` job per member, with the
  click's admission (``Admission.to_payload()``) in its payload; ``plan_job`` runs ``generate_plan``, settles that
  admission (refunded when no AI plan comes of the click) and is paused while the member is away (resumed when they
  are back).
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.ai.errors import AICancelled, AIRejected, AIUnavailableError
from app.ai.owner import AIOwner, current_owner
from app.ai.safety import LibExercise, filter_plan, is_safe, normalize_tags
from app.config import APP_DIR
from app.core.utils import utcnow
from app.models import CustomerProfile, Exercise, HealthCondition, Job, MetricEntry, User, Video, WorkoutPlan
from app.models.enums import FitnessLevel, Goal, JobStatus, NotificationCategory, Severity
from app.services.jobs import current_run, enqueue, job_handler, set_result_event

log = logging.getLogger("ironpulse.coach")

GOAL_LABELS = {
    "general_fitness": "General fitness",
    "weight_loss": "Weight loss",
    "bulking": "Muscle building",
    "calisthenics": "Calisthenics",
    "recovery": "Recovery & mobility",
}
LEVEL_LABELS = {"beginner": "Beginner", "intermediate": "Intermediate", "advanced": "Advanced"}
VIDEO_DIR = APP_DIR / "static" / "videos"
_SAFE_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,150}\.(mp4|webm|m4v|jpg|jpeg|png|webp)$")

_ALL = ("general_fitness", "weight_loss", "bulking", "calisthenics")


def _ex(slug: str, name: str, category: str, goals: tuple[str, ...], muscles: tuple[str, ...], *, equipment: str = "none",
        difficulty: str = "beginner", intensity: str = "moderate", impact: str = "low", contra: tuple[str, ...] = (),
        description: str = "", cues: tuple[str, ...] = ()) -> LibExercise:
    return LibExercise(slug=slug, name=name, category=category, goals=goals, muscles=muscles, equipment=equipment,
                       difficulty=difficulty, intensity=intensity, impact=impact, contraindications=contra,
                       description=description, cues=cues)


# Built-in fallback library: used when the ``exercises`` table has not been seeded yet.
BUILTIN_LIBRARY: tuple[LibExercise, ...] = (
    # ---- mobility
    _ex("cat-cow", "Cat-cow", "mobility", _ALL + ("recovery",), ("spine", "core"), equipment="mat", intensity="low",
        description="Alternate arching and rounding the spine on hands and knees.", cues=("Move slowly with your breath",)),
    _ex("worlds-greatest-stretch", "World's greatest stretch", "mobility", _ALL + ("recovery",), ("hips", "hamstrings", "thoracic"),
        intensity="low", description="Lunge, rotate the chest towards the front knee and reach up.", cues=("Keep the back knee soft",)),
    _ex("hip-flexor-stretch", "Half-kneeling hip flexor stretch", "mobility", _ALL + ("recovery",), ("hips", "quads"),
        equipment="mat", intensity="low", description="Tuck the pelvis and shift forward gently.", cues=("Squeeze the back glute",)),
    _ex("open-book-rotation", "Open-book thoracic rotation", "mobility", _ALL + ("recovery",), ("thoracic", "spine", "shoulders"),
        equipment="mat", intensity="low", description="Side-lying rotation that opens the upper back.", cues=("Follow your hand with your eyes",)),
    _ex("shoulder-circles", "Shoulder circles", "mobility", _ALL + ("recovery",), ("shoulders",), intensity="low",
        description="Slow controlled circles to warm up the shoulders.", cues=("Stay below any pain",)),
    _ex("ankle-mobility", "Ankle mobility rocks", "mobility", _ALL + ("recovery",), ("ankles", "calves"), intensity="low",
        description="Rock the knee over the toes to open the ankle.", cues=("Heel stays down",)),
    _ex("hamstring-stretch", "Supported hamstring stretch", "mobility", _ALL + ("recovery",), ("hamstrings",), intensity="low",
        description="Heel on a low step, hinge from the hips with a long spine.", cues=("Do not round your back",)),
    _ex("wrist-mobility", "Wrist mobility circles", "mobility", ("calisthenics", "general_fitness", "recovery"), ("wrists", "forearms"),
        intensity="low", description="Gentle wrist circles and rocks before hand-supported work.", cues=("Pain-free range only",)),
    # ---- breathing
    _ex("diaphragmatic-breathing", "Diaphragmatic breathing", "breathing", _ALL + ("recovery",), ("diaphragm", "core"),
        equipment="mat", intensity="low", description="Slow belly breathing to down-regulate after training.",
        cues=("Inhale 4 s through the nose, exhale 6 s",)),
    _ex("recovery-breathing", "Recovery breathing walk", "breathing", _ALL + ("recovery",), ("diaphragm",), intensity="low",
        description="Easy walking while lengthening each exhale.", cues=("Long, relaxed exhales",)),
    # ---- cardio
    _ex("brisk-walk", "Brisk incline walk", "cardio", _ALL + ("recovery",), ("legs", "heart"), equipment="treadmill",
        intensity="low", description="Walk briskly on a slight incline.", cues=("You should still be able to talk",)),
    _ex("stationary-bike", "Stationary bike", "cardio", _ALL, ("legs", "heart"), equipment="bike",
        description="Steady cycling at a conversational pace.", cues=("Set the saddle at hip height",)),
    _ex("rowing-machine", "Rowing machine", "cardio", _ALL, ("back", "legs", "heart"), equipment="rower",
        description="Legs, then hips, then arms - reverse on the way back.", cues=("Push with the legs first",)),
    _ex("elliptical", "Elliptical trainer", "cardio", _ALL, ("legs", "heart"), equipment="elliptical",
        description="Low-impact steady cardio.", cues=("Stand tall, light grip",)),
    _ex("easy-jog", "Easy jog", "cardio", ("weight_loss", "general_fitness"), ("legs", "heart"), impact="high",
        contra=("knee_injury", "ankle_injury", "obesity", "pregnancy", "osteoporosis", "arthritis"),
        description="Relaxed running at a conversational pace.", cues=("Short, quick steps",)),
    _ex("sprint-intervals", "Sprint intervals", "cardio", ("weight_loss",), ("legs", "heart"), difficulty="intermediate",
        intensity="high", impact="high",
        contra=("asthma", "copd", "cardiac", "hypertension", "pregnancy", "knee_injury", "ankle_injury", "anemia", "obesity", "minor"),
        description="Short fast efforts with full recovery.", cues=("Warm up for 10 minutes first",)),
    _ex("jump-rope", "Jump rope", "cardio", ("weight_loss", "calisthenics"), ("calves", "heart"), equipment="rope",
        intensity="high", impact="high",
        contra=("knee_injury", "ankle_injury", "pregnancy", "osteoporosis", "arthritis", "obesity"),
        description="Light, rhythmic skipping.", cues=("Stay on the balls of your feet",)),
    # ---- calisthenics
    _ex("push-up", "Push-up", "calisthenics", ("calisthenics", "general_fitness", "bulking", "weight_loss"),
        ("chest", "triceps", "shoulders"), contra=("wrist_injury",), description="Body in one line, lower the chest to the floor.",
        cues=("Elbows at 45 degrees",)),
    _ex("incline-push-up", "Incline push-up", "calisthenics", _ALL, ("chest", "triceps", "shoulders"), equipment="bench",
        contra=("wrist_injury",), description="Hands on a bench to make the push-up easier.", cues=("Brace your core",)),
    _ex("inverted-row", "Inverted row", "calisthenics", _ALL, ("back", "lats", "biceps"), equipment="bar",
        description="Pull the chest to a waist-height bar.", cues=("Squeeze the shoulder blades",)),
    _ex("pull-up", "Pull-up", "calisthenics", ("calisthenics", "bulking"), ("back", "lats", "biceps"), equipment="bar",
        difficulty="intermediate", description="Strict pull-up from a dead hang.", cues=("No swinging",)),
    _ex("bench-dip", "Bench dip", "calisthenics", ("calisthenics", "bulking"), ("triceps", "chest"), equipment="bench",
        contra=("shoulder_injury", "wrist_injury"), description="Lower and press from a bench behind you.", cues=("Shoulders down",)),
    _ex("forearm-plank", "Forearm plank", "calisthenics", _ALL, ("core",), equipment="mat",
        description="Hold a straight line on your forearms.", cues=("Breathe normally - never hold your breath",)),
    _ex("side-plank", "Side plank", "calisthenics", _ALL, ("core", "obliques"), equipment="mat",
        description="Stack the hips and hold on one forearm.", cues=("Hips high",)),
    _ex("bird-dog", "Bird dog", "calisthenics", _ALL + ("recovery",), ("core", "glutes", "back"), equipment="mat", intensity="low",
        description="Reach opposite arm and leg from all fours.", cues=("Keep the hips level",)),
    _ex("dead-bug", "Dead bug", "calisthenics", _ALL, ("core",), equipment="mat", intensity="low", contra=("pregnancy",),
        description="Lower opposite arm and leg while the back stays flat.", cues=("Low back stays on the floor",)),
    _ex("glute-bridge", "Glute bridge", "calisthenics", _ALL, ("glutes", "hamstrings"), equipment="mat", intensity="low",
        contra=("pregnancy",), description="Drive the hips up from the floor.", cues=("Squeeze at the top",)),
    _ex("bodyweight-squat", "Bodyweight squat", "calisthenics", _ALL, ("quads", "glutes"),
        description="Sit back and down to a comfortable depth.", cues=("Knees track over toes",)),
    _ex("box-squat", "Box squat", "calisthenics", _ALL, ("quads", "glutes"), equipment="box", intensity="low",
        description="Squat to a box or bench for a controlled depth.", cues=("Touch, do not drop",)),
    _ex("reverse-lunge", "Reverse lunge", "calisthenics", _ALL, ("quads", "glutes"), contra=("knee_injury",),
        description="Step back and lower under control.", cues=("Front knee stays stable",)),
    _ex("step-up", "Step-up", "calisthenics", _ALL, ("quads", "glutes"), equipment="box",
        description="Step onto a low box and stand tall.", cues=("Push through the whole foot",)),
    _ex("superman-hold", "Superman lift", "calisthenics", ("general_fitness", "calisthenics"), ("back", "glutes"), equipment="mat",
        intensity="low", contra=("pregnancy",), description="Lift arms and legs gently from lying face down.", cues=("Neck long",)),
    _ex("hollow-body-hold", "Hollow body hold", "calisthenics", ("calisthenics",), ("core",), equipment="mat",
        difficulty="intermediate", contra=("pregnancy", "lower_back", "hernia"), description="Press the low back down and hold.",
        cues=("Shorten the lever if your back lifts",)),
    _ex("burpee", "Burpee", "calisthenics", ("weight_loss", "calisthenics"), ("full body",), intensity="high", impact="high",
        contra=("knee_injury", "wrist_injury", "pregnancy", "hypertension", "cardiac", "vertigo", "osteoporosis", "obesity"),
        description="Squat, plank, return and jump.", cues=("Land softly",)),
    _ex("jump-squat", "Jump squat", "calisthenics", ("weight_loss", "calisthenics"), ("quads", "glutes"), intensity="high",
        impact="high", contra=("knee_injury", "ankle_injury", "pregnancy", "osteoporosis", "arthritis", "obesity"),
        description="Explosive squat jump.", cues=("Land softly",)),
    _ex("pike-push-up", "Pike push-up", "calisthenics", ("calisthenics",), ("shoulders", "triceps"), difficulty="intermediate",
        contra=("shoulder_injury", "wrist_injury", "hypertension", "cardiac", "vertigo", "pregnancy"),
        description="Hips high, lower the head towards the floor.", cues=("Elbows track back",)),
    _ex("handstand-hold", "Wall handstand hold", "calisthenics", ("calisthenics",), ("shoulders", "core"), difficulty="advanced",
        intensity="high", contra=("wrist_injury", "shoulder_injury", "hypertension", "cardiac", "epilepsy", "vertigo", "pregnancy", "migraine"),
        description="Kick up to a wall and hold.", cues=("Push the floor away",)),
    # ---- strength
    _ex("goblet-squat", "Goblet squat", "strength", _ALL, ("quads", "glutes"), equipment="dumbbell",
        description="Hold a dumbbell at the chest and squat.", cues=("Chest up",)),
    _ex("leg-press", "Leg press", "strength", ("bulking", "general_fitness", "weight_loss"), ("quads", "glutes"), equipment="machine",
        description="Press the platform through the whole foot.", cues=("Do not lock the knees",)),
    _ex("barbell-back-squat", "Barbell back squat", "strength", ("bulking",), ("quads", "glutes"), equipment="barbell",
        difficulty="intermediate", intensity="high", contra=("knee_injury", "lower_back", "hernia", "osteoporosis"),
        description="Barbell on the upper back, squat to depth.", cues=("Brace before you descend",)),
    _ex("romanian-deadlift", "Dumbbell Romanian deadlift", "strength", ("bulking", "general_fitness"), ("hamstrings", "glutes"),
        equipment="dumbbell", contra=("lower_back", "hernia"), description="Hinge at the hips with soft knees.", cues=("Long spine",)),
    _ex("hip-thrust", "Hip thrust", "strength", ("bulking", "general_fitness", "weight_loss"), ("glutes", "hamstrings"),
        equipment="bench", description="Upper back on a bench, drive the hips up.", cues=("Chin tucked",)),
    _ex("leg-curl", "Lying leg curl", "strength", ("bulking", "general_fitness"), ("hamstrings",), equipment="machine",
        description="Curl the pad towards your glutes.", cues=("Slow on the way down",)),
    _ex("calf-raise", "Standing calf raise", "strength", ("bulking", "general_fitness"), ("calves",), equipment="machine",
        description="Rise onto the toes and lower slowly.", cues=("Full range",)),
    _ex("dumbbell-bench-press", "Dumbbell bench press", "strength", ("bulking", "general_fitness"), ("chest", "triceps"),
        equipment="dumbbell", contra=("pregnancy",), description="Press dumbbells from chest level.", cues=("Feet planted",)),
    _ex("machine-chest-press", "Seated machine chest press", "strength", _ALL, ("chest", "triceps"), equipment="machine",
        description="Seated press with a supported back.", cues=("Exhale as you press",)),
    _ex("one-arm-row", "Supported one-arm dumbbell row", "strength", _ALL, ("back", "lats", "biceps"), equipment="dumbbell",
        description="Hand on a bench, row the dumbbell to the hip.", cues=("No twisting",)),
    _ex("lat-pulldown", "Lat pulldown", "strength", _ALL, ("back", "lats", "biceps"), equipment="machine",
        description="Pull the bar to the upper chest.", cues=("Lead with the elbows",)),
    _ex("seated-cable-row", "Seated cable row", "strength", _ALL, ("back", "lats"), equipment="cable",
        description="Row the handle to the stomach with a tall torso.", cues=("Shoulders down and back",)),
    _ex("overhead-press", "Dumbbell overhead press", "strength", ("bulking", "general_fitness"), ("shoulders", "triceps"),
        equipment="dumbbell", contra=("shoulder_injury", "epilepsy"), description="Press dumbbells overhead.", cues=("Ribs down",)),
    _ex("lateral-raise", "Lateral raise", "strength", ("bulking", "general_fitness"), ("shoulders",), equipment="dumbbell",
        description="Raise light dumbbells to shoulder height.", cues=("Lead with the elbows",)),
    _ex("face-pull", "Cable face pull", "strength", _ALL, ("shoulders", "back", "rear delts"), equipment="cable",
        description="Pull the rope towards your eyes.", cues=("Thumbs back",)),
    _ex("biceps-curl", "Dumbbell biceps curl", "strength", ("bulking", "general_fitness"), ("biceps",), equipment="dumbbell",
        description="Curl with the elbows by your sides.", cues=("No swinging",)),
    _ex("triceps-pushdown", "Cable triceps pushdown", "strength", ("bulking", "general_fitness"), ("triceps",), equipment="cable",
        description="Extend the elbows fully, upper arms still.", cues=("Elbows pinned",)),
    _ex("farmer-carry", "Farmer carry", "strength", _ALL, ("grip", "core", "traps"), equipment="dumbbell",
        description="Walk tall holding weights at your sides.", cues=("Short, steady steps",)),
    _ex("pallof-press", "Pallof press", "strength", _ALL + ("recovery",), ("core", "obliques"), equipment="cable", intensity="low",
        description="Press a cable straight out and resist the rotation.", cues=("Hips square",)),
)

# ------------------------------------------------------------------ template programmes
# (category, muscle hints) per slot; days per goal.
Slot = tuple[str, tuple[str, ...]]
PROGRAMS: dict[str, list[tuple[str, str, list[Slot]]]] = {
    "general_fitness": [
        ("Full body A", "Strength foundations", [("mobility", ("hips",)), ("strength", ("quads", "glutes")), ("calisthenics", ("chest",)),
                                                ("strength", ("back", "lats")), ("calisthenics", ("core",)), ("cardio", ())]),
        ("Cardio & core", "Aerobic base and trunk stability", [("mobility", ("spine",)), ("cardio", ()), ("calisthenics", ("core",)),
                                                               ("calisthenics", ("glutes",)), ("breathing", ())]),
        ("Full body B", "Balanced strength", [("mobility", ("shoulders",)), ("strength", ("hamstrings", "glutes")),
                                              ("strength", ("shoulders",)), ("strength", ("back",)), ("calisthenics", ("core",)), ("breathing", ())]),
    ],
    "weight_loss": [
        ("Circuit A", "Full-body strength circuit", [("mobility", ("hips",)), ("strength", ("quads", "glutes")), ("calisthenics", ("chest",)),
                                                    ("strength", ("back",)), ("calisthenics", ("core",)), ("cardio", ())]),
        ("Steady cardio", "Zone 2 conditioning", [("mobility", ("ankles",)), ("cardio", ()), ("calisthenics", ("core",)), ("cardio", ()),
                                                  ("breathing", ())]),
        ("Circuit B", "Posterior chain and pulling", [("mobility", ("spine",)), ("strength", ("hamstrings", "glutes")),
                                                     ("strength", ("shoulders",)), ("calisthenics", ("back",)), ("calisthenics", ("glutes",)),
                                                     ("cardio", ())]),
        ("Active recovery", "Mobility and easy movement", [("mobility", ("hips",)), ("mobility", ("thoracic",)), ("cardio", ()),
                                                          ("breathing", ())]),
    ],
    "bulking": [
        ("Upper A", "Push and pull strength", [("mobility", ("shoulders",)), ("strength", ("chest",)), ("strength", ("back", "lats")),
                                              ("strength", ("shoulders",)), ("strength", ("biceps",)), ("strength", ("triceps",))]),
        ("Lower A", "Squat pattern and legs", [("mobility", ("hips",)), ("strength", ("quads",)), ("strength", ("hamstrings",)),
                                              ("strength", ("glutes",)), ("strength", ("calves",)), ("calisthenics", ("core",))]),
        ("Upper B", "Upper-body volume", [("mobility", ("thoracic",)), ("strength", ("back", "lats")), ("calisthenics", ("chest",)),
                                         ("strength", ("rear delts", "shoulders")), ("strength", ("biceps",)), ("strength", ("triceps",))]),
        ("Lower B", "Hinge pattern and posterior chain", [("mobility", ("hamstrings",)), ("strength", ("hamstrings", "glutes")),
                                                         ("strength", ("quads",)), ("strength", ("glutes",)), ("calisthenics", ("core",)),
                                                         ("breathing", ())]),
    ],
    "calisthenics": [
        ("Push & core", "Pressing strength and a strong trunk", [("mobility", ("wrists",)), ("calisthenics", ("chest",)),
                                                                ("calisthenics", ("triceps",)), ("calisthenics", ("shoulders",)),
                                                                ("calisthenics", ("core",)), ("calisthenics", ("obliques",))]),
        ("Pull & legs", "Pulling strength and leg control", [("mobility", ("hips",)), ("calisthenics", ("back", "lats")),
                                                            ("calisthenics", ("back",)), ("calisthenics", ("quads",)),
                                                            ("calisthenics", ("glutes",)), ("calisthenics", ("core",))]),
        ("Skills & conditioning", "Body control and engine", [("mobility", ("spine",)), ("calisthenics", ("core",)),
                                                              ("calisthenics", ("chest",)), ("calisthenics", ("back",)), ("cardio", ()),
                                                              ("breathing", ())]),
    ],
}

# (sets, reps, duration_s, rest_s) per category and level
DOSE: dict[str, dict[str, tuple[int, int | None, int | None, int]]] = {
    "strength": {"beginner": (3, 10, None, 90), "intermediate": (4, 8, None, 120), "advanced": (4, 6, None, 150)},
    "calisthenics": {"beginner": (3, 8, None, 75), "intermediate": (4, 10, None, 75), "advanced": (5, 12, None, 90)},
    "cardio": {"beginner": (3, None, 180, 60), "intermediate": (4, None, 180, 45), "advanced": (5, None, 180, 45)},
    "mobility": {"beginner": (2, None, 45, 15), "intermediate": (2, None, 45, 15), "advanced": (2, None, 60, 15)},
    "breathing": {"beginner": (1, None, 120, 15), "intermediate": (1, None, 120, 15), "advanced": (1, None, 150, 15)},
}
_HOLD = re.compile(r"plank|hold|hollow|carry|wall sit", re.I)
_FALLBACK_CATS = {"strength": ("calisthenics", "mobility"), "calisthenics": ("strength", "mobility"), "cardio": ("mobility", "breathing"),
                  "mobility": ("breathing",), "breathing": ("mobility",)}
_LEVEL_RANK = {"beginner": 0, "intermediate": 1, "advanced": 2}


def _pick(library: list[LibExercise], category: str, hints: tuple[str, ...], goal: str, level: str, used_day: set[str],
          usage: dict[str, int]) -> LibExercise | None:
    lvl = _LEVEL_RANK.get(level, 0)
    for cat in (category, *_FALLBACK_CATS.get(category, ())):
        pool = [e for e in library if e.category == cat and e.slug not in used_day and _LEVEL_RANK.get(e.difficulty, 0) <= lvl]
        if not pool:
            continue
        pool.sort(key=lambda e: (
            0 if goal in e.goals else 1,
            -len(set(hints) & set(e.muscles)),
            usage.get(e.slug, 0),
            e.slug,
        ))
        return pool[0]
    return None


def _dose(ex: LibExercise, level: str, capped: bool) -> dict[str, Any]:
    sets, reps, duration, rest = DOSE.get(ex.category, DOSE["strength"]).get(level, DOSE["strength"]["beginner"])
    if ex.category in ("strength", "calisthenics") and _HOLD.search(ex.name):
        reps, duration = None, {"beginner": 30, "intermediate": 40, "advanced": 50}.get(level, 30)
    load = {
        "strength": "RPE 6-7 - leave 3 reps in reserve" if capped else "Moderate - leave 2-3 reps in reserve (RPE 7)",
        "calisthenics": "Bodyweight",
        "cardio": "Conversational pace",
        "mobility": "Gentle, pain-free range",
        "breathing": "Slow nasal breathing",
    }.get(ex.category, "Moderate")
    return {"exercise": ex.name, "sets": sets, "reps": reps, "duration_s": duration, "rest_s": rest, "load": load,
            "notes": ex.cues[0] if ex.cues else None}


def template_plan(goal: str | None, level: str | None, tags: list[str] | set[str] | tuple[str, ...] | None, *,
                  age: int | None = None, library: list[LibExercise] | tuple[LibExercise, ...] | None = None,
                  days_per_week: int | None = None) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    """Deterministic standard plan from the library, already run through the safety filter. ``days_per_week``: the
    member's sessions per week (their fitness profile) - the programme keeps at most that many days.

    Returns ``(plan, removed, notes)`` like ``filter_plan``."""
    library = list(library or BUILTIN_LIBRARY)
    tag_list = normalize_tags(tags, age)
    level = level if level in DOSE["strength"] else "beginner"
    goal = goal if goal in PROGRAMS else "general_fitness"
    if goal == "weight_loss" and "eating_disorder" in tag_list:
        goal = "general_fitness"
    safe = [e for e in library if is_safe(e, tag_list)]
    from app.ai.safety import RULES

    capped = any(RULES[t].cap_load for t in tag_list if t in RULES)
    usage: dict[str, int] = {}
    days = []
    program = PROGRAMS[goal]
    if days_per_week is not None and 1 <= int(days_per_week) < len(program):
        program = program[: int(days_per_week)]
    for name, focus, slots in program:
        used_day: set[str] = set()
        items = []
        for category, hints in slots:
            ex = _pick(safe, category, hints, goal, level, used_day, usage)
            if ex is None:
                continue
            used_day.add(ex.slug)
            usage[ex.slug] = usage.get(ex.slug, 0) + 1
            items.append(_dose(ex, level, capped))
        if items:
            days.append({"name": name, "focus": focus, "items": items})
    weekly = [
        f"Train {len(days)} days a week with at least one rest day between hard sessions.",
        "Warm up for 5-10 minutes before every session and cool down afterwards.",
        "Progress gently: add a rep or a little load only when every set feels comfortable.",
    ]
    if goal == "bulking":
        weekly.append("Eat enough protein and sleep 7-9 hours so your muscles can recover and grow.")
    elif goal == "weight_loss":
        weekly.append("Daily walks and balanced meals multiply the effect of these sessions.")
    elif goal == "calisthenics":
        weekly.append("Quality over quantity: stop a set as soon as your form breaks down.")
    plan = {
        "title": f"{GOAL_LABELS.get(goal, 'General fitness')} · {LEVEL_LABELS.get(level, 'Beginner')} plan",
        "days": days,
        "weekly_notes": weekly,
    }
    return filter_plan(plan, tag_list, None, library, goal=goal, level=level)


# ------------------------------------------------------------------ context
@dataclass(slots=True)
class CoachContext:
    user: User
    profile: CustomerProfile | None
    goal: str
    level: str
    age: int | None
    tags: list[str]
    conditions: list[HealthCondition] = field(default_factory=list)
    metric: MetricEntry | None = None
    previous: WorkoutPlan | None = None
    member: Any = None  # app.ai.member_context.MemberContext: everything the member told the app (the prompt block)

    @property
    def age_group(self) -> str:
        if self.age is None:
            return "adult (age not given)"
        if self.age < 18:
            return "minor (under 18)"
        if self.age >= 60:
            return "senior (60+)"
        return "adult"


async def get_profile(db: AsyncSession, user: User) -> CustomerProfile | None:
    """The request user has ``customer_profile`` eagerly loaded; other User objects need a query."""
    if "customer_profile" in user.__dict__:
        return user.__dict__["customer_profile"]
    return await db.get(CustomerProfile, user.id)


async def conditions_of(db: AsyncSession, user_id: int) -> list[HealthCondition]:
    """The member's conditions by kind, then name - at most ``CONDITIONS_READ_MAX`` rows (a LIMIT in SQL), chosen
    safety-relevant first, so a padded list can't push a safety tag out of the plan filter, the coach page or the
    assistant's context."""
    from app.services.health import CONDITIONS_READ_MAX, safety_first  # WP-E1: the capped, safety-first read

    stmt = (select(HealthCondition).where(HealthCondition.customer_id == user_id)
            .order_by(*safety_first()).limit(CONDITIONS_READ_MAX))
    rows = (await db.execute(stmt)).scalars().all()
    return sorted(rows, key=lambda c: (str(getattr(c.kind, "value", c.kind)), c.name))


def tags_of(conditions: list[HealthCondition], age: int | None) -> list[str]:
    return normalize_tags([c.tag for c in conditions if c.tag], age)


async def latest_metric(db: AsyncSession, user_id: int) -> MetricEntry | None:
    stmt = select(MetricEntry).where(MetricEntry.customer_id == user_id).order_by(MetricEntry.recorded_on.desc()).limit(1)
    return (await db.execute(stmt)).scalar_one_or_none()


async def active_plan(db: AsyncSession, user_id: int) -> WorkoutPlan | None:
    stmt = (
        select(WorkoutPlan)
        .where(WorkoutPlan.customer_id == user_id, WorkoutPlan.is_active.is_(True))
        .order_by(WorkoutPlan.created_at.desc(), WorkoutPlan.id.desc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def recent_plans(db: AsyncSession, user_id: int, limit: int = 5) -> list[WorkoutPlan]:
    stmt = (
        select(WorkoutPlan)
        .options(defer(WorkoutPlan.plan), defer(WorkoutPlan.removed_items), defer(WorkoutPlan.safety_notes))
        .where(WorkoutPlan.customer_id == user_id)
        .order_by(WorkoutPlan.created_at.desc(), WorkoutPlan.id.desc())
        .limit(limit)
    )
    return list((await db.execute(stmt)).scalars().all())


async def load_context(db: AsyncSession, user: User) -> CoachContext:
    """The plan's context: the ONE member context (app/ai/member_context.py - conditions safety-first, medications,
    allergies, injuries, the fitness profile) + the latest reading and the plan to progress. The safety tags the plan
    filter acts on come from the same rows the prompt shows."""
    from app.ai.member_context import load_member_context, read_conditions

    rows = await read_conditions(db, user.id)  # safety-relevant first, capped in SQL
    member = await load_member_context(db, user, conditions=rows)
    profile = await get_profile(db, user)
    age = user.age()
    goal = getattr(profile.goal, "value", profile.goal) if profile else Goal.GENERAL_FITNESS.value
    level = getattr(profile.fitness_level, "value", profile.fitness_level) if profile else FitnessLevel.BEGINNER.value
    conditions = sorted(rows, key=lambda c: (str(getattr(c.kind, "value", c.kind)), c.name))
    return CoachContext(
        user=user, profile=profile, goal=str(goal), level=str(level), age=age, tags=member.tags,
        conditions=conditions, metric=await latest_metric(db, user.id), previous=await active_plan(db, user.id),
        member=member,
    )


def preferred_library(library: list[LibExercise], ctx: CoachContext) -> list[LibExercise]:
    """The library without the exercise types the member avoids ("I hate running" -> no jogging or sprints) - unless
    that would leave too little to build a plan from."""
    avoids = list(getattr(getattr(ctx.member, "fitness", None), "avoids", []) or [])
    if not avoids:
        return library
    from app.services.fitness_profile import avoided

    kept = [e for e in library if not avoided(e.name, avoids)]
    return kept if len(kept) >= 12 else library


async def load_library(db: AsyncSession) -> list[LibExercise]:
    """The seeded exercise library (embeddings deferred), or the built-in list when it is empty."""
    rows = (await db.execute(select(Exercise).options(defer(Exercise.embedding)).order_by(Exercise.slug))).scalars().all()
    if not rows:
        return list(BUILTIN_LIBRARY)
    return [LibExercise.from_model(r) for r in rows]


def plan_summary(plan: WorkoutPlan | dict[str, Any] | None, limit: int = 600) -> str:
    """Compact one-line summary for prompts: ``"Title": Day A: Squat 3x10, ...; Day B: ...``."""
    if plan is None:
        return ""
    data = plan.plan if isinstance(plan, WorkoutPlan) else plan
    title = plan.title if isinstance(plan, WorkoutPlan) else (data or {}).get("title", "")
    parts = []
    for day in (data or {}).get("days", [])[:7]:
        items = []
        for it in day.get("items", [])[:7]:
            dose = f"{it.get('sets') or 1}x{it['reps']}" if it.get("reps") else f"{it.get('sets') or 1}x{it.get('duration_s') or 30}s"
            load = f" @{it['load']}" if it.get("load") and it.get("category") == "strength" else ""
            items.append(f"{it.get('exercise')} {dose}{load}")
        parts.append(f"{day.get('name')}: " + ", ".join(items))
    text = f'"{title}": ' + "; ".join(parts)
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ------------------------------------------------------------------ generation
@dataclass(slots=True)
class GenerateOutcome:
    plan: WorkoutPlan
    used_ai: bool
    ai_status: str  # ok | unavailable | not_included | rejected
    removed: list[dict[str, Any]]


async def save_plan(db: AsyncSession, user: User, *, goal: str, source: str, plan: dict[str, Any], removed: list[dict[str, Any]],
                    notes: list[str], ai_provider: str | None, gym_id: int | None, created_by_id: int | None = None) -> WorkoutPlan:
    await db.execute(
        update(WorkoutPlan).where(WorkoutPlan.customer_id == user.id, WorkoutPlan.is_active.is_(True)).values(is_active=False)
    )
    try:
        goal_enum = Goal(goal)
    except ValueError:
        goal_enum = Goal.GENERAL_FITNESS
    row = WorkoutPlan(
        customer_id=user.id, gym_id=gym_id, created_by_id=created_by_id, goal=goal_enum, source=source,
        title=(plan.get("title") or "Your training plan")[:160], plan=plan, safety_notes=notes, removed_items=removed,
        ai_provider=ai_provider, is_active=True,
    )
    db.add(row)
    await db.flush()
    return row


async def generate_plan(db: AsyncSession, user: User, *, access: Any, use_ai: bool = True,
                        request: str | None = None) -> GenerateOutcome:
    """AI plan (membership with AI) or template plan, always safety-filtered. The caller commits (the AI path commits
    its reads first: no transaction is held while the model thinks).

    Runs in the ``coach_plan`` job (``plan_job``). ``use_ai=False`` builds the template even for an AI membership
    (the click was not admitted for AI); ``request`` tags the plan with the click that asked for it, so an
    interrupted job that already saved it never builds a second one. AI offline -> the template ("unavailable").
    Raises ``AICancelled`` when the requester left mid-call (nobody would see a fallback plan) and ``AIRejected``
    (anything but 'busy') for a direct caller the guard refused - jobs are never refused."""
    from app.services.notify import notify

    ctx = await load_context(db, user)
    library = preferred_library(await load_library(db), ctx)
    gym_id = None
    subs = list(getattr(access, "subscriptions", []) or [])
    if subs:
        ai_subs = [s for s in subs if s.plan.includes_ai]
        gym_id = (ai_subs or subs)[0].gym_id

    ai_status = "not_included"
    result: tuple[dict[str, Any], list[dict[str, Any]], list[str]] | None = None
    provider = None
    if use_ai and getattr(access, "ai", False):
        from app.ai import coach as coach_ai

        try:
            allowed = await coach_ai.allowed_exercises(db, ctx, library)
            avoids = list(ctx.member.fitness.avoids) if ctx.member is not None else []
            if avoids:
                from app.services.fitness_profile import avoided

                allowed = [e for e in allowed if not avoided(e.name, avoids)] or allowed
            # everything is read: no transaction (nor pooled connection) is held while the model thinks - a job's
            # turn in the AI queue may come minutes later, past the server's idle-in-transaction limit
            await db.commit()
            model, provider = await coach_ai.generate_ai_plan(db, ctx, library, allowed=allowed)
            safe, removed, notes = filter_plan(model, ctx.tags, ctx.age, library, goal=ctx.goal, level=ctx.level)
            if sum(len(d["items"]) for d in safe["days"]) >= 3:
                result = (safe, removed, notes)
                ai_status = "ok"
            else:
                ai_status = "rejected"
                log.warning("AI plan for user %s had too few safe items - using the template", user.id)
        except AICancelled:
            raise  # the work was stopped because its requester left: no fallback plan for nobody
        except AIRejected as exc:
            if exc.code != "busy":
                raise  # a refusal is for the requester to see, never silently swapped for a template
            ai_status = "unavailable"
        except AIUnavailableError:
            ai_status = "unavailable"
    used_ai = result is not None
    if result is None:
        result = template_plan(ctx.goal, ctx.level, ctx.tags, age=ctx.age, library=library,
                               days_per_week=ctx.member.fitness.sessions_per_week if ctx.member is not None else None)
    safe, removed, notes = result
    safe["meta"] = {"goal": ctx.goal, "level": ctx.level, "tags": ctx.tags, "ai_status": ai_status,
                    "generated_at": utcnow().isoformat(timespec="seconds")}
    if request:
        safe["meta"]["request"] = request
    row = await save_plan(db, user, goal=ctx.goal, source="ai" if used_ai else "template", plan=safe, removed=removed,
                          notes=notes, ai_provider=provider if used_ai else None, gym_id=gym_id)
    sessions = len(safe["days"])
    body = f"“{row.title}” - {sessions} session{'s' if sessions != 1 else ''} per week."
    if removed:
        body += f" The safety filter adjusted {len(removed)} exercise{'s' if len(removed) != 1 else ''} for your health profile."
    if not used_ai and ai_status == "unavailable":
        body += " AI was unavailable, so this is a standard safety-checked plan."
    await notify(db, user, PLAN_READY, body, category=NotificationCategory.HEALTH,
                 severity=Severity.SUCCESS, link="/me/coach")
    return GenerateOutcome(plan=row, used_ai=used_ai, ai_status=ai_status, removed=removed)


# ------------------------------------------------------------------ background generation (the coach_plan job)
JOB_KIND = "coach_plan"
PLAN_JOB_ATTEMPTS = 2
PLANS_PER_DAY = 20  # load safety (approved limit): plans one member generates in 24 hours (counted at the click)


class PlanLimitError(Exception):
    """The member generated PLANS_PER_DAY plans in the last 24 hours: nothing is queued (a friendly message)."""

    def __init__(self) -> None:
        super().__init__(f"You've generated {PLANS_PER_DAY} plans in the last 24 hours - the most we allow. "
                         "Your latest plan is below; you can generate a new one tomorrow.")
        self.message = str(self)
IN_FLIGHT = (JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.PAUSED)
PLAN_READY = "Your new training plan is ready"
# GenerateOutcome.ai_status values of a click the AI answered for - its 'coach' charge stays ('rejected': the AI's plan
# failed the safety filter, so the member got the template, but the AI did the work). Anything else is refunded.
AI_ANSWERED = ("ok", "rejected")
JOB_TEXT: dict[str, tuple[str, str]] = {  # job status -> (title, message) for the coach page and its status poll
    "queued": ("Building your plan", "Queued - it starts in a moment."),
    "running": ("Building your plan", "Choosing exercises for your goal and running the safety checks…"),
    "paused": ("Plan paused", "Paused while you were away - resumes now that you're back."),
    "done": ("Your plan is ready", "Your new training plan is ready."),
    "failed": ("We couldn't build your plan", "Something went wrong while building your plan. Please try again."),
    "cancelled": ("Plan request stopped", "Building your plan was stopped. You can generate a new one any time."),
}


@dataclass(slots=True)
class QueuedPlan:
    job: Job
    created: bool  # False: the member's plan was already being built (a double click) - nothing new was queued


def job_key(user_id: int) -> str:
    """Dedupe key of a member's plan job: one row per member (recycled by every click), so a double click can
    never queue two plans."""
    return f"{JOB_KIND}:{user_id}"


def _lock_key(user_id: int) -> int:
    return int.from_bytes(hashlib.blake2b(job_key(user_id).encode(), digest_size=8).digest(), "big", signed=True)


async def plan_job_of(db: AsyncSession, user_id: int) -> Job | None:
    """The member's ``coach_plan`` job row (the latest request, whatever its status), or None."""
    return await db.scalar(select(Job).where(Job.dedupe_key == job_key(user_id)))


async def plan_job_by_id(db: AsyncSession, user_id: int, job_id: int) -> Job | None:
    """Owner-scoped lookup (GET /me/coach/status): another member's job, or another kind of job, is None."""
    return await db.scalar(select(Job).where(Job.id == job_id, Job.user_id == user_id, Job.kind == JOB_KIND))


def job_state(job: Job) -> dict[str, Any]:
    """Where a plan request stands, in the member's words (the coach page and its JSON status poll)."""
    status = job.status.value if isinstance(job.status, JobStatus) else str(job.status)
    title, message = JOB_TEXT.get(status, JOB_TEXT["queued"])
    return {"job": job.id, "status": status, "title": title, "message": message, "active": job.status in IN_FLIGHT,
            "link": "/me/coach" if job.status == JobStatus.DONE else None}


async def queue_plan(db: AsyncSession, user: User, *, access: Any, owner: AIOwner | None = None) -> QueuedPlan:
    """POST /me/coach/generate: queue the member's plan (``coach_plan`` job) instead of building it while the browser
    waits. COMMITS - the click's lock, its job row and its AI-guard charge belong together.

    * One click per member at a time (transaction-scoped advisory lock). While a plan job is queued, running or
      paused, a click queues nothing and returns that job; a paused one goes back in the queue - its member is here.
    * Background jobs are never judged by the AI guard, so the click is: a membership with AI is admitted for one
      'coach' generation before anything is queued (``AIRejected`` propagates - the caller shows why and how long
      to wait) and the charge is refunded if queueing fails. The admission travels in the job payload
      (``payload["admission"]``, ``Admission.to_payload()``): the job refunds it when no AI plan comes of the click
      (``plan_job``). Without AI the job builds the template plan: no AI, nothing to judge."""
    from app.ai import guard as guard_module

    owner = owner if owner is not None else current_owner()
    await db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _lock_key(user.id)})
    existing = await plan_job_of(db, user.id)
    if existing is not None and existing.status in IN_FLIGHT:
        if existing.status == JobStatus.PAUSED:  # paused while they were away - they are clearly back
            await enqueue(db, JOB_KIND, dict(existing.payload or {}), dedupe_key=job_key(user.id), user_id=user.id)
        await db.commit()
        return QueuedPlan(existing, created=False)
    from datetime import timedelta

    from sqlalchemy import func

    made = int(await db.scalar(select(func.count()).select_from(
        select(WorkoutPlan.id).where(WorkoutPlan.customer_id == user.id,
                                     WorkoutPlan.created_at >= utcnow() - timedelta(hours=24))
        .limit(PLANS_PER_DAY).subquery())) or 0)
    if made >= PLANS_PER_DAY:
        await db.commit()  # releases the lock; nothing was queued or charged
        raise PlanLimitError()
    use_ai = bool(getattr(access, "ai", False))
    admission = None
    if use_ai:
        try:
            admission = await guard_module.guard.admit(owner, "coach")
        except AIUnavailableError:  # AIRejected: quota / cooldown - nothing is queued (the lock ends with the request)
            raise
        except Exception:  # noqa: BLE001 - a broken guard must not switch plan generation off
            log.exception("AI guard failed for a coach plan of user %s - admitting the request", user.id)
    payload: dict[str, Any] = {"user_id": user.id, "ai": use_ai, "request": secrets.token_hex(8)}
    try:
        if admission is not None:  # settled by the job (or below): one settle in total, whoever comes first
            payload["admission"] = admission.to_payload()
        job = await enqueue(db, JOB_KIND, payload, dedupe_key=job_key(user.id), max_attempts=PLAN_JOB_ATTEMPTS,
                            user_id=user.id)
        await db.commit()
    except BaseException:
        await guard_module.guard.settle(admission, ok=False)  # nothing was queued: nobody pays for it
        raise
    return QueuedPlan(job, created=True)


def _plan_update(status: str, **extra: Any) -> None:
    """``task.update`` (docs section 7) for the plan job running in this task: the coach page follows it. Never
    raises; does nothing outside the job runner (tests and scripts calling the handler directly)."""
    owner = current_owner()
    if owner is None or owner.job_id is None or owner.user_id is None:
        return
    evt: dict[str, Any] = {"type": "task.update", "task_id": owner.job_id, "kind": JOB_KIND, "status": status,
                           "progress": None, "message": None, "link": None}
    evt.update(extra)
    try:
        from app.services.realtime import hub

        hub.publish_nowait([owner.user_id], evt)
    except Exception:  # noqa: BLE001 - a lost progress note must never affect the plan
        log.debug("task.update for plan job %s not delivered", owner.job_id, exc_info=True)


def _plan_finished(message: str) -> None:
    # how the runner announces the job's 'done' (published after the DONE write - the job never sends its own).
    # 'notify: false': the "Your new training plan is ready" notification is the island note (no second one)
    set_result_event(notify=False, link="/me/coach", message=message)


async def _settle_click(payload: dict[str, Any], user_id: int, *, ok: bool) -> bool:
    """Settle the AI-guard charge of the click that queued this plan (``payload["admission"]``, see ``queue_plan``):
    ``ok=False`` gives the member their 'coach' units back - no AI plan came of the click (AI offline or busy, no AI
    in the membership any more, nothing built); ``ok=True`` keeps them (the AI answered). The guard settles an
    admission once over every copy, retry, resume and process; a click without AI carries nothing. Never raises."""
    admission = payload.get("admission")
    if not isinstance(admission, dict):
        return False
    if admission.get("user_id") != user_id:
        log.warning("coach plan job of user %s carries another user's AI admission - left alone", user_id)
        return False
    from app.ai import guard as guard_module

    return await guard_module.guard.settle_payload(admission, ok)


@job_handler(JOB_KIND, on_exit="pause")  # stored for later: deferred while the member is away, resumed on return
async def plan_job(db: AsyncSession, payload: dict[str, Any]) -> None:
    """Build a member's plan in the background - exactly what ``generate_plan`` does: the AI when the membership
    includes it and the click was admitted for it, the safety-checked template otherwise - save it and notify
    "Your new training plan is ready" (link /me/coach).

    The click's 'coach' charge is settled here: kept when the AI answered, refunded when the plan is the template
    because the AI was offline or busy, when nothing was built (suspended, membership ended), and when the last
    attempt fails. A stopped run (the member left) keeps it: the resumed run still owes them the plan."""
    try:
        user_id = int(payload.get("user_id") or 0)
    except (TypeError, ValueError):
        return
    try:
        answered = await _build_plan(db, payload, user_id)
    except AICancelled:
        raise  # its AI work was stopped (the job is being paused or cancelled): never a reason to refund the click
    except Exception:
        run = current_run()
        if run is None or (run.last_attempt and not run.stopping):  # no retry follows: the click got no plan at all
            await _settle_click(payload, user_id, ok=False)
        raise
    await _settle_click(payload, user_id, ok=answered)


async def _build_plan(db: AsyncSession, payload: dict[str, Any], user_id: int) -> bool:
    """``plan_job``'s work. Returns True when the AI answered for this click (``AI_ANSWERED``)."""
    user = await db.get(User, user_id) if user_id > 0 else None
    if user is None or user.deleted_at is not None:
        return False
    request = str(payload.get("request") or "")[:32] or None
    current = await active_plan(db, user.id)
    meta = current.plan.get("meta") if current is not None and isinstance(current.plan, dict) else None
    if request and isinstance(meta, dict) and meta.get("request") == request:
        _plan_finished(PLAN_READY)  # an interrupted earlier run of this very click already saved the plan
        return meta.get("ai_status") in AI_ANSWERED
    profile = await get_profile(db, user)
    if profile is not None and profile.activity_suspended:  # suspended for safety since the click: no new plan
        log.info("coach plan for user %s not built: activity suspended", user.id)
        _plan_finished("Coaching is paused while your gym activity is suspended for safety.")
        return False
    from app.services.subscriptions import plan_access

    access = await plan_access(db, user.id)
    if not access.has_any:
        _plan_finished("You need an active membership to get a training plan.")
        return False
    _plan_update("running")
    outcome = await generate_plan(db, user, access=access, use_ai=bool(payload.get("ai")), request=request)
    await db.commit()  # the plan and its notification exist before the page is told to show them
    _plan_finished(PLAN_READY)
    return outcome.ai_status in AI_ANSWERED


# ------------------------------------------------------------------ videos
def media_file(name: str | None) -> Path | None:
    """Path of a generated video/poster under static/videos (None for unsafe names)."""
    if not name or not _SAFE_FILE.match(name):
        return None
    return VIDEO_DIR / name


def media_exists(name: str | None) -> bool:
    path = media_file(name)
    return bool(path and path.is_file())


async def videos_by_slug(db: AsyncSession, slugs: set[str]) -> dict[str, Video]:
    if not slugs:
        return {}
    rows = (await db.execute(select(Video).where(Video.slug.in_(sorted(slugs)), Video.is_active.is_(True)))).scalars().all()
    return {v.slug: v for v in rows}
