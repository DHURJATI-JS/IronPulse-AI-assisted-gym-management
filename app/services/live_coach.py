"""Live AI workout companion (WP-K): session building, the deterministic intent + safety layer, spoken
cues and the conversation flow.

Everything the member says (voice or text) is evaluated in this order - deterministically, BEFORE any AI:

1. RED FLAGS (chest pain/pressure, can't breathe, fainting, racing/irregular heart, stroke signs, sudden
   severe headache, vomiting, sharp pain with a "pop" ...) -> the session is "stopped_for_safety", the
   member gets calm emergency guidance with the local emergency number, gym activities are suspended
   (an admin must reactivate), a HealthAlert is stored and the member, the owners of their gyms and their
   assigned trainers are notified.
2. DISCOMFORT (pain/sore/cramp ... + body part, or the quick button) -> the exercise stops, a safe
   alternative that avoids the affected area is swapped in, a HealthAlert(WARNING) is logged and the
   assigned trainer is notified. "severe", chest or head -> treated as a red flag.
3. COMMANDS (quit, pause/resume, next/skip, set done, change exercise/workout, easier/harder, info ...).
4. Everything else -> the AI conversation (plans with AI, AI reachable) or a friendly deterministic reply. The
   per-user AI guard may refuse a reply (quota, AI pause, a repeated phrase, spam): the member then hears one short
   line instead and keeps training - layers 1-3 never reach the AI, so they are never refused or cancelled.

The AI can only suggest an emotion and an allowlisted action; this module re-validates both and the AI
can never resume or continue a session that was stopped for safety.
"""

from __future__ import annotations

import difflib
import logging
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.ai.live_coach import ACTIONS as AI_ACTIONS
from app.ai.live_coach import EMOTIONS, REPLY_MAX
from app.core.utils import utcnow
from app.models import (
    CustomerProfile,
    Employee,
    Exercise,
    Gym,
    HealthAlert,
    SuspensionEvent,
    TrainerClient,
    User,
    WorkoutPlan,
    WorkoutSession,
    WorkoutSessionEvent,
)
from app.models.enums import AnomalyLevel, EmploymentStatus, NotificationCategory, Severity
from app.services.audit import audit
from app.services.conditions import TAG_LABELS
from app.services.notify import Note, notify, notify_bulk
from app.services.subscriptions import PlanAccess, plan_access

log = logging.getLogger("ironpulse.live")

# ====================================================================================== constants
STATUSES = ("active", "paused", "completed", "stopped", "stopped_for_safety")
OPEN = frozenset({"active", "paused"})
ENDED = frozenset({"completed", "stopped", "stopped_for_safety"})
EVENT_TYPES = ("started", "set_done", "exercise_done", "paused", "resumed", "skipped", "completed")
PHASES = ("work", "rest", "paused", "ready", "done")
BODY_PARTS = ("chest", "head", "back", "knee", "shoulder", "wrist", "ankle", "neck", "stomach", "other")
BODY_PART_LABELS = {
    "chest": "chest", "head": "head", "back": "back", "knee": "knee", "shoulder": "shoulder", "wrist": "wrist",
    "ankle": "ankle", "neck": "neck", "stomach": "stomach", "other": "body",
}
BODY_PART_TAGS = {
    "knee": "knee_injury", "back": "lower_back", "shoulder": "shoulder_injury", "wrist": "wrist_injury",
    "ankle": "ankle_injury", "neck": "neck_injury",
}
# muscles to keep away from while an area is uncomfortable (the current exercise's muscles are avoided too)
AREA_MUSCLES: dict[str, set[str]] = {
    "knee": {"quads", "knees", "legs", "calves"},
    "back": {"back", "spine", "lower back", "lats", "hamstrings"},
    "shoulder": {"shoulders", "rear delts", "chest", "triceps"},
    "wrist": {"wrists", "forearms", "grip", "chest", "triceps"},
    "ankle": {"calves", "ankles", "legs", "quads"},
    "neck": {"neck", "traps", "shoulders", "thoracic"},
    "stomach": {"core", "obliques", "abs", "diaphragm"},
    "other": set(),
}
AREA_WORD_MUSCLES = {
    "hip": {"hips", "glutes"}, "hips": {"hips", "glutes"}, "groin": {"hips", "adductors"},
    "hamstring": {"hamstrings"}, "hamstrings": {"hamstrings"}, "quad": {"quads"}, "quads": {"quads"},
    "quadriceps": {"quads"}, "thigh": {"quads", "hamstrings"}, "thighs": {"quads", "hamstrings"},
    "leg": {"quads", "hamstrings", "calves", "legs"}, "legs": {"quads", "hamstrings", "calves", "legs"},
    "glute": {"glutes"}, "glutes": {"glutes"}, "butt": {"glutes"}, "elbow": {"biceps", "triceps", "forearms"},
    "elbows": {"biceps", "triceps", "forearms"}, "arm": {"biceps", "triceps", "shoulders"},
    "arms": {"biceps", "triceps", "shoulders"}, "bicep": {"biceps"}, "biceps": {"biceps"}, "tricep": {"triceps"},
    "triceps": {"triceps"}, "pec": {"chest"}, "pecs": {"chest"}, "lat": {"lats", "back"}, "lats": {"lats", "back"},
    "jaw": set(), "muscle": set(), "muscles": set(),
}
SEVERITIES = ("mild", "moderate", "severe")
CATEGORIES = ("calisthenics", "strength", "cardio", "mobility", "breathing")
INTENSITY_MIN, INTENSITY_MAX = -3, 3
INTENSITY_LABELS = {-3: "Much easier", -2: "Easier", -1: "A bit easier", 0: "Standard", 1: "A bit harder",
                    2: "Harder", 3: "Much harder"}
CAPPED_TAGS = frozenset({"cardiac", "hypertension", "pregnancy", "copd"})  # never push the intensity far
CAPPED_MAX_INTENSITY = 1
HIGH_IMPACT_TAGS = frozenset({"knee_injury", "ankle_injury", "pregnancy", "osteoporosis", "obesity", "arthritis", "vertigo"})
HIGH_INTENSITY_TAGS = frozenset({"cardiac", "hypertension", "copd", "pregnancy", "anemia"})
TEXT_MAX = 500
MESSAGE_MAX = 1000
NOTE_MAX = 300
PROFILE_PROMPT_CHARS = 520  # the member's saved profile in a live-coach prompt (the prompt stays a few hundred tokens)
# load safety (approved limits): workouts a member starts in 24 hours (behind a per-member lock), and events one session
# logs - past that only safety events (red flags, discomfort) are still recorded; the workout itself goes on
SESSIONS_PER_DAY = 30
SESSION_EVENTS_MAX = 2000
ALWAYS_LOGGED = frozenset({"red_flag", "discomfort", "safety", "suspended", "completed", "stopped"})

# ====================================================================================== emergency numbers
EU_112 = frozenset("AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE IS NO CH LI".split())
EMERGENCY_NUMBERS = {"IN": "112", "US": "911", "CA": "911", "GB": "999", "UK": "999", "AE": "999", "AU": "000",
                     "NZ": "111", "SG": "995"}
EMERGENCY_NOTES = {"GB": "For urgent advice that isn't an emergency, call NHS 111.",
                   "UK": "For urgent advice that isn't an emergency, call NHS 111."}


def emergency_info(country: str | None) -> dict[str, Any]:
    """Local emergency number: IN 112, US/CA 911, GB 999 (+ NHS 111), AE 999, AU 000, NZ 111, SG 995, EU 112."""
    cc = (country or "").strip().upper()[:2]
    number = EMERGENCY_NUMBERS.get(cc) or ("112" if cc in EU_112 else "112")
    return {"country": cc or None, "number": number, "note": EMERGENCY_NOTES.get(cc), "tel": f"tel:{number}"}


# ====================================================================================== text normalisation
_APOS = re.compile(r"[’‘'`´]")
_BOUNDARY = re.compile(r"[.,;:!?\n\r…]+|\s[-–—]+\s")
_NON_WORD = re.compile(r"[^a-z0-9| ]+")
_OUT_OF = re.compile(r"\b(\d{1,2})\s*/\s*10\b")


def normalize(text: str | None) -> str:
    """Lower-case ASCII words separated by single spaces; sentence/clause boundaries become ``|``.
    Apostrophes vanish (can't -> cant, I'm -> im) so patterns stay simple."""
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    t = _APOS.sub("", t)
    t = _OUT_OF.sub(r"\1 out of 10", t)
    t = _BOUNDARY.sub(" | ", t)
    t = _NON_WORD.sub(" ", t)
    t = re.sub(r"(?:\s*\|\s*)+", " | ", t)
    t = re.sub(r"\s+", " ", t).strip(" |")
    return t


# Words the intent patterns care about - misspellings of these are corrected (Damerau-Levenshtein <= 1/2).
VOCAB = frozenset("""
pause resume continue next skip stop quit finish easier harder lighter heavier slower faster intensity weight
chest breathe breathing breath dizzy dizziness faint fainting fainted lightheaded heart attack racing pounding
palpitations irregular fluttering numb numbness tingling weakness slurred slurring headache migraine vomit
vomiting vomited nauseous nausea pain painful hurt hurts hurting ache aching sore cramp cramping twinge strain
strained sprain sprained pulled knee knees shoulder shoulders wrist wrists ankle ankles neck stomach spine
elbow hamstring pressure tightness spinning collapse collapsed collapsing unconscious gasping wheezing choking
exercise workout session switch change something different another cardio mobility stretching calisthenics
strength enough ready tired exhausted microphone ambulance emergency seizure stroke unbearable excruciating
""".split())
# Common words that must never be "corrected" into a vocabulary word (shop -> stop, hard -> heart ...).
COMMON = frozenset("""
a about above after again against all almost also am an and any are around as at away be because been before
being best better between big bit body both but by can cant cannot could come coming did didnt do does doesnt
doing dont down during each else even ever every feel feeling feels fell felt few fine first for from full get
gets getting give go goes going gone good got great had has have having he head heard hear help her here him his
hit hold how i if im in into is isnt it its just keep kind know last least left leg legs less let lets like
little long look lot make man many may me mean might mind more most much must my need never new nice no not now
of off ok okay on once one only or other our out over part past pick plan please put quite quiet quick really
rest right round said same say see seems set sets shall she shop should show side since so some soon start step
steps still stuff such sure take talk tell than thank thanks that thats the their them then there these they
thing think this those though through time to today too tried try turn under up us use very wait want was way we
well went were what when where which while who why will with without word work worked working would yes yet you
your yeah yep nope beat bed bell belt bend bent bird board bones card care cart cat chair cheap check cheek chess
chin clap class clean clear clock close coach cold cool core cost dead deal dear deep desk diet door drink drop
easy eat edge end ends face fact fair fall fast fat fear feet field fight finger fist flat floor foot form front
fun gain game glad glute glutes grip hand hands hard harm hate hot hour huge hungry jump knew knot lift light line
lips load lost loud low lunch mat meal mile miss move moved moving neat nest pace pack paint pair palm pass pile
plank plain play pull pulse push race rain raise rate read real reach rice ride roll room row run running safe
sand save seat sick sit size skin sleep slow small snack sorry sound speak speed spin spot sprint squat stand stay
steel stick stiff stone store story strap stretch strong tall team test text thin tight tiny toe toes tone top
touch track train trainer treat tree trip true walk wall warm wash watch water weak wear week weird wet wide win
wind wise wrap wrong heat hear heard hearty rate state ready reps rep chat chest cheat break brake bring brain
train trains paint hint tint mint pint saint stain stair stairs start stars star stark sore sort short shirt
""".split())
TYPO_MAP = {
    "hart": "heart", "chst": "chest", "brethe": "breathe", "breth": "breath", "breahte": "breathe", "dizy": "dizzy",
    "dizzzy": "dizzy", "fanit": "faint", "fiant": "faint", "nauseus": "nauseous", "vomitting": "vomiting",
    "throwup": "throw up", "pasue": "pause", "puase": "pause", "contine": "continue", "nxt": "next", "skp": "skip",
    "harde": "harder", "esier": "easier", "easyer": "easier", "headache": "headache", "hedache": "headache",
    "headach": "headache", "stomache": "stomach", "stomac": "stomach", "shoulda": "shoulder", "kne": "knee",
    "ankel": "ankle", "wirst": "wrist", "nek": "neck", "paln": "pain", "pian": "pain", "hurst": "hurts",
    "plapitations": "palpitations", "palpatations": "palpitations", "imdone": "im done", "thatsenough": "thats enough",
    "nee": "knee", "bak": "back", "hed": "head", "brth": "breath", "stp": "stop", "ext": "next",
}


