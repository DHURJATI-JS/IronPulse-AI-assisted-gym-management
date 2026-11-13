"""Exercise safety guardrails (WP-E2).

AI output is *advice*: every workout plan - AI-made, template or trainer - passes through
``filter_plan`` before it is stored or shown. The filter is fully deterministic:

* each item is matched to the exercise library (slug, exact or fuzzy name);
* it is rejected when one of the customer's safety tags is listed in ``Exercise.contraindications``,
  when the exercise's attributes break a rule (high impact, high intensity, wrong category), or when a
  keyword heuristic matches (also covers exercises the library does not know);
* rejected items are replaced with a safe library exercise of the same category (or removed);
* prescriptions are clamped (sets 1-6, reps 1-30, duration <= 180 s, rest 15-300 s) and "1RM"/"max"
  load wording is rewritten.

Safety tags are the keys of ``app.services.conditions.CONDITION_TAGS`` plus ``"minor"``.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from app.services.conditions import CONDITION_TAGS, TAG_LABELS

LIMITS: dict[str, tuple[int, int]] = {"sets": (1, 6), "reps": (1, 30), "duration_s": (5, 180), "rest_s": (15, 300)}
CATEGORIES = ("calisthenics", "strength", "cardio", "mobility", "breathing")
_INTENSITY = {"low": 0, "moderate": 1, "high": 2}
_LEVELS = {"beginner": 0, "intermediate": 1, "advanced": 2}


# --------------------------------------------------------------------------- library view
@dataclass(frozen=True, slots=True)
class LibExercise:
    """Plain, session-independent view of an ``Exercise`` row (or a built-in fallback exercise)."""

    slug: str
    name: str
    category: str
    goals: tuple[str, ...] = ()
    muscles: tuple[str, ...] = ()
    equipment: str = "none"
    difficulty: str = "beginner"
    intensity: str = "moderate"
    impact: str = "low"
    contraindications: tuple[str, ...] = ()
    video_slug: str | None = None
    description: str = ""
    cues: tuple[str, ...] = ()

    @classmethod
    def from_model(cls, ex: Any) -> "LibExercise":
        def tup(v: Any) -> tuple[str, ...]:
            return tuple(str(x).strip().lower() for x in (v or []) if str(x).strip())

        difficulty = getattr(ex.difficulty, "value", ex.difficulty) or "beginner"
        return cls(
            slug=str(ex.slug), name=str(ex.name), category=str(ex.category or "strength").lower(), goals=tup(ex.goals),
            muscles=tup(ex.muscles), equipment=str(ex.equipment or "none"), difficulty=str(difficulty),
            intensity=str(ex.intensity or "moderate").lower(), impact=str(ex.impact or "low").lower(),
            contraindications=tup(ex.contraindications), video_slug=ex.video_slug, description=ex.description or "",
            cues=tuple(str(c) for c in (ex.cues or [])),
        )


# --------------------------------------------------------------------------- rules
@dataclass(frozen=True, slots=True)
class SafetyRule:
    tag: str
    label: str
    avoid: str  # human readable summary (prompt + UI)
    patterns: tuple[tuple[str, str], ...] = ()  # (regex on exercise name, reason)
    max_intensity: str | None = None  # library exercises above this intensity are rejected
    no_high_impact: bool = False
    allowed_categories: tuple[str, ...] | None = None
    cap_load: bool = False  # heavy -> moderate, RPE capped, no training to failure
    min_reps: int | None = None  # avoid heavy low-rep work
    strip_weight_talk: bool = False
    note: str = ""


_JUMP = (r"\bjump|plyo|\bhops?\b|hopping|\bbounds?\b|bounding|burpee|skipping|jump rope|box jump|tuck jump|split jump|"
         r"\bsprawl", "jumping / plyometric impact")
_SPRINT = (r"\bsprint|\bhiit\b|tabata|all[- ]out|max(imal)?[- ]effort (cardio|interval|sprint)", "sprints / HIIT")
_ONE_RM = (r"\b1\s*-?\s*rm\b|one[- ]rep[- ]max|\bmax(?:imal|imum)?\b|all[- ]out|\bpr attempt", "1RM / max-effort lifting")
_INVERSION = (r"head\s*-?stand|hand\s*-?stand|inver(sion|ted)|decline (bench|sit|crunch|push)|headstand", "head-down / inverted positions")
_VALSALVA = (r"valsalva|breath[- ]?hold", "breath holding (Valsalva)")
_ISOMETRIC = (r"heavy (isometric|hold|static)|isometric|wall sit|static hold|weighted plank|plank hold (with|weighted)", "heavy isometric holds")
_OVERHEAD = (r"overhead|military press|shoulder press|push press|arnold press|\bohp\b|snatch|\bjerks?\b|clean (and|&) jerk|"
             r"behind[- ]the[- ]neck|upright row|\bthruster", "overhead pressing")
_FLEXION = (r"sit[- ]?ups?|crunch|toe[- ]touch|jefferson curl|russian twist|\bv[- ]?ups?\b|good[- ]?mornings?", "loaded spinal flexion")
_DEADLIFT = (r"dead[- ]?lift|good[- ]?mornings?", "heavy deadlifts / good mornings")
_HEAVY_LOWER = (r"(back|front|barbell|zercher|overhead) squat|pistol|sissy squat|deep squat|ass[- ]to[- ]grass|\batg\b",
                "deep loaded squats")
_BALANCE = (r"bosu|balance board|slack ?line|wobble board|stability ball (stand|squat)|single[- ]leg .*unstable|tightrope",
            "risky balance work")
_SUPINE = (r"supine|lying|bench press|floor press|chest press|crunch|sit[- ]?ups?|dead ?bug|leg raise|glute bridge|"
           r"hip thrust|skull ?crusher|pullover", "lying on your back (supine)")
_RAPID = (r"burpee|sprawl|get[- ]?up|up[- ]?downs?|forward roll|somersault|spinning|rapid|mountain climber", "rapid position changes")
_COLD = (r"cold[- ]air|cold plunge|ice bath|winter run|outdoor run|cold water", "cold exposure")
_HOT = (r"hot yoga|bikram|sauna|sweat suit|heated room", "exercise in heat")

RULES: dict[str, SafetyRule] = {
    "asthma": SafetyRule("asthma", "Asthma", "unwarmed sprints / HIIT and cold-air running",
                         (_SPRINT, _COLD), note="Asthma: warm up for at least 10 minutes, keep your reliever inhaler nearby and avoid cold, dry air."),
    "copd": SafetyRule("copd", "COPD", "sprints / HIIT, breath holding and cold air",
                       (_SPRINT, _COLD, _VALSALVA), max_intensity="moderate",
                       note="COPD: keep to a pace where you can speak in short sentences; use pursed-lip breathing."),
    "hypertension": SafetyRule("hypertension", "High blood pressure",
                               "heavy isometric holds, max-effort lifts, Valsalva/breath holding, 1RM tests and head-down positions (RPE <= 6-7)",
                               (_ONE_RM, _VALSALVA, _ISOMETRIC, _INVERSION), max_intensity="moderate", cap_load=True, min_reps=8,
                               note="High blood pressure: breathe out on every effort, never hold your breath and stay at RPE 6-7."),
    "cardiac": SafetyRule("cardiac", "Heart condition",
                          "max-effort work, heavy isometric holds, Valsalva, 1RM tests and head-down positions (RPE <= 6-7)",
                          (_ONE_RM, _VALSALVA, _ISOMETRIC, _INVERSION, _SPRINT), max_intensity="moderate", cap_load=True, min_reps=8,
                          note="Heart condition: train only with your doctor's clearance, stay at RPE 6-7 and stop immediately if you feel chest pain or dizziness."),
    "hypotension": SafetyRule("hypotension", "Low blood pressure", "rapid position changes and head-down positions",
                              (_RAPID, _INVERSION),
                              note="Low blood pressure: rise slowly from the floor, hydrate well and sit down if you feel light-headed."),
    "diabetes": SafetyRule("diabetes", "Diabetes", "training while your blood sugar is out of range",
                           note="Diabetes: check your blood sugar before and after training and keep fast-acting carbohydrates with you."),
    "epilepsy": SafetyRule("epilepsy", "Epilepsy", "heavy overhead lifts and risky balance work",
                           (_OVERHEAD, _BALANCE, _ONE_RM, (r"rope climb|box jump", "fall-risk exercises")),
                           note="Epilepsy: train with a partner who knows your seizure plan; avoid heavy loads over your head."),
    "pregnancy": SafetyRule("pregnancy", "Pregnancy",
                            "supine work after the first trimester, jumping, contact sports, heavy lifting and overheating "
                            "(hot yoga, saunas, hot tubs)",
                            (_SUPINE, _JUMP, _ONE_RM, (r"contact|sparring|boxing|martial|\bheavy\b", "contact or heavy lifting"),
                             _VALSALVA, _INVERSION,
                             (r"hot yoga|bikram|sauna|hot tub|steam room|sweat suit|heated room",
                              "overheating (hot yoga, saunas, hot tubs)")),
                            max_intensity="moderate", no_high_impact=True, cap_load=True, min_reps=8,
                            note="Pregnancy: follow your obstetrician's advice; avoid lying on your back after the first trimester and stop if anything feels wrong."),
    "knee_injury": SafetyRule("knee_injury", "Knee injury", "jumping / plyometrics, deep loaded squats and impact lunges",
                              (_JUMP, _HEAVY_LOWER, (r"(jump|jumping|walking|plyo|split)[- ]lunge|lunge jump|deep lunge", "impact lunges"),
                               (r"\bsprint", "sprinting impact")),
                              no_high_impact=True,
                              note="Knee injury: keep movements pain-free, control the lowering phase and avoid jumping."),
    "lower_back": SafetyRule("lower_back", "Back problem", "heavy deadlifts, good mornings and loaded spinal flexion",
                             (_DEADLIFT, _FLEXION, (r"(back|front|barbell) squat|kettlebell swing|bent[- ]over row", "heavy spinal loading")),
                             note="Back problem: keep a neutral spine, brace gently and choose supported variations."),
    "shoulder_injury": SafetyRule("shoulder_injury", "Shoulder injury", "overhead pressing, dips and kipping",
                                  (_OVERHEAD, (r"\bdips?\b|bench dips?|kip(ping)?|muscle[- ]?ups?|butterfly pull", "dips / kipping"),
                                   (r"hand\s*-?stand|pike push", "overhead loading")),
                                  note="Shoulder injury: work in a pain-free range below shoulder height."),
    "wrist_injury": SafetyRule("wrist_injury", "Wrist injury", "push-ups on flat palms and handstands",
                               ((r"push[- ]?ups?|press[- ]?ups?|high plank|bear crawl|planche|burpee", "weight on flat palms"),
                                (r"hand\s*-?stand|crow pose|handstand", "handstands / hand balancing")),
                               note="Wrist injury: use fists, push-up handles or forearm variations to keep the wrist neutral."),
    "ankle_injury": SafetyRule("ankle_injury", "Ankle / foot injury", "jumping, sprinting and skipping",
                               (_JUMP, (r"\bsprint|\brun|jog|skip", "running impact")), no_high_impact=True,
                               note="Ankle injury: choose seated or supported cardio such as cycling or rowing."),
    "neck_injury": SafetyRule("neck_injury", "Neck problem", "headstands, neck bridges and loading behind the neck",
                              ((r"head\s*-?stand|neck bridge|wrestler.?s bridge|behind[- ]the[- ]neck|heavy shrug", "neck loading"),),
                              note="Neck problem: keep the neck neutral and never pull on your head during core work."),
    "arthritis": SafetyRule("arthritis", "Arthritis", "high-impact jumping and deep loaded squats",
                            (_JUMP, _HEAVY_LOWER), no_high_impact=True,
                            note="Arthritis: warm up thoroughly; low-impact work and a full pain-free range are best."),
    "osteoporosis": SafetyRule("osteoporosis", "Osteoporosis", "loaded spinal flexion and jumping",
                               (_FLEXION, _JUMP), no_high_impact=True,
                               note="Osteoporosis: keep a long spine, avoid rounding forward under load and prioritise balance training with support."),
    "hernia": SafetyRule("hernia", "Hernia", "heavy lifting and Valsalva / breath holding",
                         (_ONE_RM, _VALSALVA, _DEADLIFT, _HEAVY_LOWER, (r"\bheavy\b|leg press|clean|snatch", "heavy lifting"),
                          (r"sit[- ]?ups?|crunch|\bv[- ]?ups?\b|leg raise", "high abdominal pressure")),
                         max_intensity="moderate", cap_load=True, min_reps=10,
                         note="Hernia: keep loads light, exhale on effort and stop if you feel a bulge or pain."),
    "recent_surgery": SafetyRule("recent_surgery", "Recent surgery", "anything beyond gentle mobility until a doctor clears you",
                                 allowed_categories=("mobility", "breathing"), max_intensity="low", no_high_impact=True, cap_load=True,
                                 note="Recent surgery: only gentle mobility and breathing until your surgeon or doctor gives medical clearance."),
    "obesity": SafetyRule("obesity", "Obesity", "high-impact jumping early on", (_JUMP,), no_high_impact=True,
                          note="Build up with low-impact cardio (walking, cycling, rowing) to protect your joints."),
    "vertigo": SafetyRule("vertigo", "Vertigo / dizziness", "rapid position changes and inversions",
                          (_RAPID, _INVERSION, _BALANCE), note="Vertigo: change positions slowly and train near a wall or support."),
    "migraine": SafetyRule("migraine", "Migraine", "max-effort straining, breath holding and inversions",
                           (_ONE_RM, _VALSALVA, _INVERSION), note="Migraine: stay hydrated and keep intensity steady."),
    "anemia": SafetyRule("anemia", "Anaemia", "sprints / HIIT and max-effort work", (_SPRINT, _ONE_RM), max_intensity="moderate",
                         note="Anaemia: build intensity gradually and rest if you feel breathless or dizzy."),
    "thyroid": SafetyRule("thyroid", "Thyroid disorder", "sudden very high-intensity work",
                          note="Thyroid disorder: increase intensity gradually and watch your heart rate."),
    "kidney": SafetyRule("kidney", "Kidney disease", "exhausting high-intensity sessions and dehydration", max_intensity="moderate",
                         note="Kidney disease: follow your nephrologist's fluid advice and keep sessions moderate."),
    "eating_disorder": SafetyRule("eating_disorder", "Eating disorder", "weight-loss and calorie talk", strip_weight_talk=True,
                                  note="Your plan focuses on strength, energy and wellbeing - not on weight or calories."),
    "heat_intolerance": SafetyRule("heat_intolerance", "Heat intolerance", "hot rooms, saunas and long hard cardio in heat",
                                   (_HOT,), note="Heat intolerance: train in a cool, ventilated room and take water breaks."),
    "cold_sensitivity": SafetyRule("cold_sensitivity", "Cold sensitivity", "cold exposure (ice baths, cold-air running)",
                                   (_COLD,), note="Cold sensitivity: warm up indoors and keep hands and feet warm."),
    "respiratory_allergy": SafetyRule("respiratory_allergy", "Respiratory allergy", "outdoor cardio on high-pollen days",
                                      ((r"outdoor|trail run", "outdoor exposure"),),
                                      note="Respiratory allergy: prefer indoor cardio on high-pollen or dusty days."),
    "skin_allergy": SafetyRule("skin_allergy", "Skin allergy", "latex equipment",
                               ((r"latex", "latex equipment"),), note="Skin allergy: wipe equipment and use latex-free bands."),
    "food_allergy": SafetyRule("food_allergy", "Food allergy", "nothing exercise-specific",
                               note="Food allergy: keep your emergency medication with you at the gym."),
    "mental_health": SafetyRule("mental_health", "Anxiety / depression", "nothing - movement helps",
                                note="Regular, enjoyable movement supports mood; it is fine to scale a session down on hard days."),
    "multiple_sclerosis": SafetyRule("multiple_sclerosis", "Multiple sclerosis", "overheating, sprints and risky balance work",
                                     (_SPRINT, _BALANCE, _HOT), max_intensity="moderate",
                                     note="Multiple sclerosis: keep cool, rest between sets and train near support."),
    "minor": SafetyRule("minor", "Under 18", "1RM / max lifts - moderate loads with a technique focus",
                        (_ONE_RM,), cap_load=True, min_reps=6,
                        note="Under 18: technique first - moderate loads, no max lifts, and a guardian is informed about this plan."),
}

# every condition tag has a rule (a missing tag would silently disable the guardrail)
for _tag in CONDITION_TAGS:
    RULES.setdefault(_tag, SafetyRule(_tag, TAG_LABELS.get(_tag, _tag), "nothing specific"))

_COMPILED: dict[str, tuple[tuple[re.Pattern[str], str], ...]] = {
    tag: tuple((re.compile(rx, re.IGNORECASE), why) for rx, why in rule.patterns) for tag, rule in RULES.items()
}


def tag_label(tag: str) -> str:
    rule = RULES.get(tag)
    return rule.label if rule else tag.replace("_", " ").capitalize()


def normalize_tags(tags: Iterable[str] | None, age: int | None = None) -> list[str]:
    out = {str(t).strip().lower() for t in (tags or []) if str(t or "").strip()}
    if age is not None and age < 18:
        out.add("minor")
    return sorted(t for t in out if t in RULES)


def avoid_lines(tags: Iterable[str]) -> list[str]:
    """Compact "AVOID" lines for prompts: ``asthma: unwarmed sprints / HIIT ...``."""
    return [f"{t}: {RULES[t].avoid}" for t in tags if t in RULES and RULES[t].avoid != "nothing specific"]


def safety_notes(tags: Iterable[str]) -> list[str]:
    return [RULES[t].note for t in tags if t in RULES and RULES[t].note]


# --------------------------------------------------------------------------- matching
_WORD = re.compile(r"[a-z0-9]+")


def _norm(name: str | None) -> str:
    words = []
    for w in _WORD.findall((name or "").lower().replace("-", " ")):
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        words.append(w)
    return " ".join(words)


class LibraryIndex:
    def __init__(self, library: Iterable[LibExercise]) -> None:
        self.items = list(library)
        self.by_slug = {e.slug.lower(): e for e in self.items}
        self.by_norm = {_norm(e.name): e for e in self.items}
        for e in self.items:
            self.by_norm.setdefault(_norm(e.slug), e)
        self._names = list(self.by_norm)

    def match(self, name: str) -> LibExercise | None:
        if not name:
            return None
        raw = name.strip().lower()
        if raw in self.by_slug:
            return self.by_slug[raw]
        key = _norm(name)
        if key in self.by_norm:
            return self.by_norm[key]
        slugged = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
        if slugged in self.by_slug:
            return self.by_slug[slugged]
        close = difflib.get_close_matches(key, self._names, n=1, cutoff=0.86)
        if close:
            return self.by_norm[close[0]]
        # "Barbell Back Squat (heavy)" -> library "back squat": every library word present, in order
        best: LibExercise | None = None
        for lib_key, ex in self.by_norm.items():
            if len(lib_key) >= 6 and re.search(r"\b" + re.escape(lib_key) + r"\b", key):
                if best is None or len(lib_key) > len(_norm(best.name)):
                    best = ex
        return best


def guess_category(name: str) -> str:
    n = (name or "").lower()
    if re.search(r"breath|pranayama", n):
        return "breathing"
    if re.search(r"stretch|mobility|yoga|foam roll|cat[- ]cow|rotation|child.?s pose|range of motion|circles", n):
        return "mobility"
    if re.search(r"\brun|jog|sprint|cycl|bike|rowing|rower|elliptical|swim|walk|skipping|jump rope|stair|treadmill|hiit|cardio", n):
        return "cardio"
    if re.search(r"push[- ]?up|pull[- ]?up|chin[- ]?up|\bdips?\b|plank|hand\s*-?stand|muscle[- ]?up|burpee|bodyweight|"
                 r"hollow|l[- ]sit|pistol|mountain climber|jumping jack|\bv[- ]?ups?\b|lunge|jump|plyo|\bhops?\b|bound|"
                 r"sit[- ]?ups?|crunch|leg raise|bridge|superman|bird[- ]?dog|dead ?bug|crawl", n):
        return "calisthenics"
    return "strength"


_MUSCLE_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (r"squat|lunge|jump|leg press|step[- ]?up|split|pistol|box|wall sit|bound|hop", ("quads", "glutes")),
    (r"dead[- ]?lift|good[- ]?morning|hinge|swing|hamstring|leg curl|bridge|thrust", ("hamstrings", "glutes")),
    (r"bench|push[- ]?up|press[- ]?up|chest|fly|\bdips?\b", ("chest", "triceps")),
    (r"overhead|shoulder|military|arnold|lateral raise|push press|hand\s*-?stand|pike", ("shoulders", "triceps")),
    (r"row|pull|chin|lat |pulldown|muscle[- ]?up", ("back", "lats")),
    (r"curl", ("biceps",)),
    (r"triceps|skull|pushdown|kickback", ("triceps",)),
    (r"plank|crunch|sit[- ]?up|core|hollow|twist|leg raise|\bv[- ]?up|ab ", ("core",)),
    (r"calf", ("calves",)),
)


def guess_muscles(name: str) -> tuple[str, ...]:
    n = (name or "").lower()
    for rx, muscles in _MUSCLE_HINTS:
        if re.search(rx, n):
            return muscles
    return ()


def check_exercise(ex: LibExercise | None, name: str, tags: Iterable[str]) -> str | None:
    """Return a human-readable reason why the exercise is unsafe for ``tags`` - or None when it is safe."""
    text = " ".join(filter(None, [name, ex.name if ex else None, ex.slug.replace("-", " ") if ex else None]))
    for tag in tags:
        rule = RULES.get(tag)
        if rule is None:
            continue
        if ex is not None:
            if tag in ex.contraindications:
                return f"{rule.label}: {ex.name} is contraindicated"
            if rule.allowed_categories and ex.category not in rule.allowed_categories:
                return f"{rule.label}: {rule.avoid}"
            if rule.no_high_impact and ex.impact == "high":
                return f"{rule.label}: high-impact exercise"
            if rule.max_intensity and _INTENSITY.get(ex.intensity, 1) > _INTENSITY[rule.max_intensity]:
                return f"{rule.label}: intensity too high"
        elif rule.allowed_categories and guess_category(name) not in rule.allowed_categories:
            return f"{rule.label}: {rule.avoid}"
        for rx, why in _COMPILED.get(tag, ()):
            if rx.search(text):
                return f"{rule.label}: avoid {why}"
    return None


def is_safe(ex: LibExercise, tags: Iterable[str]) -> bool:
    return check_exercise(ex, ex.name, list(tags)) is None


def safe_library(library: Iterable[LibExercise], tags: Iterable[str]) -> list[LibExercise]:
    tags = list(tags)
    return [e for e in library if is_safe(e, tags)]


def find_replacement(category: str, library: Iterable[LibExercise], tags: Iterable[str], *, used: set[str] | None = None,
                     muscles: Iterable[str] = (), goal: str | None = None, level: str | None = None) -> LibExercise | None:
    tags = list(tags)
    used = used or set()
    want = {m.lower() for m in muscles}
    lvl = _LEVELS.get(level or "beginner", 0)
    pool = [e for e in library if e.slug not in used and is_safe(e, tags)]

    def rank(e: LibExercise) -> tuple:
        return (
            -len(want & set(e.muscles)),
            0 if (goal and goal in e.goals) else 1,
            0 if _LEVELS.get(e.difficulty, 0) <= lvl else 1,
            e.slug,
        )

    for cat in (category, "mobility", "breathing"):
        candidates = sorted((e for e in pool if e.category == cat), key=rank)
        if candidates:
            return candidates[0]
    return None


# --------------------------------------------------------------------------- prescription
_NUM = re.compile(r"\d+(?:\.\d+)?")


def to_int(value: Any) -> int | None:
    """``12`` -> 12, ``"8-12"`` -> 10, ``"45 s"`` -> 45, ``"AMRAP"`` -> None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(round(value))
    nums = [float(n) for n in _NUM.findall(str(value))[:2]]
    if not nums:
        return None
    return int(round(sum(nums) / len(nums)))


