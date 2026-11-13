"""Trainer-facing safety: who may see a member's health & safety details, the safety panel trainers and owners see
(chat, staff portal client page, owner member page), the send-time check of staff / owner chat messages and red-flag
symptoms in members' chat messages.

WHO MAY SEE (``health_access``) - exactly the people the app already lets see the member's health data:

* ``full``      the owner / admin team of a gym the member belongs to (a current or member-consented membership there,
                or registered by them - ``intake.is_related``, the rule of /owner/customers/{id}), and the member's
                assigned trainer(s): an active
                TrainerClient link of the viewer's ACTIVE, not deleted employment with a live subscription (the rule of
                /staff/clients/{id});
* ``withheld``  staff while the member has switched off "Share my health and safety information with my gym's
                trainers" (account settings, default ON): they only read ``WITHHELD_TEXT``;
* ``summary``   other staff in a conversation of their ACTIVE employment with the member: ``SUMMARY_TEXT``, never a
                detail (suspended, former and unverified staff: ``none`` - their conversations are closed to them);
* ``none``      the member themself (the trainer panel is not theirs), other members, other gyms' owners and staff,
                anonymous - nothing at all, not even whether there is something to see.

THE PANEL (``load_brief``): safety tags in plain words, what to avoid (the guardrail rules of app/ai/safety.py and the
exercise library's contraindications), allergies, medications, conditions and injuries, the notes for trainers, a
safety pause and when the list last changed - one bounded read (``PANEL_READ_MAX`` entries, safety-relevant first)
plus one profile row, whatever the member recorded.

SEND-TIME CHECK (``check_staff_message`` / ``check_text``): before a staff / owner message is stored it is checked
against the member's safety tags with the deterministic engine - exercise names and aliases of the library, common
phrasings and sanitize_load-style load rules (1RM, max effort, to failure, heavy, RPE 8+, a load of 80 % of the
member's body weight) -> ``app.ai.safety.check_exercise``; negations must govern the mention ("no box jumps", but not
"don't forget your box jumps" or "swap the bike for deadlifts"); exercise typos and glued words are read as meant. A
hit is a warning naming the condition - for ``full`` viewers only. ``summary`` staff get the same "check with their
trainer" reminder for ANY exercise or load they mention, whatever the conditions (nothing to probe). The sender edits
or sends anyway (audited with the tag only). No AI, no network: microseconds per message.

RED FLAGS (``detect_red_flag`` / ``raise_red_flag``): a member's message mentioning red-flag symptoms (the live coach's
rules: chest pain, fainting, severe breathlessness, dizziness, severe or sudden pain, heart attack / stroke signs ...)
gets immediate emergency guidance (local number - India 112); the gym admin, the staff member in the conversation and
the member's trainer(s) get an urgent notification - once per conversation per hour, never refused by a chat cap.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Any

from sqlalchemy import and_, case, exists, false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.safety import RULES, LibExercise, LibraryIndex, check_exercise, normalize_tags, safety_notes, tag_label
from app.core.utils import clamp, local_now, utcnow
from app.models import (
    ChatMessage,
    ChatThread,
    CustomerProfile,
    Employee,
    Gym,
    HealthCondition,
    Subscription,
    TrainerClient,
    User,
)
from app.models.enums import (
    ConditionKind,
    EmploymentStatus,
    NotificationCategory,
    Role,
    Severity,
    SubscriptionStatus,
)

log = logging.getLogger("ironpulse.trainer_safety")

FULL, WITHHELD, SUMMARY, NONE = "full", "withheld", "summary", "none"
WITHHELD_TEXT = "The member chose not to share health details - ask before training."
SUMMARY_TEXT = "This member has safety notes - check with their trainer."

# load safety (approved limits): what one panel / check / alert may read or say
PANEL_READ_MAX = 50  # health entries read for one panel, safety-relevant first (members hold at most 50)
BUILD_ROWS_MAX = 100  # entries a page already read (staff / owner pages: at most 100) that one panel looks at
PANEL_LIST_MAX = 12  # entries shown per list (conditions & injuries / allergies / medications) - then "+N more"
PANEL_EXERCISES_MAX = 12  # contraindicated library exercises named - then "+N more"
PANEL_TIPS_MAX = 6
NOTES_MAX = 300  # characters of the notes for trainers shown in the panel
BADGE_IDS_MAX = 100  # members checked for a chat-list safety badge in one window (a window holds 50)
WARNING_FLAGS_MAX = 5  # flagged exercises named in one send-time warning
CHECK_TEXT_MAX = 4000  # = chat MAX_BODY: characters of one message checked
RED_FLAG_WINDOW = timedelta(hours=1)  # one urgent alert (and one safety message) per conversation per hour
RED_FLAG_SCAN = 200  # latest messages of a conversation scanned for the hour's safety message (bounded)
SAFETY_PREFIX = "Safety first:"  # the system message that marks a red-flag alert in a conversation
HEAVY_SHARE = 0.8  # a stated load of >= 80 % of the member's body weight counts as heavy for load-capped conditions
HEAVY_PERCENT = 75  # "% of your max" at or above this counts as heavy for load-capped conditions


# ============================================================================ who may see
@dataclass(frozen=True, slots=True)
class Access:
    level: str  # full | withheld | summary | none
    sharing: bool = True  # the member shares health details with trainers

    @property
    def full(self) -> bool:
        return self.level == FULL

    @property
    def visible(self) -> bool:
        return self.level != NONE


def trainer_link_clauses(viewer_id: int, customer_id: Any, today: date) -> list[Any]:
    """The staff portal's client rule (the query must JOIN Employee and OUTER JOIN Subscription on the link): an
    active assignment of the viewer's ACTIVE, not deleted employment whose subscription (if any) is live."""
    return [
        TrainerClient.customer_id == customer_id,
        TrainerClient.active.is_(True),
        Employee.user_id == viewer_id,
        Employee.status == EmploymentStatus.ACTIVE,
        Employee.deleted_at.is_(None),
        or_(TrainerClient.subscription_id.is_(None),
            and_(Subscription.status == SubscriptionStatus.ACTIVE,
                 or_(Subscription.ends_on.is_(None), Subscription.ends_on >= today))),
    ]


async def is_assigned_trainer(db: AsyncSession, viewer: User, customer_id: int) -> bool:
    today = local_now(getattr(viewer, "timezone", None)).date()
    found = await db.scalar(
        select(TrainerClient.id).join(Employee, Employee.id == TrainerClient.employee_id)
        .outerjoin(Subscription, Subscription.id == TrainerClient.subscription_id)
        .where(*trainer_link_clauses(viewer.id, customer_id, today)).limit(1)
    )
    return found is not None


async def sharing_enabled(db: AsyncSession, customer_id: int) -> bool:
    """The member's choice (no profile row yet = the default, ON)."""
    value = await db.scalar(select(CustomerProfile.share_health_with_trainers)
                            .where(CustomerProfile.user_id == customer_id))
    return True if value is None else bool(value)


def active_conversation_clauses(viewer_id: int, customer_id: Any) -> list[Any]:
    """A conversation between the member and one of the viewer's ACTIVE, not deleted employments (the query must JOIN
    Employee on ChatThread.employee_id) - suspended, former and not-yet-verified staff have none."""
    return [ChatThread.customer_id == customer_id, Employee.user_id == viewer_id,
            Employee.status == EmploymentStatus.ACTIVE, Employee.deleted_at.is_(None)]


