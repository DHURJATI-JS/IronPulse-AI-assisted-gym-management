"""WP-F member management: owner registration (manual form + AI intake), health edits and member scoping.

Pure helpers (no I/O): list splitting, local misspelling fixes, e-mail / phone / date-of-birth / enum validation
and form parsing. Database helpers: ``related_customers_clause`` (which customers an owner may see),
``register_customer`` (User + CustomerProfile + HealthCondition rows + ID document + invite token/e-mail +
notifications + audit), ``update_health`` and ``send_invite``. Callers own the transaction (commit).
"""

from __future__ import annotations

import difflib
import hashlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from email_validator import EmailNotValidError, validate_email as _validate_email
from fastapi import HTTPException
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import clamp, normalize_phone, strip_bidi, utcnow
from app.models import AuthToken, CustomerProfile, Gym, HealthCondition, Subscription, User
from app.models.enums import (
    ConditionKind,
    Goal,
    IdDocType,
    NotificationCategory,
    PurchaseChannel,
    Role,
    Severity,
    SubscriptionStatus,
    TokenPurpose,
)
from app.services.conditions import normalize_condition

# ====================================================================== constants
MINOR_AGE = 18
MIN_AGE, MAX_AGE = 3, 110
MAX_ITEMS = 20
ITEM_MAX = 120
NOTES_MAX = 1000
INVITE_TTL = timedelta(days=7)
INTAKE_SOURCES = ("owner_manual", "owner_ai")

GENDERS: tuple[tuple[str, str], ...] = (
    ("female", "Female"), ("male", "Male"), ("non_binary", "Non-binary"), ("other", "Other"),
    ("prefer_not", "Prefer not to say"),
)
GENDER_LABELS = dict(GENDERS)
GOAL_LABELS: dict[str, str] = {
    Goal.GENERAL_FITNESS.value: "General fitness",
    Goal.WEIGHT_LOSS.value: "Weight loss",
    Goal.BULKING.value: "Muscle gain (bulking)",
    Goal.CALISTHENICS.value: "Calisthenics",
}
RELATIONS: tuple[str, ...] = ("Mother", "Father", "Parent", "Legal guardian", "Grandparent", "Sibling", "Spouse",
                              "Relative", "Other")

# form field -> HealthCondition kind
LIST_FIELDS: dict[str, ConditionKind] = {
    "conditions": ConditionKind.CONDITION,
    "allergies": ConditionKind.ALLERGY,
    "medications": ConditionKind.MEDICATION,
    "injuries": ConditionKind.INJURY,
}
LIST_LABELS = {"conditions": "Conditions", "allergies": "Allergies", "medications": "Medications", "injuries": "Injuries"}
KIND_TO_FIELD = {kind: name for name, kind in LIST_FIELDS.items()}

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACES = re.compile(r"\s+")
_HAS_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)
_NAME_FORBIDDEN = re.compile(r"[<>{}\[\]\\|^~`=@#$%*+_;:\"?!/]")
_PHONE_CHARS = re.compile(r"^\+?[\d\s().-]{4,32}$")
_LIST_SPLIT = re.compile(r"\s*(?:[,;\n•]|\s+and\s+|\s*&\s*|\s+\+\s+)\s*", re.IGNORECASE)
_WORD = re.compile(r"[A-Za-z]+")

# ---- local misspelling handling (deterministic, used for typed lists and the AI-unavailable fallback)
SPELLING_FIXES: dict[str, str] = {
    "asthama": "asthma", "asthema": "asthma", "athsma": "asthma", "asthmaa": "asthma", "astma": "asthma",
    "asma": "asthma", "ashtma": "asthma", "azthma": "asthma", "asthmatik": "asthmatic",
    "alergies": "allergies", "allergys": "allergies", "alergys": "allergies", "allergeis": "allergies",
    "allergie": "allergy", "alergy": "allergy", "allargy": "allergy", "alergie": "allergy", "allergey": "allergy",
    "alergic": "allergic", "allergik": "allergic", "alergik": "allergic",
    "diabetis": "diabetes", "diabeties": "diabetes", "diabites": "diabetes", "diabetees": "diabetes",
    "diabetese": "diabetes", "daibetes": "diabetes",
    "hypertention": "hypertension", "hipertension": "hypertension", "hypertensoin": "hypertension",
    "hypotention": "hypotension",
    "arthritus": "arthritis", "artheritis": "arthritis", "athritis": "arthritis", "arthiritis": "arthritis",
    "migrane": "migraine", "migrain": "migraine", "migranes": "migraines",
    "epilapsy": "epilepsy", "epilepsey": "epilepsy", "epilepsi": "epilepsy",
    "thyriod": "thyroid", "thyroide": "thyroid",
    "excema": "eczema", "exzema": "eczema", "eczema": "eczema",
    "sinusitus": "sinusitis", "bronchitus": "bronchitis", "osteoporosys": "osteoporosis", "hernea": "hernia",
    "cholestrol": "cholesterol", "colesterol": "cholesterol", "pnemonia": "pneumonia",
    "lactos": "lactose", "glutten": "gluten", "penut": "peanut", "penuts": "peanuts", "peanutt": "peanut",
    "inhailer": "inhaler", "inheler": "inhaler", "insuline": "insulin", "metformine": "metformin",
    "injurie": "injury", "injurey": "injury", "injuri": "injury", "sprane": "sprain", "fracure": "fracture",
    "fractur": "fracture", "anemea": "anemia", "anaemea": "anaemia",
}
MEDICAL_VOCAB: tuple[str, ...] = tuple(sorted(set(SPELLING_FIXES.values()) | {
    "asthma", "asthmatic", "allergy", "allergies", "allergic", "diabetes", "diabetic", "hypertension",
    "hypotension", "arthritis", "migraine", "migraines", "epilepsy", "seizures", "thyroid", "eczema",
    "sinusitis", "bronchitis", "osteoporosis", "hernia", "cholesterol", "pneumonia", "anemia", "anaemia",
    "lactose", "gluten", "peanut", "peanuts", "shellfish", "pollen", "inhaler", "insulin", "metformin",
    "injury", "injured", "sprain", "fracture", "tendinitis", "sciatica", "vertigo", "obesity", "pregnancy",
}))
_COMMON_WORDS = frozenset({
    "allergens", "address", "assistant", "however", "another", "children", "athlete", "athletic", "mother",
    "father", "member", "number", "contact", "customer", "suffers", "suffer", "suffering", "history",
    "medicine", "medicines", "medication", "medications", "condition", "conditions", "injuries",
})


