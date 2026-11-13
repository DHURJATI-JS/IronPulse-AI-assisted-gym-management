"""WP-E1 health services: deterministic core + database workflows.

Sections
* levels, input validation, BMI helpers
* the anomaly rule engine ``evaluate_metrics`` (pure, table-tested)
* metric logging: ``record_metrics`` (upsert per customer + local date, abnormal input ALWAYS logged)
  -> signed confirmation token for severe readings -> ``finalize_metrics`` (AI layer that may only escalate,
  notifications, safety suspension)
* safety suspension: ``suspend_customer`` / ``reactivate_customer`` (public API, also used by WP-F)
* AC advice: ``ac_recommendation`` (+ cache on CustomerProfile.ac_recommendation); the optional AI wording is the
  ``health_ac_note`` background job (``queue_ac_note`` / ``ac_note_job``) - no request waits for it
* bedtime: ``bedtime_for``, sleep-window maths and the ``bedtime_reminders`` periodic task
* wellness: PSS-4 scoring, red-flag symptoms, recommendations, ``record_wellness_check``
* scale / medical-report reading: ``parse_measurements`` + ``cross_check`` + ``scan_measurements``
* presentation helpers (trend chart geometry, BMI gauge, prefill values, formatting)
"""

from __future__ import annotations

import io
import logging
import math
import re
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

import sqlalchemy as sa
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import health as health_ai
from app.ai.client import AIUnavailableError, ai
from app.ai.embeddings import embed
from app.ai.errors import AICancelled, AIRejected
from app.ai.owner import AIOwner, current_owner
from app.config import get_settings
from app.core import caps
from app.core.utils import bmi as compute_bmi
from app.core.utils import local_today, safe_zone, utcnow
from app.models import (
    CustomerProfile,
    Employee,
    Gym,
    HealthAlert,
    HealthCondition,
    Job,
    MetricEntry,
    Subscription,
    SuspensionEvent,
    TrainerClient,
    User,
    WellnessCheck,
)
from app.models.enums import (
    AnomalyLevel,
    ConditionKind,
    EmploymentStatus,
    JobStatus,
    NotificationCategory,
    Role,
    Severity,
    StressLevel,
    SubscriptionStatus,
)
from app.services.audit import audit
from app.services.conditions import TAG_LABELS
from app.db import session_scope
from app.services.batching import Chunks, remember, resume_after
from app.services.jobs import current_run, enqueue, job_handler, periodic, set_failure_event, set_result_event
from app.services.notify import Note, email_address, notify, notify_bulk

log = logging.getLogger("ironpulse.health")

EMERGENCY_HINT = "your local emergency number (112 in India and the EU, 911 in the US)"


# ============================================================================ formatting
def fmt_num(value: Any, digits: int = 1) -> str:
    """72.0 -> "72", 72.45 -> "72.5" (form pre-fill / labels)."""
    if value is None or value == "":
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    text = f"{number:.{digits}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def first_name(user: User) -> str:
    parts = (user.full_name or "").split()
    return parts[0] if parts else "there"


# ============================================================================ levels
LEVEL_RANK: dict[AnomalyLevel, int] = {
    AnomalyLevel.NONE: 0,
    AnomalyLevel.INFO: 1,
    AnomalyLevel.WARNING: 2,
    AnomalyLevel.CRITICAL: 3,
    AnomalyLevel.LIFE_THREATENING: 4,
}


def as_level(value: Any) -> AnomalyLevel:
    try:
        return AnomalyLevel(getattr(value, "value", value))
    except ValueError:
        return AnomalyLevel.NONE


def level_rank(value: Any) -> int:
    return 0 if value is None else LEVEL_RANK[as_level(value)]


def max_level(*levels: Any) -> AnomalyLevel:
    best = AnomalyLevel.NONE
    for lv in levels:
        if lv is not None and level_rank(lv) > level_rank(best):
            best = as_level(lv)
    return best


# ============================================================================ validation
HEIGHT_RANGE = (50.0, 250.0)
WEIGHT_RANGE = (20.0, 300.0)


def parse_measure(raw: Any, *, lo: float, hi: float, label: str, unit: str) -> tuple[float | None, str | None]:
    text = str(raw if isinstance(raw, (str, int, float)) else "").strip().replace(",", ".")
    if not text:
        return None, f"Enter your {label} in {unit}."
    try:
        value = float(text)
    except ValueError:
        return None, f"Enter your {label} as a number in {unit}."
    if not math.isfinite(value) or value < lo or value > hi:
        return None, f"This doesn't look right - {label} must be between {lo:g} and {hi:g} {unit}."
    return round(value, 1), None


def validate_metrics(height_raw: Any, weight_raw: Any) -> tuple[float | None, float | None, dict[str, str]]:
    """Server-side validation of the daily metrics form. Returns (height, weight, field errors)."""
    errors: dict[str, str] = {}
    height, err = parse_measure(height_raw, lo=HEIGHT_RANGE[0], hi=HEIGHT_RANGE[1], label="height", unit="cm")
    if err:
        errors["height_cm"] = err
    weight, err = parse_measure(weight_raw, lo=WEIGHT_RANGE[0], hi=WEIGHT_RANGE[1], label="weight", unit="kg")
    if err:
        errors["weight_kg"] = err
    if not errors and height and weight:
        value = compute_bmi(height, weight)
        if value < 7 or value > 150:  # no living person has this height/weight combination
            errors["weight_kg"] = "This doesn't look right - please check both your height and weight."
    return height, weight, errors


# ============================================================================ BMI helpers
BMI_BANDS = ((18.5, "under", "Below the healthy range"), (25.0, "healthy", "Healthy range"),
             (30.0, "over", "Above the healthy range"), (math.inf, "obesity", "Obesity range"))
GAUGE_MIN, GAUGE_MAX = 14.0, 40.0


def bmi_category(value: float | None, age: int | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if age is not None and age < 18:  # teen BMI needs age/sex percentiles - no adult labels, no judgement
        return {"key": "youth", "label": "Read with age percentiles", "adult": False}
    for limit, key, label in BMI_BANDS:
        if value < limit:
            return {"key": key, "label": label, "adult": True}
    return None


def bmi_gauge(value: float | None, age: int | None) -> dict[str, Any] | None:
    """Geometry for the adult BMI gauge (0-100 SVG units, 14-40 BMI scale)."""
    cat = bmi_category(value, age)
    if value is None or cat is None or not cat["adult"]:
        return None

    def x(v: float) -> float:
        return round((v - GAUGE_MIN) / (GAUGE_MAX - GAUGE_MIN) * 100, 2)

    bounds = [(GAUGE_MIN, 18.5, "under"), (18.5, 25.0, "healthy"), (25.0, 30.0, "over"), (30.0, GAUGE_MAX, "obesity")]
    segments = []
    for i, (a, b, key) in enumerate(bounds):
        x1 = x(a) + (0.0 if i == 0 else 0.8)
        x2 = x(b) - (0.0 if i == len(bounds) - 1 else 0.8)
        segments.append({"x1": round(x1, 2), "x2": round(x2, 2), "active": key == cat["key"]})
    marker = min(98.5, max(1.5, x(value)))
    return {"value": value, "label": cat["label"], "segments": segments, "x": round(marker, 2)}


# ============================================================================ rule engine
ADULT_AGE = 18
ADULT_HEIGHT_AGE = 21


@dataclass(slots=True, frozen=True)
class Reading:
    recorded_on: date
    height_cm: float
    weight_kg: float
    height_valid: bool = True


def reason_codes(reasons: Any) -> set[str]:
    return {str(r.get("code")) for r in (reasons or []) if isinstance(r, dict) and r.get("code")}


def reading_of(entry: MetricEntry) -> Reading:
    return Reading(entry.recorded_on, float(entry.height_cm), float(entry.weight_kg),
                   "height_invalid" not in reason_codes(entry.anomaly_reasons))


@dataclass(slots=True)
class Finding:
    code: str
    level: AnomalyLevel
    title: str
    message: str
    confirm: bool = False
    source: str = "rule"

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "level": self.level.value, "title": self.title, "message": self.message,
                "confirm": self.confirm, "source": self.source}


@dataclass(slots=True)
class Evaluation:
    height_cm: float
    weight_kg: float
    effective_height_cm: float
    bmi: float
    age: int | None
    findings: list[Finding] = field(default_factory=list)

    @property
    def minor(self) -> bool:
        return self.age is not None and self.age < ADULT_AGE

    @property
    def level(self) -> AnomalyLevel:
        return max_level(*(f.level for f in self.findings))

    @property
    def codes(self) -> list[str]:
        return [f.code for f in self.findings]

    @property
    def height_invalid(self) -> bool:
        return "height_invalid" in self.codes

    @property
    def needs_confirmation(self) -> bool:
        return any(f.confirm for f in self.findings) or level_rank(self.level) >= level_rank(AnomalyLevel.CRITICAL)

    def reasons(self) -> list[dict[str, Any]]:
        return [f.as_dict() for f in self.findings]


def _pct(part: float, whole: float) -> float:
    return round(part / whole * 100.0, 2) if whole else 0.0


def _days(n: int) -> str:
    return "1 day" if n == 1 else f"{n} days"


def evaluate_metrics(
    age: int | None,
    recent: Iterable[Reading],
    height_cm: float,
    weight_kg: float,
    *,
    today: date | None = None,
    reference_height: float | None = None,
) -> Evaluation:
    """Deterministic anomaly rules for one daily reading.

    ``recent`` = readings of the previous 30 days (today's own entry excluded). ``reference_height`` (the
    profile height) is used for the height checks when there are no recent readings.

    height  adult (>= 21): change > 2 cm vs recent median -> WARNING (re-measure); > 5 cm -> logged as invalid
            younger:      > 3 cm gain within 7 days or > 2 cm loss -> WARNING
    weight  day-over-day > 3 % (minor 2 %) -> WARNING + confirm; >= 5 % within 7 days -> WARNING;
            >= 10 % loss within 30 days (minor 7 %) -> CRITICAL; >= 5 % loss within 3 days -> CRITICAL
    BMI     adult: < 16 CRITICAL, < 15 LIFE_THREATENING, >= 40 WARNING, >= 50 CRITICAL
            12-17: < 15.5 CRITICAL, < 14 LIFE_THREATENING
    """
    today = today or date.today()
    start = today - timedelta(days=30)
    window = sorted((r for r in recent if start <= r.recorded_on < today), key=lambda r: r.recorded_on)
    minor = age is not None and age < ADULT_AGE
    W, C, LT = AnomalyLevel.WARNING, AnomalyLevel.CRITICAL, AnomalyLevel.LIFE_THREATENING
    findings: list[Finding] = []
    effective_height = height_cm

    # ---------------------------------------------------------------- height
    valid = [r for r in window if r.height_valid]
    ref: float | None = None
    if valid:
        ref = float(statistics.median(r.height_cm for r in valid))
    elif reference_height:
        ref = float(reference_height)
    if ref is not None:
        diff = round(height_cm - ref, 1)
        if age is None or age >= ADULT_HEIGHT_AGE:
            if abs(diff) > 5:
                effective_height = ref
                findings.append(Finding(
                    "height_invalid", W, "Height reading looks off",
                    f"{fmt_num(height_cm)} cm is {fmt_num(abs(diff))} cm away from your usual {fmt_num(ref)} cm. "
                    f"Adult height doesn't change like that, so we saved it as an invalid height and kept "
                    f"{fmt_num(ref)} cm for your BMI. Please re-measure (shoes off, heels against a wall).",
                ))
            elif abs(diff) > 2:
                findings.append(Finding(
                    "height_change", W, "Please re-measure your height",
                    f"That's {fmt_num(abs(diff))} cm {'more' if diff > 0 else 'less'} than your usual "
                    f"{fmt_num(ref)} cm. Posture and time of day shift height a little, but more than 2 cm is "
                    "unusual for adults.",
                    confirm=True,
                ))
        else:
            week = [r for r in valid if r.recorded_on >= today - timedelta(days=7)]
            if week:
                base = min(week, key=lambda r: r.height_cm)
                gain = round(height_cm - base.height_cm, 1)
                if gain > 3:
                    findings.append(Finding(
                        "growth_jump", W, "That's a big growth jump",
                        f"{fmt_num(gain)} cm taller than {_days((today - base.recorded_on).days)} ago is more than "
                        "usual. Please measure again, ideally with a parent or guardian helping.",
                        confirm=True,
                    ))
            loss = round(ref - height_cm, 1)
            if loss > 2:
                findings.append(Finding(
                    "height_loss", W, "Height went down",
                    f"{fmt_num(loss)} cm shorter than your recent readings ({fmt_num(ref)} cm). Height shouldn't "
                    "go down while you're growing - please measure again with someone helping.",
                    confirm=True,
                ))

    # ---------------------------------------------------------------- weight
    if window:
        last = window[-1]
        if last.recorded_on == today - timedelta(days=1):
            pct = _pct(abs(weight_kg - last.weight_kg), last.weight_kg)
            limit = 2.0 if minor else 3.0
            if pct > limit:
                findings.append(Finding(
                    "weight_day_change", W, "Big change since yesterday",
                    f"Your weight changed by {pct:.1f}% since yesterday ({fmt_num(last.weight_kg)} → "
                    f"{fmt_num(weight_kg)} kg). Scales, clothing and time of day can make a difference - "
                    "please check the number before saving.",
                    confirm=True,
                ))
        week = [r for r in window if r.recorded_on >= today - timedelta(days=7)]
        if week:
            ref_w = max(week, key=lambda r: abs(weight_kg - r.weight_kg) / r.weight_kg)
            pct = _pct(abs(weight_kg - ref_w.weight_kg), ref_w.weight_kg)
            if pct >= 5:
                ago = _days((today - ref_w.recorded_on).days)
                if weight_kg < ref_w.weight_kg:
                    title, what = "Noticeable weight loss this week", f"{pct:.1f}% less than {ago} ago"
                else:
                    title, what = "Noticeable weight gain this week", f"{pct:.1f}% more than {ago} ago"
                advice = ("If you didn't plan this, please talk to your parent or guardian and a doctor."
                          if minor else "If this wasn't intentional, please consult a healthcare professional.")
                findings.append(Finding("weight_week_change", W, title, f"That's {what}. {advice}"))
        losses = [(r, _pct(r.weight_kg - weight_kg, r.weight_kg)) for r in window]
        r30, loss30 = max(losses, key=lambda t: t[1])
        if loss30 >= (7.0 if minor else 10.0):
            advice = ("That's a lot for a growing body - please talk to your parent or guardian and see a doctor soon."
                      if minor else "Please consult a healthcare professional soon.")
            findings.append(Finding(
                "weight_loss_30d", C, "Significant weight loss",
                f"You've lost {loss30:.1f}% of your body weight within 30 days ({fmt_num(r30.weight_kg)} → "
                f"{fmt_num(weight_kg)} kg). {advice}",
            ))
        three = [(r, p) for r, p in losses if r.recorded_on >= today - timedelta(days=3)]
        if three:
            r3, loss3 = max(three, key=lambda t: t[1])
            if loss3 >= 5:
                findings.append(Finding(
                    "weight_loss_3d", C, "Very rapid weight loss",
                    f"{loss3:.1f}% in {_days((today - r3.recorded_on).days)} ({fmt_num(r3.weight_kg)} → "
                    f"{fmt_num(weight_kg)} kg) is faster than bodies normally change. Please re-check the scale; "
                    "if it's right, consult a healthcare professional soon"
                    + (" and tell your parent or guardian." if minor else "."),
                ))

    # ---------------------------------------------------------------- BMI (from the trusted height)
    value = compute_bmi(effective_height, weight_kg)
    if age is None or age >= ADULT_AGE:
        if value < 15:
            findings.append(Finding(
                "bmi_very_low", LT, "Your BMI is in a dangerous range",
                f"A BMI of {value:.1f} is very low and can be dangerous for your heart and body. Please see a "
                f"healthcare professional as soon as possible. If you feel faint, weak or unwell, call {EMERGENCY_HINT}.",
            ))
        elif value < 16:
            findings.append(Finding(
                "bmi_low", C, "Your BMI is very low",
                f"A BMI of {value:.1f} is well below the healthy range (18.5-24.9). Please consult a healthcare "
                "professional before training hard.",
            ))
        if value >= 50:
            findings.append(Finding(
                "bmi_very_high", C, "Medical guidance needed before training",
                f"With a BMI of {value:.1f}, intense exercise can strain your heart and joints. Please consult a "
                "healthcare professional for a safe plan before training.",
            ))
        elif value >= 40:
            findings.append(Finding(
                "bmi_high", W, "Medical clearance recommended",
                f"With a BMI of {value:.1f}, please get medical clearance before starting intense training. "
                "Low-impact movement like walking or swimming is a great start.",
            ))
    elif 12 <= age <= 17:
        if value < 14:
            findings.append(Finding(
                "bmi_very_low", LT, "This reading needs urgent attention",
                f"A BMI of {value:.1f} is very low for your age and can be dangerous. Please tell your parent or "
                f"guardian right away and see a doctor as soon as possible. If you feel faint or unwell, call {EMERGENCY_HINT}.",
            ))
        elif value < 15.5:
            findings.append(Finding(
                "bmi_low", C, "Please check in with a doctor",
                f"A BMI of {value:.1f} is low for your age. Please talk to your parent or guardian and see a doctor "
                "before training hard.",
            ))

    findings.sort(key=lambda f: -level_rank(f.level))
    return Evaluation(height_cm, weight_kg, effective_height, value, age, findings)