def _osa(a: str, b: str, limit: int) -> int:
    """Optimal-string-alignment (Damerau-Levenshtein) distance with an early exit above ``limit``."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev2: list[int] | None = None
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        best = cur[0]
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if prev2 is not None and i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
            best = min(best, cur[j])
        if best > limit:
            return limit + 1
        prev2, prev = prev, cur
    return prev[len(b)]


_VOCAB_BY_LEN: dict[int, list[str]] = {}
for _w in VOCAB:
    _VOCAB_BY_LEN.setdefault(len(_w), []).append(_w)


def _correct_word(word: str) -> str:
    if word in TYPO_MAP:
        return TYPO_MAP[word]
    if len(word) < 4 or not word.isalpha() or word in _known():
        return word
    limit = 1 if len(word) < 8 else 2
    best: list[tuple[int, int, str]] = []
    for n in range(len(word) - limit, len(word) + limit + 1):
        for cand in _VOCAB_BY_LEN.get(n, ()):
            d = _osa(word, cand, limit)
            if d <= limit:
                best.append((d, 0 if cand[0] == word[0] else 1, cand))
    if not best:
        return word
    best.sort()
    top = [b for b in best if b[:2] == best[0][:2]]
    return top[0][2] if len(top) == 1 else word  # ambiguous -> leave it alone


def _known() -> frozenset[str]:
    return globals().get("KNOWN_WORDS") or (COMMON | VOCAB)


def correct_typos(norm: str) -> str:
    return " ".join(_correct_word(w) for w in norm.split(" "))


# ====================================================================================== intent detection
@dataclass(slots=True)
class Intent:
    kind: str  # red_flag | discomfort | command | chat
    name: str  # flag name / command name / "chat"
    body_part: str | None = None
    area: str | None = None  # exact body word ("hamstring")
    severity: str | None = None
    level: str | None = None  # AnomalyLevel value for red flags
    category: str | None = None  # requested exercise category (change)
    muscles: tuple[str, ...] = ()
    scope: str = "exercise"  # change scope: exercise | workout
    matched: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "name": self.name, "body_part": self.body_part, "severity": self.severity,
                "level": self.level, "category": self.category, "scope": self.scope}


_G = r"(?: [a-z0-9]+){0,3}?"  # up to three filler words - never crosses a "|" clause boundary
_G6 = r"(?: [a-z0-9]+){0,6}?"
LIFE = AnomalyLevel.LIFE_THREATENING.value
CRIT = AnomalyLevel.CRITICAL.value

_BODY_WORDS: dict[str, str] = {}
for _part, _words in (
    ("back", "lower back|upper back|back|spine|lumbar|tailbone|sciatica|slipped disc|disc"),
    ("knee", "knee cap|knee caps|kneecap|kneecaps|knee|knees|patella|acl|meniscus"),
    ("shoulder", "shoulder|shoulders|rotator cuff|rotator"),
    ("wrist", "wrist|wrists|hand|hands|palm|palms|finger|fingers|thumb|thumbs|forearm|forearms"),
    ("ankle", "ankle|ankles|foot|feet|heel|heels|achilles|toe|toes|arch|calf|calves|shin|shins"),
    ("neck", "neck|trap|traps"),
    ("stomach", "stomach|belly|tummy|abdomen|abdominals|abs|gut|side stitch|stitch|side|sides"),
    ("chest", "chest|rib|ribs|sternum|breastbone"),
    ("head", "head|temple|temples|forehead"),
    ("other", "hip|hips|groin|hamstring|hamstrings|quad|quads|quadriceps|thigh|thighs|leg|legs|glute|glutes|butt|"
              "elbow|elbows|arm|arms|bicep|biceps|tricep|triceps|muscle|muscles|jaw|pec|pecs|lat|lats"),
):
    for _w in _words.split("|"):
        _BODY_WORDS[_w] = _part
_BODY = "|".join(sorted(_BODY_WORDS, key=len, reverse=True))
_CHEST_NOT_PAIN = r"(?! (?:press|presses|fly|flies|stretch|opener|day|workout|exercises?|machine|dips?)\b)"

# (flag, level, pattern, negation allowed inside the match) - order matters only for reporting
_RED_FLAG_SPECS: list[tuple[str, str, str, bool]] = [
    ("heart_attack", LIFE, r"\bheart attack\b|\bcardiac arrest\b|\bheart (?:is )?(?:stopping|failing|stopped)\b", False),
    ("chest_pain", LIFE,
     rf"\bchest{_CHEST_NOT_PAIN}{_G} (?:pain|pains|painful|hurts?|hurting|aches?|aching|tight|tightness|tightening|"
     r"pressure|heavy|heaviness|squeez(?:e|es|ed|ing)|crush(?:ed|ing)?|burn(?:s|ing)?|sore|stabbing|sharp|weird|funny|"
     r"strange|odd|constricted)\b", False),
    ("chest_pain", LIFE,
     rf"\b(?:pain|pains|ache|aches|aching|tightness|pressure|heaviness|squeezing|crushing|burning|discomfort|stabbing)"
     rf"{_G} (?:in|on|across|around|behind|under|through) (?:my |the |his |her )?(?:chest|breastbone|sternum)\b", False),
    ("chest_pain", LIFE, r"\b(?:tight|heavy|painful|sore|crushing|tightening) chest\b", False),
    ("chest_pain", LIFE,
     rf"\b(?:pain|ache|aching|numb|numbness|tingling|heaviness){_G} (?:in |down |into |to |through |along )?"
     r"(?:my |the )?(?:left arm|jaw)\b|\b(?:left arm|jaw)" + _G + r" (?:hurts?|aches?|aching|numb|tingl\w*|pain)\b",
     False),
    ("breathing", LIFE,
     r"\b(?:cant|cannot|can not|unable to|struggling to|struggle to|hard to|difficult to|trouble|difficulty|"
     r"having trouble|having difficulty) (?:to )?(?:breathe|breath|breathing|get (?:enough |any )?air|get my breath)\b",
     True),
    ("breathing", LIFE, r"\b(?:gasping|choking|suffocating|wheezing|wheeze|wheezy|asthma attack)\b", False),
    ("breathing", LIFE,
     r"\b(?:severe(?:ly)?|very|really|extremely|badly|seriously|super|so) short(?:ness)? of breath\b", False),
    ("allergic_reaction", LIFE,
     r"\b(?:allergic reaction|anaphyla\w*|throat (?:is )?(?:closing|swelling|tight)|tongue (?:is )?swelling|"
     r"lips? (?:are |is )?swelling)\b", False),
    ("fainting", LIFE,
     r"\b(?:faint(?:ed|ing)?|feel(?:ing)? faint|pass(?:ing)? out|passed out|black(?:ing)? out|blacked out|blackout|"
     r"lost consciousness|losing consciousness|collaps(?:e|ed|ing)|going to (?:pass out|faint|collapse))\b", False),
    ("dizziness", CRIT,
     r"\b(?:dizzy|dizziness|light ?headed|woozy|vertigo|(?:room|everything|world|head) (?:is )?spinning|"
     r"seeing stars|vision (?:is )?(?:going|went|fading|blurry|blurred|dark|black|tunnel))\b", False),
    ("heart_rhythm", CRIT,
     rf"\bheart{_G} (?:racing|pounding|pumping out|beating (?:so |really |very |super |too )?(?:fast|hard|weird(?:ly)?|"
     r"strange(?:ly)?|irregular(?:ly)?|out of my chest|like crazy)|irregular|fluttering|flutter|skipping|skipped|"
     r"missing beats|jumping|thumping|going crazy|out of rhythm)\b", False),
    ("heart_rhythm", CRIT,
     r"\b(?:racing|pounding|irregular|fluttering|skipping) (?:heart|heartbeat|heart beat|pulse)\b|\bpalpitations?\b|"
     r"\barrhythmia\b", False),
    ("heart_rhythm", CRIT,
     rf"\b(?:pulse|heart ?beat|heart rate){_G} (?:racing|irregular|pounding|crazy|through the roof|all over the place|"
     r"weird|strange)\b", False),
    ("stroke", LIFE,
     rf"\b(?:numb|numbness|tingling|tingly|paralys\w*|paralyz\w*|cant (?:move|feel|lift)){_G} (?:on )?"
     r"(?:one side|my left side|my right side|left side|right side|half of my (?:body|face)|side of my (?:body|face)|"
     r"my face|face|one arm|left arm|right arm|my arm|one leg|left leg|right leg)\b", True),
    ("stroke", LIFE,
     rf"\b(?:weak|weakness|droop\w*){_G} (?:on )?(?:one side|left side|right side|half of my (?:body|face)|"
     r"side of my (?:body|face)|my face|face)\b", False),
    ("stroke", LIFE,
     r"\bslurr(?:ed|ing|y)\b|\b(?:face|mouth|smile) (?:is )?(?:drooping|droopy|crooked|lopsided)\b|"
     r"\b(?:cant|cannot|can not|struggling to|trouble) (?:get (?:my )?words out|find (?:my |the )?words)\b|"
     r"\bwords (?:are )?(?:coming out )?(?:wrong|slurred|jumbled|mixed up)\b|\bcant (?:speak|talk) (?:properly|clearly|right)\b|"
     r"\b(?:lost|losing) (?:my )?(?:vision|sight)\b|\bdouble vision\b|\bhaving a stroke\b", True),
    ("severe_headache", LIFE,
     r"\b(?:sudden|severe|terrible|worst|splitting|excruciating|thunderclap|blinding|massive|intense|really bad|very bad|"
     r"awful|horrible|explosive)(?: [a-z]+){0,2} headache\b|\bhead (?:is )?(?:exploding|splitting|about to explode)\b|"
     r"\bthunderclap\b", False),
    ("headache", CRIT,
     rf"\bheadache\b|\bmigraine\b|\bhead{_G} (?:hurts?|hurting|aches?|aching|pounding|throbbing|pain|sore)\b|"
     r"\b(?:pain|ache|pressure|throbbing) in (?:my |the )?head\b|\b(?:hit|banged|bumped|smacked) my head\b", False),
    ("vomiting", CRIT,
     r"\b(?:vomit(?:ing|ed|s)?|throw(?:ing)? up|threw up|puk(?:e|ed|ing)|being sick|retching|"
     r"(?:going|gonna|about) to (?:be sick|throw up|puke|vomit))\b", False),
    ("pop_injury", CRIT,
     r"\b(?:heard|felt) (?:a |something |it )?(?:pop|snap|crack|rip|tear)\b|\bsomething (?:just )?(?:popped|snapped|tore|ripped)\b|"
     rf"\b(?:sharp|stabbing|shooting|sudden){_G6} (?:pop|popped|snap|snapped)\b|"
     rf"\b(?:pop|popped|snap|snapped){_G6} (?:sharp|stabbing|shooting) pain\b|"
     r"\b(?:tore|torn|ripped|ruptured|snapped) (?:my |a )?(?:muscle|tendon|ligament|achilles|acl|hamstring|bicep|biceps|"
     r"calf|quad|pec)\b|"
     rf"\b(?:dislocated|broke|broken|fractured) (?:my |a )?(?:left |right )?(?:{_BODY})\b", False),
    ("severe_pain", CRIT,
     r"\b(?:unbearable|excruciating|agonizing|agonising|crippling|extreme|severe) pain\b|\bin agony\b|"
     r"\bpain (?:is )?(?:unbearable|excruciating|extreme|severe|(?:a )?(?:10|ten)(?: out of (?:10|ten))?)\b", False),
    ("low_sugar", CRIT,
     r"\b(?:(?:blood )?sugar (?:is )?(?:low|dropping|crashing)|low (?:blood )?sugar|having a hypo|going hypo|"
     r"hypoglyc\w*|shaky and sweaty|sweaty and shaky|cold sweats?)\b", False),
    ("overheating", CRIT, r"\b(?:heat ?stroke|heat exhaustion|over ?heating|overheated|stopped sweating|burning up)\b", False),
    ("seizure", LIFE, r"\b(?:seizure|seizures|convulsion|convulsions|having a fit)\b", False),
    ("emergency", LIFE,
     r"\b(?:call (?:an |the )?(?:ambulance|paramedics?|doctor|911|112|999|000|995|emergency(?: services)?)|"
     r"i need (?:an )?ambulance|medical emergency|this is an emergency|im having an emergency|i need help now|"
     r"someone help me|i need a doctor)\b|\b(?:please help me|help me please|help help)(?: \||$)", False),
]
RED_FLAGS: list[tuple[str, str, re.Pattern[str], bool]] = [(f, lvl, re.compile(rx), inside) for f, lvl, rx, inside in _RED_FLAG_SPECS]

FLAG_LABELS = {
    "chest_pain": "chest pain or pressure",
    "heart_attack": "possible heart attack symptoms",
    "breathing": "severe breathing difficulty",
    "allergic_reaction": "signs of a serious allergic reaction",
    "fainting": "fainting or near-fainting",
    "dizziness": "dizziness",
    "heart_rhythm": "a racing or irregular heartbeat",
    "stroke": "possible stroke symptoms",
    "severe_headache": "a sudden severe headache",
    "headache": "a headache or head symptoms",
    "vomiting": "vomiting",
    "pop_injury": "an acute injury (sharp pain or a pop)",
    "severe_pain": "severe pain",
    "low_sugar": "signs of low blood sugar",
    "overheating": "signs of overheating",
    "seizure": "a seizure",
    "emergency": "a call for emergency help",
}
FLAG_ADVICE = {
    "chest_pain": "Please stop now and sit down. Chest pain can be serious - call {num} right away, don't wait to see if it passes.",
    "heart_attack": "Stop now and sit down. Call {num} right away and unlock your door so help can reach you.",
    "breathing": "Stop and sit upright. If you have a reliever inhaler, use it as prescribed. If you're struggling to breathe, call {num} now.",
    "allergic_reaction": "Stop now. If you carry an adrenaline auto-injector, use it, then call {num} straight away.",
    "fainting": "Stop and lie down with your legs raised, or sit with your head low. Don't get up quickly. If you fainted or feel confused, call {num}.",
    "dizziness": "Stop and sit or lie down right away. Sip some water and breathe slowly. If it doesn't pass in a few minutes or you feel worse, call {num}.",
    "heart_rhythm": "Stop and sit down, and breathe slowly. If your heartbeat stays fast or irregular, or you feel chest pain, faint or breathless, call {num}.",
    "stroke": "This could be serious - call {num} now. Sit or lie down safely and note the time it started.",
    "severe_headache": "Stop and sit down somewhere quiet. A sudden, severe headache needs urgent help - call {num} now.",
    "headache": "Stop and rest somewhere cool and sip water. If it's sudden or severe, or comes with vision changes, weakness or confusion, call {num}.",
    "vomiting": "Stop and rest, and take small sips of water. If you keep vomiting, feel faint or have chest pain, call {num}.",
    "pop_injury": "Stop now and don't put weight on it. Rest, ice and support it. If the pain is severe or you can't move it, get urgent care or call {num}.",
    "severe_pain": "Stop now and rest. Severe pain needs checking - if it doesn't ease or you can't move normally, get urgent care or call {num}.",
    "low_sugar": "Stop and sit down. If you have diabetes, check your sugar and take fast-acting glucose now. If you feel confused or faint, call {num}.",
    "overheating": "Stop, move somewhere cool and sip water. If you feel confused, faint or stop sweating, call {num}.",
    "seizure": "Stop and lie down on your side somewhere safe, away from equipment. Call {num} now and ask someone to stay with you.",
    "emergency": "Call {num} now. Stay where you are, unlock your door and tell someone nearby.",
}
SAFETY_TAIL = "I've stopped the workout and paused your gym activities until a gym admin checks in with you."

_PAIN = (r"(?:pain|pains|painful|hurts?|hurting|aches?|aching|achy|sore|soreness|cramps?|cramping|cramped|twinges?|"
         r"twinging|strain|strained|pulled|tweaked|sprain|sprained|pinch|pinching|pinched|throbbing|tender|swollen|"
         r"swelling|stiff|discomfort|uncomfortable|killing me|giving (?:out|way)|gave (?:out|way)|locked|locking|"
         r"clicking|grinding|popping|stabbing|shooting|numb|tingling|tingly|weird|funny|bothering me|"
         r"(?:feels?|feeling) off|acting up|playing up)")
_DISCOMFORT_SPECS: list[tuple[str, bool]] = [
    (rf"\b(?P<body>{_BODY}){_G} {_PAIN}\b", False),
    (rf"\b(?P<body>{_BODY}){_G} (?:(?:is|feels?|feeling) )?(?:not right|not feeling right|doesnt feel right)\b", True),
    (rf"\b{_PAIN}{_G} (?:in|on|at|around|behind|under|near|inside) (?:my |the |his |her |both |one )?(?:left |right )?"
     rf"(?P<body>{_BODY})\b", False),
    (rf"\b(?:hurt|pulled|tweaked|strained|twisted|rolled|sprained|injured|jarred|banged|bumped|wrenched|cricked) "
     rf"(?:my |the |a )?(?:left |right )?(?P<body>{_BODY})\b", False),
]
DISCOMFORT_PATTERNS = [(re.compile(rx), inside) for rx, inside in _DISCOMFORT_SPECS]
GENERIC_PAIN = re.compile(
    rf"\b(?:it|this|that|everything|something){_G} (?:hurts|aches|is painful|is sore|is killing me|feels wrong)\b|"
    r"\b(?:im|i am) (?:in )?(?:a lot of |so much |some |real )?pain\b|\bouch\b|^ow+\b|\bowie\b|\bthat (?:hurt|stung)\b|"
    r"\bfeel(?:ing)? (?:unwell|ill|terrible|awful|weird|strange|not (?:right|well|good))\b|\bnot feeling (?:well|good|right)\b"
)
STOMACH_FEEL = re.compile(r"\b(?:feel(?:ing)? (?:sick|nauseous|queasy)|nauseous|nausea|queasy|side stitch|stitch)\b")
SEVERE_WORDS = re.compile(
    r"\b(?:severe|severely|unbearable|excruciating|agony|agonizing|agonising|terrible|horrible|horrendous|extreme|"
    r"extremely|worst|really bad|very bad|so bad|really badly|cant move|cant walk|cant stand|cant put weight|"
    r"(?:9|10|nine|ten) out of (?:10|ten))\b")
MILD_WORDS = re.compile(r"\b(?:a bit|a little|little bit|slight|slightly|mild|mildly|minor|kind of|kinda|sort of|tiny|"
                        r"twinge|niggle|niggly)\b")

NEGATORS = frozenset({"no", "not", "never", "dont", "doesnt", "isnt", "arent", "wasnt", "werent", "without", "hardly",
                      "nothing", "didnt", "neither", "nor", "aint", "zero"})
FILLERS = frozenset({"really", "real", "any", "much", "more", "serious", "have", "has", "had", "feel", "feeling", "felt",
                     "got", "getting", "get", "experiencing", "a", "an", "the", "some", "sign", "signs", "of", "is",
                     "its", "it", "think", "there", "theres", "i", "im", "am", "any", "actually", "even", "longer",
                     "anymore", "at", "all", "be", "being", "very", "too", "so"})
CLAUSE_BREAKS = frozenset({"|", "but", "and", "though", "although", "however", "yet", "because", "now", "except"})


def _negated(text: str, start: int, end: int, inside_ok: bool) -> bool:
    """True when the match at [start, end) is negated ("no chest pain", "my knee doesn't hurt")."""
    before = text[:start].split()
    steps = 0
    for tok in reversed(before):
        if tok in CLAUSE_BREAKS or steps >= 5:
            break
        if tok in NEGATORS:
            return True
        if tok not in FILLERS:
            break
        steps += 1
    if not inside_ok:
        inner = text[start:end].split()[1:-1]
        if any(t in NEGATORS for t in inner):
            return True
    return False


def _severity(text: str) -> str:
    if SEVERE_WORDS.search(text):
        return "severe"
    if MILD_WORDS.search(text):
        return "mild"
    return "moderate"


def _find_red_flag(text: str) -> Intent | None:
    found: list[Intent] = []
    for flag, level, rx, inside in RED_FLAGS:
        for m in rx.finditer(text):
            if not _negated(text, m.start(), m.end(), inside):
                found.append(Intent("red_flag", flag, level=level, matched=m.group(0).strip(" |")))
                break
    if not found:
        return None
    found.sort(key=lambda i: 0 if i.level == LIFE else 1)  # the most serious finding wins
    return found[0]


def _find_discomfort(text: str) -> Intent | None:
    hits: list[tuple[str, str, str]] = []
    for rx, inside in DISCOMFORT_PATTERNS:
        for m in rx.finditer(text):
            if _negated(text, m.start(), m.end(), inside):
                continue
            word = m.group("body")
            hits.append((_BODY_WORDS.get(word, "other"), word, m.group(0)))
    severity = _severity(text)
    if hits:
        hits.sort(key=lambda h: 0 if h[0] in ("chest", "head") else (2 if h[0] == "other" else 1))
        part, word, matched = hits[0]
        return Intent("discomfort", "discomfort", body_part=part, area=word, severity=severity, matched=matched)
    m = STOMACH_FEEL.search(text)
    if m and not _negated(text, m.start(), m.end(), False):
        return Intent("discomfort", "discomfort", body_part="stomach", area="stomach", severity=severity,
                      matched=m.group(0))
    m = GENERIC_PAIN.search(text)
    if m and not _negated(text, m.start(), m.end(), False):
        return Intent("discomfort", "discomfort", body_part="other", area=None, severity=severity, matched=m.group(0))
    return None


_CMD_SPECS: list[tuple[str, str]] = [
    ("mic_off", r"\b(?:stop listening|(?:turn|switch|shut) (?:off|down) (?:the |your )?(?:mic|microphone)|"
                r"(?:mic|microphone) off|mute (?:the |my )?(?:mic|microphone)|disable (?:the )?(?:mic|microphone))\b"),
    ("voice_off", r"\b(?:be quiet|stop talking|shut up|quiet please|voice off|(?:turn|switch) off (?:the |your )?voice|"
                  r"mute (?:yourself|your voice|the voice|voice|the coach|coach)|no more talking|silence)\b"),
    ("voice_on", r"\b(?:voice on|unmute(?: yourself| the voice| voice)?|(?:turn|switch) on (?:the |your )?voice|"
                 r"talk to me again|start talking)\b"),
    ("breather", r"\b(?:cant|cannot|can not) catch my breath\b|\bout of breath\b|\bshort(?:ness)? of breath\b|\bwinded\b|"
                 r"\bbreathing (?:so |really )?(?:hard|heavy|heavily)\b|\bneed (?:to catch|a) (?:my )?breath(?:er)?\b"),
    ("pause", r"\b(?:pause|hold on|hang on|hold up|give me a (?:sec|second|minute|moment|break)|"
              r"(?:need|take|want|have) a (?:quick |short |little )?(?:break|breather|rest|sec|second|minute|moment)|"
              r"stop for a (?:sec|second|moment|minute|bit)|time out|timeout|one (?:sec|second|moment|minute)|"
              r"wait a (?:sec|second|moment|minute)|stop the (?:timer|clock))\b|^wait\b"),
    ("quit", r"^(?:(?:ok|okay|please|coach|hey|alright|right) )*(?:stop|quit|end|finish|exit|abort|cancel)(?: (?:it|now|please|"
             r"here|everything|this|the workout|the session|workout|session|training))*$|"
             r"\b(?:quit|end|finish|stop|cancel|abort|exit) (?:the |this |my |todays )?(?:workout|session|training)\b|"
             r"\b(?:im|i am) (?:done|finished|through)(?: for (?:today|now|the day))?(?: with (?:the |this )?"
             r"(?:workout|session|training))?(?: \||$)|\b(?:thats|that is) (?:enough|it for (?:today|now)|all for (?:today|now))\b|"
             r"\bi (?:give up|quit)\b|\bcall it (?:a day|quits)\b|\bno more (?:exercise|workout|training)\b|"
             r"\bi (?:want|need|would like|wanna) to (?:stop|quit|end|finish)(?: now| the workout| the session| here)?(?: \||$)|"
             r"\blets (?:stop|end|finish|wrap up|call it)\b"),
    ("info_next", r"\b(?:whats|what is|what s) (?:next|coming|up next|after this)\b|\bwhat (?:do we do|are we doing) next\b"),
    ("set_done", r"^(?:(?:ok|okay|alright|right|and|yes) )?(?:done|finished|complete|completed)(?: (?:that|the|this|my) set| set)?$|"
                 r"\b(?:set|that set|this set) (?:is )?(?:done|finished|complete|completed)\b|"
                 r"\b(?:im|i am) (?:done|finished) with (?:the |this |that |my )?set\b|\bfinished (?:the |this |that |my )set\b|"
                 r"\bnext set\b|\bthats (?:the |one )?set\b|^(?:done|finished) with (?:the |this |that |my )?set\b|"
                 r"\bset complete\b"),
    ("next", r"^(?:(?:ok|okay|alright|right|go|please|and) )*next(?: (?:one|exercise|move|please))?(?: please)?$|"
             r"\b(?:go|move|on|skip) to the next(?: one| exercise| move)?\b|\bnext (?:exercise|move)\b|\bmove on\b|"
             r"\bskip(?: (?:it|this|that|this one|the exercise|this exercise|ahead))?\b"),
    ("intensity_down", r"\b(?:easier|too (?:hard|difficult|heavy|much|fast|intense|tough)|"
                       r"(?:lower|reduce|drop|decrease) (?:the )?(?:weight|intensity|reps|load|pace|difficulty)|"
                       r"less (?:weight|reps|intense|intensity|load)|fewer reps|slow(?:er)?(?: it)? down|go slower|"
                       r"slower please|lighter|tone it down|make it easier|take it easy|cant keep up|"
                       r"(?:its|it is|this is) (?:killing me|brutal)|"
                       r"(?:im|i am) (?:so |really |very |super )?(?:tired|exhausted|knackered|worn out|struggling|dying|gassed|spent))\b"),
    ("intensity_up", r"\b(?:harder|too easy|(?:its|this is|that was|thats) (?:so |really |too )?easy|"
                     r"more (?:reps|weight|intense|intensity|load|challenging)|heavier|"
                     r"(?:increase|raise|up) (?:the )?(?:intensity|weight|reps|load|difficulty)|step it up|push me|"
                     r"make it harder|level up|bring it on|turn it up|kick it up|challenge me|speed (?:it )?up|go faster|faster)\b"),
    ("change", r"\b(?:something (?:else|different)|another (?:exercise|one|move|workout|session)|"
               r"different (?:exercise|one|move|workout|session)|change (?:the |this |my )?(?:exercise|workout|move|routine|session|it)"
               r"(?: up)?|switch(?: (?:it up|exercises?|the exercise|this|to|it|over))?|"
               r"swap(?: (?:it|this|exercises?|the exercise|to|for|out))?|replace (?:this|it|the exercise)|"
               r"i (?:dont|do not) (?:like|want to do|want|enjoy) (?:this|that|it)(?: exercise| one)?|"
               r"(?:can|could) we (?:do|try) (?:something|another|a different)|mix it up|"
               r"(?:something|exercises?|a move|work) for (?:my |the )?(?:core|abs|legs|arms|upper body|lower body|back|chest|"
               r"shoulders|glutes|cardio)|(?:do|try|have) (?:some )?[a-z]+(?: [a-z]+)? instead)\b"),
    ("resume", r"\b(?:resume|unpause|continue|carry on|keep going|back to it|lets continue|lets keep going|"
               r"lets go(?: again)?|start again|(?:im|i am) (?:ready|back)(?: now)?|lets do (?:it|this)|good to go)\b|^ready(?: now)?$"),
    ("info_reps", r"\bhow many (?:reps|sets|more|left|to go|seconds)\b|\bhow (?:much|long) (?:longer|left|more)\b|\bwhat set\b"),
    ("info_form", r"\bhow (?:do i|should i|to|do you) (?:do|perform) (?:this|it|that)\b|\b(?:form|technique) (?:tips?|check|cues?)\b|"
                  r"\b(?:any )?tips\b|\bwhat should i (?:focus on|feel)\b|\bhow is (?:this|it) done\b|\bshow me how\b"),
    ("progress", r"\bhow (?:am i|are we) doing\b|\bhow far (?:am i|are we|along)\b|\bhow much (?:is )?left\b"),
    ("repeat", r"\b(?:repeat (?:that|please|it)|say (?:that|it) again|what did you say|come again|pardon)\b|^repeat$"),
    ("help", r"^(?:help|help me|what can i say|what can you do|commands|options)$|\bwhat (?:can|should) i say\b"),
]
COMMANDS: list[tuple[str, re.Pattern[str]]] = [(n, re.compile(rx)) for n, rx in _CMD_SPECS]


def _pattern_words(*sources: str) -> set[str]:
    words: set[str] = set()
    for src in sources:
        words |= set(re.findall(r"[a-z]{3,}", re.sub(r"\\[a-zA-Z]", " ", src)))
    return words


# every literal word the patterns use is a known word: it is never "corrected" into something else
KNOWN_WORDS = frozenset(COMMON | VOCAB | _pattern_words(
    *(rx for _, _, rx, _ in _RED_FLAG_SPECS), *(rx for rx, _ in _DISCOMFORT_SPECS), *(rx for _, rx in _CMD_SPECS),
    _PAIN, _BODY, GENERIC_PAIN.pattern, STOMACH_FEEL.pattern,
))

_CATEGORY_WORDS = [
    ("cardio", r"cardio|running|run|jog|jogging|cycling|bike|biking|rowing|rower|elliptical|treadmill|walk|walking|conditioning"),
    ("mobility", r"mobility|stretch|stretches|stretching|yoga|flexibility|loosen up|warm ?up|cool ?down"),
    ("breathing", r"breathing|breath ?work|relax|relaxing|relaxation|calm down|meditat\w*"),
    ("strength", r"strength|weights?|weight training|lifting|dumbbells?|barbell|machines?|resistance"),
    ("calisthenics", r"calisthenics|bodyweight|body weight|no equipment|push ?ups?|pull ?ups?|planks?|squats?"),
]
CATEGORY_PATTERNS = [(c, re.compile(rf"\b(?:{rx})\b")) for c, rx in _CATEGORY_WORDS]
MUSCLE_FOCUS = [
    (re.compile(r"\b(?:legs?|lower body|quads?|glutes?|hamstrings?|calves)\b"), ("quads", "glutes", "hamstrings", "calves", "legs")),
    (re.compile(r"\b(?:core|abs|abdominals?|obliques)\b"), ("core", "obliques")),
    (re.compile(r"\b(?:upper body|arms?|chest|shoulders?|back|biceps?|triceps?)\b"),
     ("chest", "triceps", "shoulders", "back", "lats", "biceps")),
]
WORKOUT_SCOPE = re.compile(r"\b(?:workout|session|routine|plan|whole thing|everything)\b")


def detect_intent(text: str | None) -> Intent:
    """Classify an utterance: red_flag > discomfort > command > chat. Deterministic, typo tolerant."""
    norm = normalize(text)
    if not norm:
        return Intent("chat", "empty")
    fixed = correct_typos(norm)
    variants = [fixed] if fixed == norm else [fixed, norm]
    for v in variants:
        flag = _find_red_flag(v)
        if flag is not None:
            return flag
    for v in variants:
        disc = _find_discomfort(v)
        if disc is not None:
            return disc
    for name, rx in COMMANDS:
        m = rx.search(fixed)
        if m is None:
            continue
        intent = Intent("command", name, matched=m.group(0).strip())
        if name == "change":
            for cat, crx in CATEGORY_PATTERNS:
                if crx.search(fixed):
                    intent.category = cat
                    break
            for mrx, muscles in MUSCLE_FOCUS:
                if mrx.search(fixed):
                    intent.muscles = muscles
                    break
            intent.scope = "workout" if WORKOUT_SCOPE.search(fixed) else "exercise"
        return intent
    return Intent("chat", "chat")


# ====================================================================================== exercise library
@dataclass(frozen=True, slots=True)
class LibItem:
    slug: str
    name: str
    category: str
    muscles: tuple[str, ...] = ()
    goals: tuple[str, ...] = ()
    equipment: str = "none"
    difficulty: str = "beginner"
    intensity: str = "moderate"
    impact: str = "low"
    contraindications: tuple[str, ...] = ()
    cues: tuple[str, ...] = ()
    video_slug: str | None = None
    description: str = ""

    @classmethod
    def from_any(cls, ex: Any) -> LibItem:
        def tup(v: Any) -> tuple[str, ...]:
            return tuple(str(x).strip().lower() for x in (v or []) if str(x).strip())

        difficulty = getattr(getattr(ex, "difficulty", None), "value", getattr(ex, "difficulty", None)) or "beginner"
        return cls(
            slug=str(ex.slug), name=str(ex.name), category=str(getattr(ex, "category", None) or "strength").lower(),
            muscles=tup(getattr(ex, "muscles", ())), goals=tup(getattr(ex, "goals", ())),
            equipment=str(getattr(ex, "equipment", None) or "none").lower(), difficulty=str(difficulty).lower(),
            intensity=str(getattr(ex, "intensity", None) or "moderate").lower(),
            impact=str(getattr(ex, "impact", None) or "low").lower(), contraindications=tup(getattr(ex, "contraindications", ())),
            cues=tuple(str(c).strip() for c in (getattr(ex, "cues", None) or []) if str(c).strip()),
            video_slug=getattr(ex, "video_slug", None), description=str(getattr(ex, "description", None) or ""),
        )


def _x(slug: str, name: str, category: str, muscles: str, *, equipment: str = "none", intensity: str = "low",
       impact: str = "low", contra: str = "", cues: str = "", description: str = "") -> LibItem:
    return LibItem(slug=slug, name=name, category=category, muscles=tuple(muscles.split()), equipment=equipment,
                   intensity=intensity, impact=impact, contraindications=tuple(contra.split()),
                   cues=tuple(c for c in cues.split("|") if c), description=description)


# Minimal last-resort library (used only if the exercises table is empty AND the coach library is unavailable).
FALLBACK_LIBRARY: tuple[LibItem, ...] = (
    _x("cat-cow", "Cat-cow", "mobility", "spine core", equipment="mat", cues="Move slowly with your breath|Round, then arch"),
    _x("shoulder-circles", "Shoulder circles", "mobility", "shoulders", cues="Big, slow circles|Stay below any pain"),
    _x("hip-circles", "Standing hip circles", "mobility", "hips", cues="Hands on hips|Draw slow circles"),
    _x("ankle-rocks", "Ankle mobility rocks", "mobility", "ankles calves", cues="Heel stays down"),
    _x("box-breathing", "Box breathing", "breathing", "diaphragm", cues="In for four, hold for four|Out for four, hold for four"),
    _x("diaphragmatic-breathing", "Diaphragmatic breathing", "breathing", "diaphragm core", equipment="mat",
       cues="Inhale through the nose into the belly|Long, slow exhale"),
    _x("march-in-place", "Marching in place", "cardio", "legs heart", intensity="moderate",
       cues="Lift the knees to hip height|Swing the arms"),
    _x("step-jacks", "Low-impact step jacks", "cardio", "legs heart shoulders", intensity="moderate",
       cues="Step out to the side|Arms overhead in rhythm"),
    _x("bodyweight-squat", "Bodyweight squat", "calisthenics", "quads glutes", intensity="moderate",
       cues="Knees track over toes|Chest up"),
    _x("glute-bridge", "Glute bridge", "calisthenics", "glutes hamstrings", equipment="mat", contra="pregnancy",
       cues="Drive through the heels|Squeeze at the top"),
    _x("incline-push-up", "Incline push-up", "calisthenics", "chest triceps shoulders", equipment="bench", intensity="moderate",
       contra="wrist_injury", cues="Body in one line|Elbows at 45 degrees"),
    _x("wall-push-up", "Wall push-up", "calisthenics", "chest triceps", contra="wrist_injury",
       cues="Hands at shoulder height|Lower your chest to the wall"),
    _x("bird-dog", "Bird dog", "calisthenics", "core glutes back", equipment="mat", cues="Keep the hips level|Reach long"),
    _x("dead-bug", "Dead bug", "calisthenics", "core", equipment="mat", contra="pregnancy",
       cues="Low back stays on the floor|Move slowly"),
    _x("forearm-plank", "Forearm plank", "calisthenics", "core", equipment="mat", intensity="moderate",
       cues="Straight line from head to heels|Keep breathing"),
    _x("superman-lift", "Superman lift", "calisthenics", "back glutes", equipment="mat", contra="pregnancy", cues="Neck long"),
    _x("seated-band-row", "Seated towel row", "strength", "back lats biceps", cues="Squeeze the shoulder blades|Tall spine"),
)


class Library:
    def __init__(self, items: Iterable[LibItem]) -> None:
        self.items = list(items)
        self.by_slug = {e.slug.lower(): e for e in self.items}
        self.by_norm = {_norm_name(e.name): e for e in self.items}
        for e in self.items:
            self.by_norm.setdefault(_norm_name(e.slug), e)
        self._names = list(self.by_norm)

    def __len__(self) -> int:
        return len(self.items)

    def match(self, name: str | None, slug: str | None = None) -> LibItem | None:
        if slug and slug.strip().lower() in self.by_slug:
            return self.by_slug[slug.strip().lower()]
        if not name:
            return None
        key = _norm_name(name)
        if key in self.by_norm:
            return self.by_norm[key]
        slugged = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
        if slugged in self.by_slug:
            return self.by_slug[slugged]
        close = difflib.get_close_matches(key, self._names, n=1, cutoff=0.86)
        return self.by_norm[close[0]] if close else None


def _norm_name(name: str | None) -> str:
    words = []
    for w in re.findall(r"[a-z0-9]+", (name or "").lower().replace("-", " ")):
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        words.append(w)
    return " ".join(words)


async def load_library(db: AsyncSession) -> Library:
    """The exercises table (embeddings deferred); the coach's built-in library when the table is empty."""
    rows = (await db.execute(select(Exercise).options(defer(Exercise.embedding)).order_by(Exercise.id))).scalars().all()
    if rows:
        return Library(LibItem.from_any(r) for r in rows)
    try:
        from app.services.coach import BUILTIN_LIBRARY  # WP-E2 (optional)

        items = [LibItem.from_any(e) for e in BUILTIN_LIBRARY]
        if items:
            return Library(items)
    except Exception as exc:  # noqa: BLE001 - never let another package break the live coach
        log.debug("coach library unavailable: %s", exc)
    return Library(FALLBACK_LIBRARY)