class IntakeError(ValueError):
    """Business-rule violation during registration (message safe to show). ``field`` names the form field."""

    def __init__(self, message: str, field: str = "") -> None:
        super().__init__(message)
        self.field = field


# ====================================================================== text helpers
def clean_line(value: Any, cap: int = 300) -> str:
    """Single line: control characters removed, whitespace collapsed, trimmed, bounded."""
    if value is None or not isinstance(value, (str, int, float)):
        return ""
    text = _CTRL.sub("", str(value)[: cap * 2])
    return _SPACES.sub(" ", text).strip()[:cap]


def clean_block(value: Any, cap: int = NOTES_MAX) -> str:
    """Multi-line text (notes): control characters removed, at most 2 blank lines, trimmed, bounded."""
    if not isinstance(value, str):
        return ""
    text = _CTRL.sub("", value[: cap * 2]).replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:cap]


def _match_case(original: str, replacement: str) -> str:
    if original.isupper() and len(original) > 1:
        return replacement.upper()
    if original[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def fix_word(word: str) -> str:
    """Correct a common misspelling of a medical term ('asthama' -> 'asthma'); other words are unchanged."""
    low = word.lower()
    if low in SPELLING_FIXES:
        return _match_case(word, SPELLING_FIXES[low])
    if len(low) >= 6 and low not in MEDICAL_VOCAB and low not in _COMMON_WORDS:
        close = difflib.get_close_matches(low, MEDICAL_VOCAB, n=1, cutoff=0.86)
        if close:
            return _match_case(word, close[0])
    return word


def fix_spelling(text: str) -> str:
    return _WORD.sub(lambda m: fix_word(m.group(0)), text or "")


def split_list(raw: Any, *, cap_items: int = MAX_ITEMS * 3) -> list[str]:
    """'asthma, high BP and dust allergy' -> ['asthma', 'high BP', 'dust allergy'] (deduplicated, trimmed)."""
    if isinstance(raw, (list, tuple)):
        parts: list[str] = []
        for item in raw:
            parts.extend(split_list(item, cap_items=cap_items))
        items = parts
    else:
        text = _CTRL.sub("", str(raw or ""))[:6000]
        items = [p.strip(" .-*\t") for p in _LIST_SPLIT.split(text)]
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        item = _SPACES.sub(" ", item).strip(" .-*\t")
        if not item or item.lower() in ("none", "nil", "no", "n/a", "na", "nothing", "-"):
            continue
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= cap_items:
            break
    return out


def canonical_item(name: str, kind: ConditionKind | None = None) -> str:
    """Spelling-fixed, capitalised list item ('asthama' -> 'Asthma', 'dust alergy' -> 'Dust allergy')."""
    fixed = fix_spelling(_SPACES.sub(" ", name).strip())
    return fixed[:1].upper() + fixed[1:] if fixed else fixed


def condition_tag(name: str, kind: ConditionKind) -> str | None:
    """The safety tag of a condition / allergy / medication / injury (the one normaliser every writer uses: the
    member's form, "remember this?" in the assistant and live coach, the gym's intake and health edit)."""
    tag = normalize_condition(fix_spelling(name))
    if tag is None and kind == ConditionKind.ALLERGY:
        low = name.lower()
        if any(k in low for k in ("peanut", "nut", "gluten", "lactose", "milk", "egg", "shellfish", "seafood", "soy", "wheat",
                                  "whey", "dairy", "casein", "fish", "sesame")):
            return "food_allergy"
        if any(k in low for k in ("dust", "pollen", "mould", "mold", "pet", "cat", "dog", "hay fever")):
            return "respiratory_allergy"
        if any(k in low for k in ("latex", "skin", "nickel", "eczema")):
            return "skin_allergy"
    return tag


def clean_items(raw: Any, kind: ConditionKind) -> tuple[list[str], str | None]:
    """Validated, deduplicated list for one HealthCondition kind (error message or None)."""
    items = [canonical_item(i, kind) for i in split_list(raw)]
    deduped: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item.lower() not in seen:
            seen.add(item.lower())
            deduped.append(item)
    too_long = [i for i in deduped if len(i) > ITEM_MAX]
    if too_long:
        return deduped, f"Keep each entry under {ITEM_MAX} characters (“{clamp(too_long[0], 40)}”)."
    if len(deduped) > MAX_ITEMS:
        return deduped[:MAX_ITEMS], f"List at most {MAX_ITEMS} entries."
    bad = [i for i in deduped if not _HAS_LETTER.search(i) or len(i) < 2]
    if bad:
        return deduped, f"“{clamp(bad[0], 40)}” isn't a valid entry."
    return deduped, None


# ====================================================================== field validators
def validate_name(raw: Any, *, what: str = "the member's full name", required: bool = True) -> tuple[str, str | None]:
    name = clean_line(strip_bidi(raw) if isinstance(raw, str) else raw, 300)  # names render everywhere: no bidi controls
    if not name:
        return "", (f"Enter {what}." if required else None)
    if len(name) < 2:
        return name, "That name is too short."
    if len(name) > 120:
        return name, "Use at most 120 characters."
    if not _HAS_LETTER.search(name):
        return name, "Use letters for the name."
    if _NAME_FORBIDDEN.search(name):
        return name, "Names can contain letters, spaces, apostrophes, dots and hyphens only."
    return name, None


def validate_email(raw: Any, *, required: bool = False) -> tuple[str, str | None]:
    """Returns (normalised lower-case e-mail or the raw text, error)."""
    text = clean_line(raw, 300)
    if not text:
        return "", ("Enter an e-mail address." if required else None)
    if len(text) > 254:
        return text, "That e-mail address is too long."
    try:
        email = _validate_email(text, check_deliverability=False).normalized.lower()
    except EmailNotValidError:
        return text, "Enter a valid e-mail address, like name@example.com."
    return email, None


def plausible_phone(normalized: str | None) -> bool:
    """Reject obviously fake numbers (+0..., all the same digit) that pass the length check."""
    if not normalized:
        return False
    digits = normalized.lstrip("+")
    if digits.startswith("0") or len(set(digits)) == 1:
        return False
    return True


def validate_phone(raw: Any, country: str | None, *, required: bool = False) -> tuple[str, str | None, str | None]:
    """Returns (as typed, E.164 normalised, error)."""
    phone = clean_line(raw, 64)
    if not phone:
        return "", None, ("Enter a phone number." if required else None)
    if not _PHONE_CHARS.fullmatch(phone):
        return phone, None, "Use digits only - spaces, dashes, brackets and a leading + are fine."
    try:
        norm = normalize_phone(phone, country)
    except ValueError:
        return phone, None, "That doesn't look like a valid phone number (include the country code if it's foreign)."
    if not plausible_phone(norm):
        return phone, None, "That doesn't look like a real phone number."
    return phone, norm, None


def age_on(dob: date, today: date) -> int:
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def validate_dob(raw: Any, *, today: date, required: bool = True) -> tuple[date | None, str | None]:
    text = clean_line(raw, 20)
    if not text:
        return None, ("Enter the member's date of birth." if required else None)
    try:
        dob = date.fromisoformat(text)
    except ValueError:
        return None, "Enter a valid date (YYYY-MM-DD)."
    if dob > today:
        return None, "The date of birth can't be in the future."
    age = age_on(dob, today)
    if age < MIN_AGE:
        return dob, f"Members must be at least {MIN_AGE} years old."
    if age > MAX_AGE:
        return dob, "Please check the year of birth."
    return dob, None


def is_minor(dob: date | None, today: date) -> bool:
    return dob is not None and age_on(dob, today) < MINOR_AGE


_GOAL_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (Goal.WEIGHT_LOSS.value, ("weight loss", "weight_loss", "lose weight", "losing weight", "fat loss", "lose fat", "slim",
                              "weightloss", "cut", "cutting", "reduce weight", "burn fat")),
    (Goal.BULKING.value, ("bulking", "bulk", "muscle gain", "gain muscle", "build muscle", "muscle building", "mass",
                          "gain weight", "weight gain", "strength", "bodybuilding")),
    (Goal.CALISTHENICS.value, ("calisthenics", "calisthenic", "bodyweight", "body weight", "street workout", "pull-up",
                               "pull up", "gymnastics")),
    (Goal.GENERAL_FITNESS.value, ("general fitness", "general_fitness", "fitness", "health", "healthy", "stay fit",
                                  "keep fit", "general", "wellness", "stamina", "endurance")),
)


