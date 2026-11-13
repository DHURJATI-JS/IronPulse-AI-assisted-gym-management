"""The member's fitness profile (``FitnessProfile``): vocabulary, caps, validation and race-safe writes.

What a member told the app once about how they train - exercise types they like and avoid, goals, equipment, sessions
per week, session length, preferred time of day and notes - typed on ``/me/health/profile/fitness`` or confirmed from
the assistant / live coach ("Remember this?", ``app.ai.memory``). Every AI feature reads it through
``app.ai.member_context``; health facts (conditions, allergies, medications, injuries) live in ``health_conditions``,
the main plan goal and the experience level on ``customer_profiles``.

Lists hold a slug of the fixed vocabulary below or the member's own short label ("other"). Caps (approved load-safety
limits, one place): ``LIST_CAPS`` items per list, ``ITEM_MAX`` characters per own label, ``NOTES_MAX`` characters of
notes, ``FORGOTTEN_MAX`` remembered "forget" requests. Every write locks the member's row (``lock_row``: the row is
created if missing, then ``SELECT ... FOR UPDATE``), so two requests at the same moment can neither pass a cap nor lose
each other's change; the table's CHECK constraints are the database backstop.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import FitnessProfile

# ====================================================================================== vocabulary
# slug -> (label, phrases that mean it). Phrases are matched as whole words in lower-case text.
EXERCISE_TYPES: dict[str, tuple[str, tuple[str, ...]]] = {
    "strength": ("Strength training / weights", (
        "strength training", "weight training", "weightlifting", "weight lifting", "lifting weights", "lifting",
        "weights", "resistance training", "bodybuilding", "powerlifting", "strength")),
    "calisthenics": ("Calisthenics / bodyweight", ("calisthenics", "bodyweight training", "bodyweight workouts",
                                                   "bodyweight", "street workout")),
    "running": ("Running / jogging", ("running", "jogging", "jog", "sprinting", "sprints", "runs")),
    "walking": ("Walking", ("walking", "walks", "brisk walks", "brisk walking")),
    "hiking": ("Hiking / trekking", ("hiking", "hikes", "trekking", "treks")),
    "cycling": ("Cycling / spinning", ("cycling", "biking", "bike rides", "spinning", "spin class", "spin classes")),
    "swimming": ("Swimming", ("swimming", "swim", "swims")),
    "yoga": ("Yoga", ("yoga",)),
    "pilates": ("Pilates", ("pilates",)),
    "hiit": ("HIIT / circuits", ("hiit", "high intensity interval training", "interval training", "intervals",
                                 "circuit training", "circuits", "tabata", "bootcamp")),
    "crossfit": ("CrossFit", ("crossfit", "cross fit")),
    "dance": ("Dance / Zumba / aerobics", ("dance", "dancing", "zumba", "aerobics")),
    "martial_arts": ("Boxing / martial arts", ("boxing", "kickboxing", "martial arts", "karate", "mma", "judo",
                                               "taekwondo", "muay thai", "jiu jitsu")),
    "rowing": ("Rowing", ("rowing",)),
    "stretching": ("Stretching / mobility", ("stretching", "stretches", "mobility work", "mobility")),
    "cardio_machines": ("Cardio machines", ("treadmill", "elliptical", "cross trainer", "stair climber",
                                            "stairmaster", "cardio machines")),
    "team_sports": ("Team & racket sports", ("football", "soccer", "basketball", "cricket", "tennis", "badminton",
                                             "volleyball", "squash", "hockey", "team sports")),
    "climbing": ("Climbing / bouldering", ("climbing", "bouldering", "rock climbing")),
    "jumping": ("Jumping / high impact", ("jumping", "jumps", "plyometrics", "plyo", "box jumps", "skipping",
                                          "jump rope", "burpees", "high impact", "high-impact")),
}
GOALS: dict[str, tuple[str, tuple[str, ...]]] = {
    "lose_weight": ("Lose weight", ("lose weight", "losing weight", "weight loss", "lose fat", "fat loss", "burn fat",
                                    "slim down", "get lean", "lose some weight", "drop weight", "lose belly fat")),
    "build_muscle": ("Build muscle", ("build muscle", "building muscle", "gain muscle", "put on muscle", "bulk up",
                                      "bulking", "get bigger", "muscle gain", "hypertrophy", "gain mass", "build mass")),
    "get_stronger": ("Get stronger", ("get stronger", "be stronger", "build strength", "gain strength",
                                      "increase my strength", "stronger")),
    "endurance": ("Build stamina / endurance", ("stamina", "endurance", "cardio fitness", "aerobic fitness")),
    "flexibility": ("Flexibility & mobility", ("flexibility", "more flexible", "be flexible", "mobility")),
    "general_fitness": ("Get fit / stay healthy", ("get fit", "stay fit", "keep fit", "stay healthy", "be healthier",
                                                    "get healthier", "general fitness", "overall fitness")),
    "tone_up": ("Tone up", ("tone up", "toning", "get toned")),
    "sport": ("Sport performance", ("sport performance", "sports performance", "athletic performance")),
    "rehab": ("Recover from an injury", ("rehab", "rehabilitation", "recover from my injury", "recover from injury")),
    "stress": ("Reduce stress", ("reduce stress", "less stress", "stress relief", "relieve stress", "de-stress",
                                 "destress")),
    "posture": ("Better posture", ("posture",)),
}
EQUIPMENT: dict[str, tuple[str, tuple[str, ...]]] = {
    "none": ("No equipment (bodyweight only)", ("no equipment", "without equipment", "bodyweight only")),
    "full_gym": ("Full gym access", ("full gym", "gym membership", "gym access", "commercial gym")),
    "dumbbells": ("Dumbbells", ("dumbbells", "dumbbell", "dumbells", "dumbell")),
    "barbell": ("Barbell & plates", ("barbell", "barbells", "olympic bar")),
    "kettlebells": ("Kettlebells", ("kettlebells", "kettlebell", "kettle bell", "kettle bells")),
    "resistance_bands": ("Resistance bands", ("resistance bands", "resistance band", "loop bands", "theraband")),
    "pull_up_bar": ("Pull-up bar", ("pull-up bar", "pull up bar", "pullup bar", "chin-up bar", "chin up bar")),
    "bench": ("Bench", ("weight bench", "bench")),
    "mat": ("Exercise / yoga mat", ("yoga mat", "exercise mat")),
    "jump_rope": ("Jump rope", ("jump rope", "skipping rope", "skip rope")),
    "treadmill": ("Treadmill", ("treadmill",)),
    "exercise_bike": ("Exercise bike", ("exercise bike", "stationary bike", "spin bike", "peloton")),
    "rower": ("Rowing machine", ("rowing machine", "rower")),
    "elliptical": ("Elliptical / cross trainer", ("elliptical", "cross trainer", "cross-trainer")),
    "cable_machine": ("Cable machine", ("cable machine", "cable station")),
    "machines": ("Weight machines", ("weight machines", "leg press", "smith machine")),
    "suspension": ("Suspension trainer (TRX)", ("trx", "suspension trainer", "gymnastic rings")),
    "medicine_ball": ("Medicine / slam ball", ("medicine ball", "med ball", "slam ball")),
    "foam_roller": ("Foam roller", ("foam roller",)),
    "step_box": ("Step / plyo box", ("plyo box", "aerobic step", "step box")),
    "stability_ball": ("Stability ball", ("stability ball", "swiss ball", "exercise ball", "gym ball")),
}
PREFERRED_TIMES: tuple[tuple[str, str], ...] = (
    ("early_morning", "Early morning (before 7)"), ("morning", "Morning"), ("midday", "Midday / lunch"),
    ("afternoon", "Afternoon"), ("evening", "Evening"), ("night", "Late evening / night"), ("varies", "It varies"),
)
TIME_LABELS = dict(PREFERRED_TIMES)
VOCAB: dict[str, dict[str, tuple[str, tuple[str, ...]]]] = {
    "likes": EXERCISE_TYPES, "avoids": EXERCISE_TYPES, "goals": GOALS, "equipment": EQUIPMENT,
}
LIST_FIELDS = ("likes", "avoids", "goals", "equipment")
FIELD_LABELS = {
    "likes": "Exercise you enjoy", "avoids": "Exercise you avoid", "goals": "Goals", "equipment": "Equipment",
    "sessions_per_week": "Sessions per week", "session_minutes": "Session length", "preferred_time": "Preferred time",
    "notes": "Notes",
}

# exercise names a member who AVOIDS a type never gets in a generated plan (deterministic, after the AI)
AVOID_PATTERNS: dict[str, re.Pattern[str]] = {
    "running": re.compile(r"\b(?:jog|jogging|run|running|sprint|sprints)\b", re.I),
    "jumping": re.compile(r"jump|burpee|skip|plyo|hop\b|bound", re.I),
    "cycling": re.compile(r"\b(?:bike|cycling|cycle|spin)\b", re.I),
    "rowing": re.compile(r"\brow(?:ing)? machine\b|\brower\b", re.I),
    "swimming": re.compile(r"\bswim", re.I),
    "cardio_machines": re.compile(r"treadmill|elliptical|stair|\bbike\b|rowing machine", re.I),
    "hiit": re.compile(r"interval|\bhiit\b|tabata|circuit", re.I),
    "walking": re.compile(r"\bwalk", re.I),
    "yoga": re.compile(r"\byoga\b", re.I),
    "pilates": re.compile(r"\bpilates\b", re.I),
}


def avoided(exercise_name: str, avoids: Iterable[str]) -> bool:
    """Does the member avoid this exercise (one of their avoided types names it)?"""
    name = str(exercise_name or "")
    return any(slug in AVOID_PATTERNS and AVOID_PATTERNS[slug].search(name) for slug in avoids)


# ====================================================================================== caps (approved limits)
LIST_CAPS: dict[str, int] = {"likes": 20, "avoids": 20, "goals": 10, "equipment": 30}
ITEM_MAX: dict[str, int] = {"likes": 40, "avoids": 40, "goals": 60, "equipment": 40}  # characters of an own label
NOTES_MAX = 500
FORGOTTEN_MAX = 50  # "forget X" requests remembered (newest first) - hidden from the AI's view of past chats
FORGOTTEN_ITEM_MAX = 120
SESSIONS_RANGE = (1, 14)
MINUTES_RANGE = (10, 240)
OTHER_INPUT_MAX = 600  # characters of an "other" text box read at all

_SPACES = re.compile(r"\s+")
_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_HAS_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)
_SPLIT = re.compile(r"\s*[,;\n•]\s*")
_LABEL_FORBIDDEN = re.compile(r"[<>{}\[\]\\|^`=@#$%*_\"]")


def clean_text(value: Any, limit: int) -> str:
    """One line of member text: control characters out, whitespace collapsed, cut to ``limit``."""
    text = _SPACES.sub(" ", _CTRL.sub(" ", str(value or ""))).strip()
    return text[:limit].rstrip()


def _key(text: str) -> str:
    return _SPACES.sub(" ", re.sub(r"[^a-z0-9+ ]", " ", text.lower())).strip()


def _phrase_index(vocab: Mapping[str, tuple[str, tuple[str, ...]]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for slug, (label, phrases) in vocab.items():
        for p in (slug.replace("_", " "), label, *phrases):
            out.setdefault(_key(p), slug)
        for part in label.split("/"):
            out.setdefault(_key(part), slug)
    return out


_INDEX = {name: _phrase_index(vocab) for name, vocab in VOCAB.items()}


def vocab_slug(field_name: str, text: str) -> str | None:
    """The vocabulary slug ``text`` names exactly ('Yoga', 'weights', 'Dumbbells' ...), or None."""
    return _INDEX.get(field_name, {}).get(_key(text))


def label_of(field_name: str, value: str) -> str:
    """Display label of a stored list value: the vocabulary label, or the member's own words."""
    vocab = VOCAB.get(field_name, {})
    return vocab[value][0] if value in vocab else str(value)