# ====================================================================================== safety checks
def _e2_check(item: LibItem, tags: set[str]) -> str | None:
    """Extra name/attribute heuristics from the coach guardrails (WP-E2) when that module is importable."""
    try:
        from app.ai import safety as e2

        ex = e2.LibExercise(slug=item.slug, name=item.name, category=item.category, goals=item.goals,
                            muscles=item.muscles, equipment=item.equipment, difficulty=item.difficulty,
                            intensity=item.intensity, impact=item.impact, contraindications=item.contraindications,
                            video_slug=item.video_slug, description=item.description, cues=item.cues)
        return e2.check_exercise(ex, item.name, sorted(tags))
    except Exception:  # noqa: BLE001
        return None


def unsafe_reason(item: LibItem, tags: set[str]) -> str | None:
    """Why ``item`` is unsafe for these safety tags - or None. Contraindication tags always win."""
    if not tags:
        return None
    hit = set(item.contraindications) & tags
    if hit:
        tag = sorted(hit)[0]
        return f"not suitable with {TAG_LABELS.get(tag, tag.replace('_', ' '))}"
    if item.impact == "high" and tags & HIGH_IMPACT_TAGS:
        return "high-impact exercise"
    if item.intensity == "high" and tags & HIGH_INTENSITY_TAGS:
        return "intensity too high"
    if "stomach_discomfort" in tags and (set(item.muscles) & {"core", "obliques", "abs"} or item.intensity == "high"):
        return "loads the stomach area"
    return _e2_check(item, tags)