def map_goal(raw: Any) -> str | None:
    text = clean_line(raw, 120).lower()
    if not text:
        return None
    if text in GOAL_LABELS:
        return text
    for goal, words in _GOAL_WORDS:
        if any(re.search(rf"\b{re.escape(w)}\b", text) for w in words):
            return goal
    return None


_GENDER_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("non_binary", ("non-binary", "non binary", "nonbinary", "non_binary", "enby")),
    ("female", ("female", "woman", "women", "girl", "lady", "f")),
    ("male", ("male", "man", "men", "boy", "gentleman", "m")),
    ("prefer_not", ("prefer not", "prefer_not", "undisclosed", "not stated")),
    ("other", ("other",)),
)


def map_gender(raw: Any) -> str | None:
    text = clean_line(raw, 40).lower()
    if not text:
        return None
    if text in GENDER_LABELS:
        return text
    for value, words in _GENDER_WORDS:
        if any(re.fullmatch(rf"{re.escape(w)}", text) or (len(w) > 1 and re.search(rf"\b{re.escape(w)}\b", text))
               for w in words):
            return value
    return None


def map_relation(raw: Any) -> str:
    text = clean_line(raw, 40)
    if not text:
        return ""
    low = text.lower()
    for rel in RELATIONS:
        if low == rel.lower():
            return rel
    synonyms = {"mom": "Mother", "mum": "Mother", "mummy": "Mother", "mommy": "Mother", "dad": "Father",
                "daddy": "Father", "papa": "Father", "guardian": "Legal guardian", "grandmother": "Grandparent",
                "grandfather": "Grandparent", "grandma": "Grandparent", "grandpa": "Grandparent", "brother": "Sibling",
                "sister": "Sibling", "husband": "Spouse", "wife": "Spouse", "uncle": "Relative", "aunt": "Relative",
                "aunty": "Relative", "parents": "Parent"}
    return synonyms.get(low, text[:1].upper() + text[1:])


def parse_country(raw: Any, default: str = "IN") -> str:
    code = clean_line(raw, 4).upper()
    return code if re.fullmatch(r"[A-Z]{2}", code) else default


# ====================================================================== form parsing
@dataclass(slots=True)
class HealthData:
    lists: dict[str, list[str]] = field(default_factory=dict)
    health_notes: str | None = None
    guardian_name: str | None = None
    guardian_relation: str | None = None
    guardian_phone: str | None = None
    guardian_email: str | None = None
    emergency_contact_name: str | None = None
    emergency_contact_phone: str | None = None
    # the owner's edit (``update_health``): each list as typed - re-read against the member's own entries, so an entry
    # is never split, renamed or re-spelled - and the signed snapshot of the entries the form showed (``health_seen``)
    raw: dict[str, str] = field(default_factory=dict)
    seen_token: str | None = None
    # set by ``update_health``: the member's own entries kept because the form could not prove the owner saw them
    # (no snapshot), and entries the member removed after the form was opened (not added back)
    kept: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RegistrationData:
    full_name: str
    email: str | None
    phone: str | None
    phone_normalized: str | None
    country_code: str
    date_of_birth: date
    gender: str | None
    city: str | None
    goal: Goal
    health: HealthData
    gym_id: int | None
    intake_source: str
    id_doc_type: IdDocType
    id_country: str


def _form_get(form: Any, name: str) -> Any:
    try:
        value = form.get(name)
    except AttributeError:
        return None
    return value if isinstance(value, str) else None