def recommendation_for(level: Any, minor: bool) -> str:
    lv = as_level(level)
    if lv == AnomalyLevel.LIFE_THREATENING:
        return (f"Please see a healthcare professional as soon as possible. If you feel unwell or in danger, call "
                f"{EMERGENCY_HINT} now." + (" Tell your parent or guardian right away." if minor else ""))
    if lv == AnomalyLevel.CRITICAL:
        return "Please consult a healthcare professional soon" + (" and let your parent or guardian know." if minor else ".")
    if lv == AnomalyLevel.WARNING:
        return "Re-measure to be sure. If the change wasn't intentional, consider talking to a healthcare professional."
    return ""


def deterministic_note(level: Any, minor: bool) -> str:
    """Shown instead of the AI assessment when every AI provider is offline."""
    lv = as_level(level)
    if lv == AnomalyLevel.LIFE_THREATENING:
        return ("This reading may be dangerous, so gym activities are paused for your safety. Please see a healthcare "
                f"professional as soon as possible; if you feel unwell or in danger, call {EMERGENCY_HINT}.")
    if lv == AnomalyLevel.CRITICAL:
        return ("This reading needs attention. Please consult a healthcare professional soon"
                + (" and let your parent or guardian know." if minor else ".") + " Take it easy until then.")
    if lv == AnomalyLevel.WARNING:
        return ("Some values changed more than usual. Re-measure if you can - and if the change wasn't intentional, "
                "consider talking to a healthcare professional.")
    return ("Reading saved. Logging at the same time each day (ideally in the morning) gives you the clearest "
            "trend - small daily ups and downs are normal.")


def alert_text(ev: Evaluation) -> tuple[str, str]:
    top = ev.findings[0]
    parts = [f.message for f in ev.findings if level_rank(f.level) >= level_rank(AnomalyLevel.WARNING)]
    reading = f"Reading: {fmt_num(ev.height_cm)} cm, {fmt_num(ev.weight_kg)} kg, BMI {ev.bmi:.1f}."
    return top.title[:160], " ".join(parts + [reading])


# ============================================================================ confirmation tokens
CONFIRM_MAX_AGE = 30 * 60


def _confirm_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt="ironpulse-health-confirm")


def make_confirm_token(user_id: int, entry: MetricEntry) -> str:
    return _confirm_serializer().dumps({
        "u": user_id, "e": entry.id, "d": entry.recorded_on.isoformat(),
        "h": float(entry.height_cm), "w": float(entry.weight_kg),
    })


def read_confirm_token(token: str | None, user_id: int, *, max_age: int = CONFIRM_MAX_AGE) -> dict[str, Any] | None:
    """Signed, user-bound, short-lived token carrying the values that need confirmation."""
    if not token or len(token) > 2000:
        return None
    try:
        data = _confirm_serializer().loads(token, max_age=max_age)
    except BadSignature:  # also covers SignatureExpired
        return None
    if not isinstance(data, dict) or data.get("u") != user_id:
        return None
    return data


def token_matches(data: dict[str, Any], entry: MetricEntry) -> bool:
    try:
        return (entry.id == data["e"] and entry.recorded_on.isoformat() == data["d"]
                and abs(float(entry.height_cm) - float(data["h"])) < 1e-6
                and abs(float(entry.weight_kg) - float(data["w"])) < 1e-6)
    except (KeyError, TypeError, ValueError):
        return False


# ============================================================================ profiles
async def ensure_profile(db: AsyncSession, user: User) -> CustomerProfile:
    profile = await db.get(CustomerProfile, user.id)
    if profile is None:
        profile = CustomerProfile(user_id=user.id, wake_time=time(6, 30), sleep_reminders_enabled=True,
                                  activity_suspended=False)
        db.add(profile)
        await db.flush()
    return profile


# Every reader of ONE member's health conditions stops at CONDITIONS_READ_MAX rows, in SQL (LIMIT): members add at most
# 50 themselves and owner intake 4 kinds x 20, so nothing real is cut - but rows from before those caps can never make a
# page, an AI prompt, a live-coach call or a job heavier. Readers that feed safety rules read ``safety_first()``.
CONDITIONS_READ_MAX = 100
CONDITIONS_TEXT_MAX = 4000  # characters of the conditions list that are embedded (feature hashing, off the event loop)
# untagged rows: what exercise safety reads first
_SAFETY_KINDS = (ConditionKind.INJURY, ConditionKind.CONDITION, ConditionKind.ALLERGY, ConditionKind.MEDICATION)


def safety_first() -> tuple[Any, ...]:
    """ORDER BY of a capped conditions read: what the safety rules act on first - the oldest row of every distinct
    tag (the tag is what the exercise, live-coach and AC guardrails read), then the other tagged rows, then injuries,
    conditions, allergies and medications - oldest first. A padded profile - untagged rows, or one tag over and over -
    can't push a safety tag out of the CONDITIONS_READ_MAX rows a reader sees."""
    kind = sa.case(*((HealthCondition.kind == k, i) for i, k in enumerate(_SAFETY_KINDS)), else_=len(_SAFETY_KINDS))
    repeat = sa.and_(HealthCondition.tag.is_not(None),
                     sa.func.row_number().over(partition_by=HealthCondition.tag, order_by=HealthCondition.id) > 1)
    return HealthCondition.tag.is_(None), repeat, kind, HealthCondition.id


async def condition_tags(db: AsyncSession, customer_id: int) -> list[str]:
    """The member's distinct safety tags (sorted): at most CONDITIONS_READ_MAX of them (DISTINCT + LIMIT in SQL; the
    taxonomy has far fewer), so a tag repeated on many rows never pushes another one out."""
    rows = await db.execute(
        select(HealthCondition.tag).distinct()
        .where(HealthCondition.customer_id == customer_id, HealthCondition.tag.is_not(None))
        .order_by(HealthCondition.tag).limit(CONDITIONS_READ_MAX)
    )
    return sorted({t for (t,) in rows.all() if t})


# ---------------------------------------------------------------------------- the per-member cap (race-safe)
# A member's profile holds at most MAX_CONDITIONS conditions / allergies / medications / injuries in total, whoever
# adds them (the member's form, "remember this?" in the assistant or live coach, the gym's registration or health
# edit). Every writer takes ``lock_conditions`` first - a transaction-scoped advisory lock per member - so a count
# followed by an insert can never pass the cap, however many requests arrive at the same moment.
MAX_CONDITIONS = 50
CONDITION_NAME_MAX = 120
CONDITION_NOTES_MAX = 255


def conditions_full_message(cap: int | None = None) -> str:
    cap = MAX_CONDITIONS if cap is None else cap
    return (f"Your profile already lists {cap} conditions, allergies, medications and injuries - the most it can hold. "
            "Remove one you no longer need on your health profile, then add this one.")