def is_safe(item: LibItem, tags: set[str]) -> bool:
    return unsafe_reason(item, tags) is None


_LEVEL_RANK = {"beginner": 0, "intermediate": 1, "advanced": 2}
_INTENSITY_RANK = {"low": 0, "moderate": 1, "high": 2}
HOME_EQUIPMENT = frozenset({"none", "mat", "bench", "box", "chair", "wall", "towel"})


def _equipment_rank(equipment: str, current: str | None = None) -> int:
    if equipment in ("none", "mat", "bodyweight") or (current and equipment == current):
        return 0
    return 1 if equipment in HOME_EQUIPMENT else 2


def pick_alternative(
    library: Library, tags: set[str], *, category: str | None = None, current: dict[str, Any] | None = None,
    avoid_muscles: Iterable[str] = (), prefer_muscles: Iterable[str] = (), exclude: Iterable[str] = (),
    prefer_low: bool = False, level: str = "beginner", strict_category: bool = False,
) -> LibItem | None:
    """A safe library exercise: requested category first (else the current one), then mobility/breathing.
    Never contraindicated for ``tags`` and never touching ``avoid_muscles``."""
    avoid = {m.lower() for m in avoid_muscles}
    want = {m.lower() for m in prefer_muscles} or {m.lower() for m in (current or {}).get("muscles") or []}
    excluded = {s.lower() for s in exclude if s}
    if current and current.get("slug"):
        excluded.add(str(current["slug"]).lower())
    lvl = _LEVEL_RANK.get(level or "beginner", 0)
    cur_equipment = str((current or {}).get("equipment") or "none")
    pool = [e for e in library.items if e.slug.lower() not in excluded and not (set(e.muscles) & avoid) and is_safe(e, tags)]

    def rank(e: LibItem) -> tuple:
        overlap = len(set(e.muscles) & want) if want else 0
        return (
            0 if _LEVEL_RANK.get(e.difficulty, 0) <= lvl else 1,
            -overlap,
            0 if (want and e.muscles and e.muscles[0] in want) else 1,
            _INTENSITY_RANK.get(e.intensity, 1) if prefer_low else 0,
            _equipment_rank(e.equipment, cur_equipment),
            e.slug,
        )

    order = [category or (current or {}).get("category") or "calisthenics"]
    if not strict_category:
        order += ["mobility", "breathing"]
    for cat in order:
        candidates = sorted((e for e in pool if e.category == cat), key=rank)
        if candidates:
            return candidates[0]
    if not strict_category and pool:
        return sorted(pool, key=lambda e: (_INTENSITY_RANK.get(e.intensity, 1), e.slug))[0]
    return None


# ====================================================================================== prescriptions
_TIMED = re.compile(r"plank|hold|hollow|carry|wall sit|dead hang|bridge hold|breath|walk|bike|cycl|row(?:ing)? machine|"
                    r"elliptical|jog|run|skip|rope|stretch|circles|rocks|rotation|cat[- ]cow|march", re.I)


def _to_int(value: Any, lo: int, hi: int) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        n = int(round(value))
    else:
        m = re.search(r"\d+", str(value))
        if not m:
            return None
        n = int(m.group(0))
    return max(lo, min(hi, n))


def default_dose(ex: LibItem, level: str = "beginner", capped: bool = False) -> dict[str, Any]:
    lvl = _LEVEL_RANK.get(level or "beginner", 0)
    if ex.category == "breathing":
        return {"sets": 1, "reps": None, "duration_s": 90, "rest_s": 15, "load": "slow and relaxed"}
    if ex.category == "mobility":
        return {"sets": 2, "reps": None, "duration_s": 40, "rest_s": 15, "load": "gentle, pain-free range"}
    if ex.category == "cardio":
        return {"sets": 3, "reps": None, "duration_s": 30 if ex.intensity == "high" else 60, "rest_s": 45,
                "load": "a pace where you can still talk"}
    sets = 2 if lvl == 0 else 3
    if _TIMED.search(ex.name):
        return {"sets": sets, "reps": None, "duration_s": (20, 30, 40)[lvl], "rest_s": 45, "load": "steady breathing"}
    if ex.category == "strength":
        load = "light to moderate - keep breathing, never hold your breath" if capped else "moderate - leave 2-3 reps in reserve"
        return {"sets": sets, "reps": (10, 10, 8)[lvl] if not capped else 12, "duration_s": None, "rest_s": 75, "load": load}
    return {"sets": sets, "reps": (8, 10, 12)[lvl], "duration_s": None, "rest_s": 45, "load": "bodyweight"}


def make_item(ex: LibItem, *, sets: int, reps: int | None, duration_s: int | None, rest_s: int, load: str | None = None,
              notes: str | None = None, cap: bool = False, swapped_from: str | None = None,
              swap_reason: str | None = None) -> dict[str, Any]:
    return {
        "exercise": ex.name, "slug": ex.slug, "category": ex.category, "sets": sets, "reps": reps,
        "duration_s": duration_s, "rest_s": rest_s, "load": (load or None), "notes": (notes or None),
        "cues": list(ex.cues[:4]), "video_slug": ex.video_slug, "muscles": list(ex.muscles[:6]),
        "equipment": ex.equipment, "description": ex.description[:240], "load_cap": cap,
        "swapped_from": swapped_from, "swap_reason": swap_reason,
    }


def item_from_lib(ex: LibItem, level: str, capped: bool, **overrides: Any) -> dict[str, Any]:
    dose = default_dose(ex, level, capped)
    dose.update({k: v for k, v in overrides.items() if k in ("sets", "reps", "duration_s", "rest_s", "load") and v is not None})
    if dose.get("reps") is not None and dose.get("duration_s") is not None:
        dose["reps"] = None if _TIMED.search(ex.name) or ex.category in ("cardio", "mobility", "breathing") else dose["reps"]
        if dose["reps"] is not None:
            dose["duration_s"] = None
    extra = {k: v for k, v in overrides.items() if k in ("notes", "swapped_from", "swap_reason")}
    return make_item(ex, sets=dose["sets"], reps=dose["reps"], duration_s=dose["duration_s"], rest_s=dose["rest_s"],
                     load=dose.get("load"), cap=capped, **extra)