def parse_health(form: Any, *, minor: bool, country: str, registering: bool = False
                 ) -> tuple[dict[str, str], dict[str, str], HealthData]:
    """Health lists + guardian + emergency contact (shared by registration and the owner's health editor).

    ``registering``: every list is new - at most MAX_ITEMS entries each and MAX_CONDITIONS in total, checked here. The
    owner's edit of an existing member (the default) keeps each list as typed (``HealthData.raw``) and the form's
    snapshot (``health_seen``): ``update_health`` reads them against the member's own entries, so the limits apply to
    what the owner ADDS - a member who holds more than MAX_ITEMS entries of one kind never blocks a save."""
    values: dict[str, str] = {}
    errors: dict[str, str] = {}
    data = HealthData()
    for name, kind in LIST_FIELDS.items():
        submitted = _form_get(form, name)
        raw = submitted or ""
        values[name] = clean_block(raw, 3000 if registering else EDIT_TEXT_MAX)
        items, err = clean_items(raw, kind)
        data.lists[name] = items
        if registering:
            if err:
                errors[name] = err
        elif submitted is not None:
            data.raw[name] = values[name]
    if not registering:
        token = _form_get(form, "health_seen")
        data.seen_token = token[:SNAPSHOT_TOKEN_MAX] if token else None
        values["health_seen"] = data.seen_token or ""
    from app.services.health import MAX_CONDITIONS  # one cap per member, whoever adds the items

    total = sum(len(v) for v in data.lists.values())
    if registering and total > MAX_CONDITIONS:
        errors.setdefault("conditions", f"A member's profile holds at most {MAX_CONDITIONS} conditions, allergies, "
                                        f"medications and injuries in total - this lists {total}. Remove some first.")
    notes = clean_block(_form_get(form, "health_notes"), NOTES_MAX)
    values["health_notes"] = notes
    data.health_notes = notes or None

    g_name, err = validate_name(_form_get(form, "guardian_name"), what="the guardian's name", required=False)
    values["guardian_name"] = g_name
    if err:
        errors["guardian_name"] = err
    relation = map_relation(_form_get(form, "guardian_relation"))
    values["guardian_relation"] = relation
    g_phone, g_norm, err = validate_phone(_form_get(form, "guardian_phone"), country)
    values["guardian_phone"] = g_phone
    if err:
        errors["guardian_phone"] = err
    g_email, err = validate_email(_form_get(form, "guardian_email"))
    values["guardian_email"] = g_email
    if err:
        errors["guardian_email"] = err
    if minor:
        if not g_name and "guardian_name" not in errors:
            errors["guardian_name"] = "A parent or guardian is mandatory for members under 18."
        if not relation:
            errors["guardian_relation"] = "Choose how the guardian is related to the member."
        if not g_phone and not g_email and "guardian_phone" not in errors and "guardian_email" not in errors:
            errors["guardian_phone"] = "Add the guardian's phone number or e-mail address."
    elif (g_phone or g_email or relation) and not g_name and "guardian_name" not in errors:
        errors["guardian_name"] = "Add the guardian's name (or clear the other guardian fields)."
    data.guardian_name = g_name or None
    data.guardian_relation = (relation[:40] or None) if g_name else None
    data.guardian_phone = (g_phone or None) if g_name else None
    data.guardian_email = (g_email or None) if g_name else None

    e_name, err = validate_name(_form_get(form, "emergency_contact_name"), what="the emergency contact's name",
                                required=False)
    values["emergency_contact_name"] = e_name
    if err:
        errors["emergency_contact_name"] = err
    e_phone, _e_norm, err = validate_phone(_form_get(form, "emergency_contact_phone"), country)
    values["emergency_contact_phone"] = e_phone
    if err:
        errors["emergency_contact_phone"] = err
    if e_phone and not e_name and "emergency_contact_name" not in errors:
        errors["emergency_contact_name"] = "Add the emergency contact's name."
    if e_name and not e_phone and "emergency_contact_phone" not in errors:
        errors["emergency_contact_phone"] = "Add the emergency contact's phone number."
    data.emergency_contact_name = e_name or None
    data.emergency_contact_phone = e_phone or None
    return values, errors, data


def parse_registration(form: Any, *, gyms: list[Gym], default_country: str, today: date
                       ) -> tuple[dict[str, str], dict[str, str], RegistrationData | None]:
    """Validate the manual registration form (everything except the uploaded ID images)."""
    values: dict[str, str] = {}
    errors: dict[str, str] = {}

    full_name, err = validate_name(_form_get(form, "full_name"))
    values["full_name"] = full_name
    if err:
        errors["full_name"] = err

    country = parse_country(_form_get(form, "country_code"), default_country)
    values["country_code"] = country
    email, err = validate_email(_form_get(form, "email"))
    values["email"] = email
    if err:
        errors["email"] = err
    phone, phone_norm, err = validate_phone(_form_get(form, "phone"), country)
    values["phone"] = phone
    if err:
        errors["phone"] = err
    if not email and not phone and "email" not in errors and "phone" not in errors:
        errors["email"] = "Add an e-mail address (recommended - the invite is sent there) or a phone number."

    dob, err = validate_dob(_form_get(form, "date_of_birth"), today=today)
    values["date_of_birth"] = clean_line(_form_get(form, "date_of_birth"), 20)
    if err:
        errors["date_of_birth"] = err
    minor = is_minor(dob, today) if dob and not err else False

    gender_raw = clean_line(_form_get(form, "gender"), 40)
    gender = map_gender(gender_raw) if gender_raw else None
    values["gender"] = gender or gender_raw
    if gender_raw and gender is None:
        errors["gender"] = "Choose a gender from the list."

    city = clean_line(_form_get(form, "city"), 300)
    values["city"] = city
    if len(city) > 120:
        errors["city"] = "Use at most 120 characters."
    elif city and (not _HAS_LETTER.search(city) or _NAME_FORBIDDEN.search(city.replace("/", ""))):
        errors["city"] = "Enter a real city name."

    goal_raw = clean_line(_form_get(form, "goal"), 40) or Goal.GENERAL_FITNESS.value
    goal = map_goal(goal_raw)
    values["goal"] = goal or goal_raw
    if goal is None:
        errors["goal"] = "Choose a fitness goal from the list."

    h_values, h_errors, health = parse_health(form, minor=minor, country=country, registering=True)
    values.update(h_values)
    errors.update(h_errors)

    gym_id: int | None = None
    raw_gym = clean_line(_form_get(form, "gym_id"), 20)
    values["gym_id"] = raw_gym
    owned = {g.id for g in gyms}
    if raw_gym:
        try:
            gym_id = int(raw_gym)
        except ValueError:
            gym_id = None
        if gym_id not in owned:
            errors["gym_id"] = "Choose one of your gyms."
            gym_id = None
    elif len(gyms) == 1:
        gym_id = gyms[0].id

    source = clean_line(_form_get(form, "intake_source"), 20)
    values["intake_source"] = source if source in INTAKE_SOURCES else "owner_manual"

    doc_type_raw = clean_line(_form_get(form, "id_doc_type"), 40)
    values["id_doc_type"] = doc_type_raw
    try:
        doc_type = IdDocType(doc_type_raw)
    except ValueError:
        doc_type = None
        errors["id_doc_type"] = "Choose the type of ID card."
    id_country = clean_line(_form_get(form, "id_country"), 4).upper()
    values["id_country"] = id_country
    if not re.fullmatch(r"[A-Z]{2}", id_country):
        errors["id_country"] = "Enter the 2-letter code of the country that issued the ID (e.g. IN)."

    consent = (_form_get(form, "consent") or "").strip().lower() in ("1", "on", "true", "yes")
    values["consent"] = "1" if consent else ""
    if not consent:
        errors["consent"] = "Confirm that the member agreed to be registered and to share this information."

    if errors or dob is None or goal is None or doc_type is None:
        return values, errors, None
    return values, errors, RegistrationData(
        full_name=full_name, email=email or None, phone=phone or None, phone_normalized=phone_norm,
        country_code=country, date_of_birth=dob, gender=gender, city=city or None, goal=Goal(goal), health=health,
        gym_id=gym_id, intake_source=values["intake_source"], id_doc_type=doc_type, id_country=id_country,
    )