async def in_active_conversation(db: AsyncSession, viewer: User, customer_id: int) -> bool:
    found = await db.scalar(select(ChatThread.id).join(Employee, Employee.id == ChatThread.employee_id)
                            .where(*active_conversation_clauses(viewer.id, customer_id)).limit(1))
    return found is not None


async def health_access(db: AsyncSession, viewer: User | None, customer_id: int, *,
                        in_conversation: bool = False) -> Access:
    """What ``viewer`` may see of member ``customer_id``'s health & safety information. ``in_conversation``: the viewer
    is a staff participant of a chat with the member (the only place a non-trainer gets the summary line) - checked
    here too: the conversation must belong to an ACTIVE, not deleted employment of the viewer."""
    if viewer is None or getattr(viewer, "deleted_at", None) is not None or viewer.id == customer_id:
        return Access(NONE)
    if viewer.role == Role.OWNER:
        from app.services.intake import is_related

        return Access(FULL) if await is_related(db, viewer.id, customer_id) else Access(NONE)
    if viewer.role == Role.STAFF:
        sharing = await sharing_enabled(db, customer_id)
        if await is_assigned_trainer(db, viewer, customer_id):
            return Access(FULL if sharing else WITHHELD, sharing)
        if in_conversation and await in_active_conversation(db, viewer, customer_id):
            return Access(SUMMARY if sharing else WITHHELD, sharing)
    return Access(NONE)


# ============================================================================ the panel
_SAFETY_KINDS = (ConditionKind.INJURY, ConditionKind.CONDITION, ConditionKind.ALLERGY, ConditionKind.MEDICATION)


def safety_order() -> tuple[Any, ...]:
    """ORDER BY of a capped read of one member's entries - the order of ``app.services.health.safety_first`` spelled
    out from plain columns (no import of the health service): the oldest entry of every distinct safety tag, then the
    other tagged entries, then injuries, conditions, allergies and medications - oldest first. A padded list (untagged
    entries, or one tag over and over) can never push a safety tag out of the rows read."""
    repeat = case((HealthCondition.tag.is_(None), false()),
                  else_=func.row_number().over(partition_by=HealthCondition.tag, order_by=HealthCondition.id) > 1)
    kind = case(*((HealthCondition.kind == k, i) for i, k in enumerate(_SAFETY_KINDS)), else_=len(_SAFETY_KINDS))
    return HealthCondition.tag.is_(None), repeat, kind, HealthCondition.id


async def member_tags(db: AsyncSession, customer: User) -> list[str]:
    """The member's safety tags (normalised; "minor" for under-18s): DISTINCT tags in SQL, at most one row per tag."""
    rows = await db.execute(
        select(HealthCondition.tag).distinct()
        .where(HealthCondition.customer_id == customer.id, HealthCondition.tag.in_(sorted(RULES)))
        .order_by(HealthCondition.tag).limit(len(RULES))
    )
    return normalize_tags([t for (t,) in rows.all() if t], customer.age())


@dataclass(slots=True)
class SafetyBrief:
    """What the safety panel shows (``level`` decides how much: details only for ``full``)."""

    level: str
    name: str = "This member"
    tags: list[tuple[str, str]] = field(default_factory=list)  # (tag, plain-words label)
    avoid: list[str] = field(default_factory=list)  # "Knee injury: jumping / plyometrics, deep loaded squats ..."
    tips: list[str] = field(default_factory=list)
    exercises: list[str] = field(default_factory=list)  # contraindicated library exercises
    exercises_more: int = 0
    conditions: list[dict[str, str]] = field(default_factory=list)  # conditions and injuries
    allergies: list[dict[str, str]] = field(default_factory=list)
    medications: list[dict[str, str]] = field(default_factory=list)
    lists_more: int = 0  # entries read but not listed (each list shows PANEL_LIST_MAX)
    more: int = 0  # entries beyond the PANEL_READ_MAX read
    notes: str | None = None
    minor: bool = False
    paused: bool = False
    paused_since: datetime | None = None
    last_updated: datetime | None = None
    has_notes: bool = False  # anything on file (summary level: only this)

    @property
    def visible(self) -> bool:
        return self.level in (FULL, WITHHELD, SUMMARY) and (self.level != SUMMARY or self.has_notes)

    @property
    def labels(self) -> list[str]:
        return [label for _, label in self.tags]

    @property
    def headline(self) -> str:
        if self.level == WITHHELD:
            return "Health details not shared"
        if self.level == SUMMARY:
            return "Safety notes on file"
        if self.tags:
            return "Safety: " + " · ".join(self.labels[:4]) + (f" +{len(self.tags) - 4}" if len(self.tags) > 4 else "")
        if self.has_notes:
            return "Health notes on file"
        return "No safety notes on file"

    @property
    def tone(self) -> str:
        if self.level == FULL and (self.tags or self.paused):
            return "warning"
        return "info"


def _first_name(user: User | None) -> str:
    name = (getattr(user, "display_name", None) or "").strip()
    return name.split(" ")[0] if name else "This member"


def _is_minor(user: User | None, today: date | None = None) -> bool:
    age = user.age(today) if user is not None else None
    return age is not None and age < 18


def avoid_text(tags: Iterable[str]) -> list[str]:
    """"Label: what to avoid" for every tag whose rule restricts something (plain words, not tag keys)."""
    out = []
    for t in tags:
        rule = RULES.get(t)
        if rule is not None and rule.avoid and not rule.avoid.startswith("nothing"):
            out.append(f"{rule.label}: {rule.avoid}")
    return out


def contraindicated(tags: Iterable[str]) -> list[str]:
    """Library exercises whose contraindications name one of ``tags`` (sorted by name)."""
    wanted = set(tags)
    if not wanted:
        return []
    return sorted({e.name for e in library() if wanted & set(e.contraindications)}, key=str.casefold)


async def load_brief(db: AsyncSession, customer: User, access: Access) -> SafetyBrief:
    """The panel for ``customer`` at ``access`` level: summary / withheld read at most one row, full reads one bounded,
    safety-first page of the member's entries and one profile row - a constant number of queries."""
    if access.level in (NONE, WITHHELD):
        return SafetyBrief(level=access.level, name=_first_name(customer))
    if access.level == SUMMARY:
        brief = SafetyBrief(level=SUMMARY, name=_first_name(customer), minor=_is_minor(customer))
        brief.has_notes = brief.minor or bool(await db.scalar(
            select(exists().where(HealthCondition.customer_id == customer.id))))
        return brief
    rows = list((await db.execute(
        select(HealthCondition).where(HealthCondition.customer_id == customer.id)
        .order_by(*safety_order()).limit(PANEL_READ_MAX + 1)
    )).scalars().all())
    profile = (await db.execute(
        select(CustomerProfile.health_notes, CustomerProfile.activity_suspended, CustomerProfile.suspended_at,
               CustomerProfile.updated_at).where(CustomerProfile.user_id == customer.id)
    )).first()
    return build_brief(customer, rows[:PANEL_READ_MAX], profile, more=len(rows) > PANEL_READ_MAX)