def scale(value: int, intensity: int) -> int:
    """value x (1 + 10 % per intensity step), rounded half-up with integer maths (never below 1)."""
    return max(1, (int(value) * (10 + int(intensity)) * 2 + 10) // 20)


def adjusted_rest(rest_s: int, intensity: int) -> int:
    """Harder = 15 s less rest per step, easier = 15 s more (10-300 s)."""
    return max(10, min(300, int(rest_s) - 15 * int(intensity)))


_LIGHTER = {1: "go a little lighter", 2: "go lighter", 3: "go much lighter"}
_HEAVIER = {1: "go a little heavier if your form stays clean", 2: "go heavier if your form stays clean",
            3: "go heavier - only with perfect form"}
_EASIER_VARIATION = {1: "use a slightly easier variation", 2: "use an easier variation", 3: "use the easiest variation"}
_HARDER_VARIATION = {1: "slow the lowering phase", 2: "add a pause at the hardest point", 3: "slow tempo with a pause"}
_PACE_DOWN = {1: "ease the pace slightly", 2: "ease the pace", 3: "keep it very easy"}
_PACE_UP = {1: "pick up the pace a little", 2: "pick up the pace", 3: "push the pace - still able to talk"}


def load_note(item: dict[str, Any], intensity: int) -> str | None:
    base = str(item.get("load") or "").strip()
    i = max(INTENSITY_MIN, min(INTENSITY_MAX, int(intensity)))
    if i == 0:
        return base or None
    category = item.get("category")
    bodyweight = "bodyweight" in base.lower() or (category == "calisthenics" and item.get("equipment") in (None, "none", "mat"))
    if category in ("cardio", "mobility", "breathing"):
        adj = _PACE_DOWN[-i] if i < 0 else ("keep a steady, comfortable pace" if item.get("load_cap") else _PACE_UP[i])
    elif bodyweight:
        adj = _EASIER_VARIATION[-i] if i < 0 else _HARDER_VARIATION[i]
    elif i < 0:
        adj = _LIGHTER[-i]
    else:
        adj = "keep the same load and slow the tempo" if item.get("load_cap") else _HEAVIER[i]
    return f"{base} - {adj}" if base else adj[0].upper() + adj[1:]


def effective(item: dict[str, Any], intensity: int) -> dict[str, Any]:
    reps = item.get("reps")
    dur = item.get("duration_s")
    return {
        "reps": min(60, scale(reps, intensity)) if reps else None,
        "duration_s": max(5, min(900, scale(dur, intensity))) if dur else None,
        "rest_s": adjusted_rest(item.get("rest_s") or 45, intensity),
        "load_note": load_note(item, intensity),
        "sets": int(item.get("sets") or 1),
    }


def dose_text(item: dict[str, Any], eff: dict[str, Any] | None = None, *, with_sets: bool = True) -> str:
    eff = eff or effective(item, 0)
    sets = eff["sets"]
    if eff.get("reps"):
        unit = f"{eff['reps']} rep{'s' if eff['reps'] != 1 else ''}"
    elif eff.get("duration_s"):
        d = eff["duration_s"]
        unit = f"{d} seconds" if d < 90 or d % 60 else f"{d // 60} minute{'s' if d >= 120 else ''}"
    else:
        unit = "your own pace"
    if not with_sets or sets <= 1:
        return unit
    return f"{sets} sets of {unit}" if eff.get("reps") else f"{sets} rounds of {unit}"


def estimate_minutes(items: list[dict[str, Any]]) -> int:
    total = 0.0
    for it in items:
        work = it.get("duration_s") or (it.get("reps") or 10) * 3.5
        total += int(it.get("sets") or 1) * (work + int(it.get("rest_s") or 30))
    return max(1, round(total / 60))


# ====================================================================================== session building
@dataclass(frozen=True, slots=True)
class QuickSpec:
    key: str
    title: str
    subtitle: str
    icon: str
    slots: tuple[tuple[str, tuple[str, ...]], ...]
    dose: dict[str, dict[str, Any]]


QUICK_SESSIONS: dict[str, QuickSpec] = {q.key: q for q in (
    QuickSpec("full_body", "10-minute full body", "A bodyweight circuit for legs, push, pull and core.", "bolt",
              (("calisthenics", ("quads", "glutes")), ("calisthenics", ("chest", "triceps")), ("calisthenics", ("core",)),
               ("calisthenics", ("back", "glutes", "hamstrings")), ("cardio", ("heart", "legs")), ("mobility", ("hips", "spine"))),
              {"calisthenics": {"sets": 2, "reps": 10, "duration_s": 30, "rest_s": 20},
               "strength": {"sets": 2, "reps": 10, "duration_s": 30, "rest_s": 20},
               "cardio": {"sets": 2, "duration_s": 40, "rest_s": 20},
               "mobility": {"sets": 1, "duration_s": 45, "rest_s": 10},
               "breathing": {"sets": 1, "duration_s": 60, "rest_s": 10}}),
    QuickSpec("mobility", "Mobility & breathing", "Gentle range of motion and calm breathing to reset body and mind.", "wind",
              (("mobility", ("spine", "core")), ("mobility", ("hips", "hamstrings")), ("mobility", ("shoulders",)),
               ("mobility", ("thoracic", "spine")), ("mobility", ("ankles", "calves")), ("breathing", ())),
              {"mobility": {"sets": 2, "duration_s": 40, "rest_s": 10},
               "breathing": {"sets": 1, "duration_s": 90, "rest_s": 10}}),
    QuickSpec("calisthenics", "Calisthenics basics", "Push, squat, pull and core fundamentals with clean form.", "dumbbell",
              (("calisthenics", ("chest", "triceps")), ("calisthenics", ("quads", "glutes")), ("calisthenics", ("back", "lats")),
               ("calisthenics", ("core",)), ("calisthenics", ("glutes", "hamstrings")), ("mobility", ("shoulders", "hips"))),
              {"mobility": {"sets": 1, "duration_s": 45, "rest_s": 15}}),
    QuickSpec("cardio", "Low-impact cardio", "Steady, joint-friendly intervals to lift your heart rate.", "heart",
              (("cardio", ("legs", "heart")), ("cardio", ("back", "heart")), ("cardio", ("heart",)), ("mobility", ("hips",)),
               ("breathing", ())),
              {"cardio": {"sets": 3, "duration_s": 60, "rest_s": 30}, "mobility": {"sets": 1, "duration_s": 45, "rest_s": 10},
               "breathing": {"sets": 1, "duration_s": 60, "rest_s": 10}}),
)}


def build_quick_items(key: str, library: Library, tags: set[str], level: str = "beginner",
                      capped: bool = False) -> tuple[list[dict[str, Any]], int]:
    """Deterministic quick session from the library, filtered against safety tags. Returns (items, slots skipped)."""
    spec = QUICK_SESSIONS[key]
    items: list[dict[str, Any]] = []
    used: set[str] = set()
    skipped = 0
    lvl = _LEVEL_RANK.get(level or "beginner", 0)
    for category, muscles in spec.slots:
        low = key in ("cardio", "mobility")
        ex = pick_alternative(library, tags, category=category, prefer_muscles=muscles, exclude=used, level=level,
                              prefer_low=low, strict_category=True)
        if ex is None:
            ex = pick_alternative(library, tags, category="mobility", exclude=used, level=level, prefer_low=True)
            skipped += 1
        if ex is None:
            continue
        if key in ("cardio", "full_body") and ex.impact == "high":  # low-impact promise
            alt = pick_alternative(library, tags | {"knee_injury"}, category=category, prefer_muscles=muscles, exclude=used,
                                   level=level, prefer_low=True, strict_category=True)
            ex = alt or ex
        used.add(ex.slug)
        dose = default_dose(ex, level, capped)
        over = dict(spec.dose.get(ex.category, {}))
        if over:
            timed = dose.get("duration_s") is not None
            dose["sets"] = over.get("sets", dose["sets"])
            dose["rest_s"] = over.get("rest_s", dose["rest_s"])
            if timed:
                dose["duration_s"] = over.get("duration_s", dose["duration_s"])
            elif over.get("reps"):
                dose["reps"] = max(4, over["reps"] + (-2 if lvl == 0 else (2 if lvl == 2 else 0)))
        items.append(make_item(ex, sets=dose["sets"], reps=dose["reps"], duration_s=dose["duration_s"],
                               rest_s=dose["rest_s"], load=dose.get("load"), cap=capped))
    return items, skipped


def build_plan_day_items(day: dict[str, Any], library: Library, tags: set[str], level: str = "beginner",
                         capped: bool = False) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Snapshot one plan day. Items that became unsafe since the plan was made are swapped (and reported)."""
    items: list[dict[str, Any]] = []
    swaps: list[dict[str, str]] = []
    used: set[str] = set()
    for raw in (day.get("items") or [])[:20]:
        if not isinstance(raw, dict):
            continue
        name = re.sub(r"\s+", " ", str(raw.get("exercise") or raw.get("name") or "")).strip()[:120]
        if not name:
            continue
        lib = library.match(name, raw.get("slug"))
        category = lib.category if lib else str(raw.get("category") or "strength")
        sets = _to_int(raw.get("sets"), 1, 10) or 3
        reps = _to_int(raw.get("reps"), 1, 100)
        duration = _to_int(raw.get("duration_s"), 5, 3600)
        rest = _to_int(raw.get("rest_s"), 0, 600)
        load = str(raw.get("load") or "").strip()[:120] or None
        notes = str(raw.get("notes") or "").strip()[:200] or None
        if lib is not None:
            reason = unsafe_reason(lib, tags)
            if reason:
                alt = pick_alternative(library, tags, category=lib.category, current={"muscles": list(lib.muscles),
                                       "slug": lib.slug, "category": lib.category, "equipment": lib.equipment},
                                       exclude=used, level=level)
                swaps.append({"from": lib.name, "to": alt.name if alt else "", "reason": reason})
                if alt is None:
                    continue
                items.append(item_from_lib(alt, level, capped, sets=sets, swapped_from=lib.name, swap_reason=reason))
                used.add(alt.slug)
                continue
            if reps is None and duration is None:
                dose = default_dose(lib, level, capped)
                reps, duration = dose["reps"], dose["duration_s"]
            item = make_item(lib, sets=sets, reps=reps if duration is None else None, duration_s=duration,
                             rest_s=rest if rest is not None else default_dose(lib, level, capped)["rest_s"],
                             load=load or default_dose(lib, level, capped).get("load"), notes=notes, cap=capped)
            used.add(lib.slug)
        else:
            reason = _e2_check(LibItem(slug="custom", name=name, category=category), tags) if tags else None
            if reason:
                alt = pick_alternative(library, tags, category=category if category in CATEGORIES else None, exclude=used,
                                       level=level)
                swaps.append({"from": name, "to": alt.name if alt else "", "reason": reason})
                if alt is None:
                    continue
                items.append(item_from_lib(alt, level, capped, sets=sets, swapped_from=name, swap_reason=reason))
                used.add(alt.slug)
                continue
            if reps is None and duration is None:
                reps = 10
            item = {
                "exercise": name, "slug": None, "category": category if category in CATEGORIES else "strength",
                "sets": sets, "reps": reps if duration is None else None, "duration_s": duration,
                "rest_s": rest if rest is not None else 60, "load": load, "notes": notes, "cues": [], "video_slug": None,
                "muscles": [], "equipment": "none", "description": "", "load_cap": capped, "swapped_from": None,
                "swap_reason": None,
            }
        items.append(item)
    return items, swaps


# ====================================================================================== member context
@dataclass
class Member:
    user: User
    profile: CustomerProfile | None
    access: PlanAccess
    tags: set[str] = field(default_factory=set)
    condition_names: list[str] = field(default_factory=list)
    ctx: Any = None  # app.ai.member_context.MemberContext - what the member told the app (the AI's view of them)

    @property
    def suspended(self) -> bool:
        return bool(self.profile is not None and self.profile.activity_suspended)

    @property
    def first_name(self) -> str:
        name = (self.user.full_name or "").strip()
        return name.split()[0] if name else "there"

    @property
    def level(self) -> str:
        lvl = getattr(self.profile, "fitness_level", None)
        return str(getattr(lvl, "value", lvl) or "beginner")

    @property
    def goal(self) -> str:
        g = getattr(self.profile, "goal", None)
        return str(getattr(g, "value", g) or "general_fitness")

    @property
    def age(self) -> int | None:
        return self.user.age()

    @property
    def age_group(self) -> str:
        age = self.age
        if age is None:
            return "adult"
        if age < 18:
            return "teenager (under 18)"
        if age >= 65:
            return "senior (65+)"
        return "adult"

    @property
    def capped(self) -> bool:
        return bool(self.tags & CAPPED_TAGS)

    @property
    def max_intensity(self) -> int:
        return CAPPED_MAX_INTENSITY if self.capped else INTENSITY_MAX

    @property
    def country(self) -> str | None:
        if self.user.country_code:
            return self.user.country_code
        for sub in self.access.subscriptions:
            gym = sub.__dict__.get("gym")
            if gym is not None and getattr(gym, "country_code", None):
                return gym.country_code
        return None

    @property
    def emergency(self) -> dict[str, Any]:
        return emergency_info(self.country)


async def _profile_of(db: AsyncSession, user: User) -> CustomerProfile | None:
    state = sa_inspect(user)
    if "customer_profile" not in state.unloaded:
        return user.customer_profile
    return await db.get(CustomerProfile, user.id)


async def load_member(db: AsyncSession, user: User) -> Member:
    """Every live-coach call loads the member through the ONE member context (app/ai/member_context.py: two queries -
    the profiles, and at most 50 health rows, safety-relevant first, LIMIT in SQL). The safety rules (tags), the AI's
    prompt and the voice cues all read that same data, so a condition saved a moment ago counts on the next call."""
    from app.ai.member_context import load_member_context

    access = await plan_access(db, user.id)
    ctx = await load_member_context(db, user)
    names = [c.name for c in ctx.conditions]
    tags = {c.tag for c in ctx.conditions if c.tag}
    age = user.age()
    if age is not None and age < 18:
        tags.add("minor")
    return Member(user=user, profile=await _profile_of(db, user), access=access, tags=tags, condition_names=names,
                  ctx=ctx)


async def active_plan(db: AsyncSession, user_id: int) -> WorkoutPlan | None:
    return (await db.execute(
        select(WorkoutPlan).where(WorkoutPlan.customer_id == user_id, WorkoutPlan.is_active.is_(True))
        .order_by(WorkoutPlan.created_at.desc(), WorkoutPlan.id.desc()).limit(1)
    )).scalars().first()


async def recent_sessions(db: AsyncSession, user_id: int, limit: int = 6) -> list[WorkoutSession]:
    return list((await db.execute(
        select(WorkoutSession).where(WorkoutSession.customer_id == user_id)
        .order_by(WorkoutSession.id.desc()).limit(limit)
    )).scalars().all())


async def get_session(db: AsyncSession, user: User, session_id: int) -> WorkoutSession | None:
    """The member's own session (None for someone else's -> callers answer 404)."""
    return (await db.execute(
        select(WorkoutSession).where(WorkoutSession.id == session_id, WorkoutSession.customer_id == user.id)
    )).scalars().first()


async def session_extra_tags(db: AsyncSession, session: WorkoutSession) -> set[str]:
    """Safety tags added during this session by discomfort reports (e.g. knee -> knee_injury)."""
    rows = (await db.execute(select(WorkoutSessionEvent.detail).where(
        WorkoutSessionEvent.session_id == session.id, WorkoutSessionEvent.kind == "discomfort"))).scalars().all()
    tags: set[str] = set()
    for d in rows:
        if isinstance(d, dict):
            for key in ("tag", "pseudo_tag"):
                if d.get(key):
                    tags.add(str(d[key]))
    return tags


def start_screen_options(plan: WorkoutPlan | None, library: Library, member: Member) -> dict[str, Any]:
    """Day cards for the active plan + quick sessions (with durations and safety swap counts)."""
    days: list[dict[str, Any]] = []
    if plan is not None and isinstance(plan.plan, dict):
        for i, day in enumerate((plan.plan.get("days") or [])[:14]):
            if not isinstance(day, dict):
                continue
            items, swaps = build_plan_day_items(day, library, member.tags, member.level, member.capped)
            days.append({
                "index": i, "name": str(day.get("name") or f"Day {i + 1}")[:80], "focus": str(day.get("focus") or "")[:80],
                "count": len(items), "minutes": estimate_minutes(items) if items else 0,
                "names": [it["exercise"] for it in items[:4]], "swaps": len(swaps),
            })
    quick = []
    for key, spec in QUICK_SESSIONS.items():
        items, skipped = build_quick_items(key, library, member.tags, member.level, member.capped)
        quick.append({"key": key, "title": spec.title, "subtitle": spec.subtitle, "icon": spec.icon, "count": len(items),
                      "minutes": estimate_minutes(items) if items else 0, "names": [it["exercise"] for it in items[:4]],
                      "skipped": skipped})
    today = date.today().weekday() % len(days) if days else 0
    return {"days": days, "quick": quick, "today_index": today}


def _sessions_lock(user_id: int) -> int:
    import hashlib

    return int.from_bytes(hashlib.blake2b(f"live_sessions:{int(user_id)}".encode(), digest_size=8).digest(), "big",
                          signed=True)


class LiveError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


async def create_session(db: AsyncSession, member: Member, *, plan_id: int | None = None, day_index: int | None = None,
                         quick: str | None = None, voice_enabled: bool = True) -> WorkoutSession:
    library = await load_library(db)
    plan: WorkoutPlan | None = None
    if quick:
        if quick not in QUICK_SESSIONS:
            raise LiveError("Unknown quick session.")
        items, _ = build_quick_items(quick, library, member.tags, member.level, member.capped)
        title = QUICK_SESSIONS[quick].title
        detail: dict[str, Any] = {"source": "quick", "key": quick}
    elif plan_id is not None:
        plan = (await db.execute(select(WorkoutPlan).where(WorkoutPlan.id == plan_id,
                                                           WorkoutPlan.customer_id == member.user.id))).scalars().first()
        if plan is None:
            raise LiveError("Workout plan not found.", 404)
        days = (plan.plan or {}).get("days") or [] if isinstance(plan.plan, dict) else []
        idx = 0 if day_index is None else int(day_index)
        if not (0 <= idx < len(days)) or not isinstance(days[idx], dict):
            raise LiveError("That plan day doesn't exist.")
        items, swaps = build_plan_day_items(days[idx], library, member.tags, member.level, member.capped)
        name = str(days[idx].get("name") or f"Day {idx + 1}")[:80]
        title = f"{name}" if name.lower().startswith("day") else f"Day {idx + 1} · {name}"
        detail = {"source": "plan", "plan_id": plan.id, "day_index": idx, "swaps": swaps}
    else:
        raise LiveError("Pick a plan day or a quick session.")
    if not items:
        raise LiveError("No safe exercises are available for this workout - try a quick session or ask your trainer.")
    now = utcnow()
    # at most SESSIONS_PER_DAY started in 24 hours, counted behind the member's lock (two taps at once can't pass it)
    await db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _sessions_lock(member.user.id)})
    started = int(await db.scalar(select(func.count()).select_from(
        select(WorkoutSession.id).where(WorkoutSession.customer_id == member.user.id,
                                        WorkoutSession.started_at >= now - timedelta(hours=24))
        .limit(SESSIONS_PER_DAY).subquery())) or 0)
    if started >= SESSIONS_PER_DAY:
        raise LiveError(f"You've started {SESSIONS_PER_DAY} workouts in the last 24 hours - the most we allow. "
                        "Pick up a recent session, or start a new one tomorrow.", 429)
    await db.execute(
        update(WorkoutSession).where(WorkoutSession.customer_id == member.user.id, WorkoutSession.status.in_(tuple(OPEN)))
        .values(status="stopped", ended_at=now, end_reason="Replaced by a new session")
    )
    s = WorkoutSession(customer_id=member.user.id, plan_id=plan.id if plan else None, title=title[:160], status="active",
                       intensity=0, current_index=0, items=items, started_at=now, voice_enabled=bool(voice_enabled),
                       mic_used=False, ai_messages=0)
    db.add(s)
    await db.flush()
    log_event(db, s, "created", detail=detail)
    return s


# ====================================================================================== serialisation
def _iso(dt: Any) -> str | None:
    return dt.isoformat() if dt is not None else None


def item_view(item: dict[str, Any], intensity: int) -> dict[str, Any]:
    eff = effective(item, intensity)
    return {
        "exercise": item.get("exercise"), "slug": item.get("slug"), "category": item.get("category"),
        "sets": eff["sets"], "reps": item.get("reps"), "duration_s": item.get("duration_s"), "rest_s": item.get("rest_s"),
        "load": item.get("load"), "notes": item.get("notes"), "cues": list(item.get("cues") or [])[:4],
        "video_slug": item.get("video_slug"), "muscles": list(item.get("muscles") or [])[:6],
        "description": item.get("description") or "", "swapped_from": item.get("swapped_from"),
        "swap_reason": item.get("swap_reason"), "effective": eff, "dose": dose_text(item, eff),
        "timed": bool(eff.get("duration_s")) and not eff.get("reps"),
    }


def session_payload(s: WorkoutSession) -> dict[str, Any]:
    items = [item_view(it, s.intensity or 0) for it in (s.items or []) if isinstance(it, dict)]
    return {
        "id": s.id, "title": s.title, "status": s.status, "intensity": s.intensity or 0,
        "intensity_label": INTENSITY_LABELS.get(s.intensity or 0, "Standard"),
        "current_index": max(0, min(int(s.current_index or 0), len(items))), "total": len(items), "items": items,
        "voice_enabled": bool(s.voice_enabled), "mic_used": bool(s.mic_used), "ai_messages": int(s.ai_messages or 0),
        "started_at": _iso(s.started_at), "ended_at": _iso(s.ended_at), "end_reason": s.end_reason,
        "ended": s.status in ENDED, "stopped_for_safety": s.status == "stopped_for_safety",
        "minutes": estimate_minutes(list(s.items or [])) if s.items else 0,
    }


def log_event(db: AsyncSession, session: WorkoutSession, kind: str, message: str | None = None,
              detail: dict[str, Any] | None = None) -> WorkoutSessionEvent:
    ev = WorkoutSessionEvent(session_id=session.id, kind=kind[:30],
                             message=(message[:MESSAGE_MAX] if message else None), detail=detail or {})
    if getattr(session, "_events_full", False) and kind not in ALWAYS_LOGGED:
        return ev  # SESSION_EVENTS_MAX reached: not stored (safety events always are)
    db.add(ev)
    return ev


async def note_event_budget(db: AsyncSession, session: WorkoutSession) -> None:
    """Mark a session whose log reached SESSION_EVENTS_MAX (a bounded count over its own events)."""
    inner = (select(WorkoutSessionEvent.id).where(WorkoutSessionEvent.session_id == session.id)
             .limit(SESSION_EVENTS_MAX).subquery())
    held = int(await db.scalar(select(func.count()).select_from(inner)) or 0)
    session._events_full = held >= SESSION_EVENTS_MAX  # type: ignore[attr-defined] - a per-request flag


@dataclass
class ClientState:
    index: int = 0
    set: int = 1
    phase: str = "work"
    elapsed_s: int = 0
    voice: bool | None = None


def parse_state(raw: Any) -> ClientState:
    if raw is None:
        return ClientState()
    get = raw.get if isinstance(raw, dict) else (lambda k, d=None: getattr(raw, k, d))
    phase = str(get("phase", "work") or "work").lower()
    voice = get("voice", None)
    return ClientState(index=_to_int(get("index", 0), 0, 500) or 0, set=_to_int(get("set", 1), 1, 50) or 1,
                       phase=phase if phase in PHASES else "work", elapsed_s=_to_int(get("elapsed_s", 0), 0, 86400) or 0,
                       voice=voice if isinstance(voice, bool) else None)


def result(session: WorkoutSession, reply: str, emotion: str, actions: list[dict[str, Any]] | None = None, *,
           source: str = "rule", intent: Intent | None = None, provider: str | None = None, **extra: Any) -> dict[str, Any]:
    from app.ai.client import AI_DISCLAIMER

    out = {"reply": reply, "emotion": emotion if emotion in EMOTIONS else "neutral", "actions": actions or [],
           "session": session_payload(session), "source": source, "intent": intent.as_dict() if intent else None,
           "provider": provider, "disclaimer": AI_DISCLAIMER}
    out.update(extra)
    return out


# ====================================================================================== spoken cues
PRAISE = ("Nice work!", "Great set!", "Strong work!", "That's it!", "Well done!", "Beautiful form!")
DONE_PRAISE = ("Great job!", "Brilliant!", "Excellent work!", "Superb!", "You smashed it!")


def _current(session: WorkoutSession) -> tuple[int, dict[str, Any] | None]:
    items = session.items or []
    idx = int(session.current_index or 0)
    return idx, (items[idx] if 0 <= idx < len(items) and isinstance(items[idx], dict) else None)


TAG_AREAS = {tag: part for part, tag in BODY_PART_TAGS.items()}  # knee_injury -> knee ...


def care_note(item: dict[str, Any] | None, member: Member | None) -> str:
    """A spoken reminder when the next exercise loads an area the member told us about ("Go gently on your knee - stop
    if it hurts."). Deterministic, from the same tags the safety rules read; '' when nothing applies."""
    if not item or member is None or not member.tags:
        return ""
    muscles = {str(m).lower() for m in (item.get("muscles") or [])}
    for tag in sorted(member.tags):
        part = TAG_AREAS.get(tag)
        if part and muscles & AREA_MUSCLES.get(part, set()):
            return f"Go gently on your {BODY_PART_LABELS.get(part, part)} - stop if it hurts."
    return ""


def _intro(item: dict[str, Any], intensity: int, *, cue: bool = True, member: Member | None = None) -> str:
    eff = effective(item, intensity)
    text = f"{item.get('exercise')} - {dose_text(item, eff)}."
    cues = item.get("cues") or []
    if cue and cues:
        text += f" {str(cues[0]).rstrip('.')}."
    care = care_note(item, member)
    if care:
        text += f" {care}"
    return text


async def ensure_safe_item(db: AsyncSession, member: Member, session: WorkoutSession, idx: int) -> str:
    """Re-check the exercise about to start against the member's CURRENT safety tags - a condition saved since the
    session was built (on the profile, confirmed from a chat, added by the gym) counts at once - and swap it for a
    safe alternative when it is no longer suitable. Returns the spoken note ('' when nothing changed)."""
    items = list(session.items or [])
    if not member.tags or not (0 <= idx < len(items)) or not isinstance(items[idx], dict):
        return ""
    item = items[idx]
    library = await load_library(db)
    lib = library.match(str(item.get("exercise") or ""), item.get("slug"))
    if lib is not None:
        reason = unsafe_reason(lib, member.tags)
    else:
        reason = _e2_check(LibItem(slug="custom", name=str(item.get("exercise") or "")[:120],
                                   category=str(item.get("category") or "strength")), member.tags)
    if not reason:
        return ""
    used = {str(it.get("slug")).lower() for it in items if isinstance(it, dict) and it.get("slug")}
    current = {"muscles": list(item.get("muscles") or []), "slug": item.get("slug"),
               "category": item.get("category"), "equipment": item.get("equipment")}
    alt = pick_alternative(library, member.tags, category=lib.category if lib else None, current=current,
                           exclude=used, level=member.level)
    if alt is None:
        return ""
    sets = _to_int(item.get("sets"), 1, 10) or 3
    items[idx] = item_from_lib(alt, member.level, member.capped, sets=sets, swapped_from=item.get("exercise"),
                               swap_reason=reason)
    session.items = items
    log_event(db, session, "exercise_change", detail={"index": idx, "from": item.get("exercise"), "to": alt.name,
                                                     "reason": reason, "source": "profile_safety"})
    return f"I've swapped {item.get('exercise')} for {alt.name} - {reason}."


def _minutes_since(session: WorkoutSession) -> int:
    if not session.started_at:
        return 0
    return max(1, round((utcnow() - session.started_at).total_seconds() / 60))


def completion_cue(session: WorkoutSession, member: Member | None = None) -> str:
    n = len(session.items or [])
    name = f", {member.first_name}" if member else ""
    return (f"That's the workout{name}! {n} exercise{'s' if n != 1 else ''} in {_minutes_since(session)} minutes. "
            "Brilliant effort - cool down gently and have some water.")


def _complete(db: AsyncSession, session: WorkoutSession, reason: str = "Workout completed") -> None:
    session.status = "completed"
    session.ended_at = session.ended_at or utcnow()
    session.end_reason = reason[:255]
    session.current_index = len(session.items or [])
    log_event(db, session, "completed", detail={"minutes": _minutes_since(session)})


async def _notify_completed(db: AsyncSession, member: Member, session: WorkoutSession) -> None:
    n = len(session.items or [])
    await notify(db, member.user, "Live workout complete",
                 f"{session.title}: {n} exercise{'s' if n != 1 else ''} in {_minutes_since(session)} minutes. Great work!",
                 category=NotificationCategory.HEALTH, severity=Severity.SUCCESS, link="/me/live", email=False)


async def handle_event(db: AsyncSession, member: Member, session: WorkoutSession, etype: str,
                       state: ClientState) -> dict[str, Any]:
    """Deterministic spoken cue + emotion for a workout event; persists progress."""
    await note_event_budget(db, session)
    if state.voice is not None:
        session.voice_enabled = state.voice
    if session.status in ENDED:
        return result(session, "", "calm", [{"type": "ended", "status": session.status}])
    if member.suspended:
        return await _suspended_stop(db, member, session)
    items = session.items or []
    n = len(items)
    idx, item = _current(session)
    intensity = session.intensity or 0
    actions: list[dict[str, Any]] = []
    if etype == "started":
        fresh = idx == 0 and state.set <= 1 and state.index == 0
        if session.status == "paused":
            session.status = "active"
        log_event(db, session, "started" if fresh else "resumed", detail={"index": idx, "set": state.set})
        if item is None:
            _complete(db, session)
            return result(session, completion_cue(session, member), "proud", [{"type": "completed"}])
        if fresh:
            swapped = await ensure_safe_item(db, member, session, idx)
            if swapped:
                idx, item = _current(session)
                assert item is not None
            cue = f"Let's go, {member.first_name}! {session.title}. {swapped + ' ' if swapped else ''}" \
                  f"First up: {_intro(item, intensity, member=member)}"
            return result(session, cue, "excited")
        sets = int(item.get("sets") or 1)
        cue = f"Welcome back! Picking up with {item.get('exercise')}, set {min(state.set, sets)} of {sets}."
        return result(session, cue, "encouraging")
    if etype == "paused":
        if session.status == "active":
            session.status = "paused"
        log_event(db, session, "paused", detail={"index": idx, "set": state.set})
        return result(session, "Paused. Take your time - say resume when you're ready.", "calm")
    if etype == "resumed":
        if session.status == "paused":
            session.status = "active"
        log_event(db, session, "resumed", detail={"index": idx, "set": state.set})
        name = item.get("exercise") if item else "the workout"
        return result(session, f"Welcome back. Let's continue with {name}.", "encouraging")
    if etype == "completed":
        _complete(db, session)
        await _notify_completed(db, member, session)
        return result(session, completion_cue(session, member), "proud", [{"type": "completed"}])
    if etype == "set_done" and item is not None:
        sets = int(item.get("sets") or 1)
        done = min(state.set, sets)
        log_event(db, session, "set_done", detail={"index": idx, "set": done})
        if done < sets:
            eff = effective(item, intensity)
            praise = PRAISE[(idx + done) % len(PRAISE)]
            cue = f"{praise} Rest {eff['rest_s']} seconds. Then set {done + 1} of {sets}."
            return result(session, cue, "encouraging" if done % 2 else "happy")
        etype = "exercise_done"
    if etype in ("exercise_done", "skipped"):
        if item is None:
            _complete(db, session)
            await _notify_completed(db, member, session)
            return result(session, completion_cue(session, member), "proud", [{"type": "completed"}])
        # the client says which exercise it finished: idempotent for repeats, self-healing if a request was lost
        k = state.index if 0 <= state.index < n else idx
        if k >= idx:
            item = items[k] if isinstance(items[k], dict) else item
            session.current_index = min(n, k + 1)
        log_event(db, session, "exercise_done" if etype == "exercise_done" else "skipped",
                  detail={"index": k, "exercise": item.get("exercise")})
        nxt_idx = int(session.current_index or 0)
        if nxt_idx >= n:
            _complete(db, session)
            await _notify_completed(db, member, session)
            return result(session, completion_cue(session, member), "proud", [{"type": "completed"}])
        swapped = await ensure_safe_item(db, member, session, nxt_idx)  # safety first: tags saved since the start count
        nxt = (session.items or items)[nxt_idx]
        note = f"{swapped} " if swapped else ""
        rest = effective(item, intensity)["rest_s"]
        if etype == "skipped":
            cue = f"No problem, skipping {item.get('exercise')}. {note}Next up: {_intro(nxt, intensity, member=member)}"
            return result(session, cue, "calm", actions)
        praise = DONE_PRAISE[idx % len(DONE_PRAISE)]
        cue = (f"{praise} That's {item.get('exercise')} done. Rest {rest} seconds. {note}"
               f"Next up: {_intro(nxt, intensity, member=member)}")
        return result(session, cue, "proud", actions)
    return result(session, "", "neutral")


# ====================================================================================== safety: red flags
async def _owner_users(db: AsyncSession, gym_ids: Iterable[int]) -> list[User]:
    ids = sorted({int(g) for g in gym_ids})
    if not ids:
        return []
    return list((await db.execute(
        select(User).join(Gym, Gym.owner_id == User.id).where(Gym.id.in_(ids), User.deleted_at.is_(None)).distinct()
    )).scalars().all())


async def _trainer_users(db: AsyncSession, customer_id: int) -> list[User]:
    return list((await db.execute(
        select(User).join(Employee, Employee.user_id == User.id)
        .join(TrainerClient, TrainerClient.employee_id == Employee.id)
        .where(TrainerClient.customer_id == customer_id, TrainerClient.active.is_(True),
               Employee.status == EmploymentStatus.ACTIVE, Employee.deleted_at.is_(None), User.deleted_at.is_(None))
        .distinct()
    )).scalars().all())


async def _suspend(db: AsyncSession, member: Member, reason: str, alert: HealthAlert) -> str:
    """Suspend gym activities. Returns "health" when WP-E1's ``suspend_customer`` did it (it also notifies the member,
    the guardian of a minor, gym owners and assigned trainers), "direct" when we did it ourselves, "already" when the
    member was already suspended."""
    if member.suspended:
        return "already"
    try:
        from app.services.health import suspend_customer  # WP-E1
    except ImportError:
        suspend_customer = None
    if suspend_customer is not None:
        try:
            await suspend_customer(db, member.user, reason, "rule", alert=alert)
            if member.profile is None:
                member.profile = await db.get(CustomerProfile, member.user.id)
            if member.profile is not None and member.profile.activity_suspended:
                return "health"
            log.warning("health.suspend_customer did not suspend the profile - suspending directly")
        except (TypeError, AttributeError, ValueError) as exc:
            log.warning("health.suspend_customer unusable (%s) - suspending directly", exc)
    profile = member.profile or await db.get(CustomerProfile, member.user.id)
    if profile is None:
        profile = CustomerProfile(user_id=member.user.id)
        db.add(profile)
    member.profile = profile
    profile.activity_suspended = True
    profile.suspension_reason = reason
    profile.suspended_at = utcnow()
    profile.suspended_by = "rule"
    db.add(SuspensionEvent(customer_id=member.user.id, action="suspend", reason=reason, actor="rule",
                           health_alert_id=alert.id))
    return "direct"


def red_flag_reply(flag: str, emergency: dict[str, Any], *, include_tail: bool = True) -> str:
    advice = FLAG_ADVICE.get(flag, FLAG_ADVICE["emergency"]).format(num=emergency["number"])
    parts = [advice]
    if emergency.get("note"):
        parts.append(emergency["note"])
    if include_tail:
        parts.append(SAFETY_TAIL)
    return " ".join(parts)


async def safety_guidance(db: AsyncSession, member: Member, session: WorkoutSession) -> str | None:
    """The emergency guidance given at the most recent red flag of this session (for the safety screen on reload)."""
    if session.status != "stopped_for_safety":
        return None
    detail = (await db.execute(select(WorkoutSessionEvent.detail).where(
        WorkoutSessionEvent.session_id == session.id, WorkoutSessionEvent.kind == "red_flag")
        .order_by(WorkoutSessionEvent.id.desc()).limit(1))).scalar()
    flag = detail.get("flag") if isinstance(detail, dict) else None
    return red_flag_reply(flag, member.emergency) if flag else None


def safety_locked_reply(emergency: dict[str, Any]) -> str:
    return (f"Your workout was stopped for your safety, so I can't restart it - a gym admin needs to check in with you "
            f"first. If you feel unwell, call {emergency['number']}.")


async def trigger_red_flag(db: AsyncSession, member: Member, session: WorkoutSession, *, flag: str, level: str,
                           text: str | None, source: str, request: Any = None) -> dict[str, Any]:
    emergency = member.emergency
    reply = red_flag_reply(flag, emergency)
    label = FLAG_LABELS.get(flag, flag.replace("_", " "))
    already = session.status == "stopped_for_safety"
    now = utcnow()
    session.status = "stopped_for_safety"
    session.ended_at = session.ended_at or now
    if not already:
        session.end_reason = f"Stopped for safety: {label}"[:255]
    guardian = bool(member.user.is_minor and member.profile is not None and member.profile.guardian_email)
    alert = HealthAlert(
        customer_id=member.user.id, level=AnomalyLevel(level),
        title=f"Live workout stopped: {label}"[:160],
        message=(f"During a live AI workout ({session.title}) the member reported {label}. "
                 f"They said: \"{(text or '').strip()[:300]}\"" if text else
                 f"During a live AI workout ({session.title}) the member reported {label} (quick button)."),
        recommendation=reply, source="rule", guardian_notified=guardian,
    )
    from app.services.health import add_alert  # under the per-member daily alert cap (never dropped)

    alert = await add_alert(db, alert)
    log_event(db, session, "red_flag", message=text, detail={
        "flag": flag, "level": level, "source": source, "emergency_number": emergency["number"], "alert_id": alert.id,
        "repeat": already,
    })
    mode = "repeat"
    if not already:
        mode = await _suspend(db, member, f"Live workout stopped: you reported {label} during {session.title}.", alert)
    if mode in ("direct", "already"):  # WP-E1's suspend_customer already notified everyone in the "health" case
        name = member.user.display_name
        await notify(db, member.user, "Workout stopped for your safety",
                     f"You reported {label} during a live workout. {reply}",
                     category=NotificationCategory.HEALTH, severity=Severity.DANGER, link=f"/me/live/{session.id}")
        body_staff = (f"{name} reported {label} during a live AI workout ({session.title}). The session was stopped, "
                      f"emergency guidance (call {emergency['number']}) was given and gym activities are suspended until "
                      "an admin reactivates them. Please check in with the member.")
        notified: set[int] = {member.user.id}
        staff_notes: list[Note] = []
        for owner in await _owner_users(db, member.access.gym_ids):
            if owner.id not in notified:
                notified.add(owner.id)
                staff_notes.append(Note(owner, f"Member safety alert: {name}", body_staff,
                                        link=f"/owner/customers/{member.user.id}",
                                        category=NotificationCategory.HEALTH, severity=Severity.DANGER))
        for trainer in await _trainer_users(db, member.user.id):
            if trainer.id not in notified:
                notified.add(trainer.id)
                staff_notes.append(Note(trainer, f"Client safety alert: {name}", body_staff,
                                        link=f"/staff/clients/{member.user.id}",
                                        category=NotificationCategory.HEALTH, severity=Severity.DANGER))
        if staff_notes:  # the owners, then the trainers: ONE bulk send (``notify_bulk``), never one per person
            await db.flush()  # as notify() did: the caller's pending changes are written before the notices
            await notify_bulk(db, staff_notes)
    if not already:
        await audit(db, member.user, "live.safety_stop", "workout_session", session.id, detail={
            "flag": flag, "level": level, "source": source, "alert_id": alert.id, "suspension": mode,
        }, request=request)
    actions = [{"type": "safety_stop", "flag": flag, "label": label, "level": level, "emergency": emergency}]
    return result(session, reply, "concerned", actions, source="safety",
                  intent=Intent("red_flag", flag, level=level), emergency=emergency, suspended=True)


# ====================================================================================== safety: discomfort
async def report_discomfort(db: AsyncSession, member: Member, session: WorkoutSession, *, body_part: str,
                            severity: str = "moderate", note: str | None = None, text: str | None = None,
                            area: str | None = None, source: str = "button", request: Any = None) -> dict[str, Any]:
    body_part = body_part if body_part in BODY_PARTS else "other"
    severity = severity if severity in SEVERITIES else "moderate"
    note = (note or "").strip()[:NOTE_MAX] or None
    said = text or note
    if severity == "severe" or body_part in ("chest", "head"):
        flag = {"chest": "chest_pain", "head": "headache"}.get(body_part, "severe_pain")
        if body_part == "head" and severity == "severe":
            flag = "severe_headache"
        level = LIFE if flag in ("chest_pain", "severe_headache") else CRIT
        return await trigger_red_flag(db, member, session, flag=flag, level=level, source=source, request=request,
                                      text=said or f"{severity} {BODY_PART_LABELS[body_part]} discomfort")
    if session.status in ENDED:
        return result(session, "Thanks for telling me. Rest up, and let your trainer know if it doesn't settle.",
                      "empathetic", source="rule")
    tag = BODY_PART_TAGS.get(body_part)
    pseudo = "stomach_discomfort" if body_part == "stomach" else None
    library = await load_library(db)
    extra = await session_extra_tags(db, session)
    tags = set(member.tags) | extra | {t for t in (tag, pseudo) if t}
    items = [dict(it) for it in (session.items or [])]
    idx, current = _current(session)
    avoid = set(AREA_MUSCLES.get(body_part, set())) | AREA_WORD_MUSCLES.get((area or "").lower(), set())
    if current:
        avoid |= {str(m).lower() for m in current.get("muscles") or []}
    used = {str(it.get("slug")) for it in items if it.get("slug")}
    part_label = BODY_PART_LABELS[body_part] if body_part != "other" else (area or "body")
    alt_item: dict[str, Any] | None = None
    if current is not None:
        alt = pick_alternative(library, tags, current=current, avoid_muscles=avoid, exclude=used, prefer_low=True,
                               level=member.level)
        if alt is not None:
            alt_item = item_from_lib(alt, member.level, member.capped, sets=int(current.get("sets") or 2),
                                     swapped_from=current.get("exercise"), swap_reason=f"{part_label} discomfort")
            items[idx] = alt_item
            used.add(alt.slug)
    later_swaps = []
    if tag or pseudo:
        for j in range(idx + 1, len(items)):
            it = items[j]
            lib = library.match(it.get("exercise"), it.get("slug"))
            probe = lib or LibItem(slug=str(it.get("slug") or "custom"), name=str(it.get("exercise") or ""),
                                   category=str(it.get("category") or "strength"), muscles=tuple(it.get("muscles") or ()))
            if is_safe(probe, tags) and not (set(probe.muscles) & set(AREA_MUSCLES.get(body_part, set()))):
                continue
            alt = pick_alternative(library, tags, current=it, avoid_muscles=AREA_MUSCLES.get(body_part, set()),
                                   exclude=used, level=member.level)
            if alt is not None:
                items[j] = item_from_lib(alt, member.level, member.capped, sets=int(it.get("sets") or 2),
                                         swapped_from=it.get("exercise"), swap_reason=f"{part_label} discomfort")
                used.add(alt.slug)
                later_swaps.append({"from": it.get("exercise"), "to": alt.name})
    session.items = items
    if session.status == "active":
        session.status = "paused"
    alert = HealthAlert(
        customer_id=member.user.id, level=AnomalyLevel.WARNING,
        title=f"Discomfort during a live workout: {part_label}"[:160],
        message=(f"{severity.capitalize()} {part_label} discomfort reported during {current.get('exercise') if current else 'a live workout'}"
                 f" ({session.title})." + (f" Member's words: \"{said[:300]}\"" if said else "")),
        recommendation="The exercise was stopped and a safer alternative offered. Check in before the next session if it persists.",
        source="rule",
    )
    from app.services.health import add_alert  # under the per-member daily alert cap (never dropped)

    alert = await add_alert(db, alert)
    log_event(db, session, "discomfort", message=said, detail={
        "body_part": body_part, "area": area, "severity": severity, "tag": tag, "pseudo_tag": pseudo, "source": source,
        "exercise": current.get("exercise") if current else None, "swapped_to": alt_item["exercise"] if alt_item else None,
        "later_swaps": later_swaps, "alert_id": alert.id,
    })
    if alt_item is not None and current is not None:
        log_event(db, session, "exercise_change", detail={"index": idx, "from": current.get("exercise"),
                                                         "to": alt_item["exercise"], "reason": "discomfort"})
    name = member.user.display_name
    # a non-emergency report is health detail: trainers hear of it only while the member shares health information
    # with them (account settings; red flags above always alert - safety first)
    from app.services.trainer_safety import sharing_enabled

    shares = (member.profile.share_health_with_trainers is not False) if member.profile is not None else \
        await sharing_enabled(db, member.user.id)
    trainers = await _trainer_users(db, member.user.id) if shares else []
    if trainers:  # ONE bulk send (``notify_bulk``), never one notify per trainer
        told = (f"{name} reported {severity} {part_label} discomfort during a live workout ({session.title})"
                + (f" while doing {current.get('exercise')}" if current else "")
                + (f". We switched to {alt_item['exercise']}." if alt_item else ". The exercise was stopped."))
        await db.flush()  # as notify() did: the session changes and its events are written before the notices
        await notify_bulk(db, [Note(trainer, f"Client discomfort: {name}", told, link=f"/staff/clients/{member.user.id}",
                                    category=NotificationCategory.HEALTH, severity=Severity.WARNING)
                               for trainer in trainers])
    sorry = f"I'm sorry your {part_label} is bothering you. Let's stop {current.get('exercise')}." if current else \
        f"I'm sorry your {part_label} is bothering you. Let's take a break."
    if alt_item is not None:
        cue = (alt_item.get("cues") or [""])[0]
        follow = f" Let's try {alt_item['exercise']} instead{(' - ' + cue.rstrip('.')) if cue else ''}. Go gently and stop if it hurts."
    else:
        follow = " Rest for now - there's no safe swap for this one."
    if severity == "moderate":
        follow += " If the pain doesn't settle, end the session and let your trainer know."
    actions: list[dict[str, Any]] = [{"type": "pause"}]
    if alt_item is not None:
        actions.append({"type": "replace_exercise", "index": idx, "exercise": alt_item["exercise"]})
    return result(session, (sorry + follow).strip(), "empathetic", actions, source="rule",
                  intent=Intent("discomfort", "discomfort", body_part=body_part, severity=severity))


# ====================================================================================== commands
def _end(db: AsyncSession, session: WorkoutSession, reason: str) -> None:
    n = len(session.items or [])
    if int(session.current_index or 0) >= n:
        _complete(db, session, reason)
        return
    session.status = "stopped"
    session.ended_at = session.ended_at or utcnow()
    session.end_reason = reason[:255]
    log_event(db, session, "stopped", detail={"reason": reason[:120], "index": session.current_index})


async def _suspended_stop(db: AsyncSession, member: Member, session: WorkoutSession) -> dict[str, Any]:
    if session.status in OPEN:
        _end(db, session, "Gym activities suspended")
    emergency = member.emergency
    reply = ("Your gym activities are paused for your safety, so we can't train right now - a gym admin needs to "
             f"reactivate your account first. If you feel unwell, call {emergency['number']}.")
    return result(session, reply, "concerned", [{"type": "suspended"}], source="rule", emergency=emergency)


def _set_intensity(db: AsyncSession, member: Member, session: WorkoutSession, delta: int) -> tuple[int, int, bool]:
    old = int(session.intensity or 0)
    hi = member.max_intensity
    new = max(INTENSITY_MIN, min(hi, old + delta))
    capped = delta > 0 and new == old and old >= hi
    if new != old:
        session.intensity = new
        log_event(db, session, "intensity_change", detail={"from": old, "to": new})
    return old, new, capped


async def _change_exercise(db: AsyncSession, member: Member, session: WorkoutSession, intent: Intent,
                           suggestion: str | None = None) -> dict[str, Any] | None:
    """Swap the current exercise (or rebuild the rest of the workout) - safe alternatives only."""
    idx, current = _current(session)
    if current is None:
        return None
    library = await load_library(db)
    tags = set(member.tags) | await session_extra_tags(db, session)
    items = [dict(it) for it in (session.items or [])]
    used = {str(it.get("slug")) for it in items if it.get("slug")}
    used |= {str(it.get("slug")) for it in items[:idx] if it.get("slug")}
    if intent.scope == "workout" and intent.category:
        new_items: list[dict[str, Any]] = []
        for _ in range(max(2, min(5, len(items) - idx))):
            ex = pick_alternative(library, tags, category=intent.category, prefer_muscles=intent.muscles, exclude=used,
                                  level=member.level, strict_category=True)
            if ex is None:
                break
            used.add(ex.slug)
            new_items.append(item_from_lib(ex, member.level, member.capped, swapped_from=None,
                                           swap_reason=f"switched to {intent.category}"))
        if not new_items:
            return None
        session.items = items[:idx] + new_items
        log_event(db, session, "exercise_change", detail={"index": idx, "scope": "workout", "category": intent.category,
                                                         "to": [i["exercise"] for i in new_items]})
        return new_items[0]
    alt: LibItem | None = None
    if suggestion:
        cand = library.match(suggestion)
        if cand is not None and cand.slug not in used and is_safe(cand, tags) and \
                (intent.category is None or cand.category == intent.category):
            alt = cand
    if alt is None:
        alt = pick_alternative(library, tags, category=intent.category, current=current,
                               prefer_muscles=intent.muscles or tuple(current.get("muscles") or ()), exclude=used,
                               level=member.level, strict_category=bool(intent.category))
    if alt is None and intent.category:
        alt = pick_alternative(library, tags, current=current, exclude=used, level=member.level)
    if alt is None:
        return None
    new = item_from_lib(alt, member.level, member.capped, sets=int(current.get("sets") or 2),
                        swapped_from=current.get("exercise"), swap_reason="your request")
    items[idx] = new
    session.items = items
    log_event(db, session, "exercise_change", detail={"index": idx, "from": current.get("exercise"), "to": new["exercise"],
                                                     "category": intent.category})
    return new


async def apply_command(db: AsyncSession, member: Member, session: WorkoutSession, intent: Intent,
                        state: ClientState, *, suggestion: str | None = None) -> tuple[str, str, list[dict[str, Any]]]:
    """Execute a deterministic command. Returns (reply, emotion, actions)."""
    name = intent.name
    idx, item = _current(session)
    n = len(session.items or [])
    intensity = int(session.intensity or 0)
    ended = session.status in ENDED

    def logc(reply: str, **detail: Any) -> None:
        log_event(db, session, "command", message=reply, detail={"command": name, **detail})

    if name == "mic_off":
        reply = "Okay, I've stopped listening. Tap the microphone when you want to talk again."
        logc(reply)
        return reply, "calm", [{"type": "mic_off"}]
    if name == "voice_off":
        session.voice_enabled = False
        reply = "Okay, I'll stay quiet - my captions stay on screen."
        logc(reply)
        return reply, "calm", [{"type": "voice_off"}]
    if name == "voice_on":
        session.voice_enabled = True
        reply = "I'm back! I'll talk you through it."
        logc(reply)
        return reply, "happy", [{"type": "voice_on"}]
    if ended:
        reply = ("This session has finished. Start a new one from the Live page whenever you're ready."
                 if session.status != "stopped_for_safety" else safety_locked_reply(member.emergency))
        logc(reply, ignored=True)
        return reply, "calm", [{"type": "ended", "status": session.status}]
    if name == "quit":
        _end(db, session, "Ended by voice command")
        reply = f"Okay, ending the session here. Great effort today, {member.first_name} - well done for listening to your body."
        logc(reply)
        if session.status == "completed":
            await _notify_completed(db, member, session)
        return reply, "proud", [{"type": "quit"}]
    if name == "pause":
        if session.status == "active":
            session.status = "paused"
        reply = "Paused. Take your time - say resume when you're ready."
        logc(reply)
        return reply, "calm", [{"type": "pause"}]
    if name == "breather":
        if session.status == "active":
            session.status = "paused"
        _, new, _ = _set_intensity(db, member, session, -1)
        reply = ("Let's take a breather. Breathe in through your nose and out slowly through your mouth. I've made it a "
                 "little easier. If your breathing doesn't settle in a minute, or your chest feels tight, stop and get help.")
        logc(reply, intensity=new)
        return reply, "calm", [{"type": "pause"}, {"type": "intensity", "value": new}]
    if name == "resume":
        if session.status == "paused":
            session.status = "active"
            reply = f"Let's go! Back to {item.get('exercise')}." if item else "Let's go!"
        else:
            reply = "We're rolling - keep it up!"
        logc(reply)
        return reply, "encouraging", [{"type": "resume"}]
    if name == "set_done":
        logc("", index=idx, set=state.set)
        return "", "happy", [{"type": "set_done"}]
    if name == "next":
        if item is None:
            _complete(db, session)
            await _notify_completed(db, member, session)
            reply = completion_cue(session, member)
            logc(reply)
            return reply, "proud", [{"type": "completed"}]
        session.current_index = idx + 1
        log_event(db, session, "skipped", detail={"index": idx, "exercise": item.get("exercise"), "via": "command"})
        if session.current_index >= n:
            _complete(db, session)
            await _notify_completed(db, member, session)
            reply = completion_cue(session, member)
            logc(reply)
            return reply, "proud", [{"type": "completed"}]
        if session.status == "paused":
            session.status = "active"
        reply = f"Okay, skipping {item.get('exercise')}. Next up: {_intro(session.items[session.current_index], intensity)}"
        logc(reply)
        return reply, "encouraging", [{"type": "next", "index": session.current_index}]
    if name == "change":
        new = await _change_exercise(db, member, session, intent, suggestion)
        if new is None:
            reply = "I couldn't find a safe swap for that right now - let's keep going, or say next to skip."
            logc(reply)
            return reply, "calm", []
        if session.status == "paused":
            session.status = "active"
        cue = (new.get("cues") or [""])[0]
        old = item.get("exercise") if item else "this one"
        if intent.scope == "workout" and intent.category:
            reply = f"Switching the rest of the workout to {intent.category}. First: {_intro(new, intensity, cue=False)}"
        else:
            reply = f"Sure - let's swap {old} for {new['exercise']}." + (f" {cue.rstrip('.')}." if cue else "")
        logc(reply, to=new["exercise"])
        return reply, "encouraging", [{"type": "replace_exercise", "index": idx, "exercise": new["exercise"]}]
    if name == "intensity_down":
        old, new, _ = _set_intensity(db, member, session, -1)
        if new == old:
            reply = "We're already at the easiest setting. Take longer rests, and stop if anything hurts."
            logc(reply)
            return reply, "empathetic", []
        eff = effective(item, new) if item else None
        reply = "Got it, making it easier" + (f": {dose_text(item, eff, with_sets=False)} with {eff['rest_s']} seconds rest."
                                              if item else ".")
        if eff and eff.get("load_note"):
            reply += f" {eff['load_note'][0].upper()}{eff['load_note'][1:]}."
        logc(reply, intensity=new)
        return reply, "calm", [{"type": "intensity", "value": new}]
    if name == "intensity_up":
        old, new, capped = _set_intensity(db, member, session, +1)
        if new == old:
            reply = ("For your safety I'll keep the intensity where it is - let's make every rep count."
                     if member.capped and capped else "That's as hard as I'll go today - keep your form perfect.")
            logc(reply)
            return reply, "focused", []
        eff = effective(item, new) if item else None
        reply = "Let's push" + (f": {dose_text(item, eff, with_sets=False)} with {eff['rest_s']} seconds rest." if item else "!")
        logc(reply, intensity=new)
        return reply, "excited", [{"type": "intensity", "value": new}]
    if name == "info_next":
        nxt = (session.items or [])[idx + 1] if idx + 1 < n else None
        reply = f"After this: {_intro(nxt, intensity, cue=False)}" if nxt else "This is the last exercise - finish strong!"
        logc(reply)
        return reply, "encouraging", []
    if name == "info_reps":
        if item is None:
            reply = "We're all done!"
        else:
            eff = effective(item, intensity)
            sets = eff["sets"]
            reply = f"This is set {min(state.set, sets)} of {sets}: {dose_text(item, eff, with_sets=False)}."
        logc(reply)
        return reply, "focused", []
    if name == "info_form":
        if item is None:
            reply = "We're all done - nothing left to do but stretch!"
        else:
            cues = [str(c).rstrip(".") for c in (item.get("cues") or [])][:2]
            reply = (f"{item.get('exercise')}: " + ". ".join(cues) + ".") if cues else \
                (item.get("description") or f"Move slowly and with control on {item.get('exercise')}.")
        logc(reply)
        return reply[:REPLY_MAX + 80], "focused", []
    if name == "progress":
        reply = (f"You're doing great, {member.first_name}! Exercise {min(idx + 1, n)} of {n}"
                 + (f", {item.get('exercise')}." if item else "."))
        logc(reply)
        return reply, "proud", []
    if name == "help":
        reply = ("You can say pause, resume, next, done, easier, harder, switch exercise or end workout - and tell me "
                 "straight away if anything hurts or you feel unwell.")
        logc(reply)
        return reply, "happy", []
    if name == "repeat":
        last = (await db.execute(select(WorkoutSessionEvent.message).where(
            WorkoutSessionEvent.session_id == session.id, WorkoutSessionEvent.kind.in_(("ai_said", "command")),
            WorkoutSessionEvent.message.is_not(None)).order_by(WorkoutSessionEvent.id.desc()).limit(1))).scalar()
        reply = last or (_intro(item, intensity) if item else "We're all done!")
        return reply, "calm", []
    return "", "neutral", []


# ====================================================================================== conversation
_MD = re.compile(r"[*_`#>~|\[\]{}]|^\s*[-+]\s+", re.M)
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️]")