# ====================================================================== scoping
FORMER_MEMBER_DAYS = 90  # a membership that ended keeps the member visible to the gym this long (renewals), no longer


def consented_subscription_clause(today: date | None = None) -> Any:
    """SQL condition on ``Subscription``: a relationship with the gym that the member agreed to and that is current -

    * an active or suspended membership;
    * a purchase the member started in their own app (waiting for the desk code or the payment);
    * a membership that expired within FORMER_MEMBER_DAYS (renewals).

    Never a purchase the desk created that never became a membership, a cancelled or failed one, or one that ended
    long ago: a member who left - or cancelled a membership they never asked for - is a stranger to the gym again, and
    the gym no longer reads their profile or health record. The same rule as ``checkout.desk_consent``."""
    today = today or utcnow().date()
    return or_(
        Subscription.status.in_((SubscriptionStatus.ACTIVE, SubscriptionStatus.SUSPENDED)),
        and_(Subscription.status.in_((SubscriptionStatus.PENDING_OTP, SubscriptionStatus.PENDING_PAYMENT)),
             Subscription.channel != PurchaseChannel.OWNER_MANUAL),
        and_(Subscription.status == SubscriptionStatus.EXPIRED,
             Subscription.ends_on >= today - timedelta(days=FORMER_MEMBER_DAYS)),
    )


def related_customers_clause(owner_id: int, gym_id: int | None = None) -> Any:
    """SQL condition on ``User``: customers with a current, consented membership at the owner's gyms
    (``consented_subscription_clause``), or registered by the owner.

    With ``gym_id`` only customers with such a membership at that (owned) gym match."""
    owned = select(Gym.id).where(Gym.owner_id == owner_id)
    subs = select(Subscription.customer_id).where(Subscription.gym_id.in_(owned), consented_subscription_clause())
    if gym_id is not None:
        subs = select(Subscription.customer_id).where(Subscription.gym_id == gym_id, Subscription.gym_id.in_(owned),
                                                      consented_subscription_clause())
        return and_(User.role == Role.CUSTOMER, User.id.in_(subs))
    registered = select(CustomerProfile.user_id).where(CustomerProfile.registered_by_id == owner_id)
    return and_(User.role == Role.CUSTOMER, or_(User.id.in_(subs), User.id.in_(registered)))


async def is_related(db: AsyncSession, owner_id: int, customer_id: int) -> bool:
    found = await db.scalar(select(User.id).where(User.id == customer_id, related_customers_clause(owner_id)).limit(1))
    return found is not None


# ====================================================================== invites
async def create_invite_token(db: AsyncSession, user: User) -> str:
    """One-time INVITE token (7 days; only its SHA-256 is stored). Earlier unused invites stop working."""
    from app.security.crypto import new_token, token_hash

    now = utcnow()
    await db.execute(
        update(AuthToken)
        .where(AuthToken.purpose == TokenPurpose.INVITE, AuthToken.user_id == user.id, AuthToken.used_at.is_(None))
        .values(used_at=now)
        .execution_options(synchronize_session=False)
    )
    raw = new_token(32)
    db.add(AuthToken(purpose=TokenPurpose.INVITE, token_hash=token_hash(raw), user_id=user.id, email=user.email,
                     expires_at=now + INVITE_TTL))
    await db.flush()
    return raw


def invite_link(raw: str) -> str:
    return f"/auth/invite?token={raw}"


async def send_invite(db: AsyncSession, user: User, *, owner: User, gym_name: str) -> bool:
    """E-mail a fresh invite link (no-op without an e-mail address). Returns True when an e-mail was queued."""
    from app.services.notify import email_address

    if not user.email:
        return False
    raw = await create_invite_token(db, user)
    await email_address(
        db, user.email, f"{gym_name} registered you on IronPulse",
        f"{owner.display_name} registered you as a member of {gym_name}. Set your password to activate your "
        f"account - the link is valid for {INVITE_TTL.days} days. If you didn't expect this e-mail you can ignore it.",
        link=invite_link(raw), recipient=user.full_name,
    )
    return True


async def last_invite_at(db: AsyncSession, user_id: int) -> Any:
    return await db.scalar(
        select(AuthToken.created_at).where(AuthToken.purpose == TokenPurpose.INVITE, AuthToken.user_id == user_id)
        .order_by(AuthToken.created_at.desc()).limit(1)
    )


# ====================================================================== registration
@dataclass(slots=True)
class RegistrationResult:
    user: User
    invite_sent: bool
    guardian_emailed: bool
    conditions: int


def _first_name(name: str) -> str:
    return (name or "").split(" ")[0] or name


async def _store_embedding(db: AsyncSession, user_id: int) -> None:
    """Refresh the member's health embedding (WP-E1 helper; best effort - never blocks a registration). Reads at most
    CONDITIONS_READ_MAX rows (safety-relevant first); the text is cut and hashed off the event loop."""
    try:
        from app.services.health import CONDITIONS_READ_MAX, conditions_vector, safety_first, store_health_embedding
    except ImportError:  # pragma: no cover - E1 not installed
        return
    rows = (await db.execute(
        select(HealthCondition).where(HealthCondition.customer_id == user_id)
        .order_by(*safety_first()).limit(CONDITIONS_READ_MAX)
    )).scalars().all()
    try:
        await store_health_embedding(db, user_id, await conditions_vector(rows))
    except Exception:  # pragma: no cover - defensive
        import logging

        logging.getLogger("ironpulse.intake").warning("health embedding update failed for user %s", user_id)