def build_brief(customer: User, rows: Iterable[HealthCondition], profile: Any, *, more: bool = False) -> SafetyBrief:
    """The full panel from entries a page has ALREADY read safety-first (the staff client page, the owner's member
    page: no second read of the member's health) and the profile (a ``CustomerProfile`` or a row of its health_notes,
    activity_suspended, suspended_at, updated_at - None when there is none)."""
    rows = list(rows)[:BUILD_ROWS_MAX]
    minor = _is_minor(customer)
    brief = SafetyBrief(level=FULL, name=_first_name(customer), minor=minor, more=1 if more else 0)
    tags = normalize_tags([r.tag for r in rows if r.tag], customer.age())
    brief.tags = [(t, tag_label(t)) for t in tags]
    brief.avoid = avoid_text(tags)
    brief.tips = safety_notes(tags)[:PANEL_TIPS_MAX]
    names = contraindicated(tags)
    brief.exercises, brief.exercises_more = names[:PANEL_EXERCISES_MAX], max(0, len(names) - PANEL_EXERCISES_MAX)
    lists: dict[str, list[dict[str, str]]] = {"conditions": [], "allergies": [], "medications": []}
    for r in sorted(rows, key=lambda c: (str(getattr(c.kind, "value", c.kind)), c.name.casefold(), c.id)):
        kind = getattr(r.kind, "value", r.kind)
        key = {"allergy": "allergies", "medication": "medications"}.get(kind, "conditions")
        label = tag_label(r.tag) if r.tag and r.tag in RULES else ""
        lists[key].append({"name": r.name, "kind": str(kind), "notes": (r.notes or "")[:120],
                           "label": label if label and label.casefold() != r.name.casefold() else ""})
    for key, items in lists.items():
        brief.lists_more += max(0, len(items) - PANEL_LIST_MAX)
        setattr(brief, key, items[:PANEL_LIST_MAX])
    stamps = [r.created_at for r in rows if r.created_at is not None]
    if profile is not None:
        if isinstance(profile, CustomerProfile):
            notes, paused, paused_at, updated = (profile.health_notes, profile.activity_suspended, profile.suspended_at,
                                                 profile.updated_at)
        else:
            notes, paused, paused_at, updated = profile
        brief.notes = clamp(notes, NOTES_MAX) if notes else None
        brief.paused, brief.paused_since = bool(paused), paused_at
        if updated is not None:
            stamps.append(updated)
    brief.last_updated = max(stamps) if stamps else None
    brief.has_notes = bool(rows) or minor or bool(brief.notes)
    return brief


async def panel_for(db: AsyncSession, viewer: User, customer: User | None, *, in_conversation: bool = False
                    ) -> SafetyBrief | None:
    """The panel ``viewer`` may see for ``customer`` (None = nothing to show: members, other gyms, deleted members)."""
    if customer is None or customer.deleted_at is not None or customer.role != Role.CUSTOMER:
        return None
    access = await health_access(db, viewer, customer.id, in_conversation=in_conversation)
    if not access.visible:
        return None
    brief = await load_brief(db, customer, access)
    return brief if brief.visible else None


async def badge_ids(db: AsyncSession, viewer: User, customers: Iterable[User | None]) -> set[int]:
    """Members of one conversation-list window that get the small safety badge: an active safety tag (or under 18),
    shown to the gym's owner, and to staff only while the member shares health details and only in a conversation of
    one of their ACTIVE, not deleted employments (never to suspended or former staff). At most two queries."""
    if viewer is None or viewer.role not in (Role.OWNER, Role.STAFF):
        return set()
    today = date.today()
    ids: set[int] = set()
    minors: set[int] = set()
    for c in customers:
        if c is None or c.deleted_at is not None or c.role != Role.CUSTOMER:
            continue
        ids.add(c.id)
        if _is_minor(c, today):
            minors.add(c.id)
    if not ids:
        return set()
    ids = set(sorted(ids)[:BADGE_IDS_MAX])
    tagged_expr = exists().where(HealthCondition.customer_id == User.id, HealthCondition.tag.in_(sorted(RULES)))
    stmt = select(User.id, tagged_expr).where(User.id.in_(ids))
    if viewer.role == Role.STAFF:
        stmt = stmt.where(exists(select(ChatThread.id).join(Employee, Employee.id == ChatThread.employee_id)
                                 .where(*active_conversation_clauses(viewer.id, User.id))))
    rows = (await db.execute(stmt)).all()
    flagged = {cid for cid, tagged in rows if tagged or cid in minors} & ids
    if viewer.role == Role.STAFF and flagged:
        hidden = {uid for (uid,) in (await db.execute(
            select(CustomerProfile.user_id).where(CustomerProfile.user_id.in_(flagged),
                                                  CustomerProfile.share_health_with_trainers.is_(False))
        )).all()}
        flagged -= hidden
    return flagged


# ============================================================================ the exercise check
@lru_cache(maxsize=1)
def library() -> tuple[LibExercise, ...]:
    """The canonical exercise catalogue (app/data/exercises.py - what the exercises table is synced from) plus the
    coach's built-in entries it lacks: in memory, no query per message."""
    from app.data.exercises import EXERCISES
    from app.services.coach import BUILTIN_LIBRARY

    items = [LibExercise(slug=e.slug, name=e.name, category=e.category, goals=tuple(e.goals), muscles=tuple(e.muscles),
                         equipment=e.equipment, difficulty=e.difficulty, intensity=e.intensity, impact=e.impact,
                         contraindications=tuple(e.contraindications), video_slug=e.video_slug)
             for e in EXERCISES]
    seen = {e.slug for e in items}
    items += [e for e in BUILTIN_LIBRARY if e.slug not in seen]
    return tuple(items)


_MODIFIERS = frozenset({"barbell", "dumbbell", "kettlebell", "cable", "machine", "seated", "standing", "lying",
                        "supported", "bodyweight", "half", "kneeling", "one", "arm", "single", "low", "impact", "easy",
                        "brisk", "stationary", "wall", "incline"})
# common words for library exercises (-> slug): a mention of the word is checked as that (conservative) exercise
_ALIASES = {
    "deadlift": "barbell-deadlift", "dead lift": "barbell-deadlift", "rdl": "romanian-deadlift",
    "romanian deadlift": "romanian-deadlift", "bench press": "barbell-bench-press", "benching": "barbell-bench-press",
    "overhead press": "barbell-overhead-press", "military press": "barbell-overhead-press", "ohp": "barbell-overhead-press",
    "shoulder press": "overhead-press", "back squat": "barbell-back-squat", "barbell squat": "barbell-back-squat",
    "pullup": "pull-up", "pull up": "pull-up", "chin up": "pull-up", "chinup": "pull-up", "pushup": "push-up",
    "push up": "push-up", "press up": "push-up", "dip": "bench-dip", "plank": "forearm-plank",
    "handstand": "handstand-hold", "jumping jack": "jumping-jacks", "star jump": "jumping-jacks",
    "sprint": "sprint-intervals", "sprinting": "sprint-intervals", "jog": "easy-jog", "jogging": "easy-jog",
    "running session": "easy-jog", "skipping rope": "jump-rope", "rope skipping": "jump-rope", "jump rope": "jump-rope",
    "mountain climber": "mountain-climbers", "high knee": "high-knees", "wall sit": "wall-sit", "lunge": "reverse-lunge",
    "farmer carry": "farmer-carry", "farmers carry": "farmer-carry", "farmer walk": "farmer-carry",
    "hip thrust": "hip-thrust", "leg press": "leg-press", "calf raise": "calf-raise",
}


def _word(w: str) -> str:
    return w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w


def _key(text: str) -> str:
    """The matching key of a name / phrase: lower-case words, hyphens as spaces, simple plurals dropped."""
    return " ".join(_word(w) for w in re.findall(r"[a-z0-9]+", (text or "").lower().replace("-", " ")))