def short_label(field_name: str, value: str) -> str:
    """A compact label for prompts and chips: 'Strength training' rather than 'Strength training / weights'."""
    label = label_of(field_name, value)
    return label.split(" / ")[0] if value in VOCAB.get(field_name, {}) else label


def item_error(field_name: str, raw: str) -> str | None:
    """Why an own label can't be stored (None = fine)."""
    limit = ITEM_MAX.get(field_name, 40)
    if len(raw) > limit:
        return f"Keep each entry under {limit} characters (“{raw[:30]}…”)."
    if len(raw) < 2 or not _HAS_LETTER.search(raw):
        return f"“{raw[:30]}” isn't a valid entry."
    if _LABEL_FORBIDDEN.search(raw):
        return f"“{raw[:30]}” contains characters that aren't allowed."
    return None


def canonical_item(field_name: str, raw: Any) -> tuple[str | None, str | None]:
    """(stored value, error): a vocabulary slug when the words name one, else the member's own label (capitalised)."""
    text = clean_text(raw, ITEM_MAX.get(field_name, 40) + 20)
    if not text:
        return None, None
    slug = vocab_slug(field_name, text)
    if slug is not None:
        return slug, None
    err = item_error(field_name, text)
    if err:
        return None, err
    return text[:1].upper() + text[1:], None