def clean_reply(text: str | None, limit: int = REPLY_MAX) -> str:
    """Speakable text: no markdown/emoji, collapsed whitespace, cut at a sentence/word boundary."""
    t = _EMOJI.sub("", _MD.sub("", text or ""))
    t = re.sub(r"\s+", " ", t).strip().strip('"').strip()
    if len(t) <= limit:
        return t
    cut = t[:limit]
    stop = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if stop >= limit * 0.5:
        return cut[: stop + 1]
    return cut[: cut.rfind(" ")].rstrip(",;:-") + "..." if " " in cut else cut


SMALLTALK = [
    (re.compile(r"\b(?:thanks|thank you|thx|cheers|appreciate it)\b"), "You're welcome! Keep it up - you're doing great.", "happy"),
    (re.compile(r"^(?:hi|hello|hey|hiya|good (?:morning|afternoon|evening))\b"), "Hi {name}! I'm right here with you.", "happy"),
    (re.compile(r"\b(?:motivat\w*|encourage me|i cant do (?:this|it)|i need help to keep going)\b"),
     "You've got this, {name}! One rep at a time - breathe out on the effort.", "encouraging"),
    (re.compile(r"\b(?:water|drink|hydrat\w*)\b"), "Good idea - take a few sips of water between sets.", "calm"),
]
OFFLINE_REPLY = "I'm offline right now, but I can still count your reps - say next, pause or easier."
UPSELL_REPLY = ("Free conversation with your AI coach comes with an AI plan. I can still guide you - say pause, next, "
                "easier, harder or end.")