def conditions_lock_key(user_id: int) -> int:
    import hashlib

    digest = hashlib.blake2b(f"health_conditions:{int(user_id)}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


async def lock_conditions(db: AsyncSession, user_id: int) -> None:
    """Serialise every change of one member's conditions list until this transaction ends."""
    await db.execute(sa.text("SELECT pg_advisory_xact_lock(:k)"), {"k": conditions_lock_key(user_id)})


async def conditions_held(db: AsyncSession, user_id: int, *, limit: int | None = None) -> int:
    """How many rows the member holds, counted up to ``limit`` (default MAX_CONDITIONS) - a bounded count."""
    limit = MAX_CONDITIONS if limit is None else limit
    inner = select(HealthCondition.id).where(HealthCondition.customer_id == user_id).limit(limit).subquery()
    return int(await db.scalar(select(sa.func.count()).select_from(inner)) or 0)


@dataclass(slots=True)
class ConditionAdd:
    status: str  # added | known | full
    condition: HealthCondition | None = None


_AUTO_TAG: Any = object()  # add_condition: derive the tag from the name (the shared normaliser)


async def add_condition(db: AsyncSession, user_id: int, *, kind: ConditionKind, name: str, notes: str | None = None,
                        source: str = "self", tag: Any = _AUTO_TAG) -> ConditionAdd:
    """Add one condition / allergy / medication / injury under the member's cap lock: 'known' when the same kind and
    name (any case) is listed already, 'full' at MAX_CONDITIONS, else 'added' (flushed). ``tag`` defaults to the shared
    normaliser (``intake.condition_tag``: the safety tag every guardrail reads). The caller commits - the lock is held
    until then."""
    from app.services.intake import condition_tag

    name = " ".join(str(name or "").split())[:CONDITION_NAME_MAX]
    await lock_conditions(db, user_id)
    existing = (await db.execute(
        select(HealthCondition).where(HealthCondition.customer_id == user_id, HealthCondition.kind == kind,
                                      sa.func.lower(HealthCondition.name) == name.lower()).limit(1)
    )).scalars().first()
    if existing is not None:
        return ConditionAdd("known", existing)
    if await conditions_held(db, user_id) >= MAX_CONDITIONS:
        return ConditionAdd("full")
    row = HealthCondition(customer_id=user_id, kind=kind, name=name,
                          tag=condition_tag(name, kind) if tag is _AUTO_TAG else tag,
                          notes=(str(notes)[:CONDITION_NOTES_MAX] or None) if notes else None, source=source[:20])
    db.add(row)
    await db.flush()
    return ConditionAdd("added", row)


async def after_conditions_change(db: AsyncSession, user: User, change: str, *, embed_fn: Any = None,
                                  notify_member: bool = True) -> bool:
    """Everything that follows a change of the member's conditions list, in the same transaction: the conditions
    embedding (a bounded, safety-first read; hashed off the event loop), the rule-based AC advice and the member's
    notification. Returns whether the plan includes AI - the caller then queues the friendlier AI wording of the AC
    advice (``queue_ac_note``) after its commit. Nothing is cached elsewhere: every AI feature reads the list afresh
    (``app.ai.member_context``), so a removed item is gone from the next answer."""
    import asyncio

    from app.services.subscriptions import plan_access

    profile = await ensure_profile(db, user)
    await db.flush()
    rows = list((await db.execute(
        select(HealthCondition).where(HealthCondition.customer_id == user.id)
        .order_by(*safety_first()).limit(CONDITIONS_READ_MAX)
    )).scalars().all())
    text_ = conditions_text(rows)[:CONDITIONS_TEXT_MAX]
    vector = await asyncio.to_thread(embed_fn or embed, text_) if text_ else None
    await store_health_embedding(db, user.id, vector)
    access = await plan_access(db, user.id)
    await refresh_ac(db, user, profile, [c.tag for c in rows if c.tag])
    if notify_member:
        await notify(
            db, user, "Health profile updated",
            f"{change}. Your coach, safety checks and air-conditioning advice now use the updated list.",
            category=NotificationCategory.ACCOUNT, severity=Severity.INFO, link="/me/health/profile",
        )
    return bool(access.ai)


async def conditions_vector(conditions: Sequence[HealthCondition]) -> list[float] | None:
    """The conditions embedding of an (already capped) list: the text is cut to CONDITIONS_TEXT_MAX characters and
    hashed in a worker thread, so a long list never stalls the event loop for everyone else."""
    import asyncio

    text = conditions_text(conditions)[:CONDITIONS_TEXT_MAX]
    return await asyncio.to_thread(embed, text) if text else None


def conditions_text(conditions: Sequence[HealthCondition]) -> str:
    parts = []
    for c in conditions:
        piece = f"{getattr(c.kind, 'value', c.kind)}: {c.name}"
        if c.tag:
            piece += f" [{TAG_LABELS.get(c.tag, c.tag)}]"
        if c.notes:
            piece += f" - {c.notes}"
        parts.append(piece)
    return "; ".join(parts)


def conditions_embedding(conditions: Sequence[HealthCondition]) -> list[float] | None:
    text = conditions_text(conditions)
    return embed(text) if text else None


async def store_health_embedding(db: AsyncSession, user_id: int, vector: Sequence[float] | None) -> None:
    """Write CustomerProfile.health_embedding with a Core UPDATE.

    ``app.db`` registers pgvector's *binary* asyncpg codec on every connection, while the ORM ``VECTOR`` type
    binds *text*, so ORM writes of vector columns fail. A raw list bound without a SQLAlchemy type goes through
    the registered codec; if no codec is registered, a text -> vector cast is used instead (inside a savepoint)."""
    from sqlalchemy import Text, bindparam, cast, update
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.types import NullType

    from app.models.base import EMBEDDING_DIM
    from pgvector.sqlalchemy import Vector

    table = CustomerProfile.__table__
    stmt = update(table).where(table.c.user_id == user_id)
    if vector is None:
        await db.execute(stmt.values(health_embedding=None))
        return
    values = [float(v) for v in vector]
    try:
        async with db.begin_nested():
            await db.execute(stmt.values(health_embedding=bindparam("emb", values, type_=NullType())))
    except DBAPIError:
        literal = "[" + ",".join(repr(v) for v in values) + "]"
        await db.execute(stmt.values(
            health_embedding=cast(cast(bindparam("emb_text", literal, type_=Text()), Text()), Vector(EMBEDDING_DIM))
        ))


# ============================================================================ metric logging
@dataclass(slots=True)
class MetricLog:
    entry: MetricEntry
    evaluation: Evaluation
    alert: HealthAlert | None


@dataclass(slots=True)
class FinalizeOutcome:
    level: AnomalyLevel
    note: str
    ai_provider: str | None = None
    ai_available: bool = False
    suspended: bool = False
    suspended_by: str | None = None
    escalated: bool = False
    ai_not_repeated: bool = False  # the member's AI safety reviews for today were used up: the rules decided alone


# ---------------------------------------------------------------------------- bounds (approved load-safety limits)
# Health alerts: a reading has ONE alert of its own per source (the rules' and the AI review's), updated in place when
# the day's reading is logged again; every new alert counts towards ALERTS_PER_DAY per member (rolling 24 h) - past it,
# the newest alert of the same kind is updated and shown as unread again instead of adding a row: a safety signal is
# never dropped and the table never grows without bound. AI safety reviews of abnormal readings ('health_metrics', never
# refused by the AI guard): at most REVIEWS_PER_DAY per member and UTC day - past it the rules decide alone (as when the
# AI is offline) and the note says so. Both become tunable in app/core/caps.py once registered there.
ALERTS_CAP = "health_alerts_per_member_day"
ALERTS_PER_DAY = 20
REVIEWS_CAP = "health_reviews_per_member_day"
REVIEWS_PER_DAY = 10
AI_NOT_REPEATED = ("The AI safety review was not repeated: your readings were already reviewed {n} times today. The "
                   "safety rules checked this reading as usual - if you feel unwell, contact a healthcare professional.")


def _cap_value(name: str, default: int) -> int:
    try:
        return caps.limit(name) if name in caps.CAPS else default
    except Exception:  # noqa: BLE001 - a broken setting must not switch a safety bound off
        return default


def alerts_per_day() -> int:
    return _cap_value(ALERTS_CAP, ALERTS_PER_DAY)


def reviews_per_day() -> int:
    return _cap_value(REVIEWS_CAP, REVIEWS_PER_DAY)


def _lock_key(scope: str, user_id: int) -> int:
    import hashlib

    digest = hashlib.blake2b(f"{scope}:{int(user_id)}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


async def lock_metrics(db: AsyncSession, user_id: int) -> None:
    """Serialise every change of one member's readings (log, confirm, the AI review's write-back) until this
    transaction ends: the day's entry is created once, its alert updated once, a reading confirmed once."""
    await db.execute(sa.text("SELECT pg_advisory_xact_lock(:k)"), {"k": _lock_key("metric_entries", user_id)})


def _alert_window() -> Any:
    return caps.since(caps.DAY)


async def add_alert(db: AsyncSession, alert: HealthAlert) -> HealthAlert:
    """Store a new health alert under the member's per-day cap (``alerts_per_day``; a per-member advisory lock makes
    the count race-safe). Past the cap the member's newest alert of the same source and title from the last 24 hours is
    updated (highest level kept, newest words) and shown as unread again instead - and a kind of alert not seen in that
    time is still stored (titles come from short fixed lists, so that stays bounded). Returns the alert that holds it.
    Flushed; the caller commits."""
    await db.execute(sa.text("SELECT pg_advisory_xact_lock(:k)"), {"k": _lock_key("health_alerts", alert.customer_id)})
    cap = alerts_per_day()
    recent = select(HealthAlert.id).where(HealthAlert.customer_id == alert.customer_id,
                                          HealthAlert.created_at >= _alert_window())
    if await caps.count_upto(db, recent, cap + 1) >= cap:
        same = (await db.execute(
            select(HealthAlert).where(HealthAlert.customer_id == alert.customer_id, HealthAlert.source == alert.source,
                                      HealthAlert.title == alert.title, HealthAlert.created_at >= _alert_window())
            .order_by(HealthAlert.id.desc()).limit(1)
        )).scalars().first()
        if same is not None:
            _reopen(same, alert, keep_highest=True)
            await db.flush()
            log.info("health alerts: cap of %s a day reached for member %s - updated alert %s", cap,
                     alert.customer_id, same.id)
            return same
    db.add(alert)
    await db.flush()
    return alert


def _reopen(target: HealthAlert, new: HealthAlert, *, keep_highest: bool) -> None:
    """``target`` takes ``new``'s words and is unread again (the newest of its kind)."""
    target.level = max_level(target.level, new.level) if keep_highest else new.level
    target.title = new.title
    target.message = new.message
    target.recommendation = new.recommendation
    target.guardian_notified = bool(new.guardian_notified)
    target.acknowledged_at = None
    target.created_at = utcnow()
    if new.metric_entry_id is not None:
        target.metric_entry_id = new.metric_entry_id
    if new.wellness_check_id is not None:
        target.wellness_check_id = new.wellness_check_id


async def _entry_alert(db: AsyncSession, entry: MetricEntry, source: str) -> HealthAlert | None:
    """The reading's own alert of ``source`` (the newest) - never one a safety pause refers to, which stays as it was."""
    paused = select(SuspensionEvent.id).where(SuspensionEvent.health_alert_id == HealthAlert.id).exists()
    return (await db.execute(
        select(HealthAlert).where(HealthAlert.metric_entry_id == entry.id, HealthAlert.source == source, ~paused)
        .order_by(HealthAlert.id.desc()).limit(1)
    )).scalars().first()


async def entry_alert(db: AsyncSession, entry: MetricEntry, new: HealthAlert) -> HealthAlert:
    """ONE alert per reading and source: the day's reading logged again updates its alert (new values, unread again)
    instead of adding another; a reading without one gets a new alert (``add_alert``, under the per-day cap)."""
    existing = await _entry_alert(db, entry, new.source)
    if existing is None:
        return await add_alert(db, new)
    _reopen(existing, new, keep_highest=False)
    await db.flush()
    return existing


async def recent_entries(db: AsyncSession, customer_id: int, today: date, days: int = 30) -> list[MetricEntry]:
    stmt = (
        select(MetricEntry)
        .where(MetricEntry.customer_id == customer_id, MetricEntry.recorded_on >= today - timedelta(days=days),
               MetricEntry.recorded_on < today)
        .order_by(MetricEntry.recorded_on)
    )
    return list((await db.execute(stmt)).scalars().all())


async def record_metrics(
    db: AsyncSession, user: User, height_cm: float, weight_kg: float, *, source: str = "manual",
    today: date | None = None,
) -> MetricLog:
    """Run the rules and upsert today's entry. Abnormal input is ALWAYS logged (anomaly fields + HealthAlert - one per
    reading, updated when the day's reading is logged again: ``entry_alert``), with ``confirmed=False`` while a severe
    reading still waits for the member's confirmation. Holds the member's readings lock (``lock_metrics``) until the
    caller's transaction ends, so posts at the same moment never create the day twice or add duplicate alerts."""
    today = today or local_today(user.timezone)
    await lock_metrics(db, user.id)
    profile = await ensure_profile(db, user)
    readings = [reading_of(e) for e in await recent_entries(db, user.id, today)]
    ev = evaluate_metrics(user.age(today), readings, height_cm, weight_kg, today=today,
                          reference_height=profile.height_cm)
    entry = await db.scalar(
        select(MetricEntry).where(MetricEntry.customer_id == user.id, MetricEntry.recorded_on == today)
    )
    if entry is None:
        entry = MetricEntry(customer_id=user.id, recorded_on=today)
        db.add(entry)
    entry.height_cm = float(height_cm)
    entry.weight_kg = float(weight_kg)
    entry.bmi = ev.bmi
    entry.source = source if source in ("manual", "ocr", "owner") else "manual"
    entry.anomaly_level = ev.level
    entry.anomaly_reasons = ev.reasons()
    entry.ai_note = None
    entry.confirmed = not ev.needs_confirmation
    await db.flush()
    alert = None
    if level_rank(ev.level) >= level_rank(AnomalyLevel.WARNING):
        title, message = alert_text(ev)
        alert = await entry_alert(db, entry, HealthAlert(
            customer_id=user.id, metric_entry_id=entry.id, level=ev.level, title=title, message=message,
            recommendation=recommendation_for(ev.level, ev.minor), source="rule", guardian_notified=False))
    return MetricLog(entry, ev, alert)


async def _review_inputs(db: AsyncSession, user: User, entry: MetricEntry, level: AnomalyLevel,
                         findings: list[dict[str, Any]]) -> dict[str, Any]:
    """``health_ai.assess_metrics`` arguments for a reading (reads its last 14 days and the member's condition tags)."""
    from app.ai.member_context import load_member_context

    age = user.age(entry.recorded_on)
    history = [(r.recorded_on.isoformat(), r.height_cm, r.weight_kg, compute_bmi(r.height_cm, r.weight_kg))
               for r in (reading_of(e) for e in await recent_entries(db, user.id, entry.recorded_on, days=14))]
    member = await load_member_context(db, user)  # the ONE member context: the tags and the profile block agree
    return {"age": age, "minor": age is not None and age < ADULT_AGE, "tags": member.all_tags,
            "history": history, "height_cm": float(entry.height_cm), "weight_kg": float(entry.weight_kg),
            "bmi": float(entry.bmi), "level": level, "findings": findings,
            "profile": "" if member.empty else member.block(max_chars=health_ai.PROFILE_PROMPT_CHARS,
                                                             include_basics=False)}


async def _ai_review(user: User, inputs: dict[str, Any]) -> tuple[Any, str | None, bool]:
    """Run the AI safety review ('health_metrics') of a reading - at most ``reviews_per_day()`` per member and UTC day
    (``guard.reserve``: race-safe across processes). Returns (assessment, provider, not_repeated): (None, None, False)
    when every provider is offline (the use is given back), (None, None, True) past the daily allowance. Called with no
    transaction open: nothing is held while the model thinks."""
    from app.ai.guard import guard as ai_guard

    reservation = await ai_guard.reserve(user.id, REVIEWS_CAP, reviews_per_day())
    if not reservation.taken:
        log.info("AI safety review of member %s not repeated: %s today", user.id, reservation.count)
        return None, None, True
    try:
        assessment, provider = await health_ai.assess_metrics(**inputs)
    except AIUnavailableError:
        await ai_guard.release(reservation)  # no review came of it: the member keeps the allowance
        return None, None, False
    except BaseException:
        await ai_guard.release(reservation)
        raise
    return assessment, provider, False


def compose_note(level: AnomalyLevel, minor: bool, assessment: Any) -> str:
    """The note shown with a reading. AI text is used only when it doesn't soften a rule-detected danger."""
    severe = level_rank(level) >= level_rank(AnomalyLevel.CRITICAL)
    if assessment is None or not assessment.assessment or (severe and not assessment.consult_professional):
        # AI offline, empty, or downplaying a rule-detected danger -> the AI can never soften the rules' message
        return deterministic_note(level, minor)[:2000]
    note = assessment.assessment
    if severe and "professional" not in note.lower() and "doctor" not in note.lower():
        note += " " + recommendation_for(level, minor)
    elif assessment.consult_professional and "professional" not in note.lower() and "doctor" not in note.lower():
        note += " Consider checking in with a healthcare professional."
    return note[:2000]


# ---------------------------------------------------------------------------- the AI note of a normal reading
NOTE_JOB_KIND = "health_ai_note"
# the AI policy of that note (app/ai/policy.py): the 'notes' quota group, background, paused while the member is away -
# a friendly sentence for later, never the 'health_metrics' safety review (which is never refused and limits nobody)
NOTE_FEATURE = "health_note"
NOTE_IN_FLIGHT = (JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.PAUSED)


def note_job_key(entry_id: int) -> str:
    """One note job per reading (one reading per member and day): re-logs while it is queued share it."""
    return f"{NOTE_JOB_KIND}:{entry_id}"


async def queue_ai_note(db: AsyncSession, user: User, entry: MetricEntry, *,
                        owner: AIOwner | None = None) -> Job | None:
    """Queue the friendly AI note of a normal reading (the ``health_ai_note`` job). The deterministic note is already
    on the reading; the caller commits.

    * Only a membership with AI gets an AI-written note: everyone else keeps the deterministic one - no AI call at all.
    * While the reading's note job is queued, running or paused, nothing new is queued or charged: it notes the
      entry's newest values when it runs (a paused one goes back in the queue - the member is here).
    * Otherwise the post is judged by the AI guard (``NOTE_FEATURE`` - background jobs are never judged, so the click
      is): a refusal (quota, cooldown) silently keeps the deterministic note and queues nothing. The admission travels
      in the job payload (``payload["admission"]``) and the job settles it: kept when an AI note came of it, refunded
      otherwise. Returns the reading's note job, or None when nothing is queued."""
    from app.ai import guard as guard_module
    from app.services.subscriptions import plan_access

    key = note_job_key(entry.id)
    existing = await db.scalar(select(Job).where(Job.dedupe_key == key))
    if existing is not None and existing.status in NOTE_IN_FLIGHT:
        if existing.status == JobStatus.PAUSED:  # admitted when it was queued: back in the queue, no new charge
            return await enqueue(db, NOTE_JOB_KIND, dict(existing.payload or {}), dedupe_key=key, max_attempts=1,
                                 user_id=user.id)
        return existing
    if not (await plan_access(db, user.id)).ai:
        return None
    admission = None
    try:
        admission = await guard_module.guard.admit(owner if owner is not None else current_owner(), NOTE_FEATURE)
    except AIUnavailableError as exc:  # AIRejected: the deterministic note stays - nothing to tell the member
        log.info("AI note of reading %s skipped: %s", entry.id, getattr(exc, "code", exc))
        return None
    except Exception:  # noqa: BLE001 - a broken guard must not switch the notes off
        log.exception("AI guard failed for the AI note of user %s - admitting it", user.id)
    payload: dict[str, Any] = {"entry_id": entry.id}
    if admission is not None:
        payload["admission"] = admission.to_payload()
    try:
        job = await enqueue(db, NOTE_JOB_KIND, payload, dedupe_key=key, max_attempts=1, user_id=user.id)
    except BaseException:
        await guard_module.guard.settle(admission, ok=False)  # nothing queued: nobody pays for it
        raise
    queued = (job.payload or {}).get("admission") if job is not None else None
    if admission is not None and (not isinstance(queued, dict) or queued.get("ref") != payload["admission"]["ref"]):
        await guard_module.guard.settle(admission, ok=False)  # a concurrent post queued the note first: one charge
    return job


async def _settle_note(payload: dict[str, Any], user_id: int | None, *, ok: bool) -> None:
    """Settle the AI-guard charge of the post that queued this note (``payload["admission"]``): ``ok=False`` gives the
    member their 'notes' unit back. Once in total over retries, resumes and processes. Never raises."""
    admission = payload.get("admission") if isinstance(payload, dict) else None
    if not isinstance(admission, dict):
        return
    if user_id is not None and admission.get("user_id") != user_id:
        log.warning("AI note job of user %s carries another user's AI admission - left alone", user_id)
        return
    from app.ai import guard as guard_module

    await guard_module.guard.settle_payload(admission, ok)


async def _note_reading(db: AsyncSession, entry: MetricEntry, user: User) -> bool:
    """The note job's work: True when an AI note was written on the reading."""
    from app.services.subscriptions import plan_access

    # reviews the entry's CURRENT values (a queued job is shared by re-logs of the same day); readings that have
    # become abnormal since are handled synchronously by finalize_metrics and are skipped here
    if not entry.confirmed or level_rank(entry.anomaly_level) >= level_rank(AnomalyLevel.WARNING):
        return False
    if not (await plan_access(db, user.id)).ai:  # no AI in the membership (any more): the deterministic note stays
        return False
    level = as_level(entry.anomaly_level)
    inputs = await _review_inputs(db, user, entry, level,
                                  [f for f in (entry.anomaly_reasons or []) if isinstance(f, dict)])
    reviewed = (entry.height_cm, entry.weight_kg)
    await db.commit()  # neither a transaction nor a pooled connection is held while the model thinks
    try:
        assessment, _provider = await health_ai.assess_metrics(**inputs, feature=NOTE_FEATURE)
    except AICancelled:
        raise  # stopped because the member is gone - the job's exit policy decides what happens next
    except AIUnavailableError:
        return False
    if not assessment.assessment:
        return False
    stmt = (select(MetricEntry).where(MetricEntry.id == entry.id).with_for_update()
            .execution_options(populate_existing=True))
    fresh = (await db.execute(stmt)).scalar_one_or_none()
    if (fresh is None or (fresh.height_cm, fresh.weight_kg) != reviewed or not fresh.confirmed
            or level_rank(fresh.anomaly_level) != level_rank(level)):
        return False  # logged again while the AI was busy: never a note about other values
    fresh.ai_note = compose_note(level, inputs["minor"], assessment)
    return True


@job_handler(NOTE_JOB_KIND, on_exit="pause")  # a friendly note for later: deferred while the member is away
async def ai_note_job(db: AsyncSession, payload: dict[str, Any]) -> None:
    """Background AI note for a normal reading (never escalates: the rules found nothing to escalate), for a membership
    with AI, under ``NOTE_FEATURE``. The post's guard admission (``queue_ai_note``) is settled here: kept when an AI note
    was written, refunded when none was (AI offline, no AI in the membership any more, the reading changed or became
    abnormal, the run failed). A run stopped because the member left keeps it for the resumed run."""
    set_result_event(notify=False)  # the note appears on the reading itself: no "Your health note - done" island note
    try:
        entry = await db.get(MetricEntry, int(payload.get("entry_id") or 0))
    except (TypeError, ValueError):
        return
    user = await db.get(User, entry.customer_id) if entry is not None else None
    run = current_run()
    user_id = user.id if user is not None else (run.user_id if run is not None else None)
    if entry is None or user is None or user.deleted_at is not None:
        await _settle_note(payload, user_id, ok=False)
        return
    try:
        noted = await _note_reading(db, entry, user)
    except AICancelled:
        raise  # its AI work was stopped (the job is being paused or cancelled): the resumed run settles the post
    except Exception:
        if run is None or (run.last_attempt and not run.stopping):  # no retry follows: no note came of the post
            await _settle_note(payload, user_id, ok=False)
        raise
    await _settle_note(payload, user_id, ok=noted)


async def finalize_metrics(db: AsyncSession, user: User, entry: MetricEntry, *, alert: HealthAlert | None = None,
                           defer_ai: bool = True) -> FinalizeOutcome:
    """A reading is final (no confirmation needed, or confirmed): notifications, suspension, AI layer.

    The rule level is authoritative and never waits for the AI: the rules' outcome (a safety pause for a
    life-threatening reading, the notices of a critical one) and the deterministic note are saved and COMMITTED first -
    so the member's readings lock (``lock_metrics``) is released and nothing is held while the model thinks. Then the
    AI safety review ('health_metrics', at most ``reviews_per_day()`` per member and day - past it the note says the
    review was not repeated) may add caution or escalate to a suspension (actor "ai") through
    ``health_ai.escalation_allowed``, but can never downgrade a rule decision; what it said is written back only onto
    the same, still confirmed reading. Normal readings are saved at once and get their AI note from the
    ``health_ai_note`` job (``defer_ai=False`` runs it inline). The caller commits the write-back."""
    today = entry.recorded_on
    age = user.age(today)
    minor = age is not None and age < ADULT_AGE
    profile = await ensure_profile(db, user)
    findings = [f for f in (entry.anomaly_reasons or []) if isinstance(f, dict)]
    level = as_level(entry.anomaly_level)
    entry.confirmed = True
    if "height_invalid" not in reason_codes(findings):
        profile.height_cm = float(entry.height_cm)
    profile.weight_kg = float(entry.weight_kg)

    if defer_ai and level_rank(level) < level_rank(AnomalyLevel.WARNING):
        # nothing for the AI to escalate: save instantly; with AI in the membership (and the post admitted by the AI
        # guard) a background job adds the friendly AI note
        entry.ai_note = deterministic_note(level, minor)
        await queue_ai_note(db, user, entry)
        return FinalizeOutcome(level=level, note=entry.ai_note)

    # 1. the rules' decision - saved before (and whatever) the AI says
    entry.ai_note = deterministic_note(level, minor)[:2000]
    outcome = FinalizeOutcome(level=level, note=entry.ai_note)
    await _rule_outcome(db, user, profile, level, findings, minor, alert, outcome)
    inputs = await _review_inputs(db, user, entry, level, findings)
    reviewed = (float(entry.height_cm), float(entry.weight_kg))
    await db.commit()

    # 2. the AI safety review: no transaction open, bounded per member and day
    assessment, provider, not_repeated = await _ai_review(user, inputs)

    # 3. what it said, onto the same reading only (logged again or changed while the model thought: left alone)
    await lock_metrics(db, user.id)
    fresh = (await db.execute(
        select(MetricEntry).where(MetricEntry.id == entry.id).with_for_update().execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if (fresh is None or not fresh.confirmed or (float(fresh.height_cm), float(fresh.weight_kg)) != reviewed
            or as_level(fresh.anomaly_level) != level):
        return outcome
    if not_repeated:
        fresh.ai_note = f"{deterministic_note(level, minor)} {AI_NOT_REPEATED.format(n=reviews_per_day())}"[:2000]
        outcome.note, outcome.ai_not_repeated = fresh.ai_note, True
        return outcome
    if assessment is None:  # every provider offline: the deterministic note stays
        return outcome
    fresh.ai_note = compose_note(level, minor, assessment)
    outcome.note, outcome.ai_provider, outcome.ai_available = fresh.ai_note, provider, True
    if level != AnomalyLevel.LIFE_THREATENING and health_ai.escalation_allowed(level, assessment):
        reason = assessment.reason or assessment.assessment or "The AI safety review flagged this reading as potentially life-threatening."
        ai_alert = await entry_alert(db, fresh, HealthAlert(
            customer_id=user.id, metric_entry_id=fresh.id, level=AnomalyLevel.LIFE_THREATENING,
            title="AI safety review: activities paused", message=reason,
            recommendation=recommendation_for(AnomalyLevel.LIFE_THREATENING, minor), source="ai", guardian_notified=False,
        ))
        fresh.anomaly_level = AnomalyLevel.LIFE_THREATENING
        fresh.anomaly_reasons = findings + [{
            "code": "ai_escalation", "level": AnomalyLevel.LIFE_THREATENING.value, "title": "AI safety review",
            "message": reason, "confirm": False, "source": "ai",
        }]
        await suspend_customer(db, user, reason=f"AI safety review: {reason}", actor="ai", alert=ai_alert)
        outcome.level = AnomalyLevel.LIFE_THREATENING
        outcome.suspended, outcome.suspended_by, outcome.escalated = True, "ai", True
    return outcome


async def _rule_outcome(db: AsyncSession, user: User, profile: CustomerProfile, level: AnomalyLevel,
                        findings: list[dict[str, Any]], minor: bool, alert: HealthAlert | None,
                        outcome: FinalizeOutcome) -> None:
    """What the rules decide for a final abnormal reading: a safety pause (life-threatening), the member's notice and
    a minor's guardian e-mail (critical), a minor's notice (warning)."""
    if level == AnomalyLevel.LIFE_THREATENING:
        top = findings[0] if findings else {}
        await suspend_customer(db, user, reason=f"{top.get('title', 'Health reading')}: {top.get('message', '')}".strip(),
                               actor="rule", alert=alert)
        outcome.suspended, outcome.suspended_by = True, "rule"
    elif level == AnomalyLevel.CRITICAL:
        title = findings[0].get("title", "Health reading needs attention") if findings else "Health reading needs attention"
        await notify(
            db, user, title,
            f"{findings[0].get('message', '') if findings else ''} {recommendation_for(level, minor)}".strip(),
            category=NotificationCategory.HEALTH, severity=Severity.DANGER, link="/me/health",
        )
        if minor and profile.guardian_email:
            await email_address(
                db, profile.guardian_email, f"Health check for {first_name(user)}",
                f"{user.full_name}'s latest reading on IronPulse needs attention: {title}. Please make sure "
                f"{first_name(user)} sees a healthcare professional soon. If {first_name(user)} seems unwell, "
                f"call {EMERGENCY_HINT}.",
                recipient=profile.guardian_name,
            )
            if alert is not None:
                alert.guardian_notified = True
    elif level == AnomalyLevel.WARNING and minor:
        title = findings[0].get("title", "A reading to double-check") if findings else "A reading to double-check"
        await notify(
            db, user, title,
            "One of today's numbers changed more than usual. Please measure again with a parent or guardian, and "
            "talk to a doctor if the change wasn't planned.",
            category=NotificationCategory.HEALTH, severity=Severity.WARNING, link="/me/health",
        )
        if alert is not None and profile.guardian_email:
            alert.guardian_notified = True  # notify() CCs the guardian for minors


# ============================================================================ safety suspension
SUSPEND_ACTORS = ("rule", "ai", "owner", "superadmin")
REACTIVATION_STATUSES = (SubscriptionStatus.ACTIVE, SubscriptionStatus.EXPIRED, SubscriptionStatus.CANCELLED,
                         SubscriptionStatus.SUSPENDED)


async def _active_gym_owners(db: AsyncSession, customer_id: int) -> tuple[list[User], list[int]]:
    today = date.today()
    rows = (await db.execute(
        select(Gym.id, Gym.owner_id)
        .join(Subscription, Subscription.gym_id == Gym.id)
        .where(Subscription.customer_id == customer_id, Subscription.status == SubscriptionStatus.ACTIVE,
               or_(Subscription.ends_on.is_(None), Subscription.ends_on >= today))
    )).all()
    gym_ids = sorted({gid for gid, _ in rows})
    owner_ids = {oid for _, oid in rows}
    if not owner_ids:
        return [], gym_ids
    owners = (await db.execute(select(User).where(User.id.in_(owner_ids), User.deleted_at.is_(None)))).scalars().all()
    return list(owners), gym_ids


async def _assigned_trainers(db: AsyncSession, customer_id: int) -> list[User]:
    sub = (
        select(Employee.user_id)
        .join(TrainerClient, TrainerClient.employee_id == Employee.id)
        .where(TrainerClient.customer_id == customer_id, TrainerClient.active.is_(True),
               Employee.status == EmploymentStatus.ACTIVE, Employee.deleted_at.is_(None), Employee.user_id.is_not(None))
    )
    return list((await db.execute(select(User).where(User.id.in_(sub), User.deleted_at.is_(None)))).scalars().all())


async def suspend_customer(db: AsyncSession, customer: User, reason: str, actor: str,
                           alert: HealthAlert | None = None) -> SuspensionEvent | None:
    """Pause a customer's gym activities for safety. ``actor``: "rule" | "ai" (or "owner:<id>" / "superadmin:<id>").

    Sets CustomerProfile.activity_suspended, writes a SuspensionEvent linked to a HealthAlert (created when not
    given) and notifies the customer (supportive), the guardian of a minor (CC + explicit e-mail), the owners of
    gyms with an active subscription and the assigned trainers. Idempotent: returns None when already paused.
    Only an admin can reactivate (``reactivate_customer``). The caller commits."""
    kind, _, ref = (actor or "").partition(":")
    kind = kind.strip().lower()
    if kind not in SUSPEND_ACTORS:
        raise ValueError(f"unknown suspension actor {actor!r}")
    reason = " ".join((reason or "").split()) or "A health reading needs medical attention."
    profile = await ensure_profile(db, customer)
    if profile.activity_suspended:
        return None
    age = customer.age()
    minor = age is not None and age < ADULT_AGE
    if alert is None:
        alert = HealthAlert(customer_id=customer.id, level=AnomalyLevel.LIFE_THREATENING,
                            title="Gym activities paused for your safety", message=reason,
                            recommendation=recommendation_for(AnomalyLevel.LIFE_THREATENING, minor),
                            source="ai" if kind == "ai" else "rule", guardian_notified=False)
        db.add(alert)
        await db.flush()
    elif alert.id is None:
        db.add(alert)
        await db.flush()
    profile.activity_suspended = True
    profile.suspension_reason = reason[:1000]
    profile.suspended_at = utcnow()
    profile.suspended_by = (actor or kind)[:40]
    event = SuspensionEvent(customer_id=customer.id, action="suspend", reason=reason, actor=kind,
                            actor_user_id=int(ref) if ref.isdigit() else None, health_alert_id=alert.id)
    db.add(event)
    await db.flush()

    name, first = customer.full_name, first_name(customer)
    if minor:
        body = ("We noticed something that may need urgent medical attention, so your gym activities are paused for "
                "now. This is not a punishment - your health comes first. Please tell your parent or guardian and see "
                f"a doctor soon. If you feel unwell or in danger, call {EMERGENCY_HINT} right away. A gym admin can "
                f"switch activities back on once it's safe. Details: {reason}")
    else:
        body = ("We noticed something that may need urgent medical attention, so your gym activities are paused for "
                "now. This is not a punishment - your wellbeing comes first. Please consult a healthcare professional "
                f"as soon as you can. If you feel unwell or in danger, call {EMERGENCY_HINT} right away. A gym admin "
                f"can reactivate your account once it's safe. Details: {reason}")
    await notify(db, customer, "Your gym activities are paused for your safety", body,
                 category=NotificationCategory.HEALTH, severity=Severity.DANGER, link="/me/health")
    if minor and profile.guardian_email:
        await email_address(
            db, profile.guardian_email, f"Safety alert for {first}",
            f"IronPulse paused {name}'s gym activities after a health check that may need urgent medical attention: "
            f"{reason} Please make sure {first} sees a healthcare professional soon. If {first} seems unwell or in "
            f"danger, call {EMERGENCY_HINT} immediately. Activities stay paused until a gym admin reactivates them.",
            recipient=profile.guardian_name,
        )
        alert.guardian_notified = True
    owners, gym_ids = await _active_gym_owners(db, customer.id)
    trainers = await _assigned_trainers(db, customer.id)
    by = {"rule": "by our safety rules", "ai": "by the AI safety review"}.get(kind, "by an admin")
    # the owners, then the assigned trainers: ONE bulk send (``notify_bulk``), never one notify per person
    staff_notes = [Note(
        owner, f"Safety pause: {name}",
        f"{name}'s gym activities were paused automatically {by} after a health reading flagged as potentially "
        "life-threatening. Please don't train this member until a healthcare professional has cleared them. "
        "You can reactivate from the member page once it's safe.",
        link=f"/owner/customers/{customer.id}", category=NotificationCategory.HEALTH, severity=Severity.DANGER,
    ) for owner in owners]
    staff_notes += [Note(
        trainer, f"Safety pause: {name}",
        f"Your client {name} has been paused {by} after a potentially dangerous health reading. Please don't run "
        "sessions with them until a gym admin reactivates the account.",
        link="/staff/clients", category=NotificationCategory.HEALTH, severity=Severity.DANGER,
    ) for trainer in trainers]
    if staff_notes:
        await db.flush()  # as notify() did: the pending e-mail rows / guardian flag are written before the notices
        await notify_bulk(db, staff_notes)
    for gym_id in gym_ids or [None]:
        await audit(db, None, "health.suspend", "user", customer.id, gym_id=gym_id,
                    detail={"actor": kind, "alert_id": alert.id, "reason": reason[:300]})
    return event


async def reactivation_gym_ids(db: AsyncSession, customer_id: int, owner: User) -> list[int]:
    rows = await db.execute(
        select(Gym.id).join(Subscription, Subscription.gym_id == Gym.id)
        .where(Subscription.customer_id == customer_id, Gym.owner_id == owner.id,
               Subscription.status.in_(REACTIVATION_STATUSES))
        .distinct()
    )
    return sorted(gid for (gid,) in rows.all())


async def can_reactivate(db: AsyncSession, customer: User, actor_user: User | None) -> bool:
    if actor_user is None or actor_user.deleted_at is not None:
        return False
    if actor_user.role == Role.SUPERADMIN:
        return True
    if actor_user.role == Role.OWNER:
        return bool(await reactivation_gym_ids(db, customer.id, actor_user))
    return False


async def reactivate_customer(db: AsyncSession, customer: User, actor_user: User, note: str) -> SuspensionEvent:
    """Lift a safety suspension. Allowed for owners of gyms where the customer has/had a subscription and for
    the super-admin; raises PermissionError otherwise and ValueError for a missing note / not suspended.
    Writes a SuspensionEvent, notifies the customer (guardian CC for minors) and audits. The caller commits."""
    if actor_user is None or actor_user.deleted_at is not None:
        raise PermissionError("Only a gym admin can reactivate a member.")
    gym_ids: list[int] = []
    if actor_user.role == Role.SUPERADMIN:
        kind = "superadmin"
    elif actor_user.role == Role.OWNER:
        gym_ids = await reactivation_gym_ids(db, customer.id, actor_user)
        if not gym_ids:
            raise PermissionError("Only an admin of this member's gym can reactivate them.")
        kind = "owner"
    else:
        raise PermissionError("Only a gym admin can reactivate a member.")
    note = " ".join((note or "").split())
    if len(note) < 5:
        raise ValueError("Add a short note explaining why it is safe to reactivate (e.g. a doctor's clearance).")
    profile = await db.get(CustomerProfile, customer.id)
    if profile is None or not profile.activity_suspended:
        raise ValueError("This member's gym activities are not paused.")
    profile.activity_suspended = False
    profile.suspension_reason = None
    profile.suspended_at = None
    profile.suspended_by = None
    event = SuspensionEvent(customer_id=customer.id, action="reactivate", reason=note[:2000], actor=kind,
                            actor_user_id=actor_user.id)
    db.add(event)
    await db.flush()
    await notify(
        db, customer, "Your gym activities are active again",
        f"{actor_user.display_name} reactivated your gym activities. Note: {note} Ease back in gently and stop "
        "if anything feels wrong.",
        category=NotificationCategory.HEALTH, severity=Severity.SUCCESS, link="/me",
    )
    await audit(db, actor_user, "health.reactivate", "user", customer.id, gym_id=gym_ids[0] if gym_ids else None,
                detail={"note": note[:500]})
    return event


# ============================================================================ AC advice
AC_VERSION = 1
VERDICT_RANK = {"recommended": 0, "caution": 1, "not_recommended": 2}
VERDICT_LABELS = {"recommended": "Recommended", "caution": "Use with care", "not_recommended": "Not recommended"}
AC_HEAT_REASONS = {
    "hypertension": "High blood pressure: heat makes your heart and blood vessels work harder, so a cool room helps.",
    "cardiac": "Heart condition: staying cool lowers the strain on your heart.",
    "heat_intolerance": "Heat intolerance: a cool room helps you avoid overheating.",
    "obesity": "A higher body weight holds more heat, so cooling supports recovery and sleep.",
    "pregnancy": "Pregnancy raises body temperature, so a cool room lowers the risk of overheating.",
    "diabetes": "Diabetes can blunt the body's cooling response - avoid overheating.",
    "multiple_sclerosis": "Heat can temporarily worsen MS symptoms, so cooling helps.",
}


def ac_recommendation(tags: Iterable[str] | None, age: int | None) -> dict[str, Any]:
    """Rule-based air-conditioning advice (JSON-safe dict, cached on CustomerProfile.ac_recommendation)."""
    tagset = sorted({str(t) for t in (tags or []) if t})
    verdict: str | None = None
    ranges: list[tuple[int, int]] = []
    reasons: list[str] = []
    tips: list[str] = []

    def bump(v: str) -> None:
        nonlocal verdict
        if verdict is None or VERDICT_RANK[v] > VERDICT_RANK[verdict]:
            verdict = v

    heat = [t for t in AC_HEAT_REASONS if t in tagset]
    if heat or (age is not None and age >= 60):
        bump("recommended")
        ranges.append((22, 26))
        reasons += [AC_HEAT_REASONS[t] for t in heat]
        if age is not None and age >= 60:
            reasons.append("Age 60+: the body regulates temperature less efficiently, so avoid overheating.")
    resp = [t for t in ("asthma", "copd") if t in tagset]
    if resp:
        bump("caution")
        ranges.append((24, 26))
        names = " and ".join(TAG_LABELS.get(t, t) for t in resp)
        reasons.append(f"{names}: cold, dry air and dust can trigger breathing symptoms.")
        tips += ["Avoid cold drafts - never point the airflow at your face or bed.",
                 "Clean or replace the filters every 2 weeks.",
                 "Keep the humidity between 40% and 60%."]
    if "respiratory_allergy" in tagset:
        bump("recommended")
        reasons.append("Respiratory allergy: filtered, closed-window cooling keeps pollen and dust out.")
        tips.append("Use clean (ideally HEPA-grade) filters and keep windows closed on high-pollen days.")
    if "arthritis" in tagset:
        bump("caution")
        ranges.append((24, 27))
        reasons.append("Arthritis: cold air can stiffen joints, so keep it mild and avoid drafts on your joints.")
        tips.append("Keep joints covered and warm up well before training in a cooled room.")
    if "cold_sensitivity" in tagset:
        bump("not_recommended")
        reasons.append("Cold sensitivity: air conditioning can trigger your symptoms.")
        tips.append("Prefer a fan or natural ventilation. If you must use AC, keep it at 26 °C or warmer with no direct airflow.")
    basis = "profile"
    if verdict is None:
        verdict, basis = "recommended", "default"
        reasons.append("Nothing in your profile needs special care - a comfortable 23-26 °C suits most people.")
    lo: int | None
    hi: int | None
    if verdict == "not_recommended":
        lo = hi = None
    else:
        if not ranges:
            ranges.append((23, 26))
        lo, hi = max(r[0] for r in ranges), min(r[1] for r in ranges)
        if lo > hi:  # conflicting needs -> the warmest (most conservative) band
            lo, hi = max(ranges)
    tips.append("Set it 1-2 °C warmer at night and drink water - air-conditioned air is dry.")
    reasons = list(dict.fromkeys(reasons))
    tips = list(dict.fromkeys(tips))
    if verdict == "not_recommended":
        summary = "Air conditioning isn't recommended for you - prefer a fan or natural ventilation."
    elif verdict == "caution":
        summary = f"Use air conditioning with care: keep it between {lo} and {hi} °C, avoid cold drafts and keep the filters clean."
    elif basis == "default":
        summary = f"Comfort setting: {lo}-{hi} °C works well for most people."
    else:
        summary = f"Air conditioning is recommended for you - keep the room between {lo} and {hi} °C."
    return {
        "version": AC_VERSION, "verdict": verdict, "label": VERDICT_LABELS[verdict], "basis": basis,
        "temp_min": lo, "temp_max": hi, "reasons": reasons, "tips": tips, "summary": summary,
        "tags": tagset, "age": age, "source": "rules", "ai_summary": None, "ai_provider": None,
        "computed_at": utcnow().isoformat(timespec="seconds"),
    }


def ac_is_current(cached: Any, tags: Iterable[str], age: int | None) -> bool:
    return (isinstance(cached, dict) and cached.get("version") == AC_VERSION
            and cached.get("tags") == sorted({t for t in tags if t}) and cached.get("age") == age)


def ac_for_display(profile: CustomerProfile | None, tags: Iterable[str], age: int | None) -> tuple[dict[str, Any], bool]:
    """(advice, changed). Uses the cache while conditions/age are unchanged, recomputes deterministically otherwise."""
    tags = list(tags)
    cached = profile.ac_recommendation if profile is not None else None
    if ac_is_current(cached, tags, age):
        return cached, False
    return ac_recommendation(tags, age), True


async def refresh_ac(db: AsyncSession, user: User, profile: CustomerProfile, tags: Iterable[str]) -> dict[str, Any]:
    """Recompute + cache the rule-based advice after condition changes. It is stored at once and no request waits for
    an AI: the friendlier AI wording (plans with AI) follows from ``queue_ac_note``. The caller commits."""
    rec = ac_recommendation(tags, user.age())
    profile.ac_recommendation = rec
    return rec


AC_JOB_KIND = "health_ac_note"
AC_JOB_ATTEMPTS = 2
AC_AI_FIELDS = ("ai_summary", "ai_provider")


def ac_job_key(user_id: int) -> str:
    """One AC-wording job per member (re-used by every change): it always words the newest advice."""
    return f"{AC_JOB_KIND}:{user_id}"


async def queue_ac_note(db: AsyncSession, user: User, *, owner: AIOwner | None = None) -> Job | None:
    """Queue the optional AI wording of the member's stored AC advice (the ``health_ac_note`` job, paused while they
    are away). Call it AFTER the advice is committed: it COMMITS its own small transaction and never raises - the
    rule-based advice is already saved and shown, the AI wording is a nicety.

    While the member's job is queued or running nothing new is queued (or charged): it words the newest advice when
    it runs. A paused one goes back in the queue. Otherwise the change is judged by the AI guard first (background
    jobs are not): a refusal (quota, cooldown) keeps the rule-based wording, and the charge is refunded when queueing
    fails. Returns the member's job, or None when nothing is queued."""
    from app.ai import guard as guard_module

    key = ac_job_key(user.id)
    existing = await db.scalar(select(Job).where(Job.dedupe_key == key))
    if existing is not None and existing.status in (JobStatus.QUEUED, JobStatus.RUNNING):
        return existing
    admission = None
    if existing is None or existing.status != JobStatus.PAUSED:  # a paused job was admitted when it was queued
        try:
            admission = await guard_module.guard.admit(owner if owner is not None else current_owner(), "health_ac")
        except AIUnavailableError as exc:  # AIRejected: the rule-based wording stays - nothing to tell the member
            log.info("AI wording of the AC advice of user %s skipped: %s", user.id, getattr(exc, "code", exc))
            return None
        except Exception:  # noqa: BLE001 - a broken guard must not lose the wording
            log.exception("AI guard failed for the AC wording of user %s - admitting it", user.id)
    try:
        job = await enqueue(db, AC_JOB_KIND, {"user_id": user.id}, dedupe_key=key, max_attempts=AC_JOB_ATTEMPTS,
                            user_id=user.id)
        await db.commit()
    except BaseException as exc:
        await guard_module.guard.settle(admission, ok=False)  # nothing queued: nobody pays for it
        if not isinstance(exc, Exception):
            raise
        # the request's session is closed (rolled back) when the request ends - its objects stay loaded meanwhile
        log.exception("could not queue the AI wording of the AC advice of user %s", user.id)
        return None
    return job


def _rules_part(rec: Any) -> dict[str, Any] | None:
    """The rule-based content of stored AC advice (what an AI wording belongs to)."""
    return {k: v for k, v in rec.items() if k not in AC_AI_FIELDS} if isinstance(rec, dict) else None


async def _word_ac(db: AsyncSession, user: User) -> bool | None:
    """One attempt to word the member's current advice: True (worded), False (nothing to do: no AI plan, already
    worded, AI offline, or the wording failed the number check), None (the advice changed while the AI was busy)."""
    from app.services.subscriptions import plan_access

    age = user.age()
    profile = await db.get(CustomerProfile, user.id)
    if profile is None or not (await plan_access(db, user.id)).ai:
        return False
    from app.ai.member_context import load_member_context

    tags = await condition_tags(db, user.id)
    rec = profile.ac_recommendation
    if not ac_is_current(rec, tags, age):  # outdated (e.g. a birthday since): word the advice the page would show
        rec = ac_recommendation(tags, age)
        profile.ac_recommendation = rec
    elif rec.get("ai_summary"):
        return False
    rules = _rules_part(rec)
    member = await load_member_context(db, user)
    block = "" if member.empty else member.block(max_chars=health_ai.PROFILE_PROMPT_CHARS, include_basics=False)
    await db.commit()  # neither a transaction nor a pooled connection is held while the model thinks
    try:
        summary, provider = await health_ai.rephrase_ac(dict(rec), minor=age is not None and age < ADULT_AGE,
                                                        profile=block)
    except AICancelled:
        raise  # stopped because the member is gone - the job's exit policy decides what happens next
    except AIUnavailableError:
        return False
    if not summary:
        return False  # numbers changed or dropped, or the verdict contradicted: keep the rule-based wording
    stmt = (select(CustomerProfile).where(CustomerProfile.user_id == user.id).with_for_update()
            .execution_options(populate_existing=True))
    fresh = (await db.execute(stmt)).scalar_one_or_none()
    if fresh is None or _rules_part(fresh.ac_recommendation) != rules:
        await db.commit()
        return None if fresh is not None else False
    fresh.ac_recommendation = {**fresh.ac_recommendation, "ai_summary": summary, "ai_provider": provider}
    await db.commit()
    return True


@job_handler(AC_JOB_KIND, on_exit="pause")  # a nicety for later: deferred while the member is away
async def ac_note_job(db: AsyncSession, payload: dict[str, Any]) -> None:
    """Friendlier AI wording of a member's stored AC advice (plans with AI). The rule-based advice was saved when
    their conditions changed; this only adds ``ai_summary`` - re-checked by ``rephrase_ac`` (numbers, verdict) and
    only while that advice is still the current one. AI offline: the rule-based wording simply stays.

    Every outcome is announced with ``notify: false`` (the runner's 'done', or its 'failed' once no retry is left):
    a reworded sentence is not news worth an island note."""
    set_result_event(notify=False)
    set_failure_event(notify=False)
    try:
        user_id = int(payload.get("user_id") or 0)
    except (TypeError, ValueError):
        return
    user = await db.get(User, user_id) if user_id > 0 else None
    if user is None or user.deleted_at is not None:
        return
    worded: bool | None = None
    for _ in range(2):  # changed while the AI was busy -> word the newest advice once more
        worded = await _word_ac(db, user)
        if worded is not None:
            break
    if worded:
        set_result_event(link="/me/health/profile", message="Your air-conditioning advice has friendlier AI wording.")


# ============================================================================ bedtime
SLEEP_NEEDS = ((5, 10, 13), (12, 9, 12), (17, 8, 10), (64, 7, 9), (200, 7, 8))  # (max age, min h, max h)
REMINDER_LEAD = timedelta(minutes=30)


def sleep_need(age: int | None) -> tuple[int, int]:
    if age is None:
        return 7, 9
    for max_age, lo, hi in SLEEP_NEEDS:
        if age <= max_age:
            return lo, hi
    return 7, 8


@dataclass(slots=True, frozen=True)
class BedtimePlan:
    wake_time: time
    need_min: int
    need_max: int
    need_hours: float
    bedtime: time
    reminder_at: time

    @property
    def need_label(self) -> str:
        return f"{self.need_min}-{self.need_max} h"

    @property
    def sleep_duration(self) -> timedelta:
        return timedelta(minutes=round(self.need_hours * 60))


def bedtime_for(age: int | None, wake_time: time | None) -> BedtimePlan:
    """Sleep need by age (6-12: 9-12 h, 13-17: 8-10 h, 18-64: 7-9 h, 65+: 7-8 h), midpoint; bedtime = wake - need."""
    wake = wake_time or time(6, 30)
    lo, hi = sleep_need(age)
    need = (lo + hi) / 2
    wake_min = wake.hour * 60 + wake.minute
    bed_min = (wake_min - round(need * 60)) % 1440
    rem_min = (bed_min - int(REMINDER_LEAD.total_seconds() // 60)) % 1440
    return BedtimePlan(wake, lo, hi, need, time(bed_min // 60, bed_min % 60), time(rem_min // 60, rem_min % 60))


def duration_label(delta: timedelta) -> str:
    mins = max(0, int(round(delta.total_seconds() / 60)))
    h, m = divmod(mins, 60)
    if h and m:
        return f"{h} h {m} min"
    if h:
        return f"{h} h"
    return f"{m} min"


def sleep_window(plan: BedtimePlan, tz: str | None, now: datetime | None = None) -> dict[str, Any]:
    """The current or next sleep window in the user's time zone (for the countdown component)."""
    zone = safe_zone(tz)
    local = (now or utcnow()).astimezone(zone)
    need = plan.sleep_duration
    for offset in (-1, 0, 1):
        start = datetime.combine(local.date() + timedelta(days=offset), plan.bedtime, tzinfo=zone)
        end = start + need
        if local < end:
            break
    if local >= start:
        state, label = "sleep", "Past your bedtime - time to sleep"
    elif start - local <= REMINDER_LEAD:
        state, label = "wind-down", f"Wind down - bedtime in {duration_label(start - local)}"
    else:
        state, label = "awake", f"in {duration_label(start - local)}"
    day_start = start - (timedelta(days=1) - need)  # this morning's wake-up
    span = (start - day_start).total_seconds()
    progress = 100.0 if local >= start else max(0.0, min(100.0, (local - day_start).total_seconds() / span * 100))
    return {
        "bedtime_at": start, "wake_at": end, "day_start": day_start, "state": state, "label": label,
        "progress": round(progress, 1), "state_label": {"sleep": "Sleep", "wind-down": "Wind down"}.get(state, "Awake"),
    }


def reminder_key(plan: BedtimePlan, tz: str | None, now: datetime, last_on: date | None) -> date | None:
    """Local date of a reminder that is due now (window: bedtime - 30 min .. bedtime; catch-up allowed inside it),
    or None. Keyed by the window's start date so a window crossing midnight still fires once."""
    zone = safe_zone(tz)
    local = now.astimezone(zone)
    for offset in (-1, 0):
        day = local.date() + timedelta(days=offset)
        start = datetime.combine(day, plan.reminder_at, tzinfo=zone)
        if start <= local < start + REMINDER_LEAD:
            return None if last_on == day else day
    return None


BEDTIME_TASK = "bedtime_reminders"
BEDTIME_BATCH = 1000  # opted-in customers looked at per chunk - one short transaction each
BEDTIME_BUDGET_S = 45.0  # the task runs every minute: a bigger cohort carries on at the next tick (resume cursor)


def _bedtime_due(rows: Sequence[Any], now: datetime) -> list[tuple[User, BedtimePlan, date]]:
    due = []
    for user, wake_time, last_on in rows:
        local_date = now.astimezone(safe_zone(user.timezone)).date()
        plan = bedtime_for(user.age(local_date), wake_time)
        key = reminder_key(plan, user.timezone, now, last_on)
        if key is not None:
            due.append((user, plan, key))
    return due


async def _claim_bedtime(db: AsyncSession, due: Sequence[tuple[User, BedtimePlan, date]]) -> set[int]:
    """Mark the chunk's due reminders sent (one UPDATE ... FROM unnest(ids, days)) and return whose mark changed: a
    reminder already sent for that day, or switched off meanwhile, is not sent (twice)."""
    pairs = sa.func.unnest(
        sa.bindparam("uids", type_=ARRAY(sa.BigInteger)), sa.bindparam("days", type_=ARRAY(sa.Date)),
    ).table_valued(sa.column("uid", sa.BigInteger), sa.column("day", sa.Date)).render_derived(name="due")
    stmt = (
        sa.update(CustomerProfile)
        .where(CustomerProfile.user_id == pairs.c.uid, CustomerProfile.sleep_reminders_enabled.is_(True),
               CustomerProfile.last_bedtime_reminder_on.is_distinct_from(pairs.c.day))
        .values(last_bedtime_reminder_on=pairs.c.day)
        .returning(CustomerProfile.user_id)
        .execution_options(synchronize_session=False)
    )
    params = {"uids": [u.id for u, _, _ in due], "days": [key for _, _, key in due]}
    return set((await db.execute(stmt, params)).scalars().all())


@periodic(BEDTIME_TASK, every_seconds=60)
async def bedtime_reminders(db: AsyncSession, now: datetime | None = None) -> int:
    """Every minute: remind customers (sleep reminders on) 30 min before their bedtime, once per local day.

    Opted-in customers are walked in keyset chunks of BEDTIME_BATCH (only the columns the bedtime maths needs - never
    the profile's embedding / JSONB). Each chunk is one short transaction: one SELECT, and only when someone is due one
    UPDATE marking the day sent plus one bulk notification insert (``notify_bulk``). A chunk commits its marks with its
    notifications, so a failed or interrupted run never re-sends; a run that spends BEDTIME_BUDGET_S leaves a resume
    cursor and the next tick carries on from there (the reminder window is 30 minutes)."""
    now = now or utcnow()
    run = Chunks(holder=db, budget_s=BEDTIME_BUDGET_S)
    last_id = resume_after(BEDTIME_TASK)
    sent = 0
    while await run.next():
        async with session_scope() as s:
            rows = (await s.execute(
                select(User, CustomerProfile.wake_time, CustomerProfile.last_bedtime_reminder_on)
                .join(CustomerProfile, CustomerProfile.user_id == User.id)
                .where(User.role == Role.CUSTOMER, User.deleted_at.is_(None), User.is_active.is_(True),
                       CustomerProfile.sleep_reminders_enabled.is_(True), User.id > last_id)
                .order_by(User.id)
                .limit(BEDTIME_BATCH)
            )).tuples().all()
            due = _bedtime_due(rows, now)
            claimed = await _claim_bedtime(s, due) if due else set()
            if claimed:
                sent += len(await notify_bulk(s, [
                    Note(user, "Time to wind down",
                         f"Time to wind down - shut this down and sleep, recovery matters. Bedtime is {plan.bedtime:%H:%M} "
                         f"({plan.need_label} of sleep suits your age; you wake at {plan.wake_time:%H:%M}).",
                         link="/me", category=NotificationCategory.REMINDER, severity=Severity.INFO)
                    for user, plan, _ in due if user.id in claimed
                ]))
        if rows:
            last_id = rows[-1][0].id
        run.done(len(rows), BEDTIME_BATCH)
    if run.stopped_early:
        remember(BEDTIME_TASK, last_id)
        log.warning("bedtime reminders: time budget used after %s customers - the next run continues", run.rows)
    if sent:
        log.info("bedtime reminders sent: %s", sent)
    return sent


# ============================================================================ wellness (PSS-4)
PSS_QUESTIONS = (
    "In the last month, how often have you felt that you were unable to control the important things in your life?",
    "In the last month, how often have you felt confident about your ability to handle your personal problems?",
    "In the last month, how often have you felt that things were going your way?",
    "In the last month, how often have you felt difficulties were piling up so high that you could not overcome them?",
)
PSS_REVERSED = (1, 2)  # zero-based: items 2 and 3 are reverse-scored
PSS_OPTIONS = ((0, "Never"), (1, "Almost never"), (2, "Sometimes"), (3, "Fairly often"), (4, "Very often"))
SYMPTOMS = (
    ("chest_pain", "Chest pain or pressure"),
    ("fainting", "Fainting or nearly fainting"),
    ("shortness_of_breath", "Shortness of breath at rest"),
    ("severe_dizziness", "Severe dizziness"),
    ("palpitations", "Palpitations (racing or irregular heartbeat)"),
    ("none", "None of these"),
)
SYMPTOM_LABELS = dict(SYMPTOMS)
RED_FLAG_SYMPTOMS = frozenset({"chest_pain", "fainting", "shortness_of_breath"})
ADVISORY_SYMPTOMS = frozenset({"severe_dizziness", "palpitations"})
BREATHING_VIDEO = "/me/videos/box-breathing"
STRESS_RANK = {StressLevel.LOW: 0, StressLevel.MODERATE: 1, StressLevel.HIGH: 2}


def pss_score(answers: Sequence[int]) -> int:
    values = [int(a) for a in answers]
    if len(values) != 4 or any(v < 0 or v > 4 for v in values):
        raise ValueError("PSS-4 needs four answers between 0 and 4")
    return sum((4 - v) if i in PSS_REVERSED else v for i, v in enumerate(values))


def pss_level(score: int) -> StressLevel:
    if score >= 9:
        return StressLevel.HIGH
    if score >= 6:
        return StressLevel.MODERATE
    return StressLevel.LOW


def combine_stress(level: StressLevel, face: Mapping[str, Any] | None) -> StressLevel:
    """The questionnaire decides; strong visible fatigue in the (optional) photo can raise it one level."""
    if not face or face.get("status") != "ok" or face.get("fatigue_signs") != "high":
        return level
    return StressLevel.MODERATE if level == StressLevel.LOW else StressLevel.HIGH


def wellness_recommendations(level: StressLevel, symptoms: Sequence[str], *, minor: bool,
                             face: Mapping[str, Any] | None = None) -> list[str]:
    recs: list[str] = []
    red = [s for s in symptoms if s in RED_FLAG_SYMPTOMS]
    if red:
        recs.append(f"Seek emergency care now - call {EMERGENCY_HINT} or go to the nearest emergency department. "
                    "Don't exercise until a doctor has checked you.")
    elif any(s in ADVISORY_SYMPTOMS for s in symptoms):
        recs.append("Dizziness or palpitations are worth checking - please consult a healthcare professional and keep "
                    "today's training light.")
    if level == StressLevel.HIGH:
        recs += [
            "Your answers suggest high stress. Talking to someone you trust - or a counsellor or doctor - really helps."
            + (" A parent, guardian or school counsellor is a great first step." if minor else ""),
            "Swap today's hard session for an easy walk or mobility work - recovery is training too.",
        ]
    elif level == StressLevel.MODERATE:
        recs += ["Moderate stress: protect your sleep tonight and keep caffeine to the morning.",
                 "A 10-minute walk outside lowers stress hormones for many people."]
    else:
        recs.append("Low stress - nice. Keep the habits that are working: sleep, movement and time with people you like.")
    recs.append(f"Try box breathing (4 seconds in, hold 4, out 4, hold 4) for four minutes: {BREATHING_VIDEO}")
    if face and face.get("status") == "ok" and face.get("fatigue_signs") in ("moderate", "high"):
        recs.append("Your photo shows some signs of tiredness - an earlier bedtime tonight could make a real difference.")
    return recs


@dataclass(slots=True)
class WellnessOutcome:
    check: WellnessCheck
    red_flags: list[str]
    suspended: bool


WELLNESS_CAP = "wellness_checks_per_member_day"  # app/core/caps.py


def wellness_checks_today(customer_id: int) -> Any:
    """The member's check-ins of the cap's 24-hour window (one column: ids)."""
    return select(WellnessCheck.id).where(WellnessCheck.customer_id == customer_id,
                                          WellnessCheck.created_at >= caps.since())


async def wellness_room(db: AsyncSession, customer_id: int) -> str | None:
    """The friendly refusal when the member already saved the day's check-ins (a cheap look without the lock, before any
    AI time is spent) - None when there is room. ``record_wellness_check`` decides race-safely."""
    used = await caps.count_upto(db, wellness_checks_today(customer_id), caps.limit(WELLNESS_CAP))
    return str(caps.CapReached(WELLNESS_CAP)) if used >= caps.limit(WELLNESS_CAP) else None


async def record_wellness_check(db: AsyncSession, user: User, *, answers: Sequence[int], symptoms: Sequence[str],
                                face: dict[str, Any] | None = None, provider: str | None = None) -> WellnessOutcome:
    """Store a PSS-4 check-in. Red-flag symptoms -> immediate suspension (actor "rule") + emergency guidance.

    At most ``wellness_checks_per_member_day`` check-ins per member in 24 hours (race-safe, ``caps.CapReached``) - one
    that reports a red-flag symptom is never refused: safety first."""
    score = pss_score(answers)
    final = combine_stress(pss_level(score), face)
    order = [k for k, _ in SYMPTOMS]
    clean = sorted({s for s in symptoms if s in SYMPTOM_LABELS and s != "none"}, key=order.index)
    red = [s for s in clean if s in RED_FLAG_SYMPTOMS]
    if not red:
        await caps.enforce(db, WELLNESS_CAP, user.id, wellness_checks_today(user.id))
    age = user.age()
    minor = age is not None and age < ADULT_AGE
    recs = wellness_recommendations(final, clean, minor=minor, face=face)
    check = WellnessCheck(customer_id=user.id, pss_answers=[int(a) for a in answers], pss_score=score, symptoms=clean,
                          face_analysis=face, stress_level=final, recommendations="\n".join(recs),
                          ai_provider=provider)
    db.add(check)
    await db.flush()
    suspended = False
    if red:
        labels = ", ".join(SYMPTOM_LABELS[s].lower() for s in red)
        alert = HealthAlert(
            customer_id=user.id, wellness_check_id=check.id, level=AnomalyLevel.LIFE_THREATENING,
            title="Red-flag symptoms reported", message=f"You reported: {labels}. These can be signs of a medical emergency.",
            recommendation=f"Seek emergency care now: call {EMERGENCY_HINT} or go to the nearest emergency department.",
            source="rule", guardian_notified=False,
        )
        db.add(alert)
        await db.flush()
        event = await suspend_customer(db, user, reason=f"Red-flag symptoms reported in a wellness check-in ({labels}). "
                                                         "Seek emergency care now.", actor="rule", alert=alert)
        if event is None:  # already paused: still make sure the emergency guidance reaches them
            await notify(db, user, "Seek emergency care now",
                         f"You reported {labels}. Please call {EMERGENCY_HINT} or go to the nearest emergency department.",
                         category=NotificationCategory.HEALTH, severity=Severity.DANGER, link=f"/me/wellness/{check.id}")
        suspended = True
    elif any(s in ADVISORY_SYMPTOMS for s in clean):
        labels = ", ".join(SYMPTOM_LABELS[s].lower() for s in clean)
        db.add(HealthAlert(
            customer_id=user.id, wellness_check_id=check.id, level=AnomalyLevel.WARNING,
            title="Symptoms worth checking", message=f"You reported: {labels}.",
            recommendation="Please consult a healthcare professional and keep training light until then.",
            source="rule", guardian_notified=False,
        ))
    if final == StressLevel.HIGH and not red:
        await notify(
            db, user, "Your stress check-in",
            "Your answers suggest high stress right now. You're not alone - talking to someone you trust helps, and "
            "a few minutes of box breathing can take the edge off.",
            category=NotificationCategory.HEALTH, severity=Severity.WARNING, link=f"/me/wellness/{check.id}",
        )
    return WellnessOutcome(check, red, suspended)


# ============================================================================ scale / medical report reading
LB_TO_KG = 0.45359237
ENGINE_PRIORITY = ("vision_model", "vision_llm", "apple_vision", "windows_ocr", "rapidocr", "tesseract")
ENGINE_LABELS = {
    "tesseract": "Tesseract", "apple_vision": "Apple Vision", "windows_ocr": "Windows OCR", "rapidocr": "RapidOCR",
    "vision_llm": "AI vision (OCR)", "vision_model": "AI vision",
}
_N = r"(\d{1,3}(?:[.,]\d{1,2})?)"
_RE_WEIGHT_LABEL = re.compile(r"\b(?:body\s*)?(?:weight|wt|mass)\b([^\d\n]{0,16}?)" + _N + r"\s*(kgs?|kilo(?:gram)?s?|lbs?|pounds?)?", re.I)
_RE_WEIGHT_UNIT = re.compile(r"(?<![\d.,])" + _N + r"\s*(kgs?|kilo(?:gram)?s?|lbs?|pounds?)\b", re.I)
_RE_HEIGHT_FTIN = re.compile(
    r"(?<![\d.,])([3-7])\s*(?:'|’|′|ft\.?|feet|foot)\s*(?:(\d{1,2}(?:[.,]\d)?)\s*(?:\"|”|″|''|in(?:ch(?:es)?)?\b\.?)?)?", re.I)
_RE_HEIGHT_LABEL = re.compile(r"\b(?:height|ht|stature)\b([^\d\n]{0,16}?)" + _N + r"\s*(cms?|centimet(?:er|re)s?|m\b|met(?:er|re)s?|in(?:ch(?:es)?)?\b)?", re.I)
_RE_HEIGHT_CM = re.compile(r"(?<![\d.,])(\d{2,3}(?:[.,]\d)?)\s*(?:cms?|centimet(?:er|re)s?)\b", re.I)
_RE_HEIGHT_M = re.compile(r"(?<![\d.,])([12][.,]\d{1,2})\s*(?:m|mtrs?|met(?:er|re)s?)\b", re.I)
_RE_HEIGHT_IN = re.compile(r"(?<![\d.,])(\d{2}(?:[.,]\d)?)\s*(?:in|inch(?:es)?)\b", re.I)
_RE_BARE = re.compile(r"(?<![\d.,])(\d{2,3}[.,]\d{1,2})(?![\d])")
_NOT_WEIGHT_CONTEXT = re.compile(r"\b(bmi|fat|water|muscle|bone|kcal|height|ht)\W*$", re.I)


def _f(text: str) -> float:
    return float(text.replace(",", "."))


def _weight_kg(value: float, unit: str | None) -> float | None:
    u = (unit or "kg").lower()
    kg = value * LB_TO_KG if u.startswith(("lb", "pound")) else value
    return round(kg, 1) if WEIGHT_RANGE[0] <= kg <= WEIGHT_RANGE[1] else None


def _height_cm(value: float, unit: str | None) -> float | None:
    u = (unit or "").lower()
    if u.startswith("in"):
        cm = value * 2.54
    elif u in ("m", "mtr", "mtrs") or u.startswith("met") or (not u and value < 3):
        cm = value * 100
    else:
        cm = value
    return round(cm, 1) if HEIGHT_RANGE[0] <= cm <= HEIGHT_RANGE[1] else None


@dataclass(slots=True)
class Measurement:
    weight_kg: float | None = None
    height_cm: float | None = None
    weight_text: str | None = None
    height_text: str | None = None


def parse_measurements(text: str | None) -> Measurement:
    """Find a body weight (kg/lb -> kg) and height (cm, m, in or ft/in -> cm) in OCR text."""
    out = Measurement()
    text = (text or "").replace("\r", "\n")
    if not text.strip():
        return out
    lb_only = bool(re.search(r"\b(lbs?|pounds?)\b", text, re.I)) and not re.search(r"\bkgs?\b", text, re.I)

    for m in _RE_WEIGHT_LABEL.finditer(text):
        unit = m.group(3) or (re.search(r"(kgs?|lbs?)", m.group(1) or "", re.I) or [None])[0]
        kg = _weight_kg(_f(m.group(2)), unit or ("lb" if lb_only else "kg"))
        if kg is not None:
            out.weight_kg, out.weight_text = kg, m.group(0).strip()
            break
    if out.weight_kg is None:
        for m in _RE_WEIGHT_UNIT.finditer(text):
            kg = _weight_kg(_f(m.group(1)), m.group(2))
            if kg is not None:
                out.weight_kg, out.weight_text = kg, m.group(0).strip()
                break

    for m in _RE_HEIGHT_FTIN.finditer(text):
        feet, inches = int(m.group(1)), _f(m.group(2)) if m.group(2) else 0.0
        if inches < 12:
            cm = _height_cm(feet * 12 + inches, "in")
            if cm is not None:
                out.height_cm, out.height_text = cm, m.group(0).strip()
                break
    if out.height_cm is None:
        for m in _RE_HEIGHT_LABEL.finditer(text):
            unit = m.group(3) or (re.search(r"\b(cm|m|in)\b", m.group(1) or "", re.I) or [None])[0]
            cm = _height_cm(_f(m.group(2)), unit)
            if cm is not None:
                out.height_cm, out.height_text = cm, m.group(0).strip()
                break
    if out.height_cm is None:
        for rx, unit in ((_RE_HEIGHT_CM, "cm"), (_RE_HEIGHT_M, "m"), (_RE_HEIGHT_IN, "in")):
            m = rx.search(text)
            if m:
                cm = _height_cm(_f(m.group(1)), unit)
                if cm is not None:
                    out.height_cm, out.height_text = cm, m.group(0).strip()
                    break

    if out.weight_kg is None:  # a bare scale display ("72.4"): first decimal that isn't BMI / fat % / height
        for m in _RE_BARE.finditer(text):
            before = text[max(0, m.start() - 14):m.start()]
            after = text[m.end():m.end() + 2]
            if _NOT_WEIGHT_CONTEXT.search(before) or after.strip().startswith("%"):
                continue
            if out.height_text and m.group(1) in out.height_text:
                continue
            kg = _weight_kg(_f(m.group(1)), "lb" if lb_only else "kg")
            if kg is not None:
                out.weight_kg, out.weight_text = kg, m.group(1)
                break
    return out


def measurement_from_vision(reading: Any) -> Measurement:
    """Convert ``app.ai.health.MeasurementReading`` into a validated Measurement (ranges re-checked)."""
    out = Measurement()
    if reading is None:
        return out
    if reading.weight is not None:
        out.weight_kg = _weight_kg(float(reading.weight), reading.weight_unit or "kg")
        if out.weight_kg is not None:
            out.weight_text = f"{fmt_num(reading.weight)} {reading.weight_unit or 'kg'}"
    if reading.height_feet is not None:
        cm = _height_cm(float(reading.height_feet) * 12 + float(reading.height_inches or 0), "in")
        if cm is not None:
            out.height_cm, out.height_text = cm, f"{fmt_num(reading.height_feet, 0)} ft {fmt_num(reading.height_inches or 0)} in"
    elif reading.height is not None:
        out.height_cm = _height_cm(float(reading.height), reading.height_unit)
        if out.height_cm is not None:
            out.height_text = f"{fmt_num(reading.height)} {reading.height_unit or 'cm'}"
    return out


@dataclass(slots=True)
class ScanResult:
    weight_kg: float | None = None
    height_cm: float | None = None
    weight_engines: list[str] = field(default_factory=list)
    height_engines: list[str] = field(default_factory=list)
    engines: list[str] = field(default_factory=list)
    readings: list[dict[str, Any]] = field(default_factory=list)
    vision_used: bool = False
    ocr_available: bool = True
    # the AI vision check was refused for this member (quota / cooldown ...): why, and when it works again -
    # the OCR engines' read-out is still shown
    ai_refusal: str | None = None
    ai_refusal_code: str | None = None
    ai_stopped: bool = False  # the AI vision check was stopped: the page that asked was closed / disconnected

    @property
    def found(self) -> bool:
        return self.weight_kg is not None or self.height_cm is not None

    def engine_labels(self, names: Sequence[str]) -> list[str]:
        return [ENGINE_LABELS.get(n, n) for n in names]


def _priority(engine: str) -> int:
    return ENGINE_PRIORITY.index(engine) if engine in ENGINE_PRIORITY else len(ENGINE_PRIORITY)


def _consensus(values: Sequence[tuple[str, float]], tolerance: float) -> tuple[float | None, list[str]]:
    """Largest group of engines that agree within ``tolerance`` (ties -> the most trusted engine)."""
    if not values:
        return None, []
    best: list[tuple[str, float]] = []
    best_key: tuple[int, int] | None = None
    for _, anchor in values:
        members = [(e, v) for e, v in values if abs(v - anchor) <= tolerance]
        engines = {e for e, _ in members}
        key = (len(engines), -min(_priority(e) for e in engines))
        if best_key is None or key > best_key:
            best, best_key = members, key
    value = round(float(statistics.median(v for _, v in best)), 1)
    return value, sorted({e for e, _ in best}, key=_priority)


def cross_check(per_engine: Mapping[str, Measurement]) -> ScanResult:
    result = ScanResult(engines=sorted(per_engine, key=_priority))
    weights = [(e, m.weight_kg) for e, m in per_engine.items() if m.weight_kg is not None]
    heights = [(e, m.height_cm) for e, m in per_engine.items() if m.height_cm is not None]
    result.weight_kg, result.weight_engines = _consensus(weights, 0.3)
    result.height_cm, result.height_engines = _consensus(heights, 1.0)
    result.readings = [
        {"engine": e, "label": ENGINE_LABELS.get(e, e), "weight_kg": m.weight_kg, "height_cm": m.height_cm,
         "weight_text": m.weight_text, "height_text": m.height_text}
        for e, m in sorted(per_engine.items(), key=lambda kv: _priority(kv[0]))
    ]
    result.vision_used = any(e in ("vision_model", "vision_llm") for e in per_engine)
    return result


def normalize_photo(data: bytes, max_side: int = 2000) -> bytes:
    """Decode + validate an uploaded photo IN MEMORY (EXIF-orient, downscale, re-encode JPEG, metadata dropped).
    Raises ``storage.UploadError`` with a user-safe message. Nothing is written to disk.

    Decoded only through ``storage.decode_image``: the pixel count is checked on the header before any pixel is decoded
    (a 100-megapixel "decompression bomb" is refused instead of taking a gigabyte). Blocking - call it through
    ``storage.run_image_decode`` (at most IMAGE_DECODE_SLOTS decodes at once per process)."""
    from app.services.storage import UploadError, decode_image

    try:
        im = decode_image(data, max_side)
    except UploadError as exc:
        if str(exc) == "That file is not a valid image.":  # truncated or non-image files: this form's own words
            raise UploadError("That file is not a valid photo. Use a JPG, PNG or WEBP image.") from exc
        raise  # too large / unsupported format: storage's friendly sentence
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90, optimize=True)
    return buf.getvalue()


SCAN_OCR_FEATURE = "ocr"  # AI policy of the OCR engines' 'vision_llm' step (one 'vision' unit, app/ai/policy.py)


async def _vision_refusal(feature: str) -> AIRejected | None:
    """The AI guard's verdict on a scan's AI vision BEFORE any AI work runs. The OCR engines report a refused
    'vision_llm' step as a plain engine failure, so without asking first a refused OCR step would be followed by the
    vision model (``health_scan``, 2 more units): a second refusal - and strike - for the same click. A refusal here is
    the click's only one (the guard books its strike) and no AI vision runs at all. Allowed: the unit is given back at
    once - the AI client charges each real call itself. A broken guard never blocks the scan (the client judges)."""
    from app.ai import guard as guard_module

    try:
        admission = await guard_module.guard.admit(current_owner(), feature)
    except AIRejected as exc:
        return exc
    except Exception:  # noqa: BLE001
        log.exception("AI guard unavailable before a scale scan - each AI call is judged on its own")
        return None
    await guard_module.guard.settle(admission, ok=False)
    return None


async def scan_measurements(image: bytes) -> ScanResult:
    """Multi-engine OCR (+ the vision model when it isn't already one of the engines) -> cross-checked values.
    The photo is only processed in memory - never stored.

    One AI refusal per click, at most: when the OCR engines include the AI vision transcription ('vision_llm'), the AI
    guard is asked once before anything runs (``_vision_refusal``) - refused, only the local engines read the photo
    and no AI vision step runs. The vision model (``health_scan``) only runs when the OCR vision step was not part of
    the scan, or ran and found no text - never after it failed (refused in a race, busy, stopped, every provider down).
    A refusal (quota, cooldown, too many at once) is reported on the result (``ai_refusal``) next to whatever the OCR
    engines read; 'busy' and an offline AI just leave the OCR read-out, like before. A stop because the page that asked
    is gone (``AICancelled``) sets ``ai_stopped``."""
    import asyncio

    per_engine: dict[str, Measurement] = {}
    ocr_ok = True
    refusal: AIRejected | None = None
    stopped = False
    vision_failed = False
    try:
        from app.services import ocr as ocr_service

        engines: list[str] | None = None  # every available engine, the AI vision transcription included
        if "vision_llm" in ocr_service.available_engines():
            refusal = await _vision_refusal(SCAN_OCR_FEATURE)
            if refusal is not None:  # the click's one refusal: the local engines only, no AI vision at all
                engines = [e for e in ocr_service.available_engines() if e != "vision_llm"]
        multi = await asyncio.wait_for(ocr_service.run_ocr(image, engines=engines, purpose="measurement"), timeout=90)
        for res in multi.ok_results:
            per_engine[res.engine] = parse_measurements(res.text)
        vision = [r for r in multi.results if r.engine == "vision_llm"]
        vision_failed = bool(vision) and not any(r.ok for r in vision)
        if refusal is None:  # admitted at the click, then refused in a race (another tab used the quota): say why
            raced = next((r.refusal for r in vision if getattr(r, "refusal", None) is not None), None)
            if raced is not None and getattr(raced, "code", None) != "busy":
                refusal = raced
    except Exception as exc:  # engines never raise; an unfinished/broken OCR module must not break the page
        ocr_ok = False
        log.warning("OCR unavailable for scale scan: %s", exc.__class__.__name__)
    if refusal is None and not vision_failed and "vision_llm" not in per_engine and ai.status().get("available"):
        try:
            reading, _provider = await health_ai.read_measurement_photo(image)
            per_engine["vision_model"] = measurement_from_vision(reading)
        except AICancelled:
            stopped = True
        except AIRejected as exc:
            if exc.code != "busy":  # 'busy' is the system's fault: the OCR read-out stands on its own, as offline AI
                refusal = exc
        except AIUnavailableError:
            pass
    result = cross_check(per_engine)
    result.ocr_available = ocr_ok
    result.ai_stopped = stopped
    if refusal is not None:
        result.ai_refusal, result.ai_refusal_code = refusal.message, refusal.code
    return result


# ============================================================================ presentation helpers
def prefill_values(entries: Sequence[MetricEntry], profile: CustomerProfile | None) -> dict[str, str]:
    """Form pre-fill: the last confirmed reading (an invalid height falls back to the trusted profile height)."""
    for e in entries:
        if not e.confirmed:
            continue
        height = e.height_cm
        if "height_invalid" in reason_codes(e.anomaly_reasons) and profile is not None and profile.height_cm:
            height = profile.height_cm
        return {"height_cm": fmt_num(height), "weight_kg": fmt_num(e.weight_kg)}
    if profile is not None:
        return {"height_cm": fmt_num(profile.height_cm), "weight_kg": fmt_num(profile.weight_kg)}
    return {"height_cm": "", "weight_kg": ""}


def bmi_hint(values: Mapping[str, Any], age: int | None) -> str:
    h, _ = parse_measure(values.get("height_cm"), lo=HEIGHT_RANGE[0], hi=HEIGHT_RANGE[1], label="height", unit="cm")
    w, _ = parse_measure(values.get("weight_kg"), lo=WEIGHT_RANGE[0], hi=WEIGHT_RANGE[1], label="weight", unit="kg")
    if h is None or w is None:
        return "Enter height (50-250 cm) and weight (20-300 kg) to see your BMI."
    value = compute_bmi(h, w)
    cat = bmi_category(value, age)
    return f"BMI {value:.1f}" + (f" · {cat['label'].lower()}" if cat and cat["adult"] else "")


def logging_streak(days: Iterable[date], today: date) -> int:
    seen = set(days)
    cursor = today if today in seen else today - timedelta(days=1)
    n = 0
    while cursor in seen:
        n += 1
        cursor -= timedelta(days=1)
    return n


def greeting_for(local: datetime) -> str:
    h = local.hour
    if 5 <= h < 12:
        return "Good morning"
    if 12 <= h < 17:
        return "Good afternoon"
    if 17 <= h < 24:
        return "Good evening"
    return "Good night"


def sparkline(entries: Sequence[MetricEntry], *, width: int = 120, height: int = 40) -> dict[str, Any] | None:
    """Tiny weight sparkline (chronological entries)."""
    pts = [(e.recorded_on, float(e.weight_kg)) for e in entries]
    if len(pts) < 2:
        return None
    vals = [v for _, v in pts]
    lo, hi = min(vals), max(vals)
    span = max(hi - lo, 0.8)
    mid = (hi + lo) / 2
    lo, hi = mid - span / 2 - span * 0.12, mid + span / 2 + span * 0.12
    n = len(pts)
    coords = [(round(i / (n - 1) * width, 2), round((hi - v) / (hi - lo) * height, 2)) for i, (_, v) in enumerate(pts)]
    line = "M " + " L ".join(f"{x} {y}" for x, y in coords)
    return {
        "w": width, "h": height, "line": line, "area": f"{line} L {width} {height} L 0 {height} Z",
        "end": {"x": coords[-1][0], "y": coords[-1][1]},
        "label": f"Weight over the last {n} readings: from {fmt_num(vals[0])} to {fmt_num(vals[-1])} kg",
    }


def trend_chart(entries: Sequence[MetricEntry], *, width: int = 600, height: int = 200) -> dict[str, Any] | None:
    """Server-side SVG geometry for the weight trend (chronological, confirmed entries)."""
    pts = [(e.recorded_on, float(e.weight_kg)) for e in entries]
    if len(pts) < 2:
        return None
    vals = [v for _, v in pts]
    lo_v, hi_v = min(vals), max(vals)
    lo, hi = math.floor(lo_v - 0.5), math.ceil(hi_v + 0.5)
    if hi - lo < 2:
        hi = lo + 2
    n = len(pts)
    points, coords = [], []
    for i, (d, v) in enumerate(pts):
        xf = i / (n - 1)
        yf = (hi - v) / (hi - lo)
        coords.append((round(xf * width, 2), round(yf * height, 2)))
        points.append({"x": round(xf, 4), "y": round(yf, 4), "v": round(v, 1), "d": d.strftime("%d %b %Y")})
    line = "M " + " L ".join(f"{x} {y}" for x, y in coords)
    mid = (hi + lo) / 2
    first, last = pts[0], pts[-1]
    middle = pts[(n - 1) // 2]
    return {
        "w": width, "h": height, "line": line, "area": f"{line} L {width} {height} L 0 {height} Z",
        "end": {"x": coords[-1][0], "y": coords[-1][1]},
        "ticks": [{"y": 0, "label": fmt_num(hi)}, {"y": height / 2, "label": fmt_num(mid)},
                  {"y": height, "label": fmt_num(lo)}],
        "x_labels": [first[0].strftime("%d %b"), middle[0].strftime("%d %b"), last[0].strftime("%d %b")],
        "points": points,
        "latest": fmt_num(last[1]), "min": fmt_num(lo_v), "max": fmt_num(hi_v),
        "summary": (f"Weight from {first[0]:%d %b} ({fmt_num(first[1])} kg) to {last[0]:%d %b} "
                    f"({fmt_num(last[1])} kg); lowest {fmt_num(lo_v)} kg, highest {fmt_num(hi_v)} kg."),
    }


def change_over(entries_desc: Sequence[MetricEntry], days: int, today: date) -> dict[str, Any] | None:
    """Weight change between the latest confirmed entry and the oldest one within ``days``."""
    rows = [e for e in entries_desc if e.confirmed]
    if len(rows) < 2:
        return None
    latest = rows[0]
    window = [e for e in rows[1:] if e.recorded_on >= today - timedelta(days=days)]
    if not window:
        return None
    base = window[-1]
    delta = round(float(latest.weight_kg) - float(base.weight_kg), 1)
    sign = "+" if delta > 0 else ("−" if delta < 0 else "±")
    return {"delta": delta, "label": f"{sign}{fmt_num(abs(delta))} kg", "since": base.recorded_on}