def _same(a: str, b: str) -> bool:
    return _key(a) == _key(b)


def contains(values: Iterable[str], value: str) -> bool:
    return any(_same(v, value) for v in values)


# ====================================================================================== plain data
@dataclass(slots=True)
class FitnessData:
    likes: list[str] = field(default_factory=list)
    avoids: list[str] = field(default_factory=list)
    goals: list[str] = field(default_factory=list)
    equipment: list[str] = field(default_factory=list)
    sessions_per_week: int | None = None
    session_minutes: int | None = None
    preferred_time: str | None = None
    notes: str | None = None
    forgotten: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_row(cls, row: FitnessProfile | None) -> FitnessData:
        if row is None:
            return cls()

        def lst(value: Any, cap: int) -> list[str]:
            return [str(v) for v in (value or []) if isinstance(v, str) and v.strip()][:cap]

        return cls(
            likes=lst(row.likes, LIST_CAPS["likes"]), avoids=lst(row.avoids, LIST_CAPS["avoids"]),
            goals=lst(row.goals, LIST_CAPS["goals"]), equipment=lst(row.equipment, LIST_CAPS["equipment"]),
            sessions_per_week=row.sessions_per_week, session_minutes=row.session_minutes,
            preferred_time=row.preferred_time if row.preferred_time in TIME_LABELS else None,
            notes=(row.notes or None),
            forgotten=[f for f in (row.forgotten or []) if isinstance(f, dict)][:FORGOTTEN_MAX],
        )

    @property
    def empty(self) -> bool:
        return not (self.likes or self.avoids or self.goals or self.equipment or self.sessions_per_week
                    or self.session_minutes or self.preferred_time or self.notes)

    def schedule_text(self) -> str:
        parts = []
        if self.sessions_per_week:
            parts.append(f"{self.sessions_per_week} session{'s' if self.sessions_per_week != 1 else ''} a week")
        if self.session_minutes:
            parts.append(f"about {self.session_minutes} min each")
        if self.preferred_time:
            parts.append(f"prefers {TIME_LABELS[self.preferred_time].lower()}")
        return ", ".join(parts)


