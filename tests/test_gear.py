"""Protective-gear recommendations: back strap / gloves etc. driven by conditions, with safety conflicts."""

from __future__ import annotations

from app.ai.client import FakeProvider, ai
from app.services.gear import personalised_gear_advice, recommend_gear


def keys(advice):
    return [i.key for i in advice.items]


def test_back_problem_gets_back_strap():
    a = recommend_gear(["Lower back pain (slipped disc)"])
    assert "back_support" in keys(a)
    assert a.items[0].priority == "essential"


def test_hand_and_grip_problems_get_gloves():
    a = recommend_gear(["blisters on palms", "weak grip after finger fracture"])
    assert "gloves" in keys(a)
    a = recommend_gear(["eczema on hands"])
    assert "gloves" in keys(a)


def test_wrist_injury_gets_wraps_and_gloves():
    a = recommend_gear(["Carpal tunnel - right wrist"])
    assert {"wrist_wraps", "gloves"} <= set(keys(a))


def test_belt_blocked_with_hernia_and_pregnancy():
    a = recommend_gear(["inguinal hernia", "lower back pain"])
    assert "back_support" not in keys(a)
    assert any(b["key"] == "back_support" for b in a.blocked)
    a = recommend_gear(["pregnant - 2nd trimester", "back pain"])
    assert "back_support" not in keys(a) and "maternity_belt" in keys(a)


def test_hypertension_adds_valsalva_caution_and_minor_downgrades():
    a = recommend_gear(["high blood pressure", "sciatica"])
    belt = next(i for i in a.items if i.key == "back_support")
    assert any("Valsalva" in c for c in belt.cautions)
    a = recommend_gear(["back pain"], age=15)
    belt = next(i for i in a.items if i.key == "back_support")
    assert belt.priority == "recommended" and any("under-18" in c for c in belt.cautions)


def test_no_conditions_no_gear():
    assert recommend_gear([]).items == []


async def test_ai_can_only_add_catalogue_items_and_never_blocked():
    p = FakeProvider("gateway", responses=[
        '{"tips": [{"key": "gloves", "tip": "Grab a breathable pair."}], '
        '"additional": [{"key": "back_support", "tip": "Use a belt."}, {"key": "rocket_boots", "tip": "x"}, '
        '{"key": "wrist_wraps", "tip": "Wrap up."}]}'
    ])
    ai.use_providers([p])
    try:
        a = await personalised_gear_advice(["hernia", "calluses on hands"])
    finally:
        ai.use_providers(None)
    ks = keys(a)
    assert "gloves" in ks and a.items[0].ai_tip == "Grab a breathable pair."
    assert "back_support" not in ks and "rocket_boots" not in ks  # blocked + not in catalogue
    assert "wrist_wraps" in ks and a.ai_provider == "gateway"


async def test_ai_unavailable_falls_back(ai_down):
    a = await personalised_gear_advice(["lumbar disc bulge"])
    assert "back_support" in keys(a) and a.ai_provider is None