@lru_cache(maxsize=1)
def _index() -> tuple[dict[str, LibExercise], LibraryIndex]:
    """Phrase key -> library exercise: every name and slug, the name without equipment / position words when that
    still leaves two words or more (the most contraindicated exercise wins a tie - medically conservative), and the
    aliases above."""
    lib = library()
    by_slug = {e.slug: e for e in lib}
    index: dict[str, LibExercise] = {}
    for e in lib:
        index.setdefault(_key(e.name), e)
    names = set(index)
    for alias, slug in _ALIASES.items():  # an alias beats a slug ("overhead press" -> the barbell lift)
        if slug in by_slug and _key(alias) not in names:
            index[_key(alias)] = by_slug[slug]
    for e in lib:
        index.setdefault(_key(e.slug), e)
    full_keys = set(index)
    for e in lib:
        words = _key(e.name).split()
        while words and words[0] in _MODIFIERS:
            words = words[1:]
        short = " ".join(words)
        if len(words) < 2 or short in full_keys:
            continue
        current = index.get(short)
        if current is None or len(e.contraindications) > len(current.contraindications):
            index[short] = e
    return index, LibraryIndex(lib)


_PHRASES = (
    # impact / plyometrics
    r"(?:box|tuck|squat|split|broad|star|lunge|vertical|depth|pogo|frog|skater|knee tuck) jumps?",
    r"jump(?:ing)? (?:squats?|lunges?|jacks?|rope)", r"jumps", r"plyo(?:metrics?|metric)?(?: (?:drills?|work|training|jumps?))?",
    r"skipping rope", r"rope skipping", r"skipping (?:drills?|sessions?|workouts?)", r"(?:single leg |lateral )?hops",
    r"hopping", r"bounding", r"burpees?", r"sprawls?",
    r"mountain climbers?", r"high knees", r"star jumps?",
    # running / intervals
    r"(?:hill |all out |max(?:imal)? )?sprints?", r"sprinting", r"hiit", r"tabata", r"interval training",
    r"running(?! (?:late|behind|out|low|short|a |an |the |on |into|over|through|around|errands?))", r"jogging",
    r"jogs?", r"go for a run",
    r"(?:a|morning|evening|long|easy|hill|trail|outdoor|treadmill|\d+ ?k|\d+ ?km|\d+ ?miles?) runs?",
    # squats / lunges / hinges
    r"(?:barbell |back |front |overhead |zercher |deep |heavy |goblet |box |bodyweight |air |pistol |sissy |split |"
    r"bulgarian split |weighted )?squats?",
    r"(?:walking |reverse |forward |lateral |deep |split |weighted )?lunges?",
    r"(?:romanian |stiff legged |stiff leg |sumo |trap bar |conventional |heavy |barbell |dumbbell )?dead ?lifts?",
    r"rdls?", r"good ?mornings?", r"(?:kettlebell|kb) swings?", r"hip thrusts?", r"glute bridges?",
    # presses / olympic lifts
    r"(?:overhead|military|shoulder|push|arnold|strict|seated|standing) press(?:es|ing)?", r"ohp", r"snatch(?:es)?",
    r"clean (?:and|&) jerks?", r"(?:power|hang) cleans?", r"thrusters?", r"behind the neck(?: press(?:es)?)?",
    r"upright rows?", r"(?:incline |decline |flat |barbell |dumbbell )?bench press(?:es|ing)?", r"benching",
    r"(?:floor|chest) press(?:es)?",
    # core / flexion / holds
    r"sit ?ups?", r"crunch(?:es)?", r"russian twists?", r"v ?ups?", r"toe touch(?:es)?", r"(?:hanging )?leg raises?",
    r"jefferson curls?", r"dead ?bugs?", r"(?:side )?planks?(?: holds?)?", r"hollow (?:body )?holds?", r"wall sits?",
    r"isometric(?: holds?)?", r"static holds?",
    # inversions / balance
    r"hand ?stands?(?: holds?| push ?ups?)?", r"head ?stands?", r"inversions?", r"inverted (?:rows?|hangs?|positions?)",
    r"decline (?:sit ?ups?|crunch(?:es)?|push ?ups?)", r"bosu(?: ball)?", r"balance board", r"slack ?line",
    r"wobble board",
    # upper body
    r"(?:pike |diamond |clap(?:ping)? |plyo |incline |wall |knee )?push ?ups?", r"press ?ups?", r"pull ?ups?",
    r"chin ?ups?", r"muscle ?ups?", r"kipping", r"(?:bench |ring |parallel bar )?dips",
    r"(?:bent over|barbell|pendlay|t bar) rows?", r"lat pull ?downs?", r"farmers? (?:carry|carries|walks?)",
    r"leg press(?:es)?", r"(?:lying )?leg curls?", r"bear crawls?", r"planche", r"rope climbs?",
    # breath / heat / cold / contact
    r"hold (?:your|the|her|his|their) breath", r"breath ?hold(?:s|ing)?", r"valsalva", r"ice baths?",
    r"cold plunges?", r"cold water (?:immersion|swims?|dips?)", r"saunas?", r"hot yoga", r"bikram", r"sweat suits?",
    r"sparring", r"kickboxing", r"boxing", r"martial arts?", r"mma", r"wrestling",
)
_LOADS = (
    r"(?P<pct>\d{1,3}) ?% (?:of )?(?:(?:your|his|her|their|the) )?(?:1 ?rm|one rep max|max)",
    r"1 ?rms?(?: tests?| attempts?| day)?", r"one rep max(?:es)?(?: tests?| attempts?)?",
    r"max(?:imal|imum)? (?:effort|lifts?|weights?|loads?|reps?|attempts?|singles?)", r"max(?:ing)? out",
    r"all out (?:effort|sprints?|sets?|intervals?|pace)", r"go(?:ing)? all out",
    r"pr attempts?", r"personal record attempts?", r"to (?:muscle |total )?failure",
    r"(?:very |really |super )?heavy (?:weights?|lifts?|lifting|sets?|singles?|loads?|day|session|squats?|deadlifts?|bench)",
    r"go(?:ing)? heavy", r"lift(?:ing)? heavy", r"rpe ?(?:8|9|10)(?:\.5)?",
)
_PHRASE_RX = re.compile(r"\b(?:" + "|".join(_PHRASES) + r")\b")
_LOAD_RX = re.compile(r"\b(?:" + "|".join(_LOADS) + r")\b")
_WEIGHT_RX = re.compile(r"\b(?P<n>\d{1,3}(?:\.\d)?) ?(?P<unit>kgs?|kilos?|kilograms?|lbs?|pounds?)\b")
# words that negate (detect_red_flag's sudden-pain check reads this set as it is)
_NEGATORS = frozenset({"no", "not", "dont", "never", "avoid", "avoiding", "skip", "without", "instead", "rather",
                       "nothing", "cant", "cannot", "shouldnt", "wont", "zero", "except", "replace", "swap", "stop",
                       "nor", "neither"})
_CLAUSE = re.compile(r"\|| but | then | so | however ")
LOAD_REASON = "keep loads moderate (RPE 6-7) - no heavy or max-effort lifting and no training to failure"