# ====================================================================================== form parsing
def _parse_list(form: Any, field_name: str, errors: dict[str, str]) -> list[str]:
    cap = LIST_CAPS[field_name]
    vocab = VOCAB[field_name]
    picked: list[str] = []
    try:
        raw_slugs = form.getlist(field_name)
    except AttributeError:
        raw_slugs = []
    for raw in raw_slugs[: len(vocab) + 5]:
        if not isinstance(raw, str):
            continue
        if raw not in vocab:
            errors[field_name] = "Choose from the list (or type it under “Other”)."
            continue
        if raw not in picked:
            picked.append(raw)
    other_raw = form.get(f"{field_name}_other") if hasattr(form, "get") else None
    other = str(other_raw or "")[:OTHER_INPUT_MAX] if isinstance(other_raw, str) else ""
    for part in _SPLIT.split(other):
        if not part.strip():
            continue
        value, err = canonical_item(field_name, part)
        if err:
            errors[f"{field_name}_other"] = err
            continue
        if value and not contains(picked, value):
            picked.append(value)
    if len(picked) > cap:
        errors.setdefault(field_name, f"Choose at most {cap} - you picked {len(picked)}.")
    return picked[:cap]


def _parse_int(raw: Any, lo: int, hi: int, label: str, field_name: str, errors: dict[str, str]) -> int | None:
    text = str(raw or "").strip()[:6] if isinstance(raw, str) or raw is None else ""
    if not text:
        return None
    if not text.isdigit():
        errors[field_name] = f"{label}: enter a whole number from {lo} to {hi}."
        return None
    value = int(text)
    if not lo <= value <= hi:
        errors[field_name] = f"{label}: enter a number from {lo} to {hi}."
        return None
    return value