async def register_customer(
    db: AsyncSession, *, owner: User, data: RegistrationData, gym: Gym | None, id_front: bytes, id_back: bytes | None,
    request: Any = None,
) -> RegistrationResult:
    """Create the member (no password), their profile, health rows and ID document; invite + notify + audit.

    Raises IntakeError for identifier conflicts and ``storage.UploadError`` for invalid ID images.
    Run inside a SAVEPOINT (``db.begin_nested()``) so a failure leaves nothing behind. The caller commits."""
    from app.services.accounts import identifier_conflict
    from app.services.audit import audit
    from app.services.id_verification import submit_identity_document
    from app.services.identity_lookup import generate_username, member_id
    from app.services.notify import email_address, notify

    conflict = await identifier_conflict(db, email=data.email, phone=data.phone, country_code=data.country_code)
    if conflict is not None:
        raise IntakeError(conflict.message, conflict.field)

    user = User(
        email=data.email, phone=data.phone, phone_normalized=data.phone_normalized, full_name=data.full_name,
        role=Role.CUSTOMER, password_hash=None, date_of_birth=data.date_of_birth, gender=data.gender, city=data.city,
        country_code=data.country_code, timezone=owner.timezone or "UTC", is_active=True,
        username=await generate_username(db, data.full_name, data.email),
    )
    db.add(user)
    await db.flush()
    h = data.health
    db.add(CustomerProfile(
        user_id=user.id, goal=data.goal, health_notes=h.health_notes, guardian_name=h.guardian_name,
        guardian_phone=h.guardian_phone, guardian_email=h.guardian_email, guardian_relation=h.guardian_relation,
        emergency_contact_name=h.emergency_contact_name, emergency_contact_phone=h.emergency_contact_phone,
        intake_source=data.intake_source, registered_by_id=owner.id, activity_suspended=False,
    ))
    source = "ai_intake" if data.intake_source == "owner_ai" else "owner"
    count = 0
    for field_name, kind in LIST_FIELDS.items():
        for name in h.lists.get(field_name, []):
            db.add(HealthCondition(customer_id=user.id, kind=kind, name=name[:ITEM_MAX],
                                   tag=condition_tag(name, kind), source=source))
            count += 1
    await db.flush()

    await submit_identity_document(db, user=user, employee=None, doc_type=data.id_doc_type, country_code=data.id_country,
                                   front=id_front, back=id_back or None, uploaded_by=owner)

    gym_name = gym.name if gym is not None else "your gym"
    invite_sent = await send_invite(db, user, owner=owner, gym_name=gym_name)
    await notify(
        db, user, f"Welcome to {gym_name}",
        f"{owner.display_name} registered you at {gym_name}. Your member ID is {member_id(user)}. Please review your "
        "health details on your profile - they keep your workouts safe.",
        category=NotificationCategory.ACCOUNT, severity=Severity.SUCCESS, link="/me/health/profile", email=False,
    )
    guardian_emailed = False
    if h.guardian_email:
        minor_note = (" Because they are under 18, you'll receive a copy of their IronPulse notifications."
                      if is_minor(data.date_of_birth, date.today()) else "")
        await email_address(
            db, h.guardian_email, f"{_first_name(data.full_name)} was registered at {gym_name}",
            f"{owner.display_name} registered {data.full_name} (member ID {member_id(user)}) at {gym_name} on IronPulse "
            f"and listed you as their {(h.guardian_relation or 'guardian').lower()}.{minor_note} If this is a mistake, "
            f"please contact {gym_name}.",
            recipient=h.guardian_name,
        )
        guardian_emailed = True
    if count:
        await _store_embedding(db, user.id)
    await audit(db, owner, "customer.register", "user", user.id, gym_id=gym.id if gym is not None else None,
                detail={"source": data.intake_source, "invite_sent": invite_sent, "health_items": count,
                        "minor": is_minor(data.date_of_birth, date.today())}, request=request)
    return RegistrationResult(user=user, invite_sent=invite_sent, guardian_emailed=guardian_emailed, conditions=count)


# ====================================================================== health edits by the owner
PROFILE_FIELDS = ("guardian_name", "guardian_relation", "guardian_phone", "guardian_email", "emergency_contact_name",
                  "emergency_contact_phone", "health_notes")
FIELD_LABELS = {
    "guardian_name": "guardian", "guardian_relation": "guardian", "guardian_phone": "guardian",
    "guardian_email": "guardian", "emergency_contact_name": "emergency contact",
    "emergency_contact_phone": "emergency contact", "health_notes": "health notes",
    "conditions": "conditions", "allergies": "allergies", "medications": "medications", "injuries": "injuries",
}
# The owner's form shows a member's entries one per line and carries a signed snapshot of them (``health_seen``, from
# ``health_form_values``). A save is applied as explicit changes against that snapshot:
# * an entry is matched by its exact text - even with commas, "and" or "&" inside - and is never split, renamed or
#   re-spelled: it keeps its notes, safety tag and source;
# * typed text that matches no entry becomes new entries (split on commas, semicolons and new lines, spelling fixed);
# * only an entry the form showed and the owner deleted is removed; one the member (or the AI's "Remember this")
#   added after the form was opened stays, and one the member removed meanwhile is not added back;
# * without a (valid) snapshot the edit can still add entries and remove the gym's own (source owner / ai_intake), but
#   never the member's own - nobody can prove the owner saw them.
# MAX_ITEMS applies to what one save ADDS to a list; MAX_CONDITIONS to the member's whole list after the save.
EDIT_TEXT_MAX = 12_000  # characters of one list in the owner's form (50 entries of 120 characters fit)
SNAPSHOT_MAX_AGE = 24 * 3600  # an older form is saved as without a snapshot
SNAPSHOT_ROWS_MAX = 200  # entries a snapshot names (the member page shows at most 100)
SNAPSHOT_TOKEN_MAX = 16_000
GYM_SOURCES = frozenset({"owner", "ai_intake"})
_EDIT_SPLIT = re.compile(r"(\s*[,;\n•]\s*)")
_KEY_SEPARATORS = re.compile(r"\s*([,;&+•/()\-])\s*")
_NOTHING = ("none", "nil", "no", "n/a", "na", "nothing", "-")


class HealthEditRefused(HTTPException):
    """An owner's health save that cannot be applied (nothing was changed). ``str(exc)`` is the friendly sentence and
    ``field`` the list it is about; uncaught it answers with the friendly 400 page, never a 500."""

    def __init__(self, message: str, field: str = "conditions") -> None:
        super().__init__(status_code=400, detail=message)
        self.message = message
        self.field = field

    def __str__(self) -> str:
        return self.message


def entry_key(text: Any) -> str:
    """How two spellings of one entry compare: case, spaces and the spaces around separators ignored."""
    return _KEY_SEPARATORS.sub(r"\1", " ".join(str(text or "").split()).lower())


def _key_hash(key: str) -> str:
    return hashlib.blake2b(key.encode("utf-8"), digest_size=6).hexdigest()