def clamp(value: int | None, key: str) -> int | None:
    if value is None:
        return None
    lo, hi = LIMITS[key]
    return max(lo, min(hi, value))


_PCT_1RM = re.compile(r"(?:~\s*)?\b\d{1,3}\s*(?:-\s*\d{1,3}\s*)?%\s*(?:of\s+)?(?:your\s+)?(?:1\s*-?\s*rm|one[- ]rep[- ]max|max)\b", re.I)
_1RM = re.compile(r"\b(?:1\s*-?\s*rm|one[- ]rep[- ]max|1[- ]rep[- ]max|rep[- ]max)(?:\s+(?:test(?:ing)?|attempts?|day))?\b", re.I)
_MAX = re.compile(r"\b(?:max(?:imum|imal)?|all[- ]out)(?:[- ](?:effort|load|weight|lift|reps?|out|intensity))?\b", re.I)
_FAILURE = re.compile(r"\b(?:to|until|till)\s+(?:muscle\s+|total\s+)?failure\b", re.I)
_HEAVY = re.compile(r"\b(?:very\s+|really\s+)?heavy\b", re.I)
_RPE = re.compile(r"\bRPE\s*(\d{1,2}(?:\.\d)?)(?:\s*[-–]\s*(\d{1,2}(?:\.\d)?))?", re.I)
_WEIGHT_TALK = re.compile(
    r"calori|kcal|burn(?:s|ing)? fat|fat[- ]?burn|fat loss|lose weight|losing weight|weight[- ]?loss|lose \d+\s*(?:kg|kgs|lb|lbs|pounds|kilos)|"
    r"deficit|\bcutting\b|\bslim|skinny|body ?fat|\bbmi\b", re.I)