# ---- the send-time check's negation scope (``_negated``) - a negation must GOVERN the mention:
# * "no", "without", "instead of" ... reach only the next couple of words: "no box jumps", "instead of sprints" - but
#   "no rest between sets of box jumps" still suggests box jumps;
# * "don't", "avoid", "skip", "stop" ... reach the rest of the clause unless it turns: "stop walking and START sprinting",
#   "skip the bike, we DO box jumps" (a new subject), "... only / just burpees";
# * "replace", "swap" ... reach only what comes before "with" / "for": "swap the bike FOR heavy deadlifts";
# * phrases that only look negative tell the member to do it: "don't forget", "never skip", "never miss", "no excuses",
#   "without fail", "no pain no gain", "no more than 10".
_NEG_NARROW = frozenset({"no", "zero", "without", "instead", "rather", "except", "nothing", "nor", "neither"})
_NEG_VERB = frozenset({"not", "dont", "never", "avoid", "avoiding", "skip", "stop", "cant", "cannot", "shouldnt", "wont",
                       "doesnt", "didnt", "mustnt", "quit"})
_NEG_SWAP = frozenset({"replace", "replacing", "swap", "swapping", "switch", "substitute", "trade", "exchange"})
_SWAP_PIVOTS = frozenset({"with", "for", "to", "into", "by"})
_NARROW_FILLERS = frozenset({"a", "an", "the", "any", "some", "more", "of", "than", "your", "my", "his", "her", "their",
                             "our", "doing", "do", "really", "even", "ever"})
_NARROW_REACH = 2  # words (fillers aside) a narrow negation reaches past: "no heavy weighted box jumps"
_AFFIRM_VERBS = frozenset({"start", "starting", "begin", "do", "doing", "try", "add", "go", "use", "hit", "include",
                           "keep", "continue", "focus", "practice", "practise", "perform", "progress", "work", "finish",
                           "make", "get", "give", "take", "move", "switch"})
_PIVOT_SKIP = frozenset({"you", "we", "i", "then", "also", "please", "lets", "let", "us", "ill", "youll", "well",
                         "will", "can", "should", "must", "to", "now"})
_SUBJECTS = frozenset({"we", "you", "youll", "well", "lets", "i", "ill"})
_AFFIRM_IDIOMS = re.compile(
    r"\b(?:(?:dont|do not|never|cant|cannot|wont|shouldnt) (?:forget|skip|miss|stop|neglect|quit|worry|hesitate)|"
    r"no (?:excuses?|exceptions?|matter what|doubt|problem|worries|days? off|skipping|slacking|pain no gain|"
    r"(?:more|less|fewer) than)|not (?:more|less|fewer) than|without (?:fail|exceptions?|excuses?)|not only|"
    r"no need to (?:avoid|skip|stop|worry))\b")
_NEG_WINDOW = 8  # words of the clause before a mention that are read for a negation

# ---- typos and glued words (deterministic and conservative): a doubled key ("box jumpps") or two swapped letters
# ("deadlfits") of an exercise word, one wrong / missing / extra letter of a LONG distinctive one ("burpess"), and
# multi-word names typed - or zero-width-joined - as one word ("boxjumps"). Known words are never "corrected": the live
# coach's common words, the exercise vocabulary and real words one slip away from an exercise (crutches, splints ...).
_TYPO_TARGETS = frozenset("""
jumps squats lunges burpee burpees deadlift deadlifts sprint sprints sprinting crunch crunches situps pushups pullups
chinups planks snatch snatches thruster thrusters kettlebell kettlebells plyometric plyometrics kickboxing handstand
handstands headstand headstands valsalva sparring boxing
""".split())
_TYPO_LONG = frozenset(w for w in _TYPO_TARGETS if len(w) >= 7)
_LOOKALIKES = frozenset("""
crutches crutch crunched splints springs spring sprains sparing sparking spurring starring scarring snitches snatched
squads squash lounges plants hoping prints lungs bumps dumps jumper jumpers punches bunches lunches thrusted
""".split())
_GLUED_EXTRA = ("box jump", "tuck jump", "squat jump", "split jump", "broad jump", "depth jump", "pogo jump", "jump squat",
                "jump lunge", "kettlebell swing", "kb swing", "sit up", "v up", "muscle up", "hand stand", "head stand",
                "good morning", "russian twist", "leg raise", "toe touch", "bear crawl", "rope climb", "power clean",
                "hang clean", "front squat", "pistol squat", "sumo deadlift", "ice bath", "cold plunge", "hot yoga",
                "breath hold", "hill sprint", "jumping jack", "push up", "pull up", "chin up")

# ---- words that make a mention something else: "sprint planning", "push up the session", "sit up straight", and the
# greeting "good morning" (the exercise is "good mornings", or a counted "10 good morning")
_WORK_NOUNS = frozenset({"planning", "meeting", "meetings", "review", "reviews", "retro", "retros", "retrospective",
                         "backlog", "demo", "velocity", "standup", "ticket", "tickets", "board", "deadline", "deadlines",
                         "kickoff"})
_PHRASAL_UP = frozenset({"push up", "pull up", "sit up", "press up", "chin up", "step up"})
_VERB_OBJECTS = frozenset({"the", "your", "my", "his", "her", "their", "our", "a", "an", "it", "its", "this", "that",
                           "these", "those", "straight", "against", "on", "to", "with", "for", "here", "there", "me",
                           "him", "them", "us", "you"})
_COUNT_WORDS = frozenset({"some", "few", "do", "doing", "try", "add", "include", "perform", "more", "of", "set", "sets",
                          "rep", "reps", "x"})


@lru_cache(maxsize=1)
def _known_words() -> frozenset[str]:
    from app.services.live_coach import COMMON, VOCAB

    index, _ = _index()
    words = {w for key in index for w in key.split()} | {w for alias in _ALIASES for w in _key(alias).split()}
    return frozenset(COMMON | VOCAB | _LOOKALIKES | _TYPO_TARGETS | words)


@lru_cache(maxsize=1)
def _glued() -> dict[str, str]:
    """"boxjump" -> "box jump": every two- or three-word exercise name written as one word."""
    index, _ = _index()
    out: dict[str, str] = {}
    for key in (*(_key(p) for p in _GLUED_EXTRA), *index):
        words = key.split()
        if 2 <= len(words) <= 3:
            out.setdefault("".join(words), key)
    return out


@lru_cache(maxsize=4096)
def _fix_word(w: str) -> str:
    """One word of a prepared message with an exercise typo corrected (unchanged when unsure)."""
    if len(w) < 5 or not w.isalpha():
        return w
    known = _known_words()
    base = _word(w)
    if w in known or base in known:
        return w
    glued = _glued()
    if base in glued:
        return glued[base] + ("s" if base != w else "")
    for i in range(len(w) - 1):
        doubled = w[:i] + w[i + 1:] if w[i] == w[i + 1] else None  # "jumpps" -> "jumps"
        swapped = w[:i] + w[i + 1] + w[i] + w[i + 2:]  # "deadlfits" -> "deadlifts"
        for cand in (doubled, swapped):
            if cand and cand in _TYPO_TARGETS:
                return cand
    if len(w) >= 7:  # one wrong / missing / extra letter: long distinctive words, same first letter, unambiguous
        from app.services.live_coach import _osa

        hits = sorted((abs(len(t) - len(w)), t) for t in _TYPO_LONG
                      if t[0] == w[0] and abs(len(t) - len(w)) <= 1 and _osa(w, t, 1) <= 1)
        if len(hits) == 1 or (len(hits) > 1 and hits[0][0] < hits[1][0]):
            return hits[0][1]
    return w