def _smalltalk(text: str, member: Member) -> tuple[str, str] | None:
    norm = normalize(text)
    for rx, reply, emotion in SMALLTALK:
        if rx.search(norm):
            return reply.format(name=member.first_name), emotion
    return None


async def _history(db: AsyncSession, session: WorkoutSession, limit: int = 6,
                   member: Member | None = None) -> list[tuple[str, str]]:
    rows = (await db.execute(
        select(WorkoutSessionEvent.kind, WorkoutSessionEvent.message).where(
            WorkoutSessionEvent.session_id == session.id,
            WorkoutSessionEvent.kind.in_(("user_said", "ai_said", "command")),
            WorkoutSessionEvent.message.is_not(None),
        ).order_by(WorkoutSessionEvent.id.desc()).limit(limit)
    )).all()
    hide = member.ctx.hide_from_history() if member is not None and member.ctx is not None else None
    return [("member" if r.kind == "user_said" else "coach", r.message) for r in reversed(rows)
            if r.message and (hide is None or not hide(r.message))]  # what the member asked to forget stays out


def ai_context(member: Member, session: WorkoutSession, state: ClientState) -> dict[str, Any]:
    idx, item = _current(session)
    n = len(session.items or [])
    intensity = int(session.intensity or 0)
    eff = effective(item, intensity) if item else None
    nxt = (session.items or [])[idx + 1] if item and idx + 1 < n else None
    tag_labels = ", ".join(sorted(TAG_LABELS.get(t, t.replace("_", " ")) for t in member.tags if t != "minor"))
    return {
        "title": session.title, "position": f"{min(idx + 1, n)}/{n}", "exercise": item.get("exercise") if item else "",
        "set": state.set, "sets": eff["sets"] if eff else 0, "dose": dose_text(item, eff, with_sets=False) if item else "",
        "load": eff.get("load_note") if eff else "", "phase": "paused" if session.status == "paused" else state.phase,
        "intensity": intensity, "next": nxt.get("exercise") if nxt else "", "first_name": member.first_name,
        "goal": member.goal.replace("_", " "), "level": member.level, "age_group": member.age_group, "tags": tag_labels,
        "cues": "; ".join(str(c) for c in (item.get("cues") or [])[:3]) if item else "",
        # what the member told the app (conditions, injuries, medications, allergies, preferences): quoted data
        "profile": member.ctx.block(max_chars=PROFILE_PROMPT_CHARS, include_basics=False, heading="SAVED PROFILE")
        if member.ctx is not None and not member.ctx.empty else "",
    }