_SENTENCE = re.compile(r"[^.!?\n]+[.!?]?")


def sanitize_load(text: str | None, tags: Iterable[str]) -> str | None:
    """Strip 1RM / max wording for everyone; cap 'heavy' / RPE / failure for load-capped tags."""
    if text is None:
        return None
    tags = list(tags)
    t = str(text).strip()[:120]
    t = _PCT_1RM.sub("a moderate load (RPE 6-7)", t)
    t = _1RM.sub("a moderate load", t)
    t = _MAX.sub("controlled effort", t)
    if any(RULES[x].cap_load for x in tags if x in RULES):
        t = _HEAVY.sub("moderate", t)
        t = _FAILURE.sub("stopping 2-3 reps short of failure", t)

        def _rpe(m: re.Match[str]) -> str:
            hi = float(m.group(2) or m.group(1))
            return "RPE 6-7" if hi > 7 else m.group(0)

        t = _RPE.sub(_rpe, t)
    return re.sub(r"\s{2,}", " ", t).strip() or None


def strip_weight_talk(text: str | None) -> str | None:
    if not text:
        return text
    kept = [s.strip() for s in _SENTENCE.findall(text) if s.strip() and not _WEIGHT_TALK.search(s)]
    return " ".join(kept) or None


def _clean_text(text: Any, tags: list[str], limit: int = 300) -> str | None:
    if text is None:
        return None
    t = re.sub(r"\s+", " ", str(text)).strip()[:limit]
    t = sanitize_load(t, tags) or ""
    if "eating_disorder" in tags:
        t = strip_weight_talk(t) or ""
    return t or None