def _snapshot_serializer() -> URLSafeTimedSerializer:
    from app.config import get_settings

    return URLSafeTimedSerializer(get_settings().secret_key, salt="ironpulse-health-edit")


_KIND_CODES = {ConditionKind.CONDITION: "c", ConditionKind.ALLERGY: "a", ConditionKind.MEDICATION: "m",
               ConditionKind.INJURY: "i"}
_CODE_KINDS = {code: kind for kind, code in _KIND_CODES.items()}


def health_snapshot(customer_id: int, conditions: Iterable[Any]) -> str:
    """Signed, member-bound snapshot of the entries a health form shows (id, kind and a hash of the text) - the hidden
    ``health_seen`` field of the owner's form."""
    rows = []
    for c in list(conditions)[:SNAPSHOT_ROWS_MAX]:
        kind = c.kind if isinstance(c.kind, ConditionKind) else ConditionKind(str(c.kind))
        if getattr(c, "id", None):
            rows.append([int(c.id), _KIND_CODES[kind], _key_hash(entry_key(c.name))])
    return _snapshot_serializer().dumps({"u": int(customer_id), "r": rows})


def read_snapshot(token: Any, customer_id: int) -> dict[int, tuple[ConditionKind, str]] | None:
    """{entry id: (kind, text hash)} of a genuine, unexpired snapshot of THIS member's entries - else None."""
    if not isinstance(token, str) or not token or len(token) > SNAPSHOT_TOKEN_MAX:
        return None
    try:
        data = _snapshot_serializer().loads(token, max_age=SNAPSHOT_MAX_AGE)
    except BadSignature:  # also SignatureExpired
        return None
    if not isinstance(data, dict) or data.get("u") != int(customer_id) or not isinstance(data.get("r"), list):
        return None
    seen: dict[int, tuple[ConditionKind, str]] = {}
    for row in data["r"][:SNAPSHOT_ROWS_MAX]:
        if (isinstance(row, list) and len(row) == 3 and isinstance(row[0], int) and row[1] in _CODE_KINDS
                and isinstance(row[2], str)):
            seen[row[0]] = (_CODE_KINDS[row[1]], row[2])
    return seen


def health_form_values(customer_id: int, conditions: Sequence[Any], profile: Any) -> dict[str, str]:
    """The owner's health form, pre-filled: each list one entry per line (an entry with a comma inside stays one
    entry), the profile fields, and ``health_seen`` - the snapshot ``update_health`` applies the save against."""
    values: dict[str, str] = {}
    for name, kind in LIST_FIELDS.items():
        values[name] = "\n".join(c.name for c in conditions if c.kind == kind)
    for attr in PROFILE_FIELDS:
        values[attr] = (getattr(profile, attr, None) or "") if profile is not None else ""
    values["health_seen"] = health_snapshot(customer_id, conditions)
    return values


def _segments(text: str) -> list[str]:
    """'a, b; c' -> ['a', ', ', 'b', '; ', 'c']: the pieces and the separators between them (kept, so a run of pieces
    can be compared with an entry that has a comma inside)."""
    parts = _EDIT_SPLIT.split(_CTRL.sub("", str(text or "").replace("\r\n", "\n").replace("\r", "\n")))
    return parts if len(parts) % 2 == 1 else parts + [""]