async def handle_message(db: AsyncSession, member: Member, session: WorkoutSession, text: str, state: ClientState, *,
                         via: str = "text", request: Any = None, ai: bool = True) -> dict[str, Any]:
    """``_handle_message`` plus memory (app/ai/memory.py): "forget ..." is handled at once (never when the words are a
    red flag or a pain report - safety first), and a durable fact the member states ("my left knee hurts on squats",
    "I have asthma") comes back as a "Remember this?" offer (``memory``) - nothing is saved until they confirm."""
    from app.ai import memory

    await note_event_budget(db, session)
    said = re.sub(r"\s+", " ", (text or "")).strip()[:TEXT_MAX]
    member_ok = member.ctx is not None and member.ctx.is_customer
    if member_ok and session.status != "stopped_for_safety":
        target = memory.forget_target(said)
        if target is not None:
            intent = detect_intent(said)
            if intent.kind not in ("red_flag", "discomfort"):
                return await _forget_turn(db, member, session, said, intent, target, via)
    out = await _handle_message(db, member, session, text, state, via=via, request=request, ai=ai)
    refused = out.get("refused") or {}
    if (member_ok and not out.get("cancelled") and session.status != "stopped_for_safety"
            and refused.get("code") != "duplicate"):
        facts = memory.extract_facts(said)
        if facts:
            offers = memory.proposals(member.ctx, facts, source="live_coach")
            if offers:
                out["memory"] = offers
    return out


async def _forget_turn(db: AsyncSession, member: Member, session: WorkoutSession, text: str, intent: Intent,
                       target: str, via: str) -> dict[str, Any]:
    from app.ai import memory

    log_event(db, session, "user_said", message=text, detail={"via": via, "intent": "forget", "name": "forget"})
    res = await memory.forget(db, member.user, target)
    reply = ("Done - I've forgotten that. Your plan and safety checks no longer use it." if res["ok"]
             else "I couldn't find that in what I know about you.")
    log_event(db, session, "ai_said", message=reply, detail={"source": "rule", "memory": "forget"})
    entry = {"op": "forgot", "status": "done" if res["ok"] else "nothing", "label": ", ".join(res["removed"][:5]),
             "detail": res["detail"], "undo": res.get("undo")}
    out = result(session, reply, "calm", [], source="rule", intent=intent, memory=[entry])
    out["_queue_ac"] = bool(res.get("queue_ac"))
    return out


async def _handle_message(db: AsyncSession, member: Member, session: WorkoutSession, text: str, state: ClientState, *,
                          via: str = "text", request: Any = None, ai: bool = True) -> dict[str, Any]:
    """The live conversation: safety layer -> commands -> AI (validated) -> deterministic fallbacks.

    ``ai=False``: the browser is waiting out an AI refusal (quota / AI pause) - everything deterministic runs exactly
    as usual, but free conversation gets no AI call (see ``_chat_paused``)."""
    text = re.sub(r"\s+", " ", (text or "")).strip()[:TEXT_MAX]
    if state.voice is not None:
        session.voice_enabled = state.voice
    if via == "voice":
        session.mic_used = True
    intent = detect_intent(text)
    log_event(db, session, "user_said", message=text, detail={"via": via, "intent": intent.kind, "name": intent.name})

    # 1. red flags - always, whatever the session status
    if intent.kind == "red_flag":
        return await trigger_red_flag(db, member, session, flag=intent.name, level=intent.level or CRIT, text=text,
                                      source=via, request=request)
    if session.status == "stopped_for_safety":
        if intent.kind == "discomfort" and (intent.severity == "severe" or intent.body_part in ("chest", "head")):
            return await report_discomfort(db, member, session, body_part=intent.body_part or "other",
                                           severity=intent.severity or "moderate", text=text, area=intent.area,
                                           source=via, request=request)
        reply = safety_locked_reply(member.emergency)
        log_event(db, session, "ai_said", message=reply, detail={"source": "safety"})
        return result(session, reply, "concerned", [{"type": "safety_stop", "emergency": member.emergency,
                                                     "label": "earlier safety stop"}],
                      source="safety", intent=intent, emergency=member.emergency)
    # 2. discomfort
    if intent.kind == "discomfort":
        return await report_discomfort(db, member, session, body_part=intent.body_part or "other",
                                       severity=intent.severity or "moderate", text=text, area=intent.area,
                                       source=via, request=request)
    if member.suspended and intent.name not in ("mic_off", "voice_off", "voice_on"):
        return await _suspended_stop(db, member, session)
    if not member.access.has_any and intent.name not in ("mic_off", "voice_off", "voice_on"):
        if session.status in OPEN:
            _end(db, session, "Membership not active")
        reply = "Your membership isn't active any more, so live coaching is paused. Renew it to keep training with me."
        log_event(db, session, "ai_said", message=reply, detail={"source": "rule"})
        return result(session, reply, "calm", [{"type": "ended", "status": session.status}], source="rule", intent=intent)
    # 3. commands
    if intent.kind == "command":
        reply, emotion, actions = await apply_command(db, member, session, intent, state)
        return result(session, reply, emotion, actions, source="rule", intent=intent)
    # 4. conversation
    if session.status in ENDED:
        reply = "This session has finished. Start a new one from the Live page whenever you're ready."
        log_event(db, session, "ai_said", message=reply, detail={"source": "rule"})
        return result(session, reply, "calm", [{"type": "ended", "status": session.status}], source="rule", intent=intent)
    if not member.access.ai:
        small = _smalltalk(text, member)
        reply, emotion = small if small else (UPSELL_REPLY, "calm")
        log_event(db, session, "ai_said", message=reply, detail={"source": "upsell"})
        return result(session, reply, emotion, [] if small else [{"type": "upsell"}], source="upsell", intent=intent,
                      ai_available=False)
    if not ai:
        return _chat_paused(db, member, session, text, intent)
    return await _ai_turn(db, member, session, text, state, intent)


def _chat_paused(db: AsyncSession, member: Member, session: WorkoutSession, text: str, intent: Intent) -> dict[str, Any]:
    """Free conversation while the browser waits out an AI refusal (``"ai": false``): no AI call and no AI guard, so
    the wait never grows. Smalltalk keeps its deterministic answer; anything else is acknowledged without a word (the
    browser already said when chat is back)."""
    small = _smalltalk(text, member)
    if small:
        reply, emotion = small
        log_event(db, session, "ai_said", message=reply, detail={"source": "rule", "chat_paused": True})
        return result(session, reply, emotion, [], source="rule", intent=intent, chat_paused=True)
    return result(session, "", "neutral", [], source="chat_paused", intent=intent, chat_paused=True)


def _refused(db: AsyncSession, member: Member, session: WorkoutSession, text: str, intent: Intent,
             exc: Any) -> dict[str, Any]:
    """The AI guard or queue refused the reply (``AIRejected``) before any compute. HTTP 200: the workout goes on.
    The member hears one short line (nothing for a repeated phrase) and ``refused`` tells the browser why and how long
    to keep AI chat paused. Nothing the AI could have done happens: no actions, no counters."""
    from app.ai.live_coach import refusal_reply

    refused = {"code": exc.code, "detail": exc.message, "retry_after": exc.retry_after}
    if exc.code == "busy":  # the queue is full or the wait was too long: the deterministic answer, as when AI is down
        small = _smalltalk(text, member)
        reply, emotion = small if small else (refusal_reply(exc), "calm")
        log_event(db, session, "ai_said", message=reply, detail={"source": "fallback", "refused": "busy"})
        return result(session, reply, emotion, [], source="fallback", intent=intent, ai_available=True, refused=refused)
    reply = refusal_reply(exc)
    log_event(db, session, "ai_refused", message=reply or None, detail=refused)
    return result(session, reply, "calm" if reply else "neutral", [], source="refused", intent=intent,
                  ai_available=True, refused=refused)


async def _ai_turn(db: AsyncSession, member: Member, session: WorkoutSession, text: str, state: ClientState,
                   intent: Intent) -> dict[str, Any]:
    """Free conversation with the AI. Red flags, discomfort and commands were handled before this point, so nothing
    here can block or delay a safety flow."""
    from app.ai.client import AIUnavailableError
    from app.ai.errors import AICancelled, AIRejected
    from app.ai.live_coach import converse

    history = await _history(db, session, member=member)
    ctx = ai_context(member, session, state)
    await db.commit()  # don't hold a transaction (or a pooled connection) open while the AI thinks
    try:
        out, provider = await converse(ctx, history, text)
    except AICancelled as exc:  # the page closed / the connection dropped mid-reply: nobody would hear it
        log.info("live coach reply for session %s stopped (%s)", session.id, exc.reason)
        return result(session, "", "neutral", [], source="cancelled", intent=intent, cancelled=True)
    except AIRejected as exc:  # before AIUnavailableError: it is a subclass, and the member must hear why
        await db.refresh(session)  # a queue wait may have taken seconds - report the workout as it is now
        return _refused(db, member, session, text, intent, exc)
    except AIUnavailableError:
        await db.refresh(session)  # the workout kept running while the AI was (not) answering
        small = _smalltalk(text, member)
        reply, emotion = small if small else (OFFLINE_REPLY, "calm")
        log_event(db, session, "ai_said", message=reply, detail={"source": "fallback"})
        return result(session, reply, emotion, [], source="fallback", intent=intent, ai_available=False)
    await db.refresh(session)  # the workout kept running while the AI was thinking
    reply = clean_reply(out.reply) or "Keep going - you're doing great!"
    emotion = (out.emotion or "").strip().lower()
    emotion = emotion if emotion in EMOTIONS else "encouraging"
    action = (out.action or "none").strip().lower()
    action = action if action in AI_ACTIONS else "none"
    actions: list[dict[str, Any]] = []
    session.ai_messages = int(session.ai_messages or 0) + 1
    if session.status == "stopped_for_safety":  # a safety stop happened meanwhile - the AI can't continue anything
        reply, emotion, action = safety_locked_reply(member.emergency), "concerned", "none"
    elif session.status in ENDED:
        action = "none"
    if action == "log_discomfort":
        log_event(db, session, "ai_said", message=reply, detail={"source": "ai", "provider": provider, "action": action})
        disc = _find_discomfort(correct_typos(normalize(text)))
        res = await report_discomfort(db, member, session, body_part=(disc.body_part if disc else "other") or "other",
                                      severity="mild" if not disc else (disc.severity or "mild"), text=text,
                                      area=disc.area if disc else None, source="ai")
        res["reply"] = reply if len(reply) > 20 else res["reply"]
        res["source"] = "ai"
        res["provider"] = provider
        return res
    if action == "quit" and not re.search(r"\b(?:stop|end|quit|done|finish|enough|call it|leave|go home|bye|wrap)\b",
                                          normalize(text)):
        action = "pause"  # never end a session the member didn't clearly ask to end
        reply = "Want to stop here? Say end workout to finish, or resume to keep going."
        emotion = "calm"
    if action != "none":
        cmd = {"pause": "pause", "resume": "resume", "next": "next", "quit": "quit", "change_exercise": "change",
               "intensity_down": "intensity_down", "intensity_up": "intensity_up"}[action]
        cmd_intent = Intent("command", cmd, category=None, scope="exercise")
        cmd_reply, cmd_emotion, actions = await apply_command(db, member, session, cmd_intent, state,
                                                              suggestion=out.exercise_suggestion)
        if action in ("change_exercise", "next") and cmd_reply:
            reply = clean_reply(f"{reply} {cmd_reply}", REPLY_MAX + 120) if len(reply) < 90 else reply
        if not actions and cmd_reply and action in ("intensity_up", "intensity_down", "change_exercise"):
            reply = cmd_reply  # the request was refused by a safety rule - say so honestly
            emotion = cmd_emotion
    log_event(db, session, "ai_said", message=reply, detail={"source": "ai", "provider": provider, "action": action})
    return result(session, reply, emotion, actions, source="ai", intent=intent, provider=provider, ai_available=True)


async def end_session(db: AsyncSession, member: Member, session: WorkoutSession, reason: str) -> dict[str, Any]:
    reason = re.sub(r"\s+", " ", reason or "").strip()[:120] or "user_ended"
    if session.status in ENDED:
        return result(session, "", "calm", [{"type": "ended", "status": session.status}])
    if reason == "completed":
        _complete(db, session)
        await _notify_completed(db, member, session)
        return result(session, completion_cue(session, member), "proud", [{"type": "completed"}])
    _end(db, session, {"user_ended": "Ended by the member"}.get(reason, reason))
    if session.status == "completed":
        await _notify_completed(db, member, session)
        return result(session, completion_cue(session, member), "proud", [{"type": "completed"}])
    return result(session, f"Session ended. Nice work today, {member.first_name} - see you next time!", "proud",
                  [{"type": "quit"}])


def recent_view(sessions: list[WorkoutSession]) -> list[dict[str, Any]]:
    out = []
    for s in sessions:
        n = len(s.items or [])
        done = min(int(s.current_index or 0), n)
        out.append({"id": s.id, "title": s.title, "status": s.status, "started_at": s.started_at,
                    "done": done, "total": n, "open": s.status in OPEN,
                    "fresh": s.status in OPEN and s.started_at is not None and utcnow() - s.started_at < timedelta(hours=12)})
    return out
