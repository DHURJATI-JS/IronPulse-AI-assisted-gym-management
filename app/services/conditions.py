"""Normalise free-text health conditions into safety tags used by every AI/rule guardrail.

``normalize_condition("High BP")`` -> ``"hypertension"``. Unknown conditions return ``None`` (they are
still stored and shown to trainers/AI as free text).
"""

from __future__ import annotations

import re

# tag -> (label, synonyms/keywords)
CONDITION_TAGS: dict[str, tuple[str, tuple[str, ...]]] = {
    "asthma": ("Asthma", ("asthma", "asthmatic", "wheez", "bronchospasm")),
    "copd": ("COPD / chronic lung disease", ("copd", "emphysema", "chronic bronchitis", "lung disease")),
    "hypertension": ("High blood pressure", ("hypertension", "high blood pressure", "high bp", "bp high", "htn")),
    "hypotension": ("Low blood pressure", ("hypotension", "low blood pressure", "low bp")),
    "cardiac": ("Heart condition", ("heart", "cardiac", "arrhythm", "angina", "coronary", "bypass", "stent", "palpitation", "pacemaker", "heart attack", "myocard")),
    "diabetes": ("Diabetes", ("diabet", "type 1", "type 2", "t1d", "t2d", "insulin", "blood sugar")),
    "epilepsy": ("Epilepsy / seizures", ("epilep", "seizure", "convulsion")),
    "pregnancy": ("Pregnancy", ("pregnan", "expecting a baby", "trimester", "postpartum", "post partum")),
    "knee_injury": ("Knee injury", ("knee", "acl", "mcl", "meniscus", "patella")),
    "lower_back": ("Back problem", ("back pain", "lower back", "slip disc", "slipped disc", "herniat", "sciatica", "spine", "spinal", "lumbar")),
    "shoulder_injury": ("Shoulder injury", ("shoulder", "rotator cuff", "dislocat", "frozen shoulder")),
    "wrist_injury": ("Wrist injury", ("wrist", "carpal tunnel")),
    "ankle_injury": ("Ankle / foot injury", ("ankle", "achilles", "plantar", "foot injury", "sprain")),
    "neck_injury": ("Neck problem", ("neck", "cervical spondyl", "whiplash")),
    "arthritis": ("Arthritis / joint pain", ("arthritis", "rheumat", "gout", "joint pain")),
    "osteoporosis": ("Osteoporosis", ("osteopor", "osteopenia", "brittle bone", "low bone density")),
    "hernia": ("Hernia", ("hernia",)),
    "recent_surgery": ("Recent surgery", ("surgery", "operation", "post-op", "post op", "operated")),
    "obesity": ("Obesity", ("obes", "morbid")),
    "vertigo": ("Vertigo / dizziness", ("vertigo", "dizz", "balance disorder", "fainting", "syncope")),
    "migraine": ("Migraine", ("migraine",)),
    "anemia": ("Anaemia", ("anemi", "anaemi", "low hemoglobin", "low haemoglobin")),
    "thyroid": ("Thyroid disorder", ("thyroid", "hypothyroid", "hyperthyroid")),
    # " ckd" / "ckd ": the abbreviation as a word of its own (the text is padded with spaces) - never inside "backdrop"
    "kidney": ("Kidney disease", ("kidney", "renal", " ckd", "ckd ", "dialysis", "nephrotic", "nephropathy",
                                  "nephritis", "glomerul")),
    "eating_disorder": ("Eating disorder", ("anorexi", "bulimi", "eating disorder", "binge eating")),
    "heat_intolerance": ("Heat intolerance", ("heat intoleran", "heat stroke", "heatstroke", "overheat")),
    "cold_sensitivity": ("Cold sensitivity", ("raynaud", "cold urticaria", "cold allergy", "cold intoleran")),
    "respiratory_allergy": ("Respiratory allergy", ("dust allergy", "pollen", "hay fever", "allergic rhinitis", "sinus")),
    "skin_allergy": ("Skin allergy", ("eczema", "dermatitis", "hives", "latex")),
    "food_allergy": ("Food allergy", ("peanut", "nut allergy", "gluten", "lactose", "shellfish", "food allergy")),
    "mental_health": ("Anxiety / depression", ("anxiety", "depress", "panic attack", "ptsd", "bipolar")),
    "multiple_sclerosis": ("Multiple sclerosis", ("multiple sclerosis", "ms ")),
}

TAG_LABELS = {tag: label for tag, (label, _) in CONDITION_TAGS.items()}


def normalize_condition(name: str | None) -> str | None:
    if not name:
        return None
    text = " " + re.sub(r"\s+", " ", name.lower()) + " "
    for tag, (_, keywords) in CONDITION_TAGS.items():
        if any(k in text for k in keywords):
            return tag
    if "allerg" in text:
        return "respiratory_allergy" if any(k in text for k in ("dust", "air", "pollen", "mould", "mold")) else None
    return None


def tags_for(names: list[str]) -> set[str]:
    return {t for t in (normalize_condition(n) for n in names) if t}