def parse_form(form: Any) -> tuple[dict[str, Any], dict[str, str], FitnessData]:
    """The edit page's POST -> (values to re-render, errors by field, data). Unknown vocabulary, too many items, junk
    numbers and over-long text are friendly errors - never a 500."""
    errors: dict[str, str] = {}
    data = FitnessData()
    for name in LIST_FIELDS:
        setattr(data, name, _parse_list(form, name, errors))
    clash = [v for v in data.likes if contains(data.avoids, v)]
    if clash:
        errors["avoids"] = f"“{label_of('likes', clash[0])}” is both enjoyed and avoided - choose one."
    get = form.get if hasattr(form, "get") else (lambda _k: None)
    data.sessions_per_week = _parse_int(get("sessions_per_week"), *SESSIONS_RANGE, "Sessions per week",
                                        "sessions_per_week", errors)
    data.session_minutes = _parse_int(get("session_minutes"), *MINUTES_RANGE, "Session length (minutes)",
                                      "session_minutes", errors)
    when = get("preferred_time")
    when = when.strip() if isinstance(when, str) else ""
    if when and when not in TIME_LABELS:
        errors["preferred_time"] = "Choose a time of day from the list."
    data.preferred_time = when if when in TIME_LABELS else None
    notes_raw = get("notes")
    notes = _SPACES.sub(" ", _CTRL.sub(" ", notes_raw if isinstance(notes_raw, str) else "")).strip()
    if len(notes) > NOTES_MAX:
        errors["notes"] = f"Keep notes under {NOTES_MAX} characters."
    data.notes = notes[:NOTES_MAX] or None
    values = {
        "likes": data.likes, "avoids": data.avoids, "goals": data.goals, "equipment": data.equipment,
        "sessions_per_week": str(get("sessions_per_week") or "")[:6], "session_minutes": str(get("session_minutes") or "")[:6],
        "preferred_time": when[:20], "notes": notes[:NOTES_MAX],
    }
    for name in LIST_FIELDS:
        raw = get(f"{name}_other")
        values[f"{name}_other"] = str(raw)[:OTHER_INPUT_MAX] if isinstance(raw, str) else ""
    return values, errors, data


# ====================================================================================== race-safe writes
async def load(db: AsyncSession, user_id: int) -> FitnessProfile | None:
    return await db.get(FitnessProfile, user_id)