def normalize_item(item: dict[str, Any], ex: LibExercise | None, tags: list[str]) -> dict[str, Any]:
    category = ex.category if ex else guess_category(str(item.get("exercise", "")))
    sets = clamp(to_int(item.get("sets")) or 3, "sets")
    reps = clamp(to_int(item.get("reps")), "reps")
    duration = clamp(to_int(item.get("duration_s")), "duration_s")
    rest = clamp(to_int(item.get("rest_s")) or (30 if category in ("mobility", "breathing") else 60), "rest_s")
    if reps is None and duration is None:
        if category in ("cardio", "mobility", "breathing"):
            duration = 60
        else:
            reps = 10
    min_reps = max((RULES[t].min_reps or 0) for t in tags) if tags else 0
    if reps is not None and min_reps and category in ("strength", "calisthenics") and reps < min_reps:
        reps = min_reps
    capped = any(RULES[t].cap_load for t in tags if t in RULES)
    if capped and duration is not None and category in ("strength", "calisthenics") and duration > 45:
        duration = 45  # no long isometric holds when loads are capped (blood pressure, pregnancy, minors ...)
    return {
        "exercise": (ex.name if ex else str(item.get("exercise", "")).strip())[:120],
        "slug": ex.slug if ex else None,
        "category": category,
        "sets": sets,
        "reps": reps,
        "duration_s": duration,
        "load": _clean_text(item.get("load"), tags, 120),
        "rest_s": rest,
        "notes": _clean_text(item.get("notes"), tags, 300),
        "video_slug": ex.video_slug if ex else None,
    }