def _prepare(text: str) -> str:
    """Lower-case ASCII (zero-width and other non-ASCII characters vanish); apostrophes vanish; hyphens / slashes are
    spaces; sentence and clause punctuation is ``|``; "85 percent" is "85%"; exercise typos corrected (``_fix_word``)."""
    t = unicodedata.normalize("NFKD", str(text or "")[:CHECK_TEXT_MAX]).encode("ascii", "ignore").decode().lower()
    t = re.sub(r"[’‘'`´]", "", t)
    t = re.sub(r"[-_/]+", " ", t)
    t = re.sub(r"(?<!\d)\.|\.(?!\d)|[!?;:,\n\r()\"]+", " | ", t)
    t = re.sub(r"[^a-z0-9%&|. ]+", " ", t)
    t = re.sub(r"\b(\d{1,3}) ?(?:percent|per cent|pct)\b", r"\1%", t)
    t = re.sub(r"\s+", " ", t).strip()
    return " ".join(_fix_word(w) for w in t.split(" ")) if t else t


def _turns(after: list[str]) -> bool:
    """After a verb negation, does the clause turn to an instruction? "... and START sprinting", "... we DO box jumps",
    "... only / just burpees" (the word right after the negation is the negated verb itself: "don't do", "never try")."""
    for j, x in enumerate(after):
        if j == 0:
            continue
        if x in ("only", "just"):
            return True
        if x in ("and", "plus") or x in _SUBJECTS:
            nxt = next((y for y in after[j + 1:] if y not in _PIVOT_SKIP), None)
            if nxt in _AFFIRM_VERBS:
                return True
    return False


def _negated(text: str, start: int) -> bool:
    """Does a negation in the same clause govern the mention at ``start``? (scopes above: "no box jumps" and "avoid
    burpees and box jumps" do; "don't forget your box jumps", "stop walking and start sprinting" and "swap the bike for
    heavy deadlifts" don't)."""
    # idioms are read a few words further back than the window, so one cut in half ("don't | stop ...") still counts
    clause = _CLAUSE.split(text[max(0, start - 400):start])[-1].split()[-(_NEG_WINDOW + 4):]
    before = _AFFIRM_IDIOMS.sub(" ", " ".join(clause)).split()[-_NEG_WINDOW:]
    for i in range(len(before) - 1, -1, -1):
        w, after = before[i], before[i + 1:]
        if w in _NEG_NARROW:
            return len([x for x in after if x not in _NARROW_FILLERS]) <= _NARROW_REACH
        if w in _NEG_SWAP:
            return not any(x in _SWAP_PIVOTS for x in after)
        if w in _NEG_VERB:
            return not _turns(after)
    return False


def _not_training(text: str, start: int, end: int, phrase: str) -> bool:
    """The mention means something else: "sprint planning", "push up the session", "sit up straight", "Good morning!"."""
    rest = text[end:].split(None, 1)
    nxt = rest[0] if rest else ""
    words = phrase.split()
    if len(words) == 1 and nxt in _WORK_NOUNS:
        return True
    key = " ".join(words)
    if key in _PHRASAL_UP and nxt in _VERB_OBJECTS:
        return True
    if key == "good morning":
        prev = text[:start].split()
        p = prev[-1] if prev else ""
        return not (p.isdigit() or re.fullmatch(r"\d*x\d+", p) is not None or p in _COUNT_WORDS)
    return False


@dataclass(frozen=True, slots=True)
class Flag:
    """One conflict between what a message suggests and one of the member's safety tags."""

    exercise: str  # as the message put it ("box jumps", "1rm")
    tag: str
    label: str  # the condition in plain words
    reason: str  # what the rule says ("avoid jumping / plyometric impact")

    def as_dict(self) -> dict[str, str]:
        return {"exercise": self.exercise, "tag": self.tag, "condition": self.label, "reason": self.reason}


def _reason_text(reason: str, label: str) -> str:
    prefix = f"{label}: "
    return reason[len(prefix):] if reason.startswith(prefix) else reason


# phrases the word scan must not read on their own ("running late") but that mean this exercise when the phrase list
# matched them ("start running", "a 5k run"), and phrases checked as the guardrail's own words
_PHRASE_SLUGS = {"running": "easy-jog", "jogs": "easy-jog", "jog": "easy-jog", "go for a run": "easy-jog"}
_CHECK_AS = ((re.compile(r"^hold \w+ breath$"), "breath hold"),)


def _phrase_exercise(phrase: str) -> LibExercise | None:
    index, fuzzy = _index()
    key = _key(phrase)
    slug = _PHRASE_SLUGS.get(key) or ("easy-jog" if key.endswith(" run") else None)
    if slug is not None:
        return next((e for e in library() if e.slug == slug), None)
    return index.get(key) or fuzzy.match(phrase)


def _mentions(text: str) -> list[tuple[int, int, str, LibExercise | None, bool]]:
    """(start, end, phrase, library exercise or None, is_load) of every exercise / load mention, longest first where
    they overlap, negated ones and other senses ("sprint planning", "Good morning!") dropped."""
    index, _fuzzy = _index()
    found: list[tuple[int, int, str, LibExercise | None, bool]] = []
    for m in _PHRASE_RX.finditer(text):
        phrase = m.group(0).strip()
        found.append((m.start(), m.end(), phrase, _phrase_exercise(phrase), False))
    for m in _LOAD_RX.finditer(text):
        pct = m.group("pct")
        if pct is not None and int(pct) < HEAVY_PERCENT:
            continue
        found.append((m.start(), m.end(), m.group(0).strip(), None, True))
    # library names the phrase list does not cover ("cat cow", "pike push up" ...): word n-grams, longest first
    words = [(w.start(), w.end(), _word(w.group(0))) for w in re.finditer(r"[a-z0-9]+", text)]
    i = 0
    while i < len(words):
        for n in (4, 3, 2, 1):
            if i + n > len(words):
                continue
            key = " ".join(w for _, _, w in words[i:i + n])
            ex = index.get(key)
            if ex is not None and (n > 1 or len(key) >= 4):
                found.append((words[i][0], words[i + n - 1][1], text[words[i][0]:words[i + n - 1][1]], ex, False))
                i += n - 1
                break
        i += 1
    found.sort(key=lambda f: (f[0], -(f[1] - f[0])))
    kept: list[tuple[int, int, str, LibExercise | None, bool]] = []
    for f in found:
        if kept and f[0] < kept[-1][1] and not f[4]:  # overlaps a longer / earlier mention
            if f[3] is not None and kept[-1][3] is None and f[1] - f[0] >= kept[-1][1] - kept[-1][0]:
                kept[-1] = f  # the library exercise of the same words is the better reading
            continue
        if not _negated(text, f[0]) and not _not_training(text, f[0], f[1], f[2]):
            kept.append(f)
    return kept


def mentions_exercise(text: str) -> bool:
    """Cheap pre-check (no database): does the message mention an exercise or a load at all?"""
    return bool(_mentions(_prepare(text)))