async def lock_row(db: AsyncSession, user_id: int) -> FitnessProfile:
    """The member's row, created if missing, locked FOR UPDATE until the transaction ends: every read-change-write of
    the profile runs behind it, so concurrent requests never pass a cap or lose each other's change."""
    await db.execute(pg_insert(FitnessProfile.__table__).values(user_id=user_id)
                     .on_conflict_do_nothing(index_elements=["user_id"]))
    row = (await db.execute(
        select(FitnessProfile).where(FitnessProfile.user_id == user_id).with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one()
    for name in (*LIST_FIELDS, "forgotten"):
        if not isinstance(getattr(row, name), list):
            setattr(row, name, [])
    return row


def _set_list(row: FitnessProfile, name: str, values: list[Any]) -> None:
    setattr(row, name, list(values))  # a new list object: SQLAlchemy sees the JSONB change


def unforget(row: FitnessProfile, *names: str) -> None:
    keys = {_key(n) for n in names if n}
    kept = [f for f in (row.forgotten or []) if isinstance(f, dict) and _key(str(f.get("n") or "")) not in keys]
    if len(kept) != len(row.forgotten or []):
        _set_list(row, "forgotten", kept)


def note_forgotten(row: FitnessProfile, name: str, tag: str | None = None, *, field_name: str | None = None,
                   value: str | None = None) -> None:
    """Remember that the member asked to forget ``name`` (newest first, ``FORGOTTEN_MAX`` kept). A vocabulary item
    keeps its field and slug, so every way of saying it ("running", "jogging") stays out of the AI's view of past
    chats (``MemberContext.hide_from_history``)."""
    text = clean_text(name, FORGOTTEN_ITEM_MAX)
    if not text:
        return
    rest = [f for f in (row.forgotten or []) if isinstance(f, dict) and _key(str(f.get("n") or "")) != _key(text)]
    entry: dict[str, Any] = {"n": text, "at": date.today().isoformat()}
    if tag:
        entry["tag"] = str(tag)[:40]
    if field_name in VOCAB and value in VOCAB[field_name]:
        entry["f"], entry["v"] = field_name, value
    _set_list(row, "forgotten", [entry, *rest][:FORGOTTEN_MAX])


@dataclass(slots=True)
class Change:
    status: str  # added | known | full | removed | missing | set | same
    field: str
    value: Any = None
    previous: Any = None
    cap: int | None = None


async def add_item(db: AsyncSession, user_id: int, field_name: str, value: str) -> Change:
    """Add one list value (a slug or an own label) - 'known' when it is there already, 'full' at the cap. An exercise
    type moves between 'likes' and 'avoids' (one can't enjoy and avoid the same thing). Caller commits."""
    if field_name not in LIST_FIELDS:
        raise ValueError(field_name)
    row = await lock_row(db, user_id)
    current = list(getattr(row, field_name) or [])
    if contains(current, value):
        return Change("known", field_name, value)
    cap = LIST_CAPS[field_name]
    if len(current) >= cap:
        return Change("full", field_name, value, cap=cap)
    other = {"likes": "avoids", "avoids": "likes"}.get(field_name)
    moved_from = None
    if other is not None:
        theirs = list(getattr(row, other) or [])
        if contains(theirs, value):
            _set_list(row, other, [v for v in theirs if not _same(v, value)])
            moved_from = other
    _set_list(row, field_name, [*current, value])
    unforget(row, label_of(field_name, value), value)
    return Change("added", field_name, value, previous=moved_from, cap=cap)


async def remove_item(db: AsyncSession, user_id: int, field_name: str, value: str, *, forget: bool = False) -> Change:
    if field_name not in LIST_FIELDS:
        raise ValueError(field_name)
    row = await lock_row(db, user_id)
    current = list(getattr(row, field_name) or [])
    kept = [v for v in current if not _same(v, value)]
    if len(kept) == len(current):
        return Change("missing", field_name, value)
    _set_list(row, field_name, kept)
    if forget:
        note_forgotten(row, label_of(field_name, value), field_name=field_name, value=value)
    return Change("removed", field_name, value)


SCALARS = ("sessions_per_week", "session_minutes", "preferred_time", "notes")


def valid_scalar(field_name: str, value: Any) -> bool:
    if value is None:
        return True
    if field_name == "sessions_per_week":
        return isinstance(value, int) and SESSIONS_RANGE[0] <= value <= SESSIONS_RANGE[1]
    if field_name == "session_minutes":
        return isinstance(value, int) and MINUTES_RANGE[0] <= value <= MINUTES_RANGE[1]
    if field_name == "preferred_time":
        return value in TIME_LABELS
    if field_name == "notes":
        return isinstance(value, str) and len(value) <= NOTES_MAX
    return False


async def set_scalar(db: AsyncSession, user_id: int, field_name: str, value: Any, *, forget: bool = False) -> Change:
    """Set (or clear, ``None``) one scalar field; ``previous`` carries the old value for an undo. Caller commits."""
    if field_name not in SCALARS or not valid_scalar(field_name, value):
        raise ValueError(field_name)
    row = await lock_row(db, user_id)
    previous = getattr(row, field_name)
    if previous == value:
        return Change("same", field_name, value, previous=previous)
    setattr(row, field_name, value)
    if forget and previous is not None:
        note_forgotten(row, f"{FIELD_LABELS[field_name]}: {describe_scalar(field_name, previous)}")
    return Change("set", field_name, value, previous=previous)


def describe_scalar(field_name: str, value: Any) -> str:
    if value is None:
        return "not set"
    if field_name == "sessions_per_week":
        return f"{value} a week"
    if field_name == "session_minutes":
        return f"about {value} minutes"
    if field_name == "preferred_time":
        return TIME_LABELS.get(str(value), str(value)).lower()
    return str(value)


async def save_form(db: AsyncSession, user_id: int, data: FitnessData) -> list[str]:
    """Write the edit page's data (already validated) under the row lock; returns the changed field labels."""
    row = await lock_row(db, user_id)
    changed: list[str] = []
    for name in LIST_FIELDS:
        new = list(getattr(data, name))[: LIST_CAPS[name]]
        if list(getattr(row, name) or []) != new:
            unforget(row, *(label_of(name, v) for v in new))  # typed back in: no longer hidden from the AI
            _set_list(row, name, new)
            changed.append(FIELD_LABELS[name].lower())
    for name in SCALARS:
        new = getattr(data, name)
        if getattr(row, name) != new:
            setattr(row, name, new)
            changed.append(FIELD_LABELS[name].lower())
    return changed