def _match_entries(text: str | None, items: Sequence[str] | None, rows: Sequence[Any]) -> tuple[set[int], list[str]]:
    """The entries ``rows`` (Core rows: id, name) that the submitted list names, and the other pieces (new text, not
    yet cleaned). Longest match first, so 'Old ACL tear, right knee' is one entry, not two pieces."""
    by_key: dict[str, list[Any]] = {}
    span = 1
    for r in rows:
        by_key.setdefault(entry_key(r.name), []).append(r)
        span = max(span, len(_segments(r.name)) // 2 + 1)
    longest = max((len(k) for k in by_key), default=0)
    pieces: list[str] = []
    if text is not None:
        parts = _segments(text)
        pieces = parts[0::2]
        joins = parts[1::2]
    else:
        pieces = [str(i) for i in (items or [])]
        joins = [", "] * max(0, len(pieces) - 1)
    # entry_key of a run of pieces is the pieces' keys joined by their separators' keys: computed once per piece
    piece_keys = [entry_key(p) for p in pieces]
    join_keys = [(j.strip() or " ") for j in joins]
    matched: set[int] = set()
    new: list[str] = []
    i = 0
    while i < len(pieces):
        if not piece_keys[i]:  # an empty piece (",,") starts no entry
            i += 1
            continue
        runs = [piece_keys[i]]  # the keys of pieces i..j with their separators, j = i .. i + span - 1
        for k in range(i, min(len(pieces), i + span) - 1):
            if len(runs[-1]) > longest:
                break
            runs.append(runs[-1] + join_keys[k] + piece_keys[k + 1])
        hit = None
        for j in range(len(runs) - 1, -1, -1):  # the longest entry first
            free = [r for r in by_key.get(runs[j], []) if r.id not in matched]
            if free:
                hit = (i + j, free[0])
                break
        if hit is not None:
            matched.add(hit[1].id)
            i = hit[0] + 1
            continue
        if pieces[i].strip(" .-*\t"):
            new.append(pieces[i])
        i += 1
    return matched, new


def _new_entries(pieces: Sequence[str], kind: ConditionKind, field_name: str) -> list[str]:
    """New entries from the unmatched pieces: cleaned, spelling fixed, deduplicated - or ``HealthEditRefused``."""
    out: list[str] = []
    keys: set[str] = set()
    for piece in pieces:
        item = _SPACES.sub(" ", piece).strip(" .-*\t")
        if not item or item.lower() in _NOTHING:
            continue
        item = canonical_item(item, kind)
        key = entry_key(item)
        if key in keys:
            continue
        label = LIST_LABELS[field_name].lower()
        if len(item) > ITEM_MAX:
            raise HealthEditRefused(f"Keep each entry under {ITEM_MAX} characters (“{clamp(item, 40)}” in {label}).",
                                    field_name)
        if len(item) < 2 or not _HAS_LETTER.search(item):
            raise HealthEditRefused(f"“{clamp(item, 40)}” isn't a valid entry ({label}).", field_name)
        keys.add(key)
        out.append(item)
    if len(out) > MAX_ITEMS:
        raise HealthEditRefused(f"Add at most {MAX_ITEMS} new {LIST_LABELS[field_name].lower()} at a time - this adds "
                                f"{len(out)}. Save some now and the rest afterwards.", field_name)
    return out


async def update_health(db: AsyncSession, *, owner: User, customer: User, profile: CustomerProfile, data: HealthData,
                        request: Any = None) -> list[str]:
    """Apply the owner's edits; returns the changed areas (empty = nothing changed). Notifies the member (guardian CC'd
    for minors) and audits. The caller commits. Lists are applied as explicit changes against the form's snapshot (see
    above); a save that cannot be applied raises ``HealthEditRefused`` before anything is written."""
    from app.services.audit import audit
    from app.services.health import CONDITIONS_READ_MAX, MAX_CONDITIONS, conditions_held, lock_conditions
    from app.services.notify import notify

    # the member's list changes behind their cap lock: a "Remember" or a form add at the same moment waits for this
    await lock_conditions(db, customer.id)
    changed: list[str] = []
    data.kept, data.skipped = [], []
    seen = read_snapshot(data.seen_token, customer.id) if data.seen_token else None
    table = HealthCondition.__table__
    fields = [f for f in LIST_FIELDS if f in data.raw] if data.raw else [f for f in LIST_FIELDS if f in data.lists]
    kinds = [LIST_FIELDS[f] for f in fields]
    # the entries the save is judged against (Core rows, bounded): those the form showed, else the first
    # CONDITIONS_READ_MAX of the edited kinds - an entry outside them is never removed
    current: list[Any] = []
    if kinds:
        stmt = select(table.c.id, table.c.kind, table.c.name, table.c.source).where(
            table.c.customer_id == customer.id, table.c.kind.in_(kinds))
        if seen is not None:
            stmt = stmt.where(table.c.id.in_(sorted(seen) or [0])).order_by(table.c.id).limit(max(1, len(seen)))
        else:
            stmt = stmt.order_by(table.c.id).limit(CONDITIONS_READ_MAX)
        current = list((await db.execute(stmt)).all())
    gone_hashes = ({(kind, h) for i, (kind, h) in seen.items() if i not in {r.id for r in current}}
                   if seen is not None else set())
    removals: list[Any] = []
    additions: dict[ConditionKind, list[str]] = {}
    for field_name, kind in zip(fields, kinds):
        rows = [r for r in current if r.kind == kind]
        matched, pieces = _match_entries(data.raw.get(field_name) if data.raw else None,
                                         None if data.raw else data.lists.get(field_name), rows)
        known = {entry_key(r.name): r for r in rows}
        new_items = []
        for item in _new_entries(pieces, kind, field_name):
            key = entry_key(item)
            if key in known:  # a re-spelled or re-cased entry the form showed: that entry, unchanged
                matched.add(known[key].id)
            elif (kind, _key_hash(key)) in gone_hashes:
                data.skipped.append(item)  # removed by the member after the form was opened
            else:
                new_items.append(item)
        for r in rows:
            if r.id in matched:
                continue
            if seen is not None or str(r.source or "self") in GYM_SOURCES:
                removals.append(r)
            else:
                data.kept.append(r.name)
        if new_items:
            additions[kind] = new_items
    if additions:  # an entry the form did not show (added meanwhile) already holds that text: nothing to add
        wanted = [and_(table.c.kind == kind, func.lower(table.c.name).in_(sorted({n.lower() for n in names})))
                  for kind, names in additions.items()]
        present = {(kind, name) for kind, name in (await db.execute(
            select(table.c.kind, func.lower(table.c.name)).where(table.c.customer_id == customer.id, or_(*wanted))
            .limit(sum(len(v) for v in additions.values()) + 1)
        )).all()}
        additions = {kind: [n for n in names if (kind, n.lower()) not in present] for kind, names in additions.items()}
        removals = [r for r in removals if (r.kind, r.name.lower()) not in present]
    added = sum(len(v) for v in additions.values())
    if added:
        held = await conditions_held(db, customer.id, limit=MAX_CONDITIONS + added + len(removals) + 1)
        after = held - len(removals) + added
        if after > MAX_CONDITIONS:
            raise HealthEditRefused(
                f"A member's profile holds at most {MAX_CONDITIONS} conditions, allergies, medications and injuries in "
                f"total - this save would make it {after}. Remove some first.", KIND_TO_FIELD[next(iter(additions))])
    if removals:
        await db.execute(delete(table).where(table.c.customer_id == customer.id,
                                             table.c.id.in_(sorted(r.id for r in removals))))
    for kind, names in additions.items():
        for name in names:
            db.add(HealthCondition(customer_id=customer.id, kind=kind, name=name[:ITEM_MAX],
                                   tag=condition_tag(name, kind), source="owner"))
    for field_name, kind in zip(fields, kinds):
        if additions.get(kind) or any(r.kind == kind for r in removals):
            changed.append(field_name)
    for attr in PROFILE_FIELDS:
        new = getattr(data, attr)
        old = getattr(profile, attr)
        if (old or None) != (new or None):
            setattr(profile, attr, new)
            label = FIELD_LABELS[attr]
            if label not in changed:
                changed.append(label)
    if not changed:
        return []
    await db.flush()
    if any(f in changed for f in LIST_FIELDS):
        await _store_embedding(db, customer.id)
    pretty = ", ".join(dict.fromkeys(FIELD_LABELS.get(c, c) for c in changed))
    await notify(
        db, customer, "Your health profile was updated",
        f"{owner.display_name} updated your {pretty}. Please check the details are correct - they keep your "
        "workouts safe. Contact your gym if anything is wrong.",
        category=NotificationCategory.ACCOUNT, severity=Severity.INFO, link="/me/health/profile",
    )
    await audit(db, owner, "customer.health_update", "user", customer.id,
                detail={"changed": changed, "removed": len(removals), "added": added, "kept": len(data.kept),
                        "snapshot": seen is not None}, request=request)
    return changed


__all__ = [
    "GENDERS", "GOAL_LABELS", "INTAKE_SOURCES", "IntakeError", "LIST_FIELDS", "LIST_LABELS", "MINOR_AGE", "RELATIONS",
    "HealthData", "HealthEditRefused", "RegistrationData", "RegistrationResult", "age_on", "canonical_item",
    "clean_items", "clean_line", "condition_tag", "create_invite_token", "entry_key", "fix_spelling", "fix_word",
    "health_form_values", "health_snapshot", "invite_link", "is_minor", "is_related", "last_invite_at", "map_gender",
    "map_goal", "map_relation", "parse_health", "parse_registration", "plausible_phone", "read_snapshot",
    "register_customer", "related_customers_clause", "consented_subscription_clause", "send_invite", "split_list",
    "update_health", "validate_dob",
    "validate_email", "validate_name", "validate_phone",
]