def check_text(text: str, tags: Iterable[str], *, weight_kg: float | None = None) -> list[Flag]:
    """Flags for every exercise / load the text suggests that one of ``tags`` rules out (``app.ai.safety`` rules,
    library contraindications, sanitize_load-style load rules). Deterministic; at most WARNING_FLAGS_MAX flags."""
    tag_list = [t for t in normalize_tags(tags) if t in RULES] if tags else []
    if not tag_list:
        return []
    norm = _prepare(text)
    if not norm:
        return []
    capped = [t for t in tag_list if RULES[t].cap_load]
    flags: list[Flag] = []
    seen: set[tuple[str, str]] = set()

    def add(phrase: str, tag: str, reason: str) -> None:
        key = (tag, _key(phrase))
        if key in seen or len(flags) >= WARNING_FLAGS_MAX:
            return
        seen.add(key)
        label = tag_label(tag)
        flags.append(Flag(exercise=clamp(phrase, 60), tag=tag, label=label, reason=_reason_text(reason, label)))

    for _start, _end, phrase, ex, is_load in _mentions(norm):
        for tag in tag_list:
            if is_load and tag in capped:
                add(phrase, tag, LOAD_REASON)
                continue
            checked = next((as_ for rx, as_ in _CHECK_AS if rx.match(phrase)), phrase)
            reason = check_exercise(ex, checked, [tag])
            if reason is not None:
                add(phrase, tag, reason)
    if capped and weight_kg and weight_kg > 0:
        for m in _WEIGHT_RX.finditer(norm):
            if _negated(norm, m.start()):
                continue
            value = float(m.group("n"))
            kg = value * 0.4536 if m.group("unit").startswith(("lb", "pound")) else value
            if kg >= HEAVY_SHARE * weight_kg:
                for tag in capped:
                    add(m.group(0), tag, f"{LOAD_REASON} ({kg:.0f} kg is about {kg / weight_kg:.0%} of their body weight)")
    return flags


SUMMARY_SEND_TEXT = ("This member has safety notes on file. Check with their trainer before suggesting exercises or "
                     "training loads - edit the message, or send it anyway if you're sure.")


@dataclass(slots=True)
class SendCheck:
    """A staff / owner message that suggests something the member's safety tags rule out (``full``), or - for staff who
    may only know that there ARE safety notes (``summary``) - any exercise or load at all, whatever the conditions."""

    level: str  # full (names the condition) | summary (check with the trainer - the same for every exercise)
    name: str
    flags: list[Flag]
    mentioned: list[str] = field(default_factory=list)  # summary: what the message itself mentions (never a condition)

    @property
    def tags(self) -> list[str]:
        return sorted({f.tag for f in self.flags})

    @property
    def exercises(self) -> list[str]:
        out: list[str] = []
        for e in [f.exercise for f in self.flags] or self.mentioned:
            if e not in out:
                out.append(e)
        return out

    @property
    def title(self) -> str:
        return "Check this before you send it"

    @property
    def message(self) -> str:
        if self.level == FULL:
            quoted = ", ".join(f"“{e}”" for e in self.exercises)
            conditions = ", ".join(sorted({f.label for f in self.flags}, key=str.casefold))
            return (f"Safety note for {self.name}: {conditions}. Your message suggests {quoted}, which may not be safe "
                    "for them. Edit it, or send it anyway if you're sure.")
        return SUMMARY_SEND_TEXT

    @property
    def lines(self) -> list[str]:
        if self.level != FULL:
            return []
        return [f"“{f.exercise}” - {f.label}: {f.reason}" for f in self.flags]

    def payload(self) -> dict[str, Any]:
        return {"title": self.title, "message": self.message, "lines": self.lines, "level": self.level,
                "flags": [f.as_dict() for f in self.flags] if self.level == FULL else []}


async def check_staff_message(db: AsyncSession, thread: ChatThread, sender: User, body: str) -> SendCheck | None:
    """The send-time check of a staff / owner message to ``thread``'s member (None = nothing to warn about):

    * ``full`` (the gym's admin team, the member's trainers): checked against the member's safety tags - a hit names the
      condition and what to avoid;
    * ``summary`` (other staff in the conversation): the SAME reminder to check with the trainer for ANY exercise or load
      the message mentions, whatever the member's conditions - all it tells is that there are safety notes, as their
      panel does - so probing messages can never reveal which conditions the member has;
    * ``withheld`` / ``none``: nothing (the member keeps their health details private, or no access at all)."""
    text = str(body or "")[:CHECK_TEXT_MAX]
    if not text.strip() or sender.id == thread.customer_id:
        return None
    mentions = _mentions(_prepare(text))
    if not mentions:
        return None
    customer = thread.customer if "customer" in thread.__dict__ else await db.get(User, thread.customer_id)
    if customer is None or customer.deleted_at is not None:
        return None
    access = await health_access(db, sender, thread.customer_id, in_conversation=True)
    if access.level == SUMMARY:
        brief = await load_brief(db, customer, access)
        if not brief.has_notes:
            return None
        mentioned = list(dict.fromkeys(clamp(phrase, 60) for _s, _e, phrase, _ex, _load in mentions))
        return SendCheck(level=SUMMARY, name=_first_name(customer), flags=[], mentioned=mentioned[:WARNING_FLAGS_MAX])
    if access.level != FULL:
        return None
    tags = await member_tags(db, customer)
    if not tags:
        return None
    weight = None
    if any(RULES[t].cap_load for t in tags if t in RULES) and _WEIGHT_RX.search(_prepare(text)):
        weight = await db.scalar(select(CustomerProfile.weight_kg).where(CustomerProfile.user_id == thread.customer_id))
    flags = check_text(text, tags, weight_kg=weight)
    if not flags:
        return None
    return SendCheck(level=access.level, name=_first_name(customer), flags=flags)


def override_detail(check: SendCheck, message_id: int | None) -> dict[str, Any]:
    """Audit detail of a "Send anyway": the safety tags and what was suggested - never the condition names or notes."""
    return {"tags": check.tags, "exercises": [clamp(e, 60) for e in check.exercises][:WARNING_FLAGS_MAX],
            "message_id": message_id, "level": check.level}


# ============================================================================ red flags (member -> trainer)
# the live coach's red flags that matter in a conversation (plain headaches and vomiting stay with the live coach)
CHAT_RED_FLAGS = frozenset({
    "heart_attack", "chest_pain", "breathing", "allergic_reaction", "fainting", "dizziness", "heart_rhythm", "stroke",
    "severe_headache", "severe_pain", "seizure", "emergency", "pop_injury", "low_sugar", "overheating",
})
_SUDDEN_PAIN = re.compile(r"\bsudden(?:ly)?(?: [a-z]+){0,4} (?:pain|pains|ache|hurts?|hurting)\b|"
                          r"\b(?:pain|ache)(?: [a-z]+){0,3} (?:came on )?(?:suddenly|out of nowhere)\b")


@dataclass(frozen=True, slots=True)
class RedFlag:
    flag: str
    level: str
    label: str
    matched: str = ""


def detect_red_flag(text: str | None) -> RedFlag | None:
    """The most serious chat red flag in a member's message (the live coach's deterministic rules, typo tolerant,
    negation aware: "no chest pain" is not one), plus severe or sudden pain. None = no red flag."""
    from app.services.live_coach import FLAG_LABELS, correct_typos, detect_intent, normalize

    raw = str(text or "")[:CHECK_TEXT_MAX]
    if not raw.strip():
        return None
    t = correct_typos(normalize(raw))
    for _ in range(4):  # skip red flags that stay with the live coach (headache, vomiting) and look again
        intent = detect_intent(t)
        if intent.kind == "red_flag":
            if intent.name in CHAT_RED_FLAGS:
                return RedFlag(intent.name, intent.level or "critical", FLAG_LABELS.get(intent.name, "a red-flag symptom"),
                               intent.matched)
            if intent.matched and intent.matched in t:
                t = t.replace(intent.matched, " | ", 1)
                continue
            break
        if intent.kind == "discomfort" and intent.severity == "severe":
            return RedFlag("severe_pain", "critical", FLAG_LABELS.get("severe_pain", "severe pain"), intent.matched)
        break
    m = _SUDDEN_PAIN.search(t)
    if m is not None and not any(w in _NEGATORS for w in t[:m.start()].split("|")[-1].split()[-4:]):
        return RedFlag("severe_pain", "critical", FLAG_LABELS.get("severe_pain", "severe pain"), m.group(0))
    return None