# --------------------------------------------------------------------------- the filter
def _as_dict(plan: Any) -> dict[str, Any]:
    if plan is None:
        return {}
    if hasattr(plan, "model_dump"):
        return plan.model_dump()
    return dict(plan)


def filter_plan(plan: Any, tags: Iterable[str] | None, age: int | None = None, library: Iterable[LibExercise] | None = None,
                *, goal: str | None = None, level: str | None = None) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    """Make a plan safe for the customer's safety tags.

    Returns ``(safe_plan, removed, notes)`` where ``removed`` is ``[{exercise, reason, replacement, day}]``.
    ``library`` defaults to the built-in exercise list (``app.services.coach.BUILTIN_LIBRARY``)."""
    if library is None:
        from app.services.coach import BUILTIN_LIBRARY

        library = BUILTIN_LIBRARY
    tag_list = normalize_tags(tags, age)
    index = LibraryIndex(library)
    data = _as_dict(plan)
    removed: list[dict[str, Any]] = []
    days_out: list[dict[str, Any]] = []
    for d_i, day in enumerate(list(data.get("days") or [])[:7]):
        day = _as_dict(day)
        day_name = re.sub(r"\s+", " ", str(day.get("name") or f"Day {d_i + 1}")).strip()[:60] or f"Day {d_i + 1}"
        used: set[str] = set()
        items_out: list[dict[str, Any]] = []
        for raw in list(day.get("items") or [])[:12]:
            item = _as_dict(raw)
            name = re.sub(r"\s+", " ", str(item.get("exercise") or "")).strip()[:120]
            if not name:
                continue
            ex = index.match(name)
            reason = check_exercise(ex, name, tag_list)
            if reason is None and (ex is not None and ex.slug in used):
                continue  # duplicate within the day
            if reason is not None:
                category = ex.category if ex else guess_category(name)
                repl = find_replacement(category, index.items, tag_list, used=(used | {ex.slug}) if ex else used,
                                        muscles=ex.muscles if ex else guess_muscles(name), goal=goal, level=level)
                removed.append({"exercise": name, "reason": reason, "replacement": repl.name if repl else None, "day": day_name})
                if repl is None:
                    continue
                item = {**item, "exercise": repl.name, "notes": f"Safer alternative to {name}."}
                if repl.category in ("mobility", "breathing") and category not in ("mobility", "breathing"):
                    item.update(reps=None, duration_s=45, load="Gentle, pain-free range")
                elif repl.equipment in ("none", "mat"):
                    item["load"] = "Bodyweight"
                elif repl.category == "cardio":
                    item.update(reps=None, load="Conversational pace")
                ex = repl
            out = normalize_item(item, ex, tag_list)
            used.add(ex.slug if ex else _norm(name))
            items_out.append(out)
        if items_out:
            focus = _clean_text(day.get("focus"), tag_list, 120)
            days_out.append({"name": _clean_text(day_name, tag_list, 60) or f"Day {d_i + 1}", "focus": focus, "items": items_out})
    raw_title = re.sub(r"\s+", " ", str(data.get("title") or "")).strip()[:160]
    title = _clean_text(raw_title, tag_list, 160)
    if not title or title != raw_title:  # rewritten "max"/"1RM"/weight wording reads badly in a title
        title = "Your personalised training plan" if raw_title else "Your training plan"
    weekly = [w for w in (_clean_text(n, tag_list, 300) for n in list(data.get("weekly_notes") or [])[:8]) if w]
    notes = safety_notes(tag_list)
    if removed:
        swapped = sum(1 for r in removed if r["replacement"])
        dropped = len(removed) - swapped
        parts = []
        if swapped:
            parts.append(f"{swapped} exercise{'s' if swapped != 1 else ''} swapped for safer alternatives")
        if dropped:
            parts.append(f"{dropped} removed")
        notes.append("Safety filter: " + " and ".join(parts) + ".")
    safe_plan = {"title": title, "days": days_out, "weekly_notes": weekly}
    return safe_plan, removed, notes


def plan_is_safe(plan: dict[str, Any], tags: Iterable[str], library: Iterable[LibExercise] | None = None) -> bool:
    """True when every item of an (already filtered) plan passes the guardrail - used by tests and audits."""
    if library is None:
        from app.services.coach import BUILTIN_LIBRARY

        library = BUILTIN_LIBRARY
    index = LibraryIndex(library)
    tag_list = normalize_tags(tags)
    for day in plan.get("days", []):
        for item in day.get("items", []):
            if check_exercise(index.match(item["exercise"]), item["exercise"], tag_list) is not None:
                return False
    return True


__all__ = [
    "LIMITS", "RULES", "LibExercise", "LibraryIndex", "SafetyRule", "avoid_lines", "check_exercise", "clamp", "filter_plan",
    "find_replacement", "guess_category", "guess_muscles", "is_safe", "normalize_item", "normalize_tags", "plan_is_safe", "safe_library",
    "safety_notes", "sanitize_load", "strip_weight_talk", "tag_label", "to_int",
]