@dataclass(slots=True)
class RedFlagAlert:
    """What a member's red-flag message triggered (the API answers it; the no-JS page flashes it)."""

    flag: str
    label: str
    guidance: str
    number: str
    tel: str
    alerted: bool  # False = an alert for this conversation went out within the hour (deduplicated)
    notified: int = 0

    def payload(self) -> dict[str, Any]:
        return {"flag": self.flag, "title": "Your safety comes first", "message": self.guidance, "number": self.number,
                "tel": self.tel, "alerted": self.alerted}


def guidance_text(flag: str, country: str | None) -> tuple[str, str, str]:
    """(message, emergency number, tel: link) for the member: the live coach's advice for the symptom + stop
    exercising + local emergency services (India and the EU 112, US 911, UK 999 ...) or a doctor."""
    from app.services.live_coach import FLAG_ADVICE, emergency_info

    em = emergency_info(country)
    number = em["number"]
    advice = FLAG_ADVICE.get(flag, "Please stop and rest.").format(num=number)
    text = (f"{SAFETY_PREFIX} {advice} Stop exercising for now. If it feels like an emergency, call {number} "
            f"(emergency services) right away, or see a doctor. Your trainer and the gym team have been alerted.")
    return text[:1000], number, em["tel"]


async def _recent_alert(db: AsyncSession, thread_id: int, now: datetime) -> bool:
    """A safety message in this conversation within RED_FLAG_WINDOW - read from its latest RED_FLAG_SCAN messages."""
    latest = (select(ChatMessage.sender_role, ChatMessage.created_at, func.left(ChatMessage.body, 20).label("head"))
              .where(ChatMessage.thread_id == thread_id).order_by(ChatMessage.id.desc()).limit(RED_FLAG_SCAN)
              .subquery("latest"))
    found = await db.scalar(
        select(func.count()).select_from(latest).where(
            latest.c.sender_role == "system", latest.c.created_at >= now - RED_FLAG_WINDOW,
            latest.c.head.startswith(SAFETY_PREFIX[:20]))
    )
    return bool(found)


async def _trainer_users(db: AsyncSession, customer_id: int, gym_id: int) -> list[int]:
    """User ids of the member's assigned trainers at this gym (the subscription's trainer + active links) with an
    ACTIVE, linked, not deleted employment."""
    today = date.today()
    via_sub = select(Subscription.trainer_id).where(
        Subscription.customer_id == customer_id, Subscription.gym_id == gym_id,
        Subscription.status == SubscriptionStatus.ACTIVE, Subscription.trainer_id.is_not(None),
        or_(Subscription.ends_on.is_(None), Subscription.ends_on >= today))
    via_link = select(TrainerClient.employee_id).where(TrainerClient.customer_id == customer_id,
                                                       TrainerClient.active.is_(True))
    rows = await db.execute(
        select(Employee.user_id).join(User, User.id == Employee.user_id).where(
            Employee.gym_id == gym_id, Employee.status == EmploymentStatus.ACTIVE, Employee.deleted_at.is_(None),
            Employee.user_id.is_not(None), or_(Employee.id.in_(via_sub), Employee.id.in_(via_link)),
            # a disabled (banned) or deleted trainer account is never alerted: health alerts reach only people who may
            # see this member's health data
            User.is_active.is_(True), User.deleted_at.is_(None))
        .limit(20)
    )
    return sorted({uid for (uid,) in rows.all() if uid})


async def raise_red_flag(db: AsyncSession, *, thread: ChatThread, gym: Gym, member: User, employee: Employee | None,
                         red: RedFlag) -> RedFlagAlert:
    """Alert the people responsible for the member - the gym owner, the staff member of this conversation and (while
    the member shares health details) their assigned trainers - at most once per conversation per RED_FLAG_WINDOW
    (a per-conversation advisory lock makes the check race-safe). Never refused by a cap. The caller posts the
    safety message (``alert.alerted``) and commits."""
    from app.core import caps
    from app.services.audit import audit
    from app.services.notify import Note, notify_bulk

    guidance, number, tel = guidance_text(red.flag, member.country_code or gym.country_code)
    alert = RedFlagAlert(flag=red.flag, label=red.label, guidance=guidance, number=number, tel=tel, alerted=False)
    now = utcnow()
    await caps.lock(db, "chat_red_flag", thread.id)
    if await _recent_alert(db, thread.id, now):
        return alert
    alert.alerted = True
    member_name = member.display_name
    participants: dict[int, str] = {}
    if gym.owner_id:
        participants[gym.owner_id] = f"/chat/{thread.id}"
    if (employee is not None and employee.user_id and employee.deleted_at is None
            and employee.status == EmploymentStatus.ACTIVE):  # never a suspended or former staff member
        participants[employee.user_id] = f"/chat/{thread.id}"
    if await sharing_enabled(db, member.id):  # trainers outside the conversation only while the member shares
        for uid in await _trainer_users(db, member.id, gym.id):
            participants.setdefault(uid, f"/staff/clients/{member.id}")
    participants.pop(member.id, None)
    title = f"Urgent: {member_name} reported {red.label}"[:160]
    notes: list[Note] = []
    for uid, link in sorted(participants.items()):
        where = "in a chat with your gym" if link.startswith("/chat/") else "in a chat with their gym"
        body = (f"{member_name} mentioned {red.label} {where} ({gym.name}). Check on them now. If they are with you, "
                f"stop the session and call {number} if needed. Open the {'conversation' if link.startswith('/chat/') else 'client page'}"
                " for details.")
        notes.append(Note(uid, title, body, link=link, category=NotificationCategory.HEALTH, severity=Severity.DANGER))
    if notes:  # ONE bulk send; a missing / deleted account gets no notification and no id, so it is not counted
        await db.flush()  # as notify() did: the caller's pending changes are written before the notices
        alert.notified += len(await notify_bulk(db, notes))
    await audit(db, member, "chat.red_flag", "chat_thread", thread.id, gym_id=thread.gym_id,
                detail={"flag": red.flag, "notified": alert.notified})
    log.info("chat red flag %s in thread %s: %s people alerted", red.flag, thread.id, alert.notified)
    return alert


__all__ = [
    "BADGE_IDS_MAX", "CHAT_RED_FLAGS", "FULL", "NONE", "PANEL_READ_MAX", "RED_FLAG_WINDOW", "SAFETY_PREFIX", "SUMMARY",
    "SUMMARY_SEND_TEXT", "SUMMARY_TEXT", "WARNING_FLAGS_MAX", "WITHHELD", "WITHHELD_TEXT", "Access", "Flag", "RedFlag",
    "RedFlagAlert", "SafetyBrief", "SendCheck", "active_conversation_clauses", "avoid_text", "badge_ids", "build_brief",
    "check_staff_message", "check_text", "contraindicated", "in_active_conversation",
    "detect_red_flag", "guidance_text", "health_access", "is_assigned_trainer", "library", "load_brief",
    "member_tags", "mentions_exercise", "override_detail", "panel_for", "raise_red_flag", "safety_order",
    "sharing_enabled", "trainer_link_clauses",
]
